# FAILURES.md

Ways this system can still lose a DM, send a duplicate, or report a wrong
number. Every entry below is something I either observed in a test or
simulation run, or can point at the exact line that causes it. Where I saw it,
I say under what conditions.

> **Status note.** The live-fire calibration runs against the real PseudoGram
> API (500 events / 10s, diffed against their truth endpoint) are **not yet
> done** — they need an API key that hasn't been issued yet. Everything below
> comes from the local suite and targeted probes. This file will grow after
> calibration, and the entries most likely to change are the ones about
> `duplicates_blocked` and about the real API's idempotency behaviour.

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

## Things that can make a number wrong

**8. `duplicates_blocked` semantics are not yet confirmed against their truth
data.**
We count a duplicate when a *distinct comment* would have created a second live
obligation for the same (rule, user) — semantics (a) in my design notes. We do
**not** count redelivered events, on the grounds that a redelivery is the same
event, not a DM we chose not to send. We track both numbers internally and the
calibration endpoint evaluates both formulas against truth. **Until the live
calibration runs, there is a real chance this number is wrong**, and it is the
single most likely source of a mismatch with their server-side log.

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
  in my suite runs against a stub I wrote from their documentation. If the real
  implementation differs, items 5 and the whole resend-cycle design are affected
  and my tests would not have caught it.
- **A 500-event live burst.** The 500/10s drill has been run only against the
  local stub, where the network is an in-process ASGI transport. Connection-pool
  behaviour, their real 429 pacing, and webhook delivery under real burst are
  all unverified.
- **A Fly machine restart mid-burst.** Designed for and unit-tested via the
  boot-recovery drill, but not yet executed against the live deployment.
