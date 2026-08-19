"""The crash drill (BLUEPRINT §9.2, §5 rows 7 and 8).

The central claim of this design is that Postgres is the queue, so a restart
loses nothing. That claim is worthless unless it is tested, and it is exactly
the kind of claim that is easy to believe and false.

Two failure modes, both drilled here:

* **crash mid-send** — a job left in SENDING. On boot it must return to QUEUED
  in the SAME cycle, so the retry reuses `job:N:c0` and the API's idempotency
  dedups it to one DM. Bumping the cycle here would be a real double-DM bug.
* **crash after the 200 but before matching** — an `events` row with
  `processed_at` NULL. We already told PseudoGram we had that event; nobody is
  going to redeliver it, so we must replay it ourselves.
"""
import asyncio
import json

import pytest

from tests import fake_pseudogram
from tests.conftest import comment_event, encode, require, wait_until


async def _insert_stale_sending(pool, rule_id, user_id, age_seconds, cycle=0):
    """A job the process died inside: status SENDING, updated_at long ago."""
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO dm_jobs (rule_id, user_id, comment_id, status, attempt,
                                 cycle, updated_at, next_attempt_at)
            VALUES ($1, $2, $3, 'SENDING', 1, $4,
                    now() - make_interval(secs => $5),
                    now() - make_interval(secs => $5))
            RETURNING job_id
            """,
            rule_id, user_id, f"cmt_{user_id}", cycle, float(age_seconds),
        )


async def _job(jobs, job_id):
    rows = await jobs(job_id=job_id)
    return rows[0] if rows else None


# --- (a) Stale SENDING is requeued in the same cycle -------------------------

async def test_boot_recovery_requeues_stale_sending_with_same_cycle(
    pool, create_rule, jobs
):
    from app import config, main

    rule_id = await create_rule("PRICE")
    stale = await _insert_stale_sending(
        pool, rule_id, "usr_crash", config.SENDING_STALE_SECONDS + 30, cycle=0)

    requeued = await main.requeue_stale_sending()
    assert requeued == 1, "boot recovery did not pick up the orphaned SENDING job"

    row = await _job(jobs, stale)
    assert row["status"] == "QUEUED", (
        "a job orphaned in SENDING stayed there forever — that is a silently "
        "lost DM, the exact thing Part A forbids"
    )
    assert row["cycle"] == 0, (
        f"boot recovery bumped the cycle to {row['cycle']}. The cycle must NOT "
        "change: the same Idempotency-Key is what lets the API dedup a send "
        "that may already have landed (BLUEPRINT §5 row 7)"
    )


async def test_boot_recovery_leaves_fresh_sending_alone(pool, create_rule, jobs):
    """A job that has been SENDING for two seconds is a live send in another
    coroutine, not a crash. Requeuing it would double-send."""
    from app import main

    rule_id = await create_rule("PRICE")
    fresh = await _insert_stale_sending(pool, rule_id, "usr_live", 1.0)

    await main.requeue_stale_sending()
    assert (await _job(jobs, fresh))["status"] == "SENDING"


async def test_recovered_job_produces_exactly_one_dm(
    pool, create_rule, jobs, loops, waiter
):
    """The full drill: the pre-crash send DID land at the API (same
    Idempotency-Key already recorded there), the process died before writing
    the dm_id, boot recovery requeues, the worker retries — and the API's
    idempotency must collapse it to ONE DM."""
    require("worker", "send_worker_loop")
    require("pseudogram", "send_dm")
    import app.pseudogram as pg
    from app import config, main

    rule_id = await create_rule("PRICE")
    job_id = await _insert_stale_sending(
        pool, rule_id, "usr_dup", config.SENDING_STALE_SECONDS + 30, cycle=0)

    # Simulate the pre-crash send having reached the API with the cycle-0 key.
    pre = await pg.send_dm("usr_dup", "Here is the price list", "cmt_usr_dup",
                           f"job:{job_id}:c0")
    assert pre.outcome == "accepted"
    assert len(fake_pseudogram.distinct_dms()) == 1

    await main.requeue_stale_sending()

    loops("worker", "reconciler")
    await waiter(
        lambda: _status_in(jobs, job_id, ("AWAITING_CONFIRM", "SENT")),
        timeout=15.0, message="requeued job never got sent again",
    )

    dms = fake_pseudogram.dms_to("usr_dup")
    assert len(dms) == 1, (
        f"the crash-recovery retry created {len(dms)} DMs. The retry must reuse "
        f"job:{job_id}:c0 so the API returns the original dm_id"
    )
    row = await _job(jobs, job_id)
    assert row["dm_id"] == pre.dm_id, (
        f"job recorded dm_id {row['dm_id']!r}, but the API's original was "
        f"{pre.dm_id!r} — the idempotent response was not used"
    )


async def _status_in(jobs, job_id, statuses):
    row = await _job(jobs, job_id)
    return row is not None and row["status"] in statuses


# --- (b) Unprocessed events are replayed after a restart ----------------------

async def _insert_unprocessed_event(pool, payload):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO events (event_id, event_type, payload, processed_at)
            VALUES ($1, $2, $3::jsonb, NULL)
            """,
            payload["event_id"], payload["event_type"], json.dumps(payload),
        )


async def test_boot_replays_events_that_were_acked_but_never_matched(
    pool, create_rule, jobs
):
    """We returned 200 for these, so PseudoGram considers them delivered and
    will never send them again. If we do not replay them the obligation is gone
    for good — a silently lost DM."""
    require("matcher", "handle_event")
    from app import main

    await create_rule("PRICE")
    await _insert_unprocessed_event(pool, comment_event(
        text="PRICE please", user_id="usr_replay", event_id="evt_replay"))

    replayed = await main.redispatch_unprocessed_events()
    assert replayed == 1

    rows = await jobs()
    assert len(rows) == 1, (
        "an acknowledged-but-unmatched event was never replayed; that DM "
        "obligation is lost permanently since nobody will redeliver it"
    )
    assert rows[0]["user_id"] == "usr_replay"


async def test_replay_of_an_already_matched_event_does_not_duplicate(
    pool, create_rule, jobs
):
    """Replay must be idempotent: if the crash happened after the job insert but
    before `processed_at` was written, replaying must not create a second job."""
    require("matcher", "handle_event")
    import app.matcher as matcher
    from app import main

    await create_rule("PRICE")
    event = comment_event(text="PRICE", user_id="usr_twice", event_id="evt_twice")
    await _insert_unprocessed_event(pool, event)
    await matcher.handle_event(event["event_id"], event["event_type"], event["data"])

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE events SET processed_at = NULL WHERE event_id = $1",
            "evt_twice")

    await main.redispatch_unprocessed_events()

    rows = await jobs()
    assert len(rows) == 1, (
        f"replaying an already-matched event created {len(rows)} jobs; the "
        "uq_live_job constraint should make replay idempotent"
    )


async def test_matcher_sweep_picks_up_unprocessed_events(
    pool, create_rule, jobs, loops, waiter
):
    """Belt and braces: even without a restart, the sweep loop must find events
    whose dispatch task died (BLUEPRINT §4.1 — the DB is the real queue, the
    create_task is just a nudge)."""
    require("matcher", "matcher_sweep_loop")

    await create_rule("PRICE")
    await _insert_unprocessed_event(pool, comment_event(
        text="PRICE", user_id="usr_sweep", event_id="evt_sweep"))

    loops("matcher")
    rows = await waiter(lambda: jobs(), timeout=10.0,
                        message="the matcher sweep never picked up the orphan event")
    assert len(rows) == 1
    assert rows[0]["user_id"] == "usr_sweep"


async def test_no_obligation_is_lost_across_a_simulated_restart(
    pool, create_rule, jobs, loops, waiter
):
    """The combined drill: three obligations in three different pre-crash
    states, one restart, and afterwards every one of them is either delivered or
    still owed — none has vanished, and nobody got two DMs."""
    require("worker", "send_worker_loop")
    require("matcher", "handle_event")
    from app import config, main

    rule_id = await create_rule("PRICE")

    # (1) orphaned mid-send
    orphan = await _insert_stale_sending(
        pool, rule_id, "usr_r1", config.SENDING_STALE_SECONDS + 30)
    # (2) acked event, never matched
    await _insert_unprocessed_event(pool, comment_event(
        text="PRICE", user_id="usr_r2", event_id="evt_r2"))
    # (3) plain queued job that was simply waiting its turn
    async with pool.acquire() as conn:
        queued = await conn.fetchval(
            "INSERT INTO dm_jobs (rule_id, user_id, comment_id, status) "
            "VALUES ($1,'usr_r3','cmt_r3','QUEUED') RETURNING job_id", rule_id)

    # --- the restart ---
    await main.requeue_stale_sending()
    await main.redispatch_unprocessed_events()

    loops("worker", "reconciler")
    await waiter(lambda: _all_settled(jobs), timeout=30.0,
                 message="queue never drained after the restart")

    rows = await jobs()
    users = {r["user_id"] for r in rows}
    assert users == {"usr_r1", "usr_r2", "usr_r3"}, (
        f"obligations lost across the restart: expected 3 users, got {users}"
    )
    for user in users:
        dms = fake_pseudogram.dms_to(user)
        assert len(dms) <= 1, f"{user} received {len(dms)} DMs across the restart"
    for row in rows:
        assert row["status"] in ("SENT", "AWAITING_CONFIRM", "QUEUED", "FAILED")


async def _all_settled(jobs):
    rows = await jobs()
    return len(rows) == 3 and all(
        r["status"] in ("SENT", "FAILED") for r in rows)
