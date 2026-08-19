"""GET /stats — the four graded numbers — plus the additive dashboard reads.

The graded query is deliberately ONE statement so the snapshot is internally
consistent even while a 500-event burst is draining (BLUEPRINT §4.6)."""
import json
import logging
import os

from fastapi import APIRouter, Query

from . import config, db

log = logging.getLogger("linkplease.stats")

router = APIRouter()

# Which formula backs `duplicates_blocked`. BLUEPRINT §4.3 lists two candidate
# semantics and says to pick empirically against the simulator's truth data;
# this env var lets Phase 3 calibration switch without a code change.
#   rule_user            -> suppressed (rule,user) obligations only  [default]
#   rule_user_plus_events-> the above plus redeliveries that would have DMed
DUPLICATES_FORMULA = os.getenv("DUPLICATES_FORMULA", "rule_user")

JOB_BUCKETS_SQL = """
    SELECT count(*) FILTER (WHERE status = 'SENT')   AS sent,
           count(*) FILTER (WHERE status = 'FAILED') AS failed,
           count(*) FILTER (WHERE status IN ('QUEUED','SENDING','AWAITING_CONFIRM'))
               AS queued
    FROM dm_jobs
"""


async def duplicates_blocked() -> int:
    """`duplicates_blocked` = DMs we deliberately did not send."""
    rule_user = await db.counter("duplicates_blocked_rule_user")
    if DUPLICATES_FORMULA == "rule_user_plus_events":
        return rule_user + await db.counter("duplicate_events_would_dm")
    if DUPLICATES_FORMULA != "rule_user":
        log.warning("unknown DUPLICATES_FORMULA %r; using rule_user", DUPLICATES_FORMULA)
    return rule_user


@router.get("/stats")
async def stats() -> dict:
    """Graded shape: exactly these four integer keys, no wrapper."""
    row = await db.fetchrow(JOB_BUCKETS_SQL)
    return {
        "sent": int(row["sent"]),
        "failed": int(row["failed"]),
        "queued": int(row["queued"]),
        "duplicates_blocked": await duplicates_blocked(),
    }


@router.get("/api/stats/extended")
async def stats_extended() -> dict:
    """Everything the dashboard wants: the graded numbers plus the internals we
    would otherwise have to read out of the database by hand."""
    buckets = await db.fetchrow(JOB_BUCKETS_SQL)

    per_status = await db.fetch(
        "SELECT status, count(*) AS n FROM dm_jobs GROUP BY status"
    )
    by_status = {r["status"]: int(r["n"]) for r in per_status}

    events = await db.fetchrow(
        """
        SELECT count(*) AS received,
               coalesce(sum(redeliveries), 0) AS redelivered,
               count(*) FILTER (WHERE processed_at IS NULL) AS unprocessed
        FROM events
        """
    )

    # Rate budget: sends in the last rolling window, against our self-imposed
    # cap of 9 (their limit is 10; we bank one for clock skew — §4.4).
    sends_recent = await db.fetchval(
        "SELECT count(*) FROM send_log WHERE sent_at > now() - make_interval(secs => $1)",
        float(config.RATE_LIMIT_WINDOW_SECONDS),
    )

    # Reconciler lag: how long the oldest unconfirmed 202 has been waiting.
    oldest_awaiting = await db.fetchval(
        """
        SELECT extract(epoch FROM now() - min(updated_at))
        FROM dm_jobs WHERE status = 'AWAITING_CONFIRM'
        """
    )

    return {
        "sent": int(buckets["sent"]),
        "failed": int(buckets["failed"]),
        "queued": int(buckets["queued"]),
        "duplicates_blocked": await duplicates_blocked(),
        "duplicates_formula": DUPLICATES_FORMULA,
        "counters": {
            "duplicates_blocked_rule_user": await db.counter("duplicates_blocked_rule_user"),
            "duplicate_events_suppressed": await db.counter("duplicate_events_suppressed"),
            "duplicate_events_would_dm": await db.counter("duplicate_events_would_dm"),
        },
        "cancelled": by_status.get("CANCELLED", 0),
        "jobs_by_status": by_status,
        "events": {
            "received": int(events["received"]),
            "redelivered": int(events["redelivered"]),
            "unprocessed": int(events["unprocessed"]),
        },
        "rate_budget": {
            "window_seconds": config.RATE_LIMIT_WINDOW_SECONDS,
            "used": int(sends_recent or 0),
            "max": config.RATE_LIMIT_MAX,
        },
        "oldest_awaiting_confirm_seconds": (
            round(float(oldest_awaiting), 3) if oldest_awaiting is not None else None
        ),
    }


@router.get("/api/jobs")
async def list_jobs(
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> list[dict]:
    """Recent job rows, newest activity first. `status` filters exactly."""
    if status:
        rows = await db.fetch(
            """
            SELECT job_id, rule_id, user_id, username, comment_id, post_id, status,
                   attempt, cycle, dm_id, last_error, created_at, updated_at
            FROM dm_jobs WHERE status = $1 ORDER BY updated_at DESC LIMIT $2
            """,
            status,
            limit,
        )
    else:
        rows = await db.fetch(
            """
            SELECT job_id, rule_id, user_id, username, comment_id, post_id, status,
                   attempt, cycle, dm_id, last_error, created_at, updated_at
            FROM dm_jobs ORDER BY updated_at DESC LIMIT $1
            """,
            limit,
        )
    return [
        {
            "job_id": r["job_id"],
            "rule_id": r["rule_id"],
            "user_id": r["user_id"],
            "username": r["username"],
            "comment_id": r["comment_id"],
            "post_id": r["post_id"],
            "status": r["status"],
            "attempt": r["attempt"],
            "cycle": r["cycle"],
            "dm_id": r["dm_id"],
            "last_error": r["last_error"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]


def _decode_payload(payload):
    """events.payload is JSONB; asyncpg returns it as a JSON string unless a
    codec is registered, so decode it here rather than shipping a quoted blob."""
    if isinstance(payload, (str, bytes)):
        try:
            return json.loads(payload)
        except ValueError:
            return None
    return payload


@router.get("/api/events")
async def list_events(limit: int = Query(50, ge=1, le=200)) -> list[dict]:
    """Recent raw events, for the audit view. The payload is returned as stored."""
    rows = await db.fetch(
        """
        SELECT event_id, event_type, payload, sent_at, received_at,
               redeliveries, processed_at
        FROM events ORDER BY received_at DESC LIMIT $1
        """,
        limit,
    )
    return [
        {
            "event_id": r["event_id"],
            "event_type": r["event_type"],
            "payload": _decode_payload(r["payload"]),
            "sent_at": r["sent_at"].isoformat() if r["sent_at"] else None,
            "received_at": r["received_at"].isoformat() if r["received_at"] else None,
            "redeliveries": r["redeliveries"],
            "processed_at": r["processed_at"].isoformat() if r["processed_at"] else None,
        }
        for r in rows
    ]
