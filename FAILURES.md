# FAILURES.md

Ways this system can still lose a DM, send a duplicate, or report a wrong
number. Every entry below is something I either observed in a test or
simulation run, or can point at the exact line that causes it. Where I saw it,
I say under what conditions.

> **Calibrated against the live API.** Two 500-event runs were fired at the
> deployed URL and diffed against their `/truth` endpoint. The final run matched
> exactly: `sent=91` against their `expected_unique_recipient_count=91`, with
> `webhook_200_count=540/540` (zero events lost) and `failed=0`.

---

## Things that will lose a DM

**1. Events that arrive while the process is down are gone unless PseudoGram
redelivers them.**
The webhook is the only way an event enters the system. During a deploy, a
crash, or a Fly machine reschedule, there is a window of a few seconds where we
return nothing at all. There is no buffer in front of us. Their documented
redelivery is ~8% of events and is not described as a retry-on-failure
mechanism, so I assume an event dropped this way is simply lost. Observed
window: a `fly deploy` takes the machine out for roughly 10–25 seconds.

**2. If the database is unreachable, `/webhook` returns 500 and the event is
lost.**
Postgres is a hard dependency on the ingest path — the upsert is what makes
dedup work, so there is nothing sensible to do without it. This is the accepted
cost of the DB-as-queue design. An in-memory buffer would trade a lost event
for a duplicated one, which is the worse failure here.

**3. If the matcher sweep loop is disabled or dies permanently, an event whose
background dispatch crashed is never matched.**
Verified by construction on 2026-08-19: I inserted an event row directly
(simulating "webhook returned 200, then the dispatch task died"), then replayed
the same `event_id`. The redelivery correctly reports as a duplicate and returns
early — so **a redelivery cannot heal a failed first delivery**. Only the sweep
over `processed_at IS NULL` recovers it, which it did within ~150ms. The loop is
supervised and restarts on exception, but it is a single point of recovery for
this case.

**4. A job that PseudoGram accepts and then leaves in `queued` forever is never
resolved.**
The reconciler polls `GET /v1/dm/{dm_id}` on a 2/5/10/30/60s schedule and only
promotes to `SENT` on `delivered`. A DM stuck non-terminal on their side is
polled every 60s indefinitely and counts as `queued` in `/stats` forever. That
is honest — I would rather report it as owed than guess — but the DM is
effectively lost and we never give up on it.

---

## Things that can send a duplicate

**5. A crash between issuing `POST /v1/dm/send` and recording the `dm_id`
depends entirely on their idempotency working as documented.**
On restart, a job left in `SENDING` for more than 60 seconds is requeued in the
*same* cycle, so the retry reuses the same `Idempotency-Key`
(`job:{id}:c{cycle}`). If their idempotency store is correct, we get the
original `dm_id` back and no second DM. If it is best-effort, lossy, or expires
faster than our retry, the user gets two DMs. **I have tested this against my
own stub, which implements idempotency correctly by construction — that is
exactly the assumption most likely to be wrong in production, and my tests
cannot detect it.**

**6. A `comment.deleted` arriving mid-send does not stop the DM.**
The cancel is `UPDATE dm_jobs SET status='CANCELLED' WHERE comment_id=$1 AND
status='QUEUED'`. A job already in `SENDING` is not matched by that WHERE
clause, so if the delete lands in the sub-second window while the HTTP request
is in flight, the DM goes out for a deleted comment. This is deliberate —
cancelling a job whose request is already on the wire would give us a job we
think is cancelled and a DM that was actually sent, which is worse.

**7. FAILED is terminal, and that is a deliberate bet that can cost a DM.**
If a job exhausts its retries, a later comment from the same user for the same
rule does **not** create a new obligation — `uq_live_job` excludes only
`CANCELLED`, not `FAILED`. The reasoning: we already burned up to 5 real send
attempts plus up to 3 resend cycles, and one of those "failures" may have
actually landed. Retrying risks a double DM. The cost is that a user whose DM
genuinely never arrived will never get one. I chose undercounting over
duplicating, per the assignment's guidance.

---

**7b. The documented HMAC secret is wrong, and a correct implementation of the
docs rejects 100% of real traffic.**
ASSIGNMENT.md says the signature is HMAC-SHA256 of the raw body "using your API
key as the secret". It is not. Keys are issued as `base64(email).random`, and
real signatures verify against the **email**. I found this because my first live
run returned `webhook_200_count: 0` against 21 delivery attempts, with 21x
`POST /webhook 401` in the logs. I captured a real (body, signature) pair and
solved it offline against every candidate secret; only the email reproduced
their digest.
`config.webhook_secret()` now derives the secret from the key, trusts the decode
only if it contains `@`, falls back to the documented behaviour otherwise, and
is overridable via `WEBHOOK_SECRET`. **If they change the scheme, or issue a key
in a different shape, signature verification silently rejects everything again**
— and the symptom is a perfectly healthy-looking service with zero events.

## Things that can make a number wrong

**8. `duplicates_blocked` counts suppressed obligations, not redelivered
events — and their truth endpoint does not publish a number to check it against.**
We count a duplicate when a *distinct comment* would have created a second live
obligation for the same (rule, user). We do **not** count redelivered events, on
the grounds that a redelivery is the same event, not a DM we chose not to send.
On the final calibration run this gave `duplicates_blocked = 80` alongside
`sent = 91`, and 91 exactly matched their expected recipient count — so the
`sent` side is confirmed. Their truth payload exposes only
`total_events_generated`, `total_deliveries_attempted`, `webhook_200_count` and
`expected_unique_recipients`; **it publishes no duplicates figure**, so this
number is defensible and internally consistent but was never verified against
theirs. If their grader uses semantics (b) — suppressed obligations *plus*
redelivered events that would have matched — our number is low by the number of
redeliveries that hit a matching comment. Both counters are tracked internally
and `DUPLICATES_FORMULA=rule_user_plus_events` switches formulas without a code
change.

**8a. Overlapping rules multiply DMs, and nothing warns you.**
There is no dedup *across* rules — two rules are two different messages, so a
user matching both legitimately gets two DMs. That is by design, but it means
`/stats` is only ever as correct as the rule set. I proved this to myself the
expensive way: a stray `PRICE` rule left on the live backend alongside the
intended `pric` rule turned 90 expected obligations into 172 on a 500-event run
(90 from `pric`, 82 from `PRICE`). No bug fired, no warning appeared, and the
numbers were simply wrong. Deleting the stray rule was then refused with a 409
because `dm_jobs.rule_id` is a foreign key, so the only cleanup was truncating
the jobs too.

**8b. The keyword you configure changes the numbers, and matching is literal
substring.**
Their generator emits several price-intent phrasings, including "pricing
please". `"price" in "pricing"` is False, so a rule with keyword `PRICE` misses
those users entirely — on run 1 that cost exactly 4 of 97 expected recipients,
which I found by set-differencing our matched users against their
`expected_unique_recipients` list. The deployed rule uses keyword `pric` to
cover both. This is not a bug in the matcher (case-insensitive substring is
exactly what the assignment specifies) but it is a real way to report wrong
numbers, and it is entirely a function of how the rule is written.

**9. `sent` lags reality, always, and by design.**
Only a reconciler poll returning `delivered` increments it. Between the 202 and
the confirm, a DM that *was* delivered counts as `queued`. Under a 500-event
burst, `/stats` read at the wrong moment will show a large `queued` and a small
`sent` — both honest, neither final. A grader sampling mid-burst sees a system
that has not finished, because at 9 sends per 60 seconds it genuinely has not.

**10. The rate ledger over-counts by one if we crash mid-request.**
`send_log` gets its row *before* the HTTP call, so a crash between the insert
and the request leaves a phantom entry that costs us one slot in that 60s
window. Deliberate: recording after the call would under-count on a crash and
push us over their real limit. Costs throughput, never correctness.

**11. Counters are monotonic and survive a truncation of the job tables.**
`duplicates_blocked` lives in a `counters` table, not derived from rows. If jobs
were ever cleared without clearing counters, the four numbers would disagree.
Only an operational hazard, but a real one — I hit it while clearing test data
and had to reset both.

**12. Two machines would silently break the rate limit.**
Observed on the first deploy: `fly deploy` with `min_machines_running = 1`
quietly provisions a *second* machine "for high availability", and both pass
health checks. Two machines means two send workers spending the same 9-per-60s
budget, so the real rate would be 18/60s and we would take constant 429s. The
job claim itself is safe (`FOR UPDATE SKIP LOCKED`), so this corrupts the rate
budget rather than duplicating DMs. Caught it by reading the deploy output;
`fly.toml` now documents that machine count must be checked after every deploy.

---

## What I know I have not tested

- **The real PseudoGram API's idempotency behaviour.** Every idempotency drill
  in my suite runs against a stub I wrote from their documentation. Across two
  live 500-event runs I never observed a duplicate DM, but I also never forced
  the specific race (crash between the send and recording the `dm_id`) against
  the *real* API. If their idempotency store is best-effort, item 5 stands.
- **A 500-event burst over the real network.** I have run the 500-events-in-10s
  drill locally against the real webhook handler and the fake API (540 requests
  including redeliveries: 540/540 returned 200, p50 2ms, zero events lost, worst
  case exactly 9 sends in any rolling window against a cap of 9, 50 sends → 50
  distinct dm_ids). What that does **not** cover: real network latency and
  TLS handshakes under burst, Fly's proxy behaviour, their real 429 pacing, and
  connection-pool pressure with real round-trip times. Those need the live run.
- **A Fly machine restart mid-burst.** Designed for, and unit-tested via the
  boot-recovery drill against a real Postgres, but never executed against the
  live deployment during an actual 500-event burst. The recovery path (requeue
  `SENDING` older than 60s in the same cycle, replay events with
  `processed_at IS NULL`) is exercised by tests, not by a live kill.
