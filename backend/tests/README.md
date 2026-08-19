# Test suite

89 tests, ~28 seconds, no network. Everything runs against a real Postgres and a
real (in-process) HTTP round trip to a stub PseudoGram.

## Running it

```bash
cd backend
./.venv/bin/python -m pytest -q            # the whole suite
./.venv/bin/python -m pytest tests/test_pipeline.py -q
./.venv/bin/python -m pytest -q -k revival # one drill
./.venv/bin/python -m pytest -v            # per-test names
```

**Prerequisite:** a local Postgres reachable at
`postgresql://pinak@localhost:5432/postgres` (override with `TEST_ADMIN_DSN`).
The session fixture creates a throwaway database `linkplease_test_<random>`,
applies `app/schema.sql` to it, and drops it at the end. If Postgres is not
running the whole suite *skips* rather than failing — so a red run always means
a real defect, never a missing daemon.

No other setup: `DATABASE_URL`, `PSEUDOGRAM_API_KEY` and the loop cadences are
all set inside `conftest.py` at module scope, before anything imports `app.*`
(`app.config` reads the environment at import time, so this ordering is
load-bearing).

## What is where

| File | Covers |
|---|---|
| `fake_pseudogram.py` | The hostile stub API. Not a test — the thing tests run against. |
| `conftest.py` | DB lifecycle, ASGI wiring, payload builders, `wait_until`. |
| `test_signature.py` | Part B: HMAC on raw bytes, forged requests → 401 and **no** DB write. |
| `test_matcher.py` | `matches()` — case-insensitive substring, emoji/unicode, multi-rule. |
| `test_webhook.py` | The 5-second contract, event-level dedup, 400s, concurrent redelivery. |
| `test_ratelimit.py` | The rolling window is never breached; 429 does not burn an attempt. |
| `test_pipeline.py` | The core end-to-end drills (see below). |
| `test_stats.py` | The four graded keys, bucket semantics, snapshot consistency. |
| `test_crash_recovery.py` | Restart drills: nothing lost, nothing sent twice. |

## The stub API (`fake_pseudogram.py`)

The real mock API is deliberately hostile: ~20% 500s, a 10-per-60s rate limit,
and ~15% of *accepted* DMs quietly turning into `failed`. Reproducing that
randomly would make every test flaky, so the stub is **deterministic by
default** — all injection rates are `0.0` and tests script exact outcomes:

```python
fake_pseudogram.CONFIG.script = ["server_error", "server_error", "accepted"]
fake_pseudogram.CONFIG.deliver_script = ["failed", "delivered"]
fake_pseudogram.CONFIG.deliver_delay = 3600   # park a DM in "queued"
fake_pseudogram.force_delivery(dm_id, "delivered")   # release it
```

Available outcomes: `accepted`, `rate_limited`, `server_error`, `bad_request`,
`timeout`.

It honours `Idempotency-Key` exactly as documented — same key returns the
original `dm_id` and creates no second DM — and records every inbound request,
so tests assert against what the API *actually received*:

```python
fake_pseudogram.accepted_sends()      # sends that created a DM
fake_pseudogram.send_attempts()       # every POST, including failures
fake_pseudogram.dms_to("usr_alice")   # the assertion behind "exactly one DM"
fake_pseudogram.STATE.reads           # GETs — must never enter the rate window
```

For soak testing there is a seeded chaos mode reproducing the documented real
rates: `fake_pseudogram.CONFIG.enable_chaos(seed=42)`.

It is also runnable standalone, which is useful for manual poking:

```bash
./.venv/bin/uvicorn tests.fake_pseudogram:app --port 9999
```

Control endpoints (`POST /_test/reset`, `/_test/config`, `/_test/script`,
`GET /_test/sends`) mirror the Python helpers for that mode.

### The timeout case needs a special transport

`httpx.ASGITransport` awaits the app in-process and enforces **no timeout at
all**, so a `sleep()` inside the stub would stall the test rather than raise.
The stub instead creates the DM (the request really did arrive — that is the
whole point of BLUEPRINT §5 row 6) and returns a sentinel `599`, which
`TimeoutTransport` in `conftest.py` converts into a genuine `httpx.ReadTimeout`.
That way Agent B's `transport_error` branch is exercised for real.

## Design decisions worth knowing

**Real Postgres, not a mock.** Every correctness claim in BLUEPRINT §3 is
enforced by a database constraint — `uq_live_job` is what makes "the same user
never gets DMed twice" true, not application logic. Mocking the DB would test
our fantasy of Postgres.

**Real HTTP through ASGI, not a monkeypatched `send_dm`.** The 202/429/500/400
branching, the `Retry-After` header parse and the `Idempotency-Key` header all
need to be genuinely exercised. `conftest.wire_pseudogram` swaps
`app.pseudogram`'s httpx client for one bound to the stub; no socket is opened.

**One session-wide event loop.** asyncpg connections are bound to the loop that
created them, so `pytest_configure` sets
`asyncio_default_test_loop_scope = "session"`. Without it every query raises
"attached to a different loop".

**No sleep-and-hope.** All waiting goes through `wait_until(predicate, timeout)`
in `conftest.py`. It is fast when the system works and produces a real message
when it does not. Note the trap it exists to avoid: `lambda: _status(...) ==
"SENT"` compares a *coroutine object* to a string and is always false — use the
`_is(jobs, job_id, ...)` helper, which returns an awaitable.

**Compressed clocks, unchanged policy.** Pipeline tests monkeypatch
`BACKOFF_CAP_SECONDS` to 0.05 and the reconciler schedule to zeros; the rate
limit test compresses the window to 3 seconds with a cap of 3. The *policy under
test* (5 attempts, rolling window, 429 not counting as an attempt) is unchanged
— only the wall-clock constants shrink.

**Skips instead of errors for missing modules.** `require("worker",
"send_worker_loop")` skips with a message naming exactly what is absent, so the
suite collects and runs against a partially-built backend.

## The drills in `test_pipeline.py`

Each one maps to a specific promise in ASSIGNMENT or a row of BLUEPRINT §5:

1. A matching comment sends exactly once and reaches `SENT` **only** after the
   reconciler confirms `delivered` — a 202 alone must not count.
2. A 202 that later reports `failed` is resent with a **different**
   Idempotency-Key (`job:N:c0` then `job:N:c1`); reusing the key would return
   the original failed `dm_id` forever.
3. Five consecutive 500s → `FAILED` with `last_error` set, `/stats.failed == 1`,
   and exactly `MAX_ATTEMPTS` attempts made — never a silently dropped row.
4. A 400 → `FAILED` after exactly **one** attempt (retrying cannot help and
   spends rate budget).
5. The same user commenting five times → one DM, `duplicates_blocked == 4`.
6. `comment.deleted` before the send → `CANCELLED`, zero sends.
7. `comment.deleted` arriving *before* its `comment.created` → the tombstone is
   honoured, no job, no DM.
8. A cancelled obligation revived by a new comment → exactly one live job, one
   DM, and a fresh Idempotency-Key.
9. A timeout followed by a retry reuses the **same** key → exactly one DM.

## Known divergence from BLUEPRINT

BLUEPRINT §4.2 sketches revival as *flipping the CANCELLED row back to QUEUED
with cycle+1*. `app/matcher.py` instead lets the partial unique index (which
ignores CANCELLED) admit a **fresh row**, keeping the cancelled one as audit
history. Both produce exactly one live obligation and exactly one DM; the fresh
row also gets a naturally fresh key (`job:{new_id}:c0`). The tests assert the
invariant (one live job, one DM, a key distinct from the cancelled job's) rather
than the representation.
