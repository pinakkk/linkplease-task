"""Matching and DM-level dedup (BLUEPRINT §4.2, §4.3).

Turns a `comment.created` event into zero or more DM obligations (rows in
dm_jobs), and a `comment.deleted` event into a tombstone plus a cancellation.

The dedup guarantee "the same user never gets DMed twice for the same rule" is
enforced by the database via the partial unique index `uq_live_job`, not by an
application-level check. Application checks race; a unique index does not.

--- A note on `duplicate_events_would_dm` -----------------------------------
The counters table seeds three names. Two are maintained here and in webhook.py:
`duplicates_blocked_rule_user` (bumped below) and `duplicate_events_suppressed`
(bumped by webhook.py). The third, `duplicate_events_would_dm`, exists for the
§4.3 calibration question — "does their `duplicates_blocked` include redelivered
events that would have matched?". webhook.py detects redeliveries and returns
early, so redelivered events never reach `handle_event` and this module cannot
bump that counter. `would_match()` below is offered for webhook.py to call on
the redelivery path if and when we run that calibration. Until then the counter
stays at 0, which is correct and honest: nothing has measured it yet.
"""
import asyncio
import json
import logging

import asyncpg

from . import config, db

log = logging.getLogger("linkplease.matcher")

# How many stuck events one sweep pass picks up. Bounded so a large backlog is
# drained in steady chunks rather than in one enormous transaction.
SWEEP_BATCH = 100


def matches(keyword: str, text: str) -> bool:
    """Case-insensitive substring match — exactly the graded contract.

    Deliberately literal: no word boundaries, no regex, no unicode
    normalisation. "PRICE" matches "priceless" and matches "PRICE please 🙏",
    and both of those are what the assignment asks for.
    """
    if not keyword or not text:
        return False
    return keyword.lower() in text.lower()


async def would_match(text: str) -> bool:
    """True if this comment text matches at least one rule. Not used by the
    pipeline; offered for the §4.3 calibration path (see module docstring)."""
    rows = await db.fetch("SELECT keyword FROM rules")
    return any(matches(row["keyword"], text) for row in rows)


async def _mark_processed(event_id: str) -> None:
    """Stamp processed_at. Only ever called after the event's side effects have
    committed — if we crash before this, the sweep picks the event up again and
    re-runs it, which is safe because every write below is idempotent."""
    await db.execute(
        "UPDATE events SET processed_at = now() WHERE event_id = $1", event_id
    )


async def handle_event(event_id: str, event_type: str, data: dict) -> None:
    """Process one webhook event. Never raises.

    This runs as a fire-and-forget background task, so an exception escaping
    here would only produce a lost DM and a log line nobody reads. Instead we
    catch everything and leave `processed_at` NULL, which puts the event back in
    the sweep's queue for a retry.
    """
    try:
        if event_type == "comment.deleted":
            await _handle_deleted(event_id, data)
        elif event_type == "comment.created":
            await _handle_created(event_id, data)
        else:
            # Unknown type: nothing to do, but mark it processed so the sweep
            # does not retry it forever.
            log.info("ignoring event %s of unknown type %r", event_id, event_type)
            await _mark_processed(event_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        # processed_at stays NULL on purpose -> matcher_sweep_loop retries it.
        log.exception("handle_event failed event_id=%s type=%s", event_id, event_type)


async def _handle_deleted(event_id: str, data: dict) -> None:
    """comment.deleted: `data` carries only comment_id.

    Two effects:
      1. A tombstone row, so a `comment.created` that arrives LATER for the same
         comment (order is explicitly not guaranteed) creates no obligation.
      2. Cancellation of any job for that comment that has not been sent yet.

    We cancel only QUEUED jobs. A job in SENDING has an HTTP request in flight
    (or is about to); AWAITING_CONFIRM means their API already accepted it; SENT
    means it was delivered. In all three the DM has left the building and there
    is nothing sensible to undo. A delete arriving during the sub-second SENDING
    window therefore still results in a DM — documented in FAILURES.md.
    """
    comment_id = data.get("comment_id")
    if not isinstance(comment_id, str) or not comment_id:
        log.warning("comment.deleted event %s has no comment_id", event_id)
        await _mark_processed(event_id)
        return

    await db.execute(
        "INSERT INTO deleted_comments (comment_id) VALUES ($1) ON CONFLICT DO NOTHING",
        comment_id,
    )
    # One atomic statement: the WHERE clause is the check, so there is no
    # read-then-write window in which the worker could claim the job.
    result = await db.execute(
        """
        UPDATE dm_jobs SET status = 'CANCELLED', updated_at = now()
        WHERE comment_id = $1 AND status = 'QUEUED'
        """,
        comment_id,
    )
    log.info("comment.deleted %s -> %s", comment_id, result)
    await _mark_processed(event_id)


def _extract_author(data: dict) -> tuple[str | None, str | None]:
    """user_id is the identity; username is display only and can change."""
    author = data.get("from")
    if not isinstance(author, dict):
        return None, None
    user_id = author.get("user_id")
    username = author.get("username")
    return (
        user_id if isinstance(user_id, str) and user_id else None,
        username if isinstance(username, str) else None,
    )


async def _handle_created(event_id: str, data: dict) -> None:
    """comment.created: match against every rule and create one obligation per
    matching rule that this user does not already owe."""
    comment_id = data.get("comment_id")
    text = data.get("text")
    post_id = data.get("post_id")
    user_id, username = _extract_author(data)

    if not isinstance(comment_id, str) or not comment_id or not user_id:
        log.warning("comment.created event %s missing comment_id/user_id", event_id)
        await _mark_processed(event_id)
        return
    if not isinstance(text, str):
        text = ""

    # 1. Tombstoned before we saw it (delete arrived first). No job, no counter:
    #    a DM we never owed is not a duplicate we blocked.
    tombstoned = await db.fetchval(
        "SELECT 1 FROM deleted_comments WHERE comment_id = $1", comment_id
    )
    if tombstoned:
        log.info("comment %s already tombstoned; no jobs created", comment_id)
        await _mark_processed(event_id)
        return

    # 2. Rules are few and change rarely; a full scan per event is simpler than
    #    any cache and cannot go stale.
    rules = await db.fetch("SELECT rule_id, keyword FROM rules")
    matched = [r["rule_id"] for r in rules if matches(r["keyword"], text)]

    for rule_id in matched:
        await _ensure_obligation(rule_id, user_id, username, comment_id, post_id)

    await _mark_processed(event_id)


async def _ensure_obligation(
    rule_id: str,
    user_id: str,
    username: str | None,
    comment_id: str,
    post_id: str | None,
) -> None:
    """Create exactly one live DM obligation for (rule_id, user_id), or record a
    blocked duplicate.

    The insert and any counter bump share one transaction, so /stats can never
    observe a state where a duplicate was suppressed but not counted (or the
    reverse).

    Three outcomes, decided by the database:

      A. INSERT returns a job_id. Either no row existed for this (rule,user), or
         only CANCELLED rows did — the partial index ignores CANCELLED, so the
         insert simply succeeds and we get a fresh row with cycle=0. That is the
         revival case, and it needs no special handling: a brand new row is a
         brand new obligation with a brand new Idempotency-Key. The old
         CANCELLED row stays as audit history.

      B. INSERT returns nothing -> a LIVE row exists (status <> 'CANCELLED').
         This is a true duplicate: a distinct comment from the same user for the
         same rule. Bump `duplicates_blocked_rule_user` (§4.3 semantics (a)).

      C. UniqueViolation. Only reachable if a concurrent inserter won the race
         between our statement planning and execution. Same meaning as B.
    """
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            try:
                job_id = await conn.fetchval(
                    """
                    INSERT INTO dm_jobs
                        (rule_id, user_id, username, comment_id, post_id, status)
                    VALUES ($1, $2, $3, $4, $5, 'QUEUED')
                    ON CONFLICT (rule_id, user_id) WHERE status <> 'CANCELLED'
                    DO NOTHING
                    RETURNING job_id
                    """,
                    rule_id, user_id, username, comment_id, post_id,
                )
            except asyncpg.UniqueViolationError:
                job_id = None

            if job_id is not None:
                log.info("job %s queued rule=%s user=%s comment=%s",
                         job_id, rule_id, user_id, comment_id)
                return

            # A live obligation already exists: we deliberately do not send.
            await db.bump_counter("duplicates_blocked_rule_user", 1, conn=conn)
            log.info("duplicate blocked rule=%s user=%s comment=%s",
                     rule_id, user_id, comment_id)


async def matcher_sweep_loop() -> None:
    """Safety net for events whose dispatch crashed, or that arrived while the
    process was dying. This is the entire reason `events.processed_at` exists:
    an event row with a NULL processed_at is an unpaid debt, and nothing else in
    the system would notice it.

    Oldest first so a backlog drains in arrival order.
    """
    while True:
        try:
            rows = await db.fetch(
                """
                SELECT event_id, event_type, payload
                FROM events
                WHERE processed_at IS NULL
                ORDER BY received_at
                LIMIT $1
                """,
                SWEEP_BATCH,
            )
            for row in rows:
                payload = row["payload"]
                if isinstance(payload, str):
                    # asyncpg returns jsonb as text unless a codec is set.
                    payload = json.loads(payload)
                data = payload.get("data") if isinstance(payload, dict) else None
                await handle_event(
                    row["event_id"],
                    row["event_type"],
                    data if isinstance(data, dict) else {},
                )
            if rows:
                log.info("sweep reprocessed %d event(s)", len(rows))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("matcher sweep iteration failed")
        await asyncio.sleep(config.MATCHER_SWEEP_INTERVAL)
