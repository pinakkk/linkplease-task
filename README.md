# LinkPlease — comment-to-DM automation

Someone comments `PRICE` on a post; they get DMed the price list. Once, exactly
once, even though the platform API fails 20% of the time, rate-limits us to 10
requests a minute, redelivers 8% of events, and reports success on DMs that
never arrive.

Built for the LinkPlease tech-intern assignment. **Parts A + B + C.**

- **Backend (the graded URL):** https://linkplease-backend.fly.dev
- **Dashboard:** _(Cloudflare URL — see Deployment below)_
- **Known failure modes:** [`FAILURES.md`](FAILURES.md) — read this one.

---

## The contract

Three routes, exact shapes, graded by an automated script.

```bash
# Create a rule
curl -X POST https://linkplease-backend.fly.dev/rules \
  -H 'Content-Type: application/json' \
  -d '{"keyword":"PRICE","dm_message":"Here is the price list: ..."}'
# 201 {"rule_id":"rule_83370b3abd7a","keyword":"PRICE","dm_message":"..."}

# Live counters
curl https://linkplease-backend.fly.dev/stats
# {"sent":0,"failed":0,"queued":0,"duplicates_blocked":0}

# Webhook (HMAC-signed; forged requests get 401)
curl -X POST https://linkplease-backend.fly.dev/webhook \
  -H "X-PseudoGram-Signature: sha256=$SIG" -d "$BODY"
# 200 {"status":"ok"}   — in ~3ms of server time
```

---

## Architecture

```
                    Fly.io — ONE machine, ONE uvicorn worker
  PseudoGram ──▶ ┌──────────────────────────────────────────────┐
   webhooks      │  FastAPI                                      │
                 │  /webhook  ─┐                                 │
  Grader ──────▶ │  /rules     │  HMAC + one upsert, then 200    │
   script        │  /stats     │                                 │
                 │             ▼                                 │
                 │      ┌─────────────┐                          │
                 │      │  Postgres   │ ◀── the queue, the dedup │
                 │      │             │     ledger, the rate     │
                 │      └──┬───────┬──┘     log, and the stats   │
                 │         │       │                             │
                 │  ┌──────▼──┐ ┌──▼─────────┐ ┌──────────────┐  │
                 │  │ send    │ │ reconciler │ │ matcher      │  │
                 │  │ worker  │ │ 202≠sent   │ │ sweep        │  │
                 │  └────┬────┘ └──────┬─────┘ └──────────────┘  │
                 └───────┼─────────────┼────────────────────────┘
                         │ POST /v1/dm/send (≤9 per 60s)
                         │ GET  /v1/dm/{id} (free, unbudgeted)
                         ▼
                   PseudoGram mock API
```

### The one tradeoff worth explaining

**Postgres is the queue.** No Redis, no Celery, no broker.

What we gave up: horizontal scale. There is exactly one send-worker loop, and
adding a second machine would actively break correctness — two workers would
spend the rate budget twice over.

What we got: one source of truth. Nothing pending ever lives only in memory, so
a crash mid-send loses nothing. `/stats` is a single SQL query, so it is
internally consistent even mid-burst. And one person can explain the whole
thing.

At 9 sends per minute — the platform's limit, not ours — a broker would be
ceremony. The queue depth this system is designed for is *small and slow by
force*. If the rate limit were 10,000/min, this design would be wrong.

### Four decisions that carry the weight

**1. A 202 is not a delivery.** `POST /v1/dm/send` returning `202 Accepted`
means accepted, and ~15% of those still fail. A job that got a 202 goes to
`AWAITING_CONFIRM`, and only a reconciler poll returning `delivered` moves it to
`SENT`. `sent` in `/stats` counts nothing else. This is why our `sent` may lag —
it is the honest number, and inflated numbers are worse than low ones.

**2. Dedup is a database constraint, not application logic.**

```sql
CREATE UNIQUE INDEX uq_live_job ON dm_jobs (rule_id, user_id)
    WHERE status <> 'CANCELLED';
```

"The same user never gets DMed twice for the same rule" is enforced by Postgres.
The application cannot violate it, no matter how the events interleave. The
`WHERE` clause is deliberate: a CANCELLED job (comment deleted before we sent)
never reached the user, so a new comment may legitimately create a fresh
obligation.

**3. The idempotency key changes only when we want a genuinely new send.**
`job:{job_id}:c{cycle}` — stable across retries within a cycle, so a timeout
retry cannot double-send; different after a reconciler-ordered resend, because
reusing the key would just return the original *failed* `dm_id` forever.

**4. The rate budget is 9, not 10.** Their limit is 10 per rolling 60s. We spend
9 and bank one for clock skew between our clock and theirs. The ledger is a
Postgres table, so the budget survives restarts.

---

## Running it

### Backend

```bash
cd backend
python3.12 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp .env.example .env       # fill in PSEUDOGRAM_API_KEY and DATABASE_URL
./.venv/bin/uvicorn app.main:app --reload --port 8000
```

The schema applies itself at startup (`CREATE TABLE IF NOT EXISTS`), so a fresh
Postgres needs no migration step.

### Tests

```bash
cd backend
DATABASE_URL="postgresql://<you>@localhost:5432/postgres" ./.venv/bin/python -m pytest -q
# 89 passed
```

Needs a local Postgres. The suite runs against an in-process fake PseudoGram
that reproduces the hostile behaviours — 429s, 500s, 400s, idempotency keys,
and 202-then-failed deliveries — deterministically, so the drills are
repeatable rather than flaky.

### Frontend

```bash
cd frontend
npm install
echo 'NEXT_PUBLIC_API_BASE=https://linkplease-backend.fly.dev' > .env.local
npm run dev
```

---

## Deployment

**Backend — Fly.io.** One machine, always on:

```bash
cd backend
fly deploy --remote-only
fly machines list --app linkplease-backend   # MUST show exactly 1
```

That last line is not optional. `fly deploy` will quietly add a second machine
"for high availability", which for this system means two send workers sharing
one rate limit. `auto_stop_machines = false` and `min_machines_running = 1` keep
the URL alive for the 7 days after the deadline.

**Frontend — Cloudflare Workers** via `@opennextjs/cloudflare`:

```bash
cd frontend && npm run deploy
```

---

## Layout

```
backend/app/
  main.py         lifespan, boot recovery, loop supervision, /healthz
  webhook.py      HMAC verify + idempotent upsert + background dispatch
  rules.py        POST /rules
  stats.py        GET /stats (one query) + dashboard read routes
  matcher.py      keyword matching, dedup, tombstones, the sweep loop
  worker.py       the single send loop: claim, budget, send, classify
  reconciler.py   polls dm status; the only path to SENT
  ratelimit.py    rolling-window budget over send_log
  pseudogram.py   the API client, with every failure mode classified
  simulate.py     simulation proxy + truth-diff calibration report
  schema.sql      six tables; the constraints do the real work
backend/tests/    89 tests + the hostile fake PseudoGram
frontend/         Next.js dashboard
docs/observations.md   the test-run log FAILURES.md is written from
```

---

## Licence

Business Source License 1.1 — © 2026 Pinak Kundu. Not open source; see
[`LICENSE`](LICENSE) and [`AGENTS.md`](AGENTS.md). LinkPlease may clone and run
this to evaluate the submission.
