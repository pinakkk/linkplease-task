# Copyright (c) 2026 Pinak Kundu. All rights reserved.
# Licensed under the Business Source License 1.1 (see LICENSE).
# No use, copying, or modification without written permission.
"""FastAPI application: routes, lifespan, boot recovery and the supervised
background loops.

Every piece of pending state lives in Postgres, so a restart loses nothing that
was already accepted; the boot-recovery step below is what turns that promise
into practice (BLUEPRINT §5 rows 7 and 8)."""
import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import config, db, rules, simulate, stats, webhook

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("linkplease.main")

# How many unmatched events we replay at boot. Bounded so a huge backlog cannot
# stall startup; the matcher sweep loop picks up whatever is left.
BOOT_REDISPATCH_LIMIT = 300


# --- Boot recovery -----------------------------------------------------------

async def requeue_stale_sending() -> int:
    """A job left in SENDING is one the process died in the middle of. Put it
    back in the QUEUED pool in the SAME cycle: the Idempotency-Key is
    `job:{id}:c{cycle}`, so if the send did land, the retry gets the original
    dm_id back instead of sending twice (BLUEPRINT §5 row 7)."""
    result = await db.execute(
        """
        UPDATE dm_jobs
           SET status = 'QUEUED', next_attempt_at = now(), updated_at = now()
         WHERE status = 'SENDING'
           AND updated_at < now() - make_interval(secs => $1)
        """,
        float(config.SENDING_STALE_SECONDS),
    )
    return _rowcount(result)


async def redispatch_unprocessed_events() -> int:
    """Events we acknowledged with a 200 but crashed before matching. Without
    this they would sit unprocessed forever — we already told PseudoGram we had
    them, so nobody is going to redeliver them for us."""
    try:
        from .matcher import handle_event
    except ImportError:
        log.error("matcher unavailable at boot; unprocessed events not replayed")
        return 0

    rows = await db.fetch(
        """
        SELECT event_id, event_type, payload
        FROM events
        WHERE processed_at IS NULL AND event_type = 'comment.created'
        ORDER BY received_at
        LIMIT $1
        """,
        BOOT_REDISPATCH_LIMIT,
    )
    replayed = 0
    for row in rows:
        payload = _as_dict(row["payload"])
        data = payload.get("data")
        if not isinstance(data, dict):
            data = {}
        try:
            await handle_event(row["event_id"], row["event_type"], data)
            replayed += 1
        except Exception:
            log.exception("boot replay failed for event %s", row["event_id"])
    return replayed


def _rowcount(command_tag: str) -> int:
    """asyncpg returns tags like 'UPDATE 3'; the trailing number is the count."""
    try:
        return int(command_tag.rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return 0


def _as_dict(payload) -> dict:
    """events.payload is JSONB; asyncpg hands it back as a JSON string unless a
    codec is registered, so accept both."""
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (str, bytes)):
        try:
            parsed = json.loads(payload)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


# --- Supervised background loops ---------------------------------------------

async def supervise(name: str, loop_factory) -> None:
    """Run a background loop forever. If it raises we log the traceback and
    restart it after a short pause, so one bad iteration — a transient DB blip,
    an unexpected response shape — can never silently kill the pipeline."""
    while True:
        try:
            await loop_factory()
            log.warning("loop %s returned; restarting", name)
        except asyncio.CancelledError:
            log.info("loop %s cancelled", name)
            raise
        except Exception:
            log.exception("loop %s crashed; restarting in 2s", name)
        await asyncio.sleep(2.0)


def _background_loops() -> list[tuple[str, object]]:
    """Import Agent B's loops lazily and individually, so a missing module
    disables one loop loudly instead of preventing the app from booting."""
    found: list[tuple[str, object]] = []
    for module_name, attr in (
        ("worker", "send_worker_loop"),
        ("reconciler", "reconciler_loop"),
        ("matcher", "matcher_sweep_loop"),
    ):
        try:
            module = __import__(f"{__package__}.{module_name}", fromlist=[attr])
            found.append((attr, getattr(module, attr)))
        except (ImportError, AttributeError):
            log.error("BACKGROUND LOOP MISSING: app.%s.%s", module_name, attr)
    return found


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await db.connect()
    log.info("database connected")

    requeued = await requeue_stale_sending()
    log.info("boot recovery: %d stale SENDING jobs requeued", requeued)
    replayed = await redispatch_unprocessed_events()
    log.info("boot recovery: %d unprocessed events replayed", replayed)

    tasks: list[asyncio.Task] = []
    for name, loop_fn in _background_loops():
        tasks.append(asyncio.create_task(supervise(name, loop_fn), name=name))
        log.info("started background loop %s", name)

    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await db.close()
        log.info("shutdown complete")


app = FastAPI(
    title="LinkPlease",
    description="Instagram comment -> DM automation on the PseudoGram mock API",
    version="1.0.0",
    lifespan=lifespan,
)

# `*` with credentials is rejected by browsers, so the two settings move together.
_allow_credentials = config.CORS_ORIGINS != ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhook.router)
app.include_router(rules.router)
app.include_router(stats.router)
app.include_router(simulate.router)


def _heartbeat(module_name: str) -> float | None:
    """Read LAST_BEAT off a loop module if it publishes one. Kept optional so
    the health check works whether or not the loops advertise liveness."""
    try:
        module = __import__(f"{__package__}.{module_name}", fromlist=["LAST_BEAT"])
    except ImportError:
        return None
    beat = getattr(module, "LAST_BEAT", None)
    return float(beat) if isinstance(beat, (int, float)) else None


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """DB ping plus loop liveness. 503 when the database is unreachable, because
    without it we cannot accept a single webhook."""
    try:
        await db.fetchval("SELECT 1")
    except Exception as exc:
        log.exception("healthz: database ping failed")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)

    now = time.time()
    beats = {}
    for module_name in ("worker", "reconciler"):
        beat = _heartbeat(module_name)
        beats[module_name] = {
            "last_beat": beat,
            "age_seconds": round(now - beat, 3) if beat else None,
        }
    return JSONResponse({"ok": True, "database": "up", "loops": beats})


@app.get("/")
async def root() -> dict:
    return {
        "service": "linkplease",
        "version": "1.0.0",
        "contract_routes": ["POST /webhook", "POST /rules", "GET /stats"],
        "docs": "/docs",
    }
