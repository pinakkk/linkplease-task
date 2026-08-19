# Copyright (c) 2026 Pinak Kundu. All rights reserved.
# Licensed under the Business Source License 1.1 (see LICENSE).
# No use, copying, or modification without written permission.
"""The reconciler: 202 is not a delivery (BLUEPRINT §4.5, Part C).

Roughly 15% of DMs the API accepts end up `failed`, and the only way to learn
that is to ask. This loop polls `GET /v1/dm/{dm_id}` for every job sitting in
AWAITING_CONFIRM and moves it to its real fate.

This is the ONLY path that makes a job count as `sent` in /stats. That is the
whole point: a system that counted 202s would report numbers that are ~15%
higher than the truth, and the assignment says inflated numbers are worse than
honest low ones.

Reads are free against the rate limit (ASSIGNMENT), so nothing here touches the
send budget.
"""
import asyncio
import logging
import time

from . import config, db, pseudogram

log = logging.getLogger("linkplease.reconciler")

# Read by /healthz, same contract as worker.LAST_BEAT.
LAST_BEAT: float = time.time()

# How many jobs one pass looks at, and how many polls run concurrently. The
# reads are free but not instant; modest parallelism drains a burst backlog
# without opening 500 sockets at once.
BATCH_SIZE = 50
CONCURRENCY = 10


def next_check_delay(checks: int) -> float:
    """Poll schedule for a job, indexed by how many times we have already polled
    it this cycle: 2s, 5s, 10s, 30s, then every 60s forever.

    Front-loaded because most DMs resolve within seconds; the 60s tail means a
    job their side never resolves costs us almost nothing to keep watching.
    """
    schedule = config.CONFIRM_SCHEDULE
    if checks < len(schedule):
        return float(schedule[checks])
    return float(config.CONFIRM_INTERVAL_AFTER)


async def reconciler_loop() -> None:
    """Forever: find due AWAITING_CONFIRM jobs and resolve them."""
    global LAST_BEAT
    while True:
        LAST_BEAT = time.time()
        try:
            rows = await db.fetch(
                """
                SELECT job_id, dm_id, cycle, checks
                FROM dm_jobs
                WHERE status = 'AWAITING_CONFIRM'
                  AND (check_after IS NULL OR check_after <= now())
                ORDER BY check_after NULLS FIRST
                LIMIT $1
                """,
                BATCH_SIZE,
            )
            for start in range(0, len(rows), CONCURRENCY):
                chunk = rows[start:start + CONCURRENCY]
                await asyncio.gather(
                    *(_reconcile_one(dict(r)) for r in chunk),
                    return_exceptions=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reconciler iteration failed")
        await asyncio.sleep(config.RECONCILER_INTERVAL)


async def _reconcile_one(job: dict) -> None:
    """Poll one job's delivery status and act on it."""
    job_id = job["job_id"]
    dm_id = job["dm_id"]

    if not dm_id:
        # AWAITING_CONFIRM with no dm_id should be impossible (the worker sets
        # both in one statement). If it happens we cannot ask about it, so
        # requeue in a new cycle rather than leaving it stranded forever.
        log.error("job %s is AWAITING_CONFIRM with no dm_id; requeueing", job_id)
        await _requeue_new_cycle(job_id, job["cycle"])
        return

    try:
        status = await pseudogram.get_dm(dm_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("get_dm raised for job %s", job_id)
        status = None

    if status == "delivered":
        await db.execute(
            """
            UPDATE dm_jobs SET status = 'SENT', last_error = NULL,
                               check_after = NULL, updated_at = now()
            WHERE job_id = $1
            """,
            job_id,
        )
        log.info("job %s delivered (dm_id=%s)", job_id, dm_id)
        return

    if status == "failed":
        if job["cycle"] + 1 < config.MAX_CYCLES:
            await _requeue_new_cycle(job_id, job["cycle"])
            log.warning("job %s reported failed; resending in cycle %s",
                        job_id, job["cycle"] + 1)
        else:
            await db.execute(
                """
                UPDATE dm_jobs SET status = 'FAILED', last_error = $2,
                                   check_after = NULL, updated_at = now()
                WHERE job_id = $1
                """,
                job_id,
                f"delivery failed after {job['cycle'] + 1} cycle(s)",
            )
            log.error("job %s failed permanently after %s cycle(s)",
                      job_id, job["cycle"] + 1)
        return

    # "queued", or None because the read itself failed. Either way we learned
    # nothing terminal, so we schedule the next poll and leave the job in
    # AWAITING_CONFIRM — where /stats keeps counting it as `queued`. A DM their
    # side never resolves therefore shows up as owed forever, which is honest.
    checks = job["checks"] + 1
    await db.execute(
        """
        UPDATE dm_jobs
        SET checks = $2, check_after = now() + make_interval(secs => $3),
            updated_at = now()
        WHERE job_id = $1
        """,
        job_id, checks, next_check_delay(checks),
    )


async def _requeue_new_cycle(job_id: int, cycle: int) -> None:
    """Send it again from scratch under a NEW cycle number.

    cycle+1 changes the Idempotency-Key. Reusing the old key would just hand us
    back the original, already-failed dm_id forever and the "resend" would be a
    no-op loop (BLUEPRINT §4.4).
    """
    await db.execute(
        """
        UPDATE dm_jobs
        SET status = 'QUEUED', cycle = $2, attempt = 0, dm_id = NULL,
            checks = 0, check_after = NULL, next_attempt_at = now(),
            updated_at = now()
        WHERE job_id = $1
        """,
        job_id, cycle + 1,
    )
