"""End-to-end drills against the in-process fake PseudoGram.

This is the file that actually tests the assignment's four hard promises:

* the same user never gets DMed twice for the same rule;
* no DM is silently lost when the API fails;
* a 202 that later reports `failed` is caught and resent (Part C);
* `comment.deleted` is handled sensibly, including out of order.

Everything asserts against the fake API's recorded traffic (which sends it
actually received, with which `Idempotency-Key`) and against the job rows in
Postgres — never against a log line or a return value alone.
"""
import asyncio

import pytest

from tests import fake_pseudogram
from tests.conftest import comment_event, deleted_event, encode, require, wait_until


@pytest.fixture(autouse=True)
def need_agent_b():
    require("matcher", "handle_event")
    require("worker", "send_worker_loop")
    require("reconciler", "reconciler_loop")
    require("pseudogram", "send_dm", "get_dm")


@pytest.fixture
def fast_retries(monkeypatch):
    """Compress the backoff so a 5-attempt exhaustion drill runs in a second
    instead of 2^5 seconds. The *policy* under test is unchanged."""
    from app import config

    monkeypatch.setattr(config, "BACKOFF_CAP_SECONDS", 0.05)
    monkeypatch.setattr(config, "CONFIRM_SCHEDULE", (0, 0, 0, 0))
    monkeypatch.setattr(config, "CONFIRM_INTERVAL_AFTER", 0.1)
    for module_name in ("worker", "reconciler"):
        try:
            module = __import__(f"app.{module_name}", fromlist=["x"])
        except ImportError:
            continue
        for attr, value in (
            ("BACKOFF_CAP_SECONDS", 0.05),
            ("CONFIRM_SCHEDULE", (0, 0, 0, 0)),
            ("CONFIRM_INTERVAL_AFTER", 0.1),
        ):
            if hasattr(module, attr):
                monkeypatch.setattr(module, attr, value)


async def _ingest(matcher, event: dict) -> None:
    """Feed an event straight to the matcher — the webhook path is covered in
    test_webhook.py, and going direct keeps these drills about the pipeline."""
    await matcher.handle_event(event["event_id"], event["event_type"], event["data"])


async def _job(jobs, **where):
    rows = await jobs(**where)
    return rows[0] if rows else None


async def _status(jobs, job_id):
    row = await _job(jobs, job_id=job_id)
    return row["status"] if row else None


# --- 1. Happy path: SENT only after the reconciler confirms delivery ---------

async def test_matching_comment_sends_once_and_reaches_sent_only_after_confirm(
    create_rule, jobs, loops, waiter, fast_retries
):
    import app.matcher as matcher

    await create_rule("PRICE", "Here is the price list")
    # The DM stays 'queued' on their side until we flip it, so we can prove SENT
    # is not reached on the 202 alone.
    fake_pseudogram.CONFIG.deliver_delay = 3600.0
    fake_pseudogram.CONFIG.deliver_script = ["delivered"]

    await _ingest(matcher, comment_event(text="PRICE?", user_id="usr_happy"))
    job = await _job(jobs)
    assert job is not None

    loops("worker", "reconciler")

    await waiter(lambda: _status(jobs, job["job_id"]) == "AWAITING_CONFIRM",
                 timeout=10.0, message="job never reached AWAITING_CONFIRM")

    sends = fake_pseudogram.accepted_sends()
    assert len(sends) == 1, f"expected exactly one send, saw {len(sends)}"
    assert sends[0].recipient_user_id == "usr_happy"
    assert sends[0].message == "Here is the price list"

    # Still not SENT: a 202 is not a delivery (ASSIGNMENT).
    await asyncio.sleep(0.3)
    assert await _status(jobs, job["job_id"]) == "AWAITING_CONFIRM", (
        "job reached SENT on the 202 alone; only reconciler-confirmed "
        "'delivered' may count as sent"
    )

    # Now let it deliver.
    fake_pseudogram.force_delivery(sends[0].dm_id, "delivered")
    await waiter(lambda: _status(jobs, job["job_id"]) == "SENT",
                 timeout=10.0, message="reconciler never promoted the job to SENT")
    assert len(fake_pseudogram.accepted_sends()) == 1, "confirmation caused a resend"


# --- 2. 202 then failed -> resend with a DIFFERENT idempotency key ------------

async def test_accepted_then_failed_is_resent_with_a_new_idempotency_key(
    create_rule, jobs, loops, waiter, fast_retries, api
):
    """ASSIGNMENT: '~15% of accepted DMs end up as failed. You only find out by
    checking.' The resend MUST use a new key — reusing `job:N:c0` would just
    return the original *failed* dm_id forever (BLUEPRINT §4.4)."""
    import app.matcher as matcher

    await create_rule("PRICE")
    fake_pseudogram.CONFIG.deliver_script = ["failed", "delivered"]

    await _ingest(matcher, comment_event(text="PRICE", user_id="usr_flip"))
    job = await _job(jobs)
    job_id = job["job_id"]

    loops("worker", "reconciler")

    await waiter(lambda: _status(jobs, job_id) == "SENT", timeout=20.0,
                 message="job never recovered from the failed delivery")

    keys = [s.idempotency_key for s in fake_pseudogram.accepted_sends()]
    assert len(keys) == 2, f"expected two sends (original + resend), saw {keys}"
    assert keys[0] == f"job:{job_id}:c0", f"first key was {keys[0]!r}"
    assert keys[1] == f"job:{job_id}:c1", (
        f"resend used key {keys[1]!r}; it must change with the cycle or the API "
        "returns the original failed dm_id and the resend is a no-op loop"
    )
    assert len(set(keys)) == 2

    row = await _job(jobs, job_id=job_id)
    assert row["cycle"] == 1


async def test_failed_delivery_does_not_count_as_sent_meanwhile(
    create_rule, jobs, loops, waiter, fast_retries, api
):
    """While the resend is pending the job must report as `queued`, not `sent`.
    'Inflated numbers are worse than honest low numbers' (ASSIGNMENT)."""
    import app.matcher as matcher

    await create_rule("PRICE")
    # First delivery fails; the *second* hangs in 'queued' so we can observe the
    # in-between state.
    fake_pseudogram.CONFIG.deliver_script = ["failed"]
    fake_pseudogram.CONFIG.deliver_failure_rate = 0.0

    await _ingest(matcher, comment_event(text="PRICE", user_id="usr_mid"))
    job_id = (await _job(jobs))["job_id"]
    loops("worker", "reconciler")

    await waiter(lambda: _cycle_at_least(jobs, job_id, 1), timeout=20.0,
                 message="reconciler never noticed the failed delivery")

    resp = await api.get("/stats")
    body = resp.json()
    assert body["sent"] == 0, (
        "a DM the API reported as failed was still counted as sent"
    )
    row = await _job(jobs, job_id=job_id)
    assert row["status"] in ("QUEUED", "SENDING", "AWAITING_CONFIRM")


async def _cycle_at_least(jobs, job_id, n):
    row = await _job(jobs, job_id=job_id)
    return row is not None and row["cycle"] >= n


# --- 3. Five consecutive 500s -> FAILED, not lost ----------------------------

async def test_five_consecutive_500s_fail_the_job_honestly(
    create_rule, jobs, loops, waiter, fast_retries, api, counters
):
    """~20% of calls 500 (ASSIGNMENT). Retrying is safe, but not forever: after
    MAX_ATTEMPTS the job is FAILED — a row that still exists and is still
    reported, never a silently dropped obligation."""
    import app.matcher as matcher
    from app import config

    await create_rule("PRICE")
    fake_pseudogram.CONFIG.script = ["server_error"] * (config.MAX_ATTEMPTS + 3)

    await _ingest(matcher, comment_event(text="PRICE", user_id="usr_500"))
    job_id = (await _job(jobs))["job_id"]

    loops("worker")
    await waiter(lambda: _status(jobs, job_id) == "FAILED", timeout=20.0,
                 message="job never reached FAILED after repeated 500s")

    row = await _job(jobs, job_id=job_id)
    assert row["attempt"] >= config.MAX_ATTEMPTS
    assert row["last_error"], "a FAILED job must record why"

    attempts = [s for s in fake_pseudogram.send_attempts()
                if s.outcome == "server_error"]
    assert len(attempts) == config.MAX_ATTEMPTS, (
        f"made {len(attempts)} send attempts, policy is MAX_ATTEMPTS="
        f"{config.MAX_ATTEMPTS}"
    )

    resp = await api.get("/stats")
    assert resp.json()["failed"] == 1
    assert resp.json()["sent"] == 0


async def test_a_500_then_success_recovers(
    create_rule, jobs, loops, waiter, fast_retries
):
    """Retries must actually retry: two 500s then a 202 is a delivered DM."""
    import app.matcher as matcher

    await create_rule("PRICE")
    fake_pseudogram.CONFIG.script = ["server_error", "server_error", "accepted"]

    await _ingest(matcher, comment_event(text="PRICE", user_id="usr_recover"))
    job_id = (await _job(jobs))["job_id"]

    loops("worker", "reconciler")
    await waiter(lambda: _status(jobs, job_id) == "SENT", timeout=20.0,
                 message="job never recovered after two 500s")
    assert len(fake_pseudogram.accepted_sends()) == 1


# --- 4. 400 -> FAILED immediately, exactly one attempt ------------------------

async def test_400_fails_immediately_after_one_attempt(
    create_rule, jobs, loops, waiter, fast_retries, api
):
    """'Your payload is malformed. Retrying will not help.' (ASSIGNMENT).
    Burning five attempts on a 400 would waste rate budget other jobs need."""
    import app.matcher as matcher

    await create_rule("PRICE")
    fake_pseudogram.CONFIG.script = ["bad_request"] * 6

    await _ingest(matcher, comment_event(text="PRICE", user_id="usr_400"))
    job_id = (await _job(jobs))["job_id"]

    loops("worker")
    await waiter(lambda: _status(jobs, job_id) == "FAILED", timeout=10.0,
                 message="a 400 did not fail the job")

    await asyncio.sleep(0.3)  # let a buggy retry show itself
    attempts = fake_pseudogram.send_attempts()
    assert len(attempts) == 1, (
        f"a 400 was retried: {len(attempts)} send attempts. Retrying a malformed "
        "payload cannot help and spends rate budget"
    )
    assert (await api.get("/stats")).json()["failed"] == 1


# --- 5. The headline promise: one DM per (rule, user) -------------------------

async def test_same_user_five_comments_one_dm_four_duplicates_blocked(
    create_rule, jobs, loops, waiter, fast_retries, api, counters
):
    """ASSIGNMENT Part A: 'The same user never gets DMed twice for the same
    rule, no matter how many times they comment.'"""
    import app.matcher as matcher

    await create_rule("PRICE")
    for i in range(5):
        await _ingest(matcher, comment_event(
            text=f"PRICE? ({i})", user_id="usr_spam", comment_id=f"cmt_spam_{i}"))

    rows = await jobs()
    assert len(rows) == 1, f"5 comments produced {len(rows)} obligations, expected 1"
    assert await counters("duplicates_blocked_rule_user") == 4

    loops("worker", "reconciler")
    await waiter(lambda: _status(jobs, rows[0]["job_id"]) == "SENT", timeout=15.0,
                 message="the single obligation never completed")

    dms = fake_pseudogram.dms_to("usr_spam")
    assert len(dms) == 1, f"user received {len(dms)} DMs for one rule"

    body = (await api.get("/stats")).json()
    assert body["duplicates_blocked"] == 4
    assert body["sent"] == 1


# --- 6. comment.deleted before the send --------------------------------------

async def test_delete_before_send_cancels_the_job_with_zero_sends(
    create_rule, jobs, loops, waiter, fast_retries
):
    import app.matcher as matcher

    await create_rule("PRICE")
    event = comment_event(text="PRICE", user_id="usr_del", comment_id="cmt_del")
    await _ingest(matcher, event)
    job_id = (await _job(jobs))["job_id"]
    assert await _status(jobs, job_id) == "QUEUED"

    # Delete arrives before the worker ever runs.
    await _ingest(matcher, deleted_event("cmt_del"))

    assert await _status(jobs, job_id) == "CANCELLED"

    loops("worker")
    await asyncio.sleep(0.5)
    assert fake_pseudogram.send_attempts() == [], (
        "a DM was sent for a comment that had already been deleted"
    )


async def test_delete_out_of_order_before_create_creates_no_job(
    create_rule, jobs, loops
):
    """ASSIGNMENT: 'Order is not guaranteed... Think about what should happen if
    it arrives before you've sent the DM.' The tombstone table is what stops a
    ghost DM here (BLUEPRINT §4.2)."""
    import app.matcher as matcher

    await create_rule("PRICE")
    await _ingest(matcher, deleted_event("cmt_ooo"))
    await _ingest(matcher, comment_event(
        text="PRICE", user_id="usr_ooo", comment_id="cmt_ooo"))

    live = [r for r in await jobs() if r["status"] != "CANCELLED"]
    assert live == [], "a tombstoned comment produced a live DM obligation"

    loops("worker")
    await asyncio.sleep(0.5)
    assert fake_pseudogram.send_attempts() == []


async def test_delete_of_a_different_comment_does_not_cancel(
    create_rule, jobs, loops, waiter, fast_retries
):
    import app.matcher as matcher

    await create_rule("PRICE")
    await _ingest(matcher, comment_event(
        text="PRICE", user_id="usr_keep", comment_id="cmt_keep"))
    await _ingest(matcher, deleted_event("cmt_unrelated"))

    job = await _job(jobs)
    assert job["status"] == "QUEUED"


# --- 7. Revival: CANCELLED + a new comment -> a real DM ----------------------

async def test_cancelled_job_revived_by_a_new_comment_sends_exactly_one_dm(
    create_rule, jobs, loops, waiter, fast_retries
):
    """BLUEPRINT §3 revival rule: the user never received the cancelled DM, so a
    new qualifying comment is a legitimately new obligation — same row, back to
    QUEUED, cycle+1 so the idempotency key is fresh."""
    import app.matcher as matcher

    await create_rule("PRICE")
    await _ingest(matcher, comment_event(
        text="PRICE", user_id="usr_revive", comment_id="cmt_r1"))
    job_id = (await _job(jobs))["job_id"]
    await _ingest(matcher, deleted_event("cmt_r1"))
    assert await _status(jobs, job_id) == "CANCELLED"
    cancelled_cycle = (await _job(jobs, job_id=job_id))["cycle"]

    # A brand new comment from the same user.
    await _ingest(matcher, comment_event(
        text="PRICE again", user_id="usr_revive", comment_id="cmt_r2"))

    rows = await jobs()
    assert len(rows) == 1, (
        f"revival created a second row ({len(rows)} total); it must reuse the "
        "same (rule,user) obligation"
    )
    revived = rows[0]
    assert revived["status"] == "QUEUED"
    assert revived["cycle"] == cancelled_cycle + 1, (
        "revival must bump the cycle so the resend gets a fresh Idempotency-Key"
    )
    assert revived["comment_id"] == "cmt_r2"

    loops("worker", "reconciler")
    await waiter(lambda: _status(jobs, job_id) == "SENT", timeout=15.0,
                 message="revived job never sent")
    assert len(fake_pseudogram.dms_to("usr_revive")) == 1


# --- 8. Timeout then retry: SAME key, exactly one DM -------------------------

async def test_timeout_then_retry_reuses_the_key_and_yields_one_dm(
    create_rule, jobs, loops, waiter, fast_retries
):
    """BLUEPRINT §5 row 6: the send may have landed even though we never saw the
    response. The same-cycle Idempotency-Key is the only thing standing between
    us and a double DM."""
    import app.matcher as matcher

    await create_rule("PRICE")
    # First call hangs past the client timeout (but the stub still creates
    # nothing for it), then the retry succeeds.
    fake_pseudogram.CONFIG.script = ["timeout", "accepted"]

    await _ingest(matcher, comment_event(text="PRICE", user_id="usr_timeout"))
    job_id = (await _job(jobs))["job_id"]

    loops("worker", "reconciler")
    await waiter(lambda: _status(jobs, job_id) in ("AWAITING_CONFIRM", "SENT"),
                 timeout=25.0, message="job never recovered from the timeout")

    keys = [s.idempotency_key for s in fake_pseudogram.send_attempts()]
    assert len(set(k for k in keys if k)) == 1, (
        f"retry after a timeout used a different key: {keys}. The same cycle "
        "must reuse the key or the retry can double-send"
    )
    assert keys[0] == f"job:{job_id}:c0"
    assert len(fake_pseudogram.dms_to("usr_timeout")) == 1


async def test_duplicate_send_with_same_key_returns_the_original_dm_id():
    """A property of the stub itself, asserted so the tests above mean what they
    say: same key in, same dm_id out, no second DM created."""
    import app.pseudogram as pg

    first = await pg.send_dm("usr_k", "hello", "cmt_k", "job:99:c0")
    second = await pg.send_dm("usr_k", "hello", "cmt_k", "job:99:c0")
    assert first.outcome == "accepted"
    assert second.dm_id == first.dm_id
    assert len(fake_pseudogram.distinct_dms()) == 1


# --- 9. Concurrency: many users, no cross-contamination ----------------------

async def test_burst_of_distinct_users_each_get_exactly_one_dm(
    create_rule, jobs, loops, waiter, fast_retries
):
    import app.matcher as matcher

    await create_rule("PRICE")
    users = [f"usr_burst_{i}" for i in range(6)]
    for u in users:
        # Each user comments twice — only the first should create an obligation.
        await _ingest(matcher, comment_event(text="PRICE", user_id=u))
        await _ingest(matcher, comment_event(text="PRICE again", user_id=u))

    assert len(await jobs()) == 6

    loops("worker", "reconciler")
    await waiter(lambda: _all_terminal(jobs), timeout=30.0,
                 message="burst never drained")

    for u in users:
        assert len(fake_pseudogram.dms_to(u)) == 1, f"{u} got the wrong DM count"


async def _all_terminal(jobs):
    rows = await jobs()
    return rows and all(r["status"] in ("SENT", "FAILED", "CANCELLED") for r in rows)
