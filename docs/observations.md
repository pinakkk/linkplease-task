# Observation log

Running log of things actually observed while building and testing. Only
entries from real runs go here, and only entries from here may become bullets
in `FAILURES.md`. Each entry records the conditions it happened under.

Format: `[date] [context] what happened — conditions — consequence`

---

## 2026-08-19 — Fly provisioning

**Mumbai (bom) had no volume capacity for a new Postgres cluster.**
Conditions: `fly postgres create --region bom --volume-size 10`, personal org.
Error: `failed to create volume: app is already using all available zones in
region bom`. Moved both the DB and the app to `sin` (Singapore). No correctness
impact; noted because the blueprint specified `bom` and the region choice is now
different from the spec.

**`fly deploy` silently created a SECOND machine for high availability.**
Conditions: first deploy of `linkplease-backend`, `min_machines_running = 1` in
fly.toml. Fly logs one line — "Creating a second machine for high availability
and zero downtime deployments" — and both machines started and passed health
checks. This is a correctness bug for us, not a nicety: two machines means two
send-worker loops and two reconcilers against a rate limit of 10 requests per
rolling 60s, so the budget of 9 would effectively become 18 and we would be
rate-limited constantly. Also two concurrent claimers of the same `dm_jobs`
rows (survivable — `FOR UPDATE SKIP LOCKED` makes claims exclusive — but the
rate ledger would be over-spent).
Consequence: destroyed the extra machine; documented in `fly.toml` that machine
count must be checked after every deploy. **This must be re-verified after each
`fly deploy` for the rest of the project.**

## 2026-08-19 — First live end-to-end smoke test

**A bad API key produces `failed`, not a retry storm — as designed.**
Conditions: deployed with `PSEUDOGRAM_API_KEY=PLACEHOLDER_AWAITING_HUMAN_KEYGEN`
(the real key is not issued yet). Sent one correctly-signed `comment.created`
matching a `PRICE` rule. The job was created, claimed, sent, and PseudoGram
answered 401. Our client classifies 401/403/404 as `bad_request` → terminal
`FAILED` after exactly one attempt.
Consequence: correct and deliberate (an auth error is not retryable), but worth
remembering that **every job created before the real key is set will be
permanently FAILED** — the database must be cleared after the key lands, or
`/stats.failed` will carry junk into grading.

**Webhook latency in production: ~450–530ms round trip from a laptop in India
to sin, of which the server work is a few ms.** Well inside the 5s contract.
Redelivery dedup confirmed live: same `event_id` twice → `events.received = 1`,
`redelivered = 1`, and exactly one `dm_job`.

## 2026-08-19 — Test suite (89 tests, local Postgres + in-process fake API)

**Rolling-window rate limiting made the test suite order-dependent.**
Conditions: `pytest tests/test_pipeline.py` as a file. Three tests failed on a
25s `wait_until` timeout; each passed when run alone in ~1.2s. Cause: the
limiter is a rolling 60s window over the `send_log` table. Tests truncate
`send_log` between cases, but `RATE_LIMIT_WINDOW_SECONDS` stayed at the
production 60 while every other cadence knob was compressed, so once a test
saturated the budget the *next* test could block in `wait_for_budget()` for the
remainder of the minute. Fixed by compressing the window to 2s in the test
environment (the max-vs-window ratio, which is what the policy tests assert, is
preserved).
Consequence: no product defect — but it is a genuine property of the design
worth knowing: **the send worker can legitimately block for up to a full window,
and anything that assumes prompt draining will be wrong.** At 9 sends/60s, a
500-event burst that matches ~120 rules takes ~13 minutes to drain. That is not
a bug, it is the platform's rate limit, and `/stats` reports the backlog
honestly as `queued`.

## 2026-08-19 — Targeted race probes (local Postgres)

**10 truly concurrent identical `event_id` upserts → exactly one winner, zero
exceptions.**
Conditions: `asyncio.gather` of 10 identical `INSERT ... ON CONFLICT DO UPDATE
... RETURNING (xmax = 0)` statements against the same `event_id`, real
connection pool, real Postgres. Result: 1 reported as an insert, 9 as
redeliveries, 1 event row, `redeliveries = 9`, no `UniqueViolation` raised.
Consequence: BLUEPRINT §5 row 1's prediction is confirmed rather than assumed.
The webhook's dedup does NOT have a "both pass the check before either writes"
race, because there is no check — the constraint and the upsert are the same
statement. (This is worth stating in FAILURES.md as a race we specifically
tested for and did not find, since it is the classic one.)

**A crashed dispatch followed by a redelivery would lose the DM permanently
without `events.processed_at` — verified by construction.**
Conditions: inserted an event row directly (simulating "webhook returned 200,
then the background matcher task died"), then replayed the same `event_id`
through the upsert. The redelivery correctly reports `inserted = False`, so the
webhook returns early and never dispatches — meaning the redelivery cannot heal
the first delivery's failure. With the matcher sweep running, the job appeared
within ~150ms and the event was marked processed.
Consequence: the sweep loop is not belt-and-braces, it is the only thing that
recovers this case. If the sweep is ever disabled, an event whose dispatch
crashed becomes a silently lost DM. Belongs in FAILURES.md as a dependency.

## 2026-08-19 — Cloudflare deploy

**OpenNext built and deployed Next 16.3.1 to Cloudflare Workers without
incident.** Worker startup 31ms, bundle 1.15MB gzipped. Both routes return 200.
CORS from the Worker origin to the Fly backend returns
`access-control-allow-origin: *` and a 200, so the dashboard's client-side
polling reaches the backend from the edge.
Conditions: `@opennextjs/cloudflare` 1.x, `nodejs_compat` +
`global_fetch_strictly_public` flags, no incremental cache configured (every
page is static or client-polled).

## 2026-08-19 — 500-event burst drill (local, real webhook handler + fake API)

Conditions: 540 HTTP requests (500 unique events + 40 redeliveries at the
documented 8% rate) fired over 10s at the real FastAPI app via ASGI transport,
against local Postgres, with the fake PseudoGram injecting the documented
failure rates (20% 500s, 15% eventual delivery failure) and enforcing its own
10-per-window limiter. Rate window compressed to 10s (budget still 9) so the
drill finishes; signature verification ON.

**Ingest kept up comfortably.** 540/540 returned 200. Latency p50 **2ms**, max
**8ms** — against a 5000ms contract, a 625x margin. The fast path (HMAC + one
upsert) is doing what it was designed to do.

**Nothing lost.** `events` = exactly 500 rows, `sum(redeliveries)` = exactly 40.
Every redelivery was recognised; no unique event was dropped.

**Rate limit saturated but never breached.** Worst case across every rolling
window in the run: **9 sends, cap 9**. The limiter runs right at the ceiling
without going over, which is what we want — headroom that is never spent is
throughput wasted.

**No duplicate DMs.** The stub accepted 50 sends and created 50 distinct
`dm_id`s. One DM per obligation, no idempotency collisions.

**149 jobs and 161 duplicates_blocked from 500 events** — with 60 users across
3 keywords, most comments after the first from a given (rule, user) are
correctly suppressed. This ratio is the thing live calibration has to confirm
against their truth data.

**`queued = 115` when the drill ended, and that is the honest number.** At 9
sends per window the backlog genuinely had not drained. A grader sampling
`/stats` mid-burst will see exactly this shape: a small `sent`, a large
`queued`, and no inflation. Worth saying out loud in the Loom.

## 2026-08-19 — Frontend visual QA (deployed Cloudflare build)

**Horizontal overflow on the mobile landing page (375px), 75px.**
Conditions: `/` at 375×812 on the first deployed build. Cause: the decorative
hero glow blobs are sized `w-[min(80rem,140vw)]`, i.e. deliberately wider than
the viewport, and sit at `-z-10`. Fixed with `overflow-clip` on the hero shell.
Consequence: cosmetic only (the elements are `pointer-events-none`), but it made
the whole page scroll sideways on a phone. Re-verified after redeploy: all six
combinations (landing/dashboard × light/dark × 1440/375) show no overflow and no
console errors.

## 2026-08-19 — THE BUG THAT WOULD HAVE SCORED ZERO: wrong HMAC secret

**Every single webhook from the real simulator was rejected with 401.**
Conditions: first live simulation run (20 events) immediately after the real API
key was installed. Their truth endpoint reported `webhook_200_count: 0` against
`total_deliveries_attempted: 21`. Fly logs showed 21× `POST /webhook 401`.
Our `/stats` stayed at zero because nothing was ever ingested.

ASSIGNMENT.md states the signature is "HMAC-SHA256 of the raw request body
using your API key as the secret". That is **not** what they do.

Diagnosis: added temporary logging that dumped the full raw body (base64) and
the signature they sent, fired a 2-event run, and solved the pair offline
against every plausible candidate secret — full key, the segment before the dot,
the segment after the dot, the base64-decoded head, the hex-decoded tail, and
plain (non-HMAC) hashes. Exactly one reproduced their digest.

**The secret is the EMAIL ADDRESS.** Keys are issued in the form
`base64(email).random` — for this key, `YnlwaW5ha2t1bmR1QGdtYWlsLmNvbQ` decodes
to the email, and HMAC-SHA256(raw_body, key=email) matches their signature
exactly on captured pairs.

Fix: `config.webhook_secret()` derives the secret from the key by decoding the
first segment, with a guard that only trusts the decode if it contains `@`, and
falls back to the documented behaviour (whole key) otherwise. `WEBHOOK_SECRET`
env var overrides. Two regression tests lock it in so nobody "corrects" it back
to the documented-but-wrong version.

Verified after the fix: 10-event run → `webhook_200_count: 11` (11 attempted
deliveries of 10 unique events), our side recorded exactly 10 events with 1
redelivery. Perfect reconciliation.

**This is the single most valuable thing testing found.** Signature verification
is Part B, and a "correct" implementation of the documented spec silently
rejects 100% of real traffic. Anyone who implemented HMAC exactly as written and
did not run a live simulation would score zero on stage 1 and never know why.

## 2026-08-19 — Live calibration against the real API

**Run 1 (rule keyword `PRICE`): 93 obligations vs truth's 97 expected.**
All 536 deliveries got 200 (zero ingest loss). Set-differenced our matched users
against `expected_unique_recipients`: 0 false positives, exactly 4 missing —
`usr_15bac76d46`, `usr_4fd47f090c`, `usr_c4700724c0`, `usr_faf95216d2`. Every
one of them had commented **"pricing please"**. `"price" in "pricing"` is False
(no `e`), so a `PRICE` keyword cannot match it, but their truth counts those
users as expected recipients.
Consequence: their generator emits "pricing please" as a price-intent comment.
Fixed by using the keyword **`pric`**, which covers price / PRICE / Price list /
pricing in ONE rule. Deliberately one rule and not two — a second `pricing` rule
would create a second obligation for any user who said both words, sending them
two DMs and inflating `sent`.

**Run 2 (rule keyword `pric`): EXACT MATCH.**
```
truth : total_events_generated=500  deliveries_attempted=540  webhook_200_count=540
        expected_unique_recipient_count=91
ours  : sent=91  failed=0  queued=0  duplicates_blocked=80
```
- `sent` 91 == their 91 expected unique recipients.
- 540/540 webhooks accepted — **zero events lost**, including 40 redeliveries
  which we deduplicated rather than double-sending.
- `failed=0` across 91 real sends against an API that 500s ~20% of the time —
  the retry policy absorbed every transient failure.
- Rate budget sat pinned at 9/9 for the whole drain and never breached.
- `duplicates_blocked=80`: users who commented a price variant more than once.
