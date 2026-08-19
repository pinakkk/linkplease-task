"""Rate budget for POST /v1/dm/send.

Their limit is 10 requests per rolling 60 seconds. We spend `config.RATE_LIMIT_MAX`
(9) and bank one, because our clock and their clock do not agree and the spare
request absorbs the skew (BLUEPRINT §4.4).

The ledger lives in Postgres (`send_log`), not in memory, for two reasons:
  1. It survives a restart — an in-memory counter would let a redeploy blow the
     budget in the first seconds after boot.
  2. There is exactly one send loop, so there is one writer and no races; the
     table is simply the durable record of that loop's spending.

Every arithmetic comparison uses the DATABASE clock on both sides. Our local
clock never enters the calculation, so a skewed container clock cannot make us
sleep too little (429s) or too long (a stalled queue).
"""
import asyncio
import logging

import asyncpg

from . import config, db

log = logging.getLogger("linkplease.ratelimit")

# Never sleep a zero/negative interval (that would spin the loop hot), and never
# sleep longer than one window (a clock jump must not park the worker forever).
_MIN_SLEEP = 0.25


async def used_in_window() -> int:
    """How many sends we have issued inside the current rolling window."""
    value = await db.fetchval(
        "SELECT count(*) FROM send_log WHERE sent_at > now() - make_interval(secs => $1)",
        float(config.RATE_LIMIT_WINDOW_SECONDS),
    )
    return int(value or 0)


async def window_seconds() -> int:
    """The window length, exposed so /healthz and the dashboard can describe the
    budget without importing config themselves."""
    return int(config.RATE_LIMIT_WINDOW_SECONDS)


async def _seconds_until_slot() -> float:
    """Seconds until the oldest in-window send falls out of the window, computed
    entirely inside Postgres so both `now()` and `sent_at` come from one clock.

    Returns 0.0 if there is nothing in the window (a slot is already free).
    """
    value = await db.fetchval(
        """
        SELECT EXTRACT(EPOCH FROM
                 (min(sent_at) + make_interval(secs => $1)) - now())
        FROM send_log
        WHERE sent_at > now() - make_interval(secs => $1)
        """,
        float(config.RATE_LIMIT_WINDOW_SECONDS),
    )
    if value is None:
        return 0.0
    return float(value)


async def wait_for_budget() -> None:
    """Block until issuing one more send keeps us inside the budget.

    Loop rather than sleep-once: after waking we re-check, because the window is
    rolling and our arithmetic could be a few milliseconds early.
    """
    while True:
        used = await used_in_window()
        if used < config.RATE_LIMIT_MAX:
            return
        wait = await _seconds_until_slot()
        # Clamp: a positive floor stops a hot spin when the slot frees within
        # microseconds; the window is the ceiling because no in-window entry can
        # ever be more than one window away from expiring.
        wait = max(_MIN_SLEEP, min(wait, float(config.RATE_LIMIT_WINDOW_SECONDS)))
        log.info("rate budget full (%s/%s), sleeping %.2fs",
                 used, config.RATE_LIMIT_MAX, wait)
        await asyncio.sleep(wait)


async def record_send(job_id: int, conn: asyncpg.Connection | None = None) -> None:
    """Record that a request was ISSUED to POST /v1/dm/send.

    Called for every issued request — including ones that came back 429, 500, or
    timed out. Those still reached their limiter and still consumed one of their
    ten. Counting only successes would be the optimistic reading and would drift
    us straight into sustained 429s. This conservative reading is deliberate.
    """
    sql = "INSERT INTO send_log (job_id) VALUES ($1)"
    if conn is not None:
        await conn.execute(sql, job_id)
    else:
        await db.execute(sql, job_id)


async def prune(older_than_seconds: int = 3600) -> None:
    """Drop ledger rows far outside any window. Only the last 60 seconds are ever
    read; the rest is kept briefly for debugging and then discarded so the table
    does not grow without bound over a long run."""
    await db.execute(
        "DELETE FROM send_log WHERE sent_at < now() - make_interval(secs => $1)",
        float(older_than_seconds),
    )
