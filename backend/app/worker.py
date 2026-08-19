# Copyright (c) 2026 Pinak Kundu. All rights reserved.
# Licensed under the Business Source License 1.1 (see LICENSE).
# No use, copying, or modification without written permission.
"""The send worker: one loop, one job at a time, rate-budgeted (BLUEPRINT §4.4).

Exactly one of these runs in the process (and exactly one process runs — see the
`--workers 1` note in fly.toml). That single-consumer property is what makes the
Postgres rate ledger race-free.

A job is only ever "sent" in the eyes of /stats once the reconciler confirms
delivery. This loop's job ends at AWAITING_CONFIRM.
"""
import asyncio
import logging
import random
import time

from . import config, db, pseudogram, ratelimit

log = logging.getLogger("linkplease.worker")

# Read by /healthz. A loop that stops beating is a loop that died silently, and
# a silently dead send loop looks exactly like an empty queue from outside.
LAST_BEAT: float = time.time()

# Prune the rate ledger roughly every this many iterations. It is housekeeping,
# not correctness, so it does not need its own loop.
_PRUNE_EVERY = 200
# Check for crash-orphaned SENDING jobs about this often (in iterations).
_RECOVER_EVERY = 50


def jitter() -> float:
    """0-1s of noise on every backoff. Without it, a batch of jobs that all
    failed at the same instant would retry at the same instant forever."""
    return random.random()


def backoff_seconds(attempt: int) -> float:
    """Exponential backoff, capped. `attempt` is the count AFTER incrementing,
    so the first retry waits ~2s, then 4, 8, 16 ... up to the cap."""
    return min(2.0 ** attempt, config.BACKOFF_CAP_SECONDS) + jitter()


async def claim_job() -> dict | None:
    """Atomically take the next due job and mark it SENDING.

    One statement, so a crash between "pick" and "mark" is impossible. FOR
    UPDATE SKIP LOCKED costs nothing with a single consumer but means a second
    worker, if one ever existed, would take a different row rather than block.
    """
    row = await db.fetchrow(
        """
        UPDATE dm_jobs SET status = 'SENDING', updated_at = now()
        WHERE job_id = (
            SELECT job_id FROM dm_jobs
            WHERE status = 'QUEUED' AND next_attempt_at <= now()
            ORDER BY next_attempt_at
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING job_id, rule_id, user_id, comment_id, attempt, cycle
        """
    )
    if row is None:
        return None
    # Second lookup rather than a join in the UPDATE: keeps the claim statement
    # small and readable, and rules are a tiny table.
    message = await db.fetchval(
        "SELECT dm_message FROM rules WHERE rule_id = $1", row["rule_id"]
    )
    job = dict(row)
    job["dm_message"] = message
    return job


async def _due_job_exists() -> bool:
    """Is there anything worth waiting for budget for? Checked before the (often
    long) budget wait so an idle system does not sit blocked on a rate limit it
    has no work for."""
    return bool(await db.fetchval(
        """
        SELECT 1 FROM dm_jobs
        WHERE status = 'QUEUED' AND next_attempt_at <= now()
        LIMIT 1
        """
    ))


async def send_worker_loop() -> None:
    """Forever: wait for budget, claim a job, send it, record the outcome."""
    global LAST_BEAT
    iterations = 0
    while True:
        LAST_BEAT = time.time()
        iterations += 1
        try:
            # ORDERING (deliberate, BLUEPRINT §4.4): we wait for the rate budget
            # BEFORE claiming, not after. If we claimed first, a job would sit
            # in SENDING for as long as the budget wait lasts — up to a full 60s
            # window — and the crash-recovery rule ("SENDING older than
            # SENDING_STALE_SECONDS was orphaned, requeue it") could not tell
            # that job apart from one abandoned by a crash. Waiting first means
            # SENDING only ever spans a single HTTP call, so the stale threshold
            # is unambiguous. The cost is that we may wait for budget and then
            # find the queue empty; that is a wasted sleep, not a correctness
            # problem.
            if not await _due_job_exists():
                # An idle queue is exactly when an orphaned SENDING job would go
                # unnoticed, so recovery runs on this branch too.
                if iterations % _RECOVER_EVERY == 0:
                    await requeue_stale_sending()
                await asyncio.sleep(config.WORKER_IDLE_SLEEP)
                continue

            await ratelimit.wait_for_budget()

            job = await claim_job()
            if job is None:
                # The job we saw was cancelled or taken while we waited.
                await asyncio.sleep(config.WORKER_IDLE_SLEEP)
                continue

            await _send_one(job)

            if iterations % _PRUNE_EVERY == 0:
                await ratelimit.prune()
            if iterations % _RECOVER_EVERY == 0:
                await requeue_stale_sending()
        except asyncio.CancelledError:
            raise
        except Exception:
            # The loop must outlive every individual failure. A short sleep
            # keeps a persistent error (e.g. DB down) from becoming a hot spin.
            log.exception("send worker iteration failed")
            await asyncio.sleep(1.0)


async def _send_one(job: dict) -> None:
    """Issue one DM and write the outcome back to the job row."""
    job_id = job["job_id"]

    if not job["dm_message"]:
        # The rule vanished under us. Nothing to send and no retry can fix it.
        log.error("job %s has no rule message; failing it", job_id)
        await _fail(job_id, "rule missing or has no dm_message")
        return

    # Stable across every retry within this cycle, different in the next cycle.
    # Stable-within-cycle is what makes a timeout-retry safe (their API returns
    # the original dm_id). Changing-per-cycle is what makes a reconciler-ordered
    # resend actually send, instead of getting the old failed dm_id back
    # forever. Both halves are load-bearing (BLUEPRINT §4.4).
    idempotency_key = f"job:{job_id}:c{job['cycle']}"

    # Recorded BEFORE the request goes out. If we crash mid-request we will have
    # over-counted by one, which costs us a little throughput; recording after
    # would under-count on a crash and push us over their real limit.
    await ratelimit.record_send(job_id)

    result = await pseudogram.send_dm(
        recipient_user_id=job["user_id"],
        message=job["dm_message"],
        comment_id=job["comment_id"],
        idempotency_key=idempotency_key,
    )

    if result.outcome == "accepted":
        # 202 is NOT a delivery. Hand off to the reconciler.
        await db.execute(
            """
            UPDATE dm_jobs
            SET status = 'AWAITING_CONFIRM', dm_id = $2, checks = 0,
                check_after = now() + make_interval(secs => $3),
                last_error = NULL, updated_at = now()
            WHERE job_id = $1
            """,
            job_id, result.dm_id, float(config.CONFIRM_SCHEDULE[0]),
        )
        log.info("job %s accepted dm_id=%s", job_id, result.dm_id)
        return

    if result.outcome == "rate_limited":
        # Their limiter disagreed with our budget (clock skew, or a stray
        # request). Back off by what they asked for. A 429 does NOT consume an
        # attempt: we never got to try, so it must not eat the retry budget.
        delay = (result.retry_after if result.retry_after is not None else 5.0) + jitter()
        await db.execute(
            """
            UPDATE dm_jobs
            SET status = 'QUEUED',
                next_attempt_at = now() + make_interval(secs => $2),
                last_error = $3, updated_at = now()
            WHERE job_id = $1
            """,
            job_id, float(delay), "429 rate_limited",
        )
        log.warning("job %s rate limited, retrying in %.1fs", job_id, delay)
        return

    if result.outcome == "bad_request":
        # Our payload is wrong. Retrying identical bytes cannot help, so fail
        # fast and make the log loud enough that a human notices.
        detail = f"HTTP {result.status_code}: {result.detail}"
        log.error("job %s BAD REQUEST - not retrying: %s", job_id, detail)
        await _fail(job_id, detail)
        return

    # server_error or transport_error: retryable with the SAME key.
    attempt = job["attempt"] + 1
    detail = f"{result.outcome} HTTP {result.status_code}: {result.detail}"
    if attempt >= config.MAX_ATTEMPTS:
        log.error("job %s exhausted %s attempts: %s", job_id, attempt, detail)
        await _fail(job_id, detail, attempt=attempt)
        return
    delay = backoff_seconds(attempt)
    await db.execute(
        """
        UPDATE dm_jobs
        SET status = 'QUEUED', attempt = $2,
            next_attempt_at = now() + make_interval(secs => $3),
            last_error = $4, updated_at = now()
        WHERE job_id = $1
        """,
        job_id, attempt, float(delay), detail,
    )
    log.warning("job %s attempt %s failed, retrying in %.1fs: %s",
                job_id, attempt, delay, detail)


async def _fail(job_id: int, error: str, attempt: int | None = None) -> None:
    """Terminal failure. FAILED is never revived — we already burned real send
    attempts for this user, and one of those 'failures' may in fact have landed
    (BLUEPRINT §3, revival rule)."""
    if attempt is None:
        await db.execute(
            """
            UPDATE dm_jobs SET status = 'FAILED', last_error = $2, updated_at = now()
            WHERE job_id = $1
            """,
            job_id, error,
        )
    else:
        await db.execute(
            """
            UPDATE dm_jobs SET status = 'FAILED', attempt = $2, last_error = $3,
                               updated_at = now()
            WHERE job_id = $1
            """,
            job_id, attempt, error,
        )


async def requeue_stale_sending() -> int:
    """Boot / periodic recovery: a job left in SENDING longer than the stale
    threshold was orphaned by a crash mid-request. Put it back on the queue in
    the SAME cycle, so the retry reuses the same Idempotency-Key and cannot
    produce a second DM if the original request actually landed.

    Safe precisely because of the wait-before-claim ordering above: SENDING only
    ever spans one HTTP call, so "old and still SENDING" unambiguously means
    "nobody is working on this".
    """
    result = await db.execute(
        """
        UPDATE dm_jobs
        SET status = 'QUEUED', next_attempt_at = now(), updated_at = now()
        WHERE status = 'SENDING'
          AND updated_at < now() - make_interval(secs => $1)
        """,
        float(config.SENDING_STALE_SECONDS),
    )
    count = int(result.rsplit(" ", 1)[-1]) if result.startswith("UPDATE") else 0
    if count:
        log.warning("requeued %d stale SENDING job(s)", count)
    return count
