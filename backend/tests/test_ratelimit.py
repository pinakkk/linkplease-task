"""The rolling-window rate budget.

Their limit is 10 requests per rolling 60s (ASSIGNMENT). We spend 9 and bank one
for clock skew (BLUEPRINT §4.4). The property that actually matters is not "we
sleep sometimes" but: **at no instant does any 60-second span contain more than
the configured maximum sends**. That is what the test below measures, by driving
real sends through the fake API with the window compressed to a couple of
seconds so the assertion is cheap.

Reads (`GET /v1/dm/{id}`) are explicitly free and must never enter the ledger.
"""
import asyncio
import time

import pytest

from tests.conftest import require, wait_until


@pytest.fixture
def rl():
    return require("ratelimit", "wait_for_budget", "record_send", "used_in_window")


@pytest.fixture
def compressed_window(monkeypatch):
    """A 3-second window keeps the whole test under a few seconds while testing
    exactly the same code path as the 60-second production window."""
    from app import config

    monkeypatch.setattr(config, "RATE_LIMIT_WINDOW_SECONDS", 3)
    monkeypatch.setattr(config, "RATE_LIMIT_MAX", 3)
    import app.ratelimit as ratelimit
    for attr, value in (
        ("RATE_LIMIT_WINDOW_SECONDS", 3),
        ("RATE_LIMIT_MAX", 3),
    ):
        if hasattr(ratelimit, attr):
            monkeypatch.setattr(ratelimit, attr, value)
    return 3, 3


async def _fill_window(pool, n: int, seconds_ago: float = 0.0):
    async with pool.acquire() as conn:
        for _ in range(n):
            await conn.execute(
                "INSERT INTO send_log (job_id, sent_at) "
                "VALUES (NULL, now() - make_interval(secs => $1))",
                float(seconds_ago),
            )


# --- used_in_window() ---------------------------------------------------------

async def test_used_in_window_counts_only_the_window(rl, pool):
    from app import config

    await _fill_window(pool, 4, seconds_ago=1.0)
    await _fill_window(pool, 5, seconds_ago=config.RATE_LIMIT_WINDOW_SECONDS + 30)

    used = await rl.used_in_window()
    assert used == 4, (
        f"used_in_window() returned {used}; only the 4 recent sends are inside "
        f"the {config.RATE_LIMIT_WINDOW_SECONDS}s window"
    )


async def test_record_send_appends_to_the_ledger(rl, pool):
    before = await rl.used_in_window()
    await rl.record_send(123)
    assert await rl.used_in_window() == before + 1
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM send_log ORDER BY id DESC LIMIT 1")
    assert row["job_id"] == 123


# --- wait_for_budget() --------------------------------------------------------

async def test_wait_for_budget_returns_immediately_when_under_budget(rl, pool):
    from app import config

    await _fill_window(pool, config.RATE_LIMIT_MAX - 2, seconds_ago=1.0)
    start = time.monotonic()
    await asyncio.wait_for(rl.wait_for_budget(), timeout=2.0)
    assert time.monotonic() - start < 0.5, "must not sleep while budget remains"


async def test_wait_for_budget_blocks_at_the_cap(rl, pool, compressed_window):
    """With the window full, the call must block — not sail through and let us
    breach the limit."""
    window, cap = compressed_window
    await _fill_window(pool, cap, seconds_ago=0.0)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(rl.wait_for_budget(), timeout=0.6)


async def test_wait_for_budget_unblocks_when_the_window_rolls(rl, pool, compressed_window):
    """Blocking forever would be as broken as not blocking at all: once the
    oldest send ages out of the window, the budget must free up on its own."""
    window, cap = compressed_window
    # Entries planted most of the way through the window, so they expire soon.
    await _fill_window(pool, cap, seconds_ago=window - 0.7)

    start = time.monotonic()
    await asyncio.wait_for(rl.wait_for_budget(), timeout=window + 2.0)
    waited = time.monotonic() - start
    assert waited >= 0.3, "returned before the oldest entry could have expired"


# --- The property that matters: the window is never breached -----------------

async def test_rolling_window_never_exceeds_the_cap_under_load(
    rl, pool, compressed_window
):
    """Drive ~15 sends through the budget with a compressed window and assert
    that at EVERY send, the number of sends in the preceding window is within
    the cap. This is the assertion that would have caught a fixed-bucket
    (rather than rolling) limiter."""
    window, cap = compressed_window
    timestamps: list[float] = []

    async def one_send(i: int) -> None:
        await rl.wait_for_budget()
        await rl.record_send(i)
        timestamps.append(time.monotonic())

    for i in range(15):
        await asyncio.wait_for(one_send(i), timeout=window * 3)

    # For every send, count how many sends (including it) fall in the window
    # ending at its timestamp. That is exactly what the server sees.
    for i, t in enumerate(timestamps):
        in_window = sum(1 for s in timestamps[: i + 1] if s > t - window)
        assert in_window <= cap, (
            f"send #{i} saw {in_window} sends inside a {window}s span, cap is "
            f"{cap} — the rolling window is being breached"
        )

    total_span = timestamps[-1] - timestamps[0]
    assert total_span >= window * 3, (
        f"15 sends at {cap} per {window}s should take at least ~{window * 4:.0f}s; "
        f"took {total_span:.2f}s — the limiter is not actually throttling"
    )


async def test_reads_do_not_consume_budget(rl, pool):
    """`GET /v1/dm/{id}` is documented as free. If the reconciler's polls
    entered send_log they would starve the send worker."""
    pg = require("pseudogram", "get_dm", "send_dm")
    from tests import fake_pseudogram

    result = await pg.send_dm("usr_a", "hi", "cmt_1", "job:1:c0")
    assert result.outcome == "accepted"
    baseline = await rl.used_in_window()

    for _ in range(5):
        await pg.get_dm(result.dm_id)

    assert await rl.used_in_window() == baseline, (
        "status reads consumed rate budget; ASSIGNMENT says reads are free"
    )
    assert len(fake_pseudogram.STATE.reads) == 5


# --- A 429 must not burn a retry attempt -------------------------------------

async def test_429_does_not_consume_a_retry_attempt(
    pool, loops, create_rule, jobs, waiter
):
    """BLUEPRINT §4.4: a 429 means 'come back later', not 'this send failed'.
    Counting it as an attempt would burn the 5-attempt budget on backpressure
    and FAIL a job that nothing was ever wrong with."""
    require("worker", "send_worker_loop")
    from tests import fake_pseudogram

    rule_id = await create_rule("PRICE")
    async with pool.acquire() as conn:
        job_id = await conn.fetchval(
            "INSERT INTO dm_jobs (rule_id, user_id, comment_id) "
            "VALUES ($1,$2,$3) RETURNING job_id",
            rule_id, "usr_429", "cmt_429",
        )

    # Three 429s, then success. Retry-After kept tiny so the test stays fast.
    fake_pseudogram.CONFIG.script = ["rate_limited"] * 3 + ["accepted"]
    fake_pseudogram.CONFIG.retry_after = 0

    loops("worker")
    await waiter(
        lambda: _status_is(jobs, job_id, ("AWAITING_CONFIRM", "SENT")),
        timeout=15.0,
        message="job never got past the 429s",
    )

    rows = await jobs(job_id=job_id)
    assert rows[0]["attempt"] == 0, (
        f"attempt counter is {rows[0]['attempt']} after three 429s; a 429 must "
        "not consume a retry attempt (BLUEPRINT §4.4)"
    )


async def _status_is(jobs, job_id, statuses):
    rows = await jobs(job_id=job_id)
    return rows and rows[0]["status"] in statuses
