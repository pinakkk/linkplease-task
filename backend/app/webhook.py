"""POST /webhook — the ingest path. Graded on returning 200 within 5 seconds
(ASSIGNMENT), so the only work done inline is an HMAC check and a single
Postgres upsert. Matching, dedup and sending all happen in the background
(BLUEPRINT §4.1)."""
import asyncio
import datetime as dt
import hmac
import hashlib
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import config, db

log = logging.getLogger("linkplease.webhook")

router = APIRouter()

SIGNATURE_HEADER = "X-PseudoGram-Signature"


# --- Signature verification (Part B) -----------------------------------------

def verify_signature(raw: bytes, header: str | None, secret: str) -> bool:
    """HMAC-SHA256 of the RAW request bytes, keyed with our API key.

    The header is documented as `sha256=<hex>`; we tolerate a bare hex digest
    too so a slightly different sender still verifies. Comparison is
    constant-time so we leak no timing information about the expected digest.
    """
    if not header:
        return False
    provided = header.strip()
    if provided.startswith("sha256="):
        provided = provided[len("sha256="):]
    provided = provided.strip()
    if not provided:
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided.lower(), expected)


# --- Small parsing helpers ---------------------------------------------------

def parse_sent_at(value) -> dt.datetime | None:
    """Their `sent_at` is ISO8601 ending in 'Z', which fromisoformat rejects on
    older Pythons and which we never want to crash ingest over. Anything we
    cannot parse is stored as NULL — it is audit metadata, not a decision input
    (order is explicitly not guaranteed, BLUEPRINT §4.2)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# --- Background dispatch -----------------------------------------------------

async def dispatch(event_id: str, event_type: str, data: dict) -> None:
    """Hand a freshly-ingested event to the matcher. Runs in its own task so it
    can never delay the 200. Imported lazily so main/webhook import order can
    never deadlock and so the app still boots if matcher.py is not present."""
    try:
        from .matcher import handle_event
    except ImportError:
        log.error("matcher.handle_event unavailable; event %s not matched", event_id)
        return
    await handle_event(event_id, event_type, data)


async def _dispatch_guarded(event_id: str, event_type: str, data: dict) -> None:
    """Wrapper so an exception inside the task is logged rather than becoming a
    'Task exception was never retrieved' warning nobody reads."""
    try:
        await dispatch(event_id, event_type, data)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("dispatch failed for event %s", event_id)


def schedule_dispatch(event_id: str, event_type: str, data: dict) -> None:
    """Fire-and-forget, with a strong reference kept until the task finishes so
    the event loop cannot garbage-collect a pending task mid-flight."""
    task = asyncio.create_task(_dispatch_guarded(event_id, event_type, data))
    _in_flight.add(task)
    task.add_done_callback(_in_flight.discard)


_in_flight: set[asyncio.Task] = set()


# --- The route ---------------------------------------------------------------

@router.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    # 1. Raw bytes first. Re-serialising parsed JSON would change the bytes and
    #    break the HMAC.
    raw = await request.body()

    # 2. Signature (Part B). Forged requests never touch the database.
    if config.VERIFY_SIGNATURES:
        header = request.headers.get(SIGNATURE_HEADER)
        if not verify_signature(raw, header, config.PSEUDOGRAM_API_KEY):
            return JSONResponse({"error": "invalid_signature"}, status_code=401)

    # 3. Parse.
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_json"}, status_code=400)

    # 4. Extract. event_id is the dedup key, so it is the only hard requirement.
    event_id = body.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        return JSONResponse({"error": "missing_event_id"}, status_code=400)
    event_type = body.get("event_type") or "unknown"
    sent_at = parse_sent_at(body.get("sent_at"))
    data = body.get("data")
    if not isinstance(data, dict):
        data = {}

    # 5. One round trip that both records the event and tells us whether we have
    #    seen it before. `xmax = 0` is the standard Postgres upsert-detection
    #    trick: on a genuine INSERT the row has no updating transaction, so xmax
    #    is 0; on the DO UPDATE branch xmax carries our own xid and is non-zero.
    row = await db.fetchrow(
        """
        INSERT INTO events (event_id, event_type, payload, sent_at)
        VALUES ($1, $2, $3::jsonb, $4)
        ON CONFLICT (event_id) DO UPDATE
            SET redeliveries = events.redeliveries + 1
        RETURNING (xmax = 0) AS inserted, redeliveries
        """,
        event_id,
        event_type,
        json.dumps(body),
        sent_at,
    )

    # 6. Redelivery: already ingested once. Count it and stop — dispatching
    #    again would risk a second DM for the same comment.
    if not row["inserted"]:
        await db.bump_counter("duplicate_events_suppressed")
        log.info("redelivery event_id=%s count=%s", event_id, row["redeliveries"])
        return JSONResponse({"status": "ok"}, status_code=200)

    # 7. New event: match it in the background and answer immediately.
    schedule_dispatch(event_id, event_type, data)
    return JSONResponse({"status": "ok"}, status_code=200)
