"""POST /webhook — the ingest contract.

Two graded promises live here (ASSIGNMENT):

* "Must return 200 within 5 seconds" — so nothing on this path may call
  PseudoGram or block on a send.
* "event_id can repeat. We redeliver roughly 8% of events." — a redelivery must
  produce exactly one event row and exactly one DM obligation, not two.
"""
import asyncio
import time

import pytest

from tests.conftest import (
    comment_event,
    deleted_event,
    encode,
    modules_present,
    wait_until,
)


async def _events(pool):
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM events ORDER BY received_at")


# --- The 5-second contract ----------------------------------------------------

async def test_returns_200_fast(api, pool):
    payload = comment_event()
    start = time.monotonic()
    resp = await api.post("/webhook", content=encode(payload))
    elapsed = time.monotonic() - start

    assert resp.status_code == 200
    assert elapsed < 1.0, (
        f"/webhook took {elapsed:.2f}s; the graded budget is 5s and the ingest "
        "path is supposed to be one HMAC check plus one upsert"
    )


async def test_response_is_200_even_with_no_rules(api, pool):
    resp = await api.post("/webhook", content=encode(comment_event()))
    assert resp.status_code == 200


# --- Event-level dedup --------------------------------------------------------

async def test_same_event_id_twice_creates_one_row_and_bumps_redeliveries(api, pool):
    payload = comment_event(event_id="evt_repeat")
    raw = encode(payload)

    r1 = await api.post("/webhook", content=raw)
    r2 = await api.post("/webhook", content=raw)
    assert (r1.status_code, r2.status_code) == (200, 200)

    rows = await _events(pool)
    assert len(rows) == 1, "a redelivered event_id must not create a second row"
    assert rows[0]["redeliveries"] == 1, "the redelivery must be counted"


async def test_redelivery_does_not_create_a_second_job(api, pool, create_rule, jobs):
    if not modules_present("matcher"):
        pytest.skip("app/matcher.py not implemented yet")

    await create_rule("PRICE")
    raw = encode(comment_event(text="PRICE please", user_id="usr_a",
                               event_id="evt_dup", comment_id="cmt_dup"))

    await api.post("/webhook", content=raw)
    await api.post("/webhook", content=raw)
    await api.post("/webhook", content=raw)

    await wait_until(lambda: _len(jobs()), timeout=3.0,
                     message="matcher never created a job")
    await asyncio.sleep(0.2)  # give a buggy second dispatch time to land
    rows = await jobs()
    assert len(rows) == 1, (
        f"3 deliveries of the same event_id produced {len(rows)} jobs; "
        "event-level dedup should stop after the first"
    )


async def _len(coro):
    return len(await coro)


async def test_redelivery_increments_the_suppression_counter(api, pool, counters):
    raw = encode(comment_event(event_id="evt_counted"))
    await api.post("/webhook", content=raw)
    before = await counters("duplicate_events_suppressed")
    await api.post("/webhook", content=raw)
    after = await counters("duplicate_events_suppressed")
    assert after == before + 1


# --- Rejections ---------------------------------------------------------------

async def test_malformed_json_is_400(api, pool):
    resp = await api.post("/webhook", content=b"{not json at all")
    assert resp.status_code == 400
    assert await _events(pool) == []


async def test_non_object_json_is_400(api, pool):
    resp = await api.post("/webhook", content=b'["a","list"]')
    assert resp.status_code == 400
    assert await _events(pool) == []


async def test_missing_event_id_is_400(api, pool):
    payload = comment_event()
    del payload["event_id"]
    resp = await api.post("/webhook", content=encode(payload))
    assert resp.status_code == 400
    assert await _events(pool) == [], "an unidentifiable event must not be stored"


async def test_empty_event_id_is_400(api, pool):
    payload = comment_event()
    payload["event_id"] = ""      # set after building; "" is falsy in the builder
    resp = await api.post("/webhook", content=encode(payload))
    assert resp.status_code == 400


async def test_empty_body_is_400(api, pool):
    resp = await api.post("/webhook", content=b"")
    assert resp.status_code == 400


# --- Matching behaviour through the route ------------------------------------

async def test_matching_comment_creates_exactly_one_job(api, create_rule, jobs):
    if not modules_present("matcher"):
        pytest.skip("app/matcher.py not implemented yet")
    await create_rule("PRICE")
    await api.post("/webhook", content=encode(
        comment_event(text="what is the PRICE?", user_id="usr_x")))
    rows = await wait_until(lambda: jobs(), timeout=3.0,
                            message="no job created for a matching comment")
    assert len(rows) == 1


async def test_non_matching_comment_creates_no_job(api, create_rule, jobs):
    if not modules_present("matcher"):
        pytest.skip("app/matcher.py not implemented yet")
    await create_rule("PRICE")
    await api.post("/webhook", content=encode(
        comment_event(text="great photo!", user_id="usr_y")))
    await asyncio.sleep(0.3)
    assert await jobs() == [], "a comment matching no rule must create no obligation"


async def test_event_row_records_type_and_payload(api, pool):
    payload = comment_event(text="PRICE 🙏", event_id="evt_shape")
    await api.post("/webhook", content=encode(payload))
    rows = await _events(pool)
    assert rows[0]["event_id"] == "evt_shape"
    assert rows[0]["event_type"] == "comment.created"


async def test_deleted_event_is_ingested(api, pool):
    await api.post("/webhook", content=encode(deleted_event("cmt_gone")))
    rows = await _events(pool)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "comment.deleted"


# --- Concurrency --------------------------------------------------------------

async def test_concurrent_redeliveries_still_yield_one_row(api, pool):
    """ASSIGNMENT says redeliveries arrive 'sometimes seconds apart'. Two in the
    same millisecond race the upsert; the events primary key must still leave
    exactly one row standing (BLUEPRINT §5 row 1)."""
    raw = encode(comment_event(event_id="evt_race"))
    results = await asyncio.gather(*(api.post("/webhook", content=raw) for _ in range(8)))
    assert all(r.status_code == 200 for r in results)
    rows = await _events(pool)
    assert len(rows) == 1
    assert rows[0]["redeliveries"] == 7
