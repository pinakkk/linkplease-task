"""A deliberately hostile stand-in for the PseudoGram mock API.

The real mock API (ASSIGNMENT, "The mock API") fails ~20% of the time with a
500, rate-limits at 10 requests per rolling 60s, and lets ~15% of *accepted*
DMs quietly turn into `failed` later. That behaviour is the assignment, so the
test suite has to reproduce it — but reproducing it *randomly* would make every
test flaky, and a flaky test that fails 1 run in 20 teaches us nothing.

So this stub is fully deterministic by default:

* `CONFIG.script` is a list of forced outcomes, consumed one per send. That is
  how a test says "the next five calls are 500s" and gets exactly that.
* the injection *rates* are 0.0 unless a test sets them.
* `CONFIG.chaos` flips it into seeded-random mode reproducing the documented
  real rates, for soak testing.

It is importable (mounted in-process over `httpx.ASGITransport`, so no socket is
ever opened) and also runnable standalone:

    uvicorn tests.fake_pseudogram:app --port 9999
"""
from __future__ import annotations

import datetime as dt
import itertools
import random
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

# Outcomes a test can force. "accepted" means a 202; the delivery outcome that
# follows is a separate axis (see `deliver_script` / `deliver_failure_rate`).
Outcome = Literal["accepted", "rate_limited", "server_error", "bad_request", "timeout"]


# --- Recorded traffic ---------------------------------------------------------

@dataclass
class SendRecord:
    """One inbound POST /v1/dm/send, whether or not it produced a DM."""
    idempotency_key: str | None
    recipient_user_id: str | None
    message: str | None
    comment_id: str | None
    outcome: str
    dm_id: str | None
    at: float = field(default_factory=time.monotonic)


@dataclass
class DM:
    dm_id: str
    recipient_user_id: str | None
    message: str | None
    comment_id: str | None
    status: str = "queued"          # queued | delivered | failed
    created: float = field(default_factory=time.monotonic)
    # Absolute monotonic time at which `status` becomes terminal. `created`
    # means "already terminal" when the delay is 0.
    terminal_at: float = 0.0
    terminal_status: str = "delivered"
    idempotency_key: str | None = None


# --- Injection configuration --------------------------------------------------

@dataclass
class Config:
    """Everything a test can dial. Module-level singleton, reset per test."""

    # --- deterministic sequencing (highest precedence) ---
    # Outcomes consumed one per send. Empty = fall through to rates/limiter.
    script: list[Outcome] = field(default_factory=list)
    # Per-DM delivery outcomes ("delivered"/"failed"), consumed per accepted DM.
    deliver_script: list[str] = field(default_factory=list)

    # --- rate injection (0.0 = never, deterministic "off") ---
    error_rate_500: float = 0.0
    error_rate_429: float = 0.0
    error_rate_400: float = 0.0
    deliver_failure_rate: float = 0.0

    # --- rolling-window rate limiter (the real one is 10 per 60s) ---
    rate_limit_enabled: bool = False
    rate_limit_max: int = 10
    rate_limit_window: float = 60.0
    retry_after: int = 5

    # --- delivery timing ---
    # 0.0 => a DM is terminal the instant it is created, so the reconciler
    # confirms on its first poll and tests stay fast.
    deliver_delay: float = 0.0

    # --- chaos mode: seeded random at the documented real rates ---
    chaos: bool = False
    seed: int = 1234

    # --- latency injection, for the timeout tests ---
    send_delay: float = 0.0

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)

    def reset(self, **overrides: Any) -> None:
        self.script = []
        self.deliver_script = []
        self.error_rate_500 = 0.0
        self.error_rate_429 = 0.0
        self.error_rate_400 = 0.0
        self.deliver_failure_rate = 0.0
        self.rate_limit_enabled = False
        self.rate_limit_max = 10
        self.rate_limit_window = 60.0
        self.retry_after = 5
        self.deliver_delay = 0.0
        self.chaos = False
        self.seed = 1234
        self.send_delay = 0.0
        self.rng = random.Random(self.seed)
        for key, value in overrides.items():
            setattr(self, key, value)
        if "seed" in overrides:
            self.rng = random.Random(self.seed)

    def enable_chaos(self, seed: int = 1234) -> None:
        """Reproduce the documented real rates: ~20% 500s, ~15% of accepted DMs
        eventually failing, and the real 10-per-60s limiter."""
        self.reset(
            chaos=True,
            seed=seed,
            error_rate_500=0.20,
            deliver_failure_rate=0.15,
            rate_limit_enabled=True,
            rate_limit_max=10,
            rate_limit_window=60.0,
        )


class State:
    """All mutable server state, so `reset()` is one assignment per field."""

    def __init__(self) -> None:
        self.dms: dict[str, DM] = {}
        self.by_key: dict[str, str] = {}         # Idempotency-Key -> dm_id
        self.sends: list[SendRecord] = []        # every inbound send attempt
        self.reads: list[str] = []               # every GET /v1/dm/{id}
        self.window: list[float] = []            # send timestamps, for the limiter
        self.ids = itertools.count(1)

    def reset(self) -> None:
        self.__init__()

    def new_dm_id(self) -> str:
        return f"dm_{next(self.ids):06x}"


CONFIG = Config()
STATE = State()


def reset(**overrides: Any) -> None:
    """Convenience used by the conftest fixture and by tests directly."""
    CONFIG.reset(**overrides)
    STATE.reset()


# --- Helpers ------------------------------------------------------------------

def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _next_scripted() -> Outcome | None:
    if CONFIG.script:
        return CONFIG.script.pop(0)
    return None


def _roll(rate: float) -> bool:
    return rate > 0.0 and CONFIG.rng.random() < rate


def _decide_outcome() -> Outcome:
    """Deterministic script first; then the rolling limiter (which is a real
    property of the server, not injected noise); then the injected rates."""
    scripted = _next_scripted()
    if scripted is not None:
        return scripted

    if CONFIG.rate_limit_enabled:
        cutoff = time.monotonic() - CONFIG.rate_limit_window
        STATE.window = [t for t in STATE.window if t > cutoff]
        if len(STATE.window) >= CONFIG.rate_limit_max:
            return "rate_limited"

    if _roll(CONFIG.error_rate_429):
        return "rate_limited"
    if _roll(CONFIG.error_rate_500):
        return "server_error"
    if _roll(CONFIG.error_rate_400):
        return "bad_request"
    return "accepted"


def _decide_delivery() -> str:
    if CONFIG.deliver_script:
        return CONFIG.deliver_script.pop(0)
    return "failed" if _roll(CONFIG.deliver_failure_rate) else "delivered"


def _record(outcome: str, key: str | None, body: dict, dm_id: str | None) -> None:
    STATE.sends.append(
        SendRecord(
            idempotency_key=key,
            recipient_user_id=body.get("recipient_user_id"),
            message=body.get("message"),
            comment_id=body.get("comment_id"),
            outcome=outcome,
            dm_id=dm_id,
        )
    )


def _settled(dm: DM) -> str:
    """Lazily apply the queued -> terminal transition when its delay elapses.
    Doing it on read means no background task and no wall-clock coupling."""
    if dm.status == "queued" and time.monotonic() >= dm.terminal_at:
        dm.status = dm.terminal_status
    return dm.status


# --- The API ------------------------------------------------------------------

router = APIRouter()


@router.post("/v1/dm/send")
async def send_dm(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_request", "detail": "bad json"}, 400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_request", "detail": "not an object"}, 400)

    key = request.headers.get("Idempotency-Key")

    # Idempotency is checked BEFORE any failure injection: the documented
    # contract is "same key twice returns the original dm_id instead of sending
    # again", and our whole timeout-retry story (BLUEPRINT §4.4) depends on it.
    if key and key in STATE.by_key:
        dm = STATE.dms[STATE.by_key[key]]
        _record("accepted_idempotent", key, body, dm.dm_id)
        return JSONResponse({"dm_id": dm.dm_id, "status": "queued"}, 202)

    outcome = _decide_outcome()

    if outcome == "timeout":
        # Force a client-side timeout by sleeping past any sane HTTP timeout.
        # The send is recorded as received, which is exactly the nasty case:
        # the server did the work, the client never learned about it.
        _record("timeout", key, body, None)
        import asyncio
        await asyncio.sleep(30.0)
        return JSONResponse({"dm_id": "unreachable", "status": "queued"}, 202)

    if outcome == "rate_limited":
        _record("rate_limited", key, body, None)
        return JSONResponse(
            {"error": "rate_limited"},
            429,
            headers={"Retry-After": str(CONFIG.retry_after)},
        )
    if outcome == "server_error":
        _record("server_error", key, body, None)
        return JSONResponse({"error": "internal_error"}, 500)
    if outcome == "bad_request":
        _record("bad_request", key, body, None)
        return JSONResponse(
            {"error": "invalid_request", "detail": "injected"}, 400
        )

    # Accepted: 202, a dm_id, and a *pending* delivery outcome.
    if CONFIG.send_delay:
        import asyncio
        await asyncio.sleep(CONFIG.send_delay)

    dm_id = STATE.new_dm_id()
    dm = DM(
        dm_id=dm_id,
        recipient_user_id=body.get("recipient_user_id"),
        message=body.get("message"),
        comment_id=body.get("comment_id"),
        terminal_at=time.monotonic() + CONFIG.deliver_delay,
        terminal_status=_decide_delivery(),
        idempotency_key=key,
    )
    STATE.dms[dm_id] = dm
    if key:
        STATE.by_key[key] = dm_id
    STATE.window.append(time.monotonic())
    _record("accepted", key, body, dm_id)
    return JSONResponse({"dm_id": dm_id, "status": "queued"}, 202)


@router.get("/v1/dm/{dm_id}")
async def get_dm(dm_id: str) -> JSONResponse:
    """Status reads. Explicitly free — they never touch the rate window."""
    STATE.reads.append(dm_id)
    dm = STATE.dms.get(dm_id)
    if dm is None:
        return JSONResponse({"error": "not_found"}, 404)
    return JSONResponse(
        {
            "dm_id": dm.dm_id,
            "status": _settled(dm),
            "recipient_user_id": dm.recipient_user_id,
            "updated_at": _now_iso(),
        },
        200,
    )


# --- Test-only control plane --------------------------------------------------

control = APIRouter(prefix="/_test")


@control.post("/reset")
async def _reset(request: Request) -> dict:
    try:
        overrides = await request.json()
    except Exception:
        overrides = {}
    reset(**(overrides if isinstance(overrides, dict) else {}))
    return {"ok": True}


@control.post("/config")
async def _set_config(request: Request) -> dict:
    body = await request.json()
    for key, value in body.items():
        setattr(CONFIG, key, value)
    if "seed" in body:
        CONFIG.rng = random.Random(CONFIG.seed)
    return {"ok": True}


@control.post("/script")
async def _script(request: Request) -> dict:
    """Force the next N send outcomes, in order."""
    body = await request.json()
    CONFIG.script = list(body.get("outcomes", []))
    if "deliveries" in body:
        CONFIG.deliver_script = list(body["deliveries"])
    return {"ok": True, "pending": len(CONFIG.script)}


@control.get("/sends")
async def _sends() -> dict:
    """Everything we received, so a test can assert 'exactly one DM per
    (rule,user)' and 'the retry reused the same Idempotency-Key'."""
    return {
        "sends": [
            {
                "idempotency_key": s.idempotency_key,
                "recipient_user_id": s.recipient_user_id,
                "message": s.message,
                "comment_id": s.comment_id,
                "outcome": s.outcome,
                "dm_id": s.dm_id,
            }
            for s in STATE.sends
        ],
        "dms": [
            {
                "dm_id": d.dm_id,
                "recipient_user_id": d.recipient_user_id,
                "status": _settled(d),
                "idempotency_key": d.idempotency_key,
            }
            for d in STATE.dms.values()
        ],
        "reads": list(STATE.reads),
    }


# --- Python-side inspection helpers (used by in-process tests) ---------------

def accepted_sends() -> list[SendRecord]:
    """Sends that actually created a DM (excludes idempotent replays)."""
    return [s for s in STATE.sends if s.outcome == "accepted"]


def send_attempts() -> list[SendRecord]:
    """Every inbound POST, including failures and idempotent replays."""
    return list(STATE.sends)


def idempotency_keys() -> list[str | None]:
    return [s.idempotency_key for s in STATE.sends]


def distinct_dms() -> list[DM]:
    return list(STATE.dms.values())


def dms_to(user_id: str) -> list[DM]:
    return [d for d in STATE.dms.values() if d.recipient_user_id == user_id]


def force_delivery(dm_id: str, status: str) -> None:
    """Flip an already-accepted DM's eventual outcome (the '202 then failed'
    case from ASSIGNMENT: ~15% of accepted DMs end up failed)."""
    dm = STATE.dms[dm_id]
    dm.terminal_status = status
    dm.status = status if time.monotonic() >= dm.terminal_at else "queued"


app = FastAPI(title="fake-pseudogram")
app.include_router(router)
app.include_router(control)
