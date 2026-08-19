"""Simulator proxy and truth-diff report — the Phase 3 calibration tool
(BLUEPRINT §4.3, §6, §9.3).

Two additive routes, neither of which the grader ever calls:

  POST /api/simulate                 -> proxy to their POST /v1/simulate/start
  GET  /api/simulate/{run_id}/report -> fetch their truth, diff it against ours

Everything here is read-only with respect to the pipeline. It creates one table
of its own (`simulate_runs`) and otherwise only reads. That matters: the three
graded routes must behave identically whether or not anybody ever calls these.

--- The single most important decision in this file -------------------------
`/stats` reports LIFETIME totals. Truth data describes ONE run. Diffing lifetime
totals against one run's truth works exactly once — on the second run every
number is inflated by the first run's traffic and the diff is garbage forever
after. So POST /api/simulate snapshots the four graded numbers at the instant
the run starts, and the report diffs `current - baseline` (the DELTA) against
truth. Without that baseline this whole endpoint is a lie after run #1.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import config, db, pseudogram, stats

log = logging.getLogger("linkplease.simulate")

router = APIRouter()

# Guard rails on what we will ask them to fire at us. Their API may well accept
# more; we do not want a fat-fingered 500000 to burn the day's budget.
MAX_COUNT = 1000
MIN_DURATION = 1
MAX_DURATION = 600

# How much of their truth payload we echo back under `truth_raw`. Enough to see
# the shape and a few sample records, bounded so a 500-event dump does not make
# the response unreadable (or enormous) in a browser.
TRUTH_RAW_LIST_LIMIT = 25
TRUTH_RAW_CHAR_LIMIT = 20000

_table_ready = False


# --- Our own tiny table ------------------------------------------------------
# schema.sql belongs to another agent, so this module owns its own DDL and runs
# it lazily. IF NOT EXISTS everywhere means it is safe on every request; the
# module-level flag means we pay for it once per process, not once per call.

_DDL = """
CREATE TABLE IF NOT EXISTS simulate_runs (
    run_id             TEXT PRIMARY KEY,
    requested_count    INT,
    duration_seconds   INT,
    webhook_url        TEXT,
    started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Baseline snapshot of the four graded numbers AT RUN START. The report
    -- diffs against (current - these), so a second run is not polluted by the
    -- first. See the module docstring.
    base_sent               BIGINT NOT NULL DEFAULT 0,
    base_failed             BIGINT NOT NULL DEFAULT 0,
    base_queued             BIGINT NOT NULL DEFAULT 0,
    base_duplicates_blocked BIGINT NOT NULL DEFAULT 0,
    -- Both §4.3 candidate counters, so the report can evaluate either formula
    -- on this run's delta without re-deriving them.
    base_dup_rule_user      BIGINT NOT NULL DEFAULT 0,
    base_dup_events_would_dm BIGINT NOT NULL DEFAULT 0,
    base_events_received    BIGINT NOT NULL DEFAULT 0
)
"""


async def _ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    await db.execute(_DDL)
    _table_ready = True


def _error(status: int, message: str, **extra: Any) -> JSONResponse:
    """Structured error. These routes never surface a stack trace: a 500 with a
    traceback during 2am calibration tells you nothing you can act on."""
    body = {"error": message, **extra}
    return JSONResponse(body, status_code=status)


def _iso(value: Any) -> Any:
    """Datetimes are not JSON-serialisable and a human reads this output."""
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return value


# --- Baseline snapshot -------------------------------------------------------

async def _snapshot() -> dict[str, int]:
    """The four graded numbers plus both duplicate counters, read the same way
    /stats reads them so the baseline and the later reading are comparable."""
    row = await db.fetchrow(stats.JOB_BUCKETS_SQL)
    return {
        "sent": int(row["sent"]) if row else 0,
        "failed": int(row["failed"]) if row else 0,
        "queued": int(row["queued"]) if row else 0,
        "duplicates_blocked": await stats.duplicates_blocked(),
        "dup_rule_user": await db.counter("duplicates_blocked_rule_user"),
        "dup_events_would_dm": await db.counter("duplicate_events_would_dm"),
        "events_received": int(await db.fetchval("SELECT count(*) FROM events") or 0),
    }


# --- POST /api/simulate ------------------------------------------------------

class SimulateIn(BaseModel):
    count: int = Field(default=500, ge=1)
    duration_seconds: int = Field(default=10, ge=1)


def _clamp(payload: SimulateIn) -> tuple[int, int]:
    count = min(payload.count, MAX_COUNT)
    duration = min(max(payload.duration_seconds, MIN_DURATION), MAX_DURATION)
    return count, duration


@router.post("/api/simulate")
async def start_simulation(payload: SimulateIn | None = None) -> JSONResponse:
    """Ask PseudoGram to fire `count` events at OUR webhook over `duration`."""
    try:
        await _ensure_table()
    except Exception as exc:
        log.exception("simulate: could not ensure simulate_runs table")
        return _error(503, "database_unavailable", detail=str(exc))

    count, duration = _clamp(payload or SimulateIn())

    base = (config.PUBLIC_BASE_URL or "").strip().rstrip("/")
    if not base:
        # Guessing a URL here would send 500 events into the void and we would
        # spend an hour wondering why nothing arrived.
        return _error(
            400,
            "public_base_url_not_configured",
            detail=(
                "PUBLIC_BASE_URL must be set to this service's externally "
                "reachable base URL (e.g. https://linkplease.fly.dev) before "
                "the simulator can be pointed at our /webhook."
            ),
        )
    webhook_url = base + "/webhook"

    # Snapshot BEFORE we ask them to start, so no event of this run can land
    # between the snapshot and the run beginning.
    try:
        snapshot = await _snapshot()
    except Exception as exc:
        log.exception("simulate: baseline snapshot failed")
        return _error(503, "baseline_snapshot_failed", detail=str(exc))

    body = {
        "webhook_url": webhook_url,
        "count": count,
        "duration_seconds": duration,
    }
    try:
        response = await pseudogram.get_client().post(
            "/v1/simulate/start",
            json=body,
            headers={"X-API-Key": config.PSEUDOGRAM_API_KEY},
            timeout=config.HTTP_TIMEOUT_SECONDS,
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        log.warning("simulate start transport error: %r", exc)
        return _error(502, "pseudogram_unreachable", detail=repr(exc),
                      requested=body)

    parsed = _safe_json(response)
    if not 200 <= response.status_code < 300:
        # Return theirs verbatim. A 401 here is the expected answer while the
        # API key is still a placeholder, and the body is what says so.
        log.warning("simulate start non-2xx %s: %s",
                    response.status_code, (response.text or "")[:500])
        return _error(
            502,
            "pseudogram_error",
            pseudogram_status=response.status_code,
            pseudogram_body=parsed if parsed is not None else (response.text or "")[:2000],
            requested=body,
            hint=(
                "401/403 means the PSEUDOGRAM_API_KEY is not valid yet "
                "(apply + keygen are manual steps)."
                if response.status_code in (401, 403)
                else None
            ),
        )

    run_id = None
    if isinstance(parsed, dict):
        for key in ("run_id", "runId", "id"):
            value = parsed.get(key)
            if isinstance(value, str) and value:
                run_id = value
                break
    if not run_id:
        return _error(502, "no_run_id_in_response",
                      pseudogram_body=parsed, requested=body)

    try:
        await db.execute(
            """
            INSERT INTO simulate_runs (
                run_id, requested_count, duration_seconds, webhook_url,
                base_sent, base_failed, base_queued, base_duplicates_blocked,
                base_dup_rule_user, base_dup_events_would_dm, base_events_received
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT (run_id) DO NOTHING
            """,
            run_id, count, duration, webhook_url,
            snapshot["sent"], snapshot["failed"], snapshot["queued"],
            snapshot["duplicates_blocked"], snapshot["dup_rule_user"],
            snapshot["dup_events_would_dm"], snapshot["events_received"],
        )
    except Exception as exc:
        # The run IS running on their side; failing to record it only costs us
        # the scoped report, so say so rather than pretending the run failed.
        log.exception("simulate: failed to record run %s", run_id)
        return JSONResponse(
            {
                "run_id": run_id,
                "requested": body,
                "recorded": False,
                "warning": f"run started but not recorded locally: {exc}",
            },
            status_code=200,
        )

    return JSONResponse(
        {
            "run_id": run_id,
            "requested": body,
            "recorded": True,
            "baseline": snapshot,
            "pseudogram_response": parsed,
        },
        status_code=200,
    )


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


# --- GET /api/simulate (history) --------------------------------------------

@router.get("/api/simulate")
async def list_runs(limit: int = 25) -> JSONResponse:
    """Recorded runs, newest first — the dashboard's history dropdown."""
    try:
        await _ensure_table()
        rows = await db.fetch(
            """
            SELECT run_id, requested_count, duration_seconds, webhook_url,
                   started_at, base_sent, base_failed, base_queued,
                   base_duplicates_blocked
            FROM simulate_runs ORDER BY started_at DESC LIMIT $1
            """,
            max(1, min(limit, 200)),
        )
    except Exception as exc:
        log.exception("simulate: list runs failed")
        return _error(503, "database_unavailable", detail=str(exc))

    return JSONResponse(
        [
            {
                "run_id": r["run_id"],
                "requested_count": r["requested_count"],
                "duration_seconds": r["duration_seconds"],
                "webhook_url": r["webhook_url"],
                "started_at": _iso(r["started_at"]),
                "baseline": {
                    "sent": int(r["base_sent"]),
                    "failed": int(r["base_failed"]),
                    "queued": int(r["base_queued"]),
                    "duplicates_blocked": int(r["base_duplicates_blocked"]),
                },
            }
            for r in rows
        ]
    )


# --- Defensive truth parsing -------------------------------------------------
# Their truth shape is documented only as prose ("every event we sent, which
# were duplicates, which users matched which keywords"). Everything below
# assumes as little as possible and never raises on a missing field: a parser
# that crashes on an unexpected key turns the calibration tool into a second
# thing to debug at exactly the wrong moment.

_EVENT_LIST_KEYS = ("events", "deliveries", "sent", "sent_events", "items",
                    "data", "results", "records", "webhooks")
_MATCH_LIST_KEYS = ("matches", "expected_dms", "expected_matches", "dms",
                    "matched", "expected")


def _as_list_of_dicts(value: Any) -> list[dict]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _find_event_list(truth: Any) -> tuple[list[dict], str]:
    """Return (events, where_we_found_them). Accepts a bare list, a dict with a
    known list key, or a dict with exactly one list-of-dicts value."""
    if isinstance(truth, list):
        return _as_list_of_dicts(truth), "top-level list"
    if not isinstance(truth, dict):
        return [], "unrecognised payload type"

    for key in _EVENT_LIST_KEYS:
        found = _as_list_of_dicts(truth.get(key))
        if found:
            return found, f"key {key!r}"
        # One level of nesting: {"run": {"events": [...]}} and friends.
        nested = truth.get(key)
        if isinstance(nested, dict):
            for inner in _EVENT_LIST_KEYS:
                found = _as_list_of_dicts(nested.get(inner))
                if found:
                    return found, f"key {key!r}.{inner!r}"

    # Last resort: any list-of-dicts that looks like events.
    for key, value in truth.items():
        found = _as_list_of_dicts(value)
        if found and any(_event_id_of(item) for item in found):
            return found, f"inferred key {key!r}"
    return [], "no event list found"


def _first(item: dict, *names: str) -> Any:
    for name in names:
        if name in item and item[name] not in (None, ""):
            return item[name]
    return None


def _event_id_of(item: dict) -> str | None:
    value = _first(item, "event_id", "eventId", "id")
    return value if isinstance(value, str) and value else None


def _is_duplicate(item: dict) -> bool:
    """A truth record is a duplicate if it says so under any plausible name, or
    if it carries a redelivery counter greater than zero."""
    for name in ("is_duplicate", "duplicate", "is_redelivery", "redelivery",
                 "was_duplicate", "is_dup"):
        value = item.get(name)
        if isinstance(value, bool):
            if value:
                return True
        elif isinstance(value, (int, float)) and value:
            return True
        elif isinstance(value, str) and value.lower() in ("true", "yes", "1"):
            return True
    for name in ("redeliveries", "delivery_attempt", "attempt", "copy_number"):
        value = item.get(name)
        if isinstance(value, (int, float)) and value > (1 if name != "redeliveries" else 0):
            return True
    return False


def _nested(item: dict, *path_options: tuple[str, ...]) -> Any:
    """Walk the first path that resolves. Truth records may inline fields or
    nest them under `data` exactly like the webhook payload does."""
    for path in path_options:
        cursor: Any = item
        for step in path:
            if not isinstance(cursor, dict) or step not in cursor:
                cursor = None
                break
            cursor = cursor[step]
        if cursor not in (None, ""):
            return cursor
    return None


def _user_of(item: dict) -> str | None:
    value = _nested(
        item,
        ("user_id",), ("userId",), ("recipient_user_id",),
        ("from", "user_id"), ("data", "from", "user_id"),
        ("data", "user_id"), ("author", "user_id"),
    )
    return value if isinstance(value, str) and value else None


def _comment_of(item: dict) -> str | None:
    value = _nested(item, ("comment_id",), ("commentId",), ("data", "comment_id"))
    return value if isinstance(value, str) and value else None


def _keywords_of(item: dict) -> list[str]:
    """Truth may name a matched keyword, a list of them, or none at all."""
    value = _nested(item, ("keyword",), ("matched_keyword",), ("keywords",),
                    ("matched_keywords",), ("matches",))
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v]
    return []


def _truth_excerpt(truth: Any) -> Any:
    """A bounded view of what they actually returned, so a human can eyeball the
    real shape without scrolling through 500 records."""
    if isinstance(truth, list):
        excerpt: Any = truth[:TRUTH_RAW_LIST_LIMIT]
    elif isinstance(truth, dict):
        excerpt = {}
        for key, value in truth.items():
            if isinstance(value, list):
                excerpt[key] = value[:TRUTH_RAW_LIST_LIMIT]
                if len(value) > TRUTH_RAW_LIST_LIMIT:
                    excerpt[f"{key}__truncated_total"] = len(value)
            else:
                excerpt[key] = value
    else:
        excerpt = truth
    try:
        text = json.dumps(excerpt, default=str)
    except (TypeError, ValueError):
        return {"unserialisable": str(excerpt)[:TRUTH_RAW_CHAR_LIMIT]}
    if len(text) > TRUTH_RAW_CHAR_LIMIT:
        return {"truncated": text[:TRUTH_RAW_CHAR_LIMIT]}
    return excerpt


def summarise_truth(truth: Any) -> dict:
    """Everything we can learn from their payload, with provenance attached."""
    events, source = _find_event_list(truth)
    top_level_keys = (
        sorted(truth.keys()) if isinstance(truth, dict)
        else ["<list>"] if isinstance(truth, list) else ["<scalar>"]
    )
    log.info("truth payload top-level keys=%s event_list_source=%s count=%d",
             top_level_keys, source, len(events))

    event_ids: list[str] = []
    duplicate_event_ids: list[str] = []
    seen: set[str] = set()
    unique_event_ids: set[str] = set()
    match_pairs: set[tuple[str, str]] = set()
    matching_events = 0

    for item in events:
        event_id = _event_id_of(item)
        if event_id:
            event_ids.append(event_id)
            # Two ways truth can express a redelivery: a flag on the record, or
            # simply the same event_id appearing twice in the list. Both count.
            if event_id in seen or _is_duplicate(item):
                duplicate_event_ids.append(event_id)
            seen.add(event_id)
            unique_event_ids.add(event_id)
        elif _is_duplicate(item):
            duplicate_event_ids.append("<unnamed>")

        keywords = _keywords_of(item)
        user_id = _user_of(item)
        if keywords and user_id:
            matching_events += 1
            for keyword in keywords:
                match_pairs.add((user_id, keyword.lower()))

    # Some truth payloads carry an explicit match list separate from events.
    explicit_pairs, explicit_source = _explicit_match_pairs(truth)
    if explicit_pairs:
        match_pairs |= explicit_pairs

    return {
        "top_level_keys": top_level_keys,
        "event_list_source": source,
        "total_events": len(events),
        "unique_event_ids": len(unique_event_ids),
        "duplicate_events": len(duplicate_event_ids),
        "events_with_a_match": matching_events,
        "unique_match_pairs": len(match_pairs),
        "expected_dms": len(match_pairs),
        # Their notion of suppressed duplicates, if they state one outright.
        "stated_duplicates_blocked": _stated_number(
            truth, "duplicates_blocked", "duplicates_suppressed",
            "expected_duplicates_blocked", "suppressed",
        ),
        "stated_expected_dms": _stated_number(
            truth, "expected_dms", "expected_sent", "dms_expected",
        ),
        "match_pairs_source": explicit_source or "derived from event records",
        "_event_ids": event_ids,
        "_unique_event_ids": unique_event_ids,
        "_match_pairs": match_pairs,
    }


def _explicit_match_pairs(truth: Any) -> tuple[set[tuple[str, str]], str | None]:
    if not isinstance(truth, dict):
        return set(), None
    for key in _MATCH_LIST_KEYS:
        records = _as_list_of_dicts(truth.get(key))
        if not records:
            continue
        pairs = set()
        for record in records:
            user_id = _user_of(record)
            for keyword in _keywords_of(record):
                if user_id:
                    pairs.add((user_id, keyword.lower()))
        if pairs:
            return pairs, f"explicit key {key!r}"
    return set(), None


def _stated_number(truth: Any, *names: str) -> int | None:
    """If truth states a number outright, prefer it over anything we derived."""
    if not isinstance(truth, dict):
        return None
    containers = [truth]
    for value in truth.values():
        if isinstance(value, dict):
            containers.append(value)
    for container in containers:
        for name in names:
            value = container.get(name)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return value
            if isinstance(value, float) and value.is_integer():
                return int(value)
    return None


# --- Our side of the diff ----------------------------------------------------

async def _our_run_numbers(
    run: Any, truth_event_ids: set[str]
) -> tuple[dict, str]:
    """Count what WE did for this run. Returns (numbers, scoping_description).

    Preferred scope is the event_id join: truth names every event they sent, and
    `events.event_id` is our primary key, so the intersection is exact and
    immune to clock skew. Only when truth gives us no ids do we fall back to a
    time window, which is approximate — any unrelated traffic in the window is
    counted in — so the response always says which one was used.
    """
    started_at = run["started_at"]

    if truth_event_ids:
        scoping = f"event_id join against truth ({len(truth_event_ids)} ids)"
        rows = await db.fetch(
            """
            SELECT event_id, event_type, redeliveries, payload
            FROM events WHERE event_id = ANY($1::text[])
            """,
            sorted(truth_event_ids),
        )
    else:
        scoping = f"time window: events received since {_iso(started_at)}"
        rows = await db.fetch(
            """
            SELECT event_id, event_type, redeliveries, payload
            FROM events WHERE received_at >= $1
            """,
            started_at,
        )

    our_event_ids = {r["event_id"] for r in rows}
    redeliveries = sum(int(r["redeliveries"] or 0) for r in rows)
    comment_ids = {c for c in (_comment_id_from_payload(r["payload"]) for r in rows) if c}

    # Jobs are scoped by the comments of this run's events, which is the only
    # honest link between a job row and a run.
    if comment_ids:
        job_rows = await db.fetch(
            """
            SELECT status, count(*) AS n FROM dm_jobs
            WHERE comment_id = ANY($1::text[]) GROUP BY status
            """,
            sorted(comment_ids),
        )
        jobs_scoping = "jobs joined via comment_id of this run's events"
    else:
        job_rows = await db.fetch(
            "SELECT status, count(*) AS n FROM dm_jobs WHERE created_at >= $1 GROUP BY status",
            started_at,
        )
        jobs_scoping = "jobs by created_at >= run start (no comment ids available)"

    by_status = {r["status"]: int(r["n"]) for r in job_rows}
    return (
        {
            "events_received": len(rows),
            "distinct_event_ids": len(our_event_ids),
            "redeliveries": redeliveries,
            "distinct_comment_ids": len(comment_ids),
            "jobs_created": sum(by_status.values()),
            "jobs_by_status": by_status,
            "cancelled": by_status.get("CANCELLED", 0),
            "jobs_scoping": jobs_scoping,
            "_event_ids": our_event_ids,
        },
        scoping,
    )


def _comment_id_from_payload(payload: Any) -> str | None:
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    comment_id = data.get("comment_id")
    return comment_id if isinstance(comment_id, str) and comment_id else None


def _delta(current: dict, run: Any) -> dict:
    """current - baseline, floored at zero. Negative would mean the baseline was
    taken after some of the run's work landed; report 0 and flag it rather than
    emitting a nonsense negative count."""
    return {
        "sent": max(0, current["sent"] - int(run["base_sent"])),
        "failed": max(0, current["failed"] - int(run["base_failed"])),
        # `queued` is a LEVEL, not a cumulative total: it goes up and back down
        # as the queue drains. Its delta is therefore "how much deeper the queue
        # is than before the run", which is only meaningful mid-burst.
        "queued": current["queued"] - int(run["base_queued"]),
        "duplicates_blocked": max(
            0, current["duplicates_blocked"] - int(run["base_duplicates_blocked"])
        ),
    }


def _compare(ours: int | None, theirs: int | None, label: str) -> dict:
    entry: dict[str, Any] = {"ours": ours, "theirs": theirs, "metric": label}
    if ours is None or theirs is None:
        entry["delta"] = None
        entry["match"] = None
        entry["note"] = "not comparable: truth did not state this number"
    else:
        entry["delta"] = ours - theirs
        entry["match"] = ours == theirs
    return entry


def _duplicates_analysis(run: Any, dup_rule_user: int, dup_would_dm: int,
                         theirs: int | None) -> dict:
    """BLUEPRINT §4.3: two candidate formulas, one open question. Evaluate both
    on this run's DELTA and say which (if either) reproduces their number."""
    a = max(0, dup_rule_user - int(run["base_dup_rule_user"]))
    b = a + max(0, dup_would_dm - int(run["base_dup_events_would_dm"]))
    candidates = {
        "a_rule_user_only": {
            "value": a,
            "formula": "duplicates_blocked_rule_user (delta)",
            "matches_truth": (a == theirs) if theirs is not None else None,
        },
        "b_rule_user_plus_duplicate_events": {
            "value": b,
            "formula": "duplicates_blocked_rule_user + duplicate_events_would_dm (delta)",
            "matches_truth": (b == theirs) if theirs is not None else None,
        },
    }
    if theirs is None:
        verdict = "inconclusive: truth stated no duplicates number to match"
    elif a == theirs and b == theirs:
        verdict = "both formulas match (a == b on this run; run one with redeliveries that match a rule to discriminate)"
    elif a == theirs:
        verdict = "formula (a) reproduces their number -> keep DUPLICATES_FORMULA=rule_user"
    elif b == theirs:
        verdict = "formula (b) reproduces their number -> set DUPLICATES_FORMULA=rule_user_plus_events"
    else:
        verdict = "NEITHER formula reproduces their number -> semantics are something else; inspect truth_raw"
    return {
        "their_number": theirs,
        "active_formula": stats.DUPLICATES_FORMULA,
        "candidates": candidates,
        "verdict": verdict,
    }


# --- GET /api/simulate/{run_id}/report ---------------------------------------

@router.get("/api/simulate/{run_id}/report")
async def run_report(run_id: str) -> JSONResponse:
    try:
        await _ensure_table()
        run = await db.fetchrow(
            "SELECT * FROM simulate_runs WHERE run_id = $1", run_id
        )
    except Exception as exc:
        log.exception("report: database unavailable")
        return _error(503, "database_unavailable", detail=str(exc))

    if run is None:
        return _error(
            404, "unknown_run",
            detail=(f"run_id {run_id!r} was not started through POST /api/simulate, "
                    "so there is no baseline snapshot to diff against."),
        )

    truth, truth_error = await _fetch_truth(run_id)
    summary = summarise_truth(truth) if truth is not None else None

    try:
        current = await _snapshot()
        ours, scoping = await _our_run_numbers(
            run, summary["_unique_event_ids"] if summary else set()
        )
    except Exception as exc:
        log.exception("report: computing our numbers failed")
        return _error(503, "our_numbers_failed", detail=str(exc))

    delta = _delta(current, run)

    # --- the four graded numbers, ours (delta) vs theirs -----------------
    theirs_sent = summary["stated_expected_dms"] or summary["expected_dms"] if summary else None
    # Only ever a number truth STATED. We deliberately do not fall back to
    # their duplicate-event count here: guessing their semantics is exactly the
    # error this endpoint exists to prevent. `duplicates_analysis` below shows
    # both our candidates so a human can decide.
    theirs_duplicates = summary["stated_duplicates_blocked"] if summary else None

    diff = {
        "sent": _compare(delta["sent"], theirs_sent, "DMs delivered"),
        "failed": _compare(delta["failed"], None, "DMs given up on"),
        "queued": _compare(delta["queued"], None, "still owed (level, not total)"),
        "duplicates_blocked": _compare(
            delta["duplicates_blocked"], theirs_duplicates, "DMs deliberately not sent"
        ),
    }

    # --- event-level reconciliation: the "nothing lost" number ------------
    ours_ids: set[str] = ours.pop("_event_ids", set())
    theirs_ids: set[str] = summary["_unique_event_ids"] if summary else set()
    missing = sorted(theirs_ids - ours_ids)     # they sent, we never got: DROPPED
    unexpected = sorted(ours_ids - theirs_ids)  # we have, they never sent: a bug

    discrepancies: list[str] = []
    if truth_error:
        discrepancies.append(f"truth unavailable: {truth_error}")
    for key, entry in diff.items():
        if entry["match"] is False:
            discrepancies.append(
                f"{key}: ours={entry['ours']} theirs={entry['theirs']} "
                f"(delta {entry['delta']:+d})"
            )
    if missing:
        discrepancies.append(
            f"{len(missing)} event(s) truth says were sent never reached our /webhook "
            "(dropped webhooks - this is the number that matters for 'nothing lost')"
        )
    if unexpected and theirs_ids:
        discrepancies.append(
            f"{len(unexpected)} event(s) we received are not in truth "
            "(scoping artefact from an earlier run, or a bug)"
        )
    if summary and summary["total_events"] and run["requested_count"] and \
            summary["total_events"] != run["requested_count"]:
        discrepancies.append(
            f"truth reports {summary['total_events']} events but we requested "
            f"{run['requested_count']}"
        )
    if delta["queued"] > 0:
        discrepancies.append(
            f"{delta['queued']} job(s) still queued - the run has not drained; "
            "sent/failed will keep moving, so re-run this report after drain"
        )

    body = {
        "run_id": run_id,
        "run": {
            "requested_count": run["requested_count"],
            "duration_seconds": run["duration_seconds"],
            "webhook_url": run["webhook_url"],
            "started_at": _iso(run["started_at"]),
        },
        # THE delta-baseline. Everything compared against truth uses `delta`,
        # never `current` — see the module docstring.
        "stats": {
            "baseline_at_run_start": {
                "sent": int(run["base_sent"]),
                "failed": int(run["base_failed"]),
                "queued": int(run["base_queued"]),
                "duplicates_blocked": int(run["base_duplicates_blocked"]),
            },
            "current_lifetime": {k: current[k] for k in
                                 ("sent", "failed", "queued", "duplicates_blocked")},
            "delta_this_run": delta,
        },
        "scoping": {
            "events": scoping,
            "jobs": ours.pop("jobs_scoping", None),
            "reliable": bool(theirs_ids),
        },
        "ours": ours,
        "theirs": _public_summary(summary),
        "diff": diff,
        "duplicates_analysis": _duplicates_analysis(
            run, current["dup_rule_user"], current["dup_events_would_dm"],
            theirs_duplicates,
        ),
        "events_missing_from_ours": {
            "count": len(missing),
            "sample": missing[:50],
        },
        "events_not_in_truth": {
            "count": len(unexpected),
            "sample": unexpected[:50],
        },
        "discrepancies": discrepancies,
        "truth_error": truth_error,
        "truth_raw": _truth_excerpt(truth) if truth is not None else None,
    }
    return JSONResponse(body)


def _public_summary(summary: dict | None) -> dict | None:
    """Strip the private working sets (prefixed `_`) before serialising: they
    are sets, which are not JSON-serialisable, and nobody reads 500 raw ids."""
    if summary is None:
        return None
    return {k: v for k, v in summary.items() if not k.startswith("_")}


async def _fetch_truth(run_id: str) -> tuple[Any, str | None]:
    """GET /v1/simulate/{run_id}/truth. Returns (payload, error_message)."""
    try:
        response = await pseudogram.get_client().get(
            f"/v1/simulate/{run_id}/truth",
            headers={"X-API-Key": config.PSEUDOGRAM_API_KEY},
            timeout=config.HTTP_TIMEOUT_SECONDS,
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return None, f"pseudogram unreachable: {exc!r}"

    if not 200 <= response.status_code < 300:
        hint = ""
        if response.status_code in (401, 403):
            hint = " (PSEUDOGRAM_API_KEY is not valid yet)"
        return None, (
            f"pseudogram returned {response.status_code}{hint}: "
            f"{(response.text or '')[:500]}"
        )

    parsed = _safe_json(response)
    if parsed is None:
        return None, "truth response was not JSON"
    return parsed, None
