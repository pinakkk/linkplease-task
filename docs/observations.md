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
