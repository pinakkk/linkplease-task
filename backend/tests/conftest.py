"""Test harness.

Three things make this suite trustworthy rather than decorative:

1. **A real Postgres.** Every correctness claim in BLUEPRINT §3 is enforced by a
   DB constraint (`uq_live_job`, the `events` primary key). A mock or SQLite
   would test our fantasy of Postgres, not Postgres.
2. **A real HTTP round trip to the fake API**, over `httpx.ASGITransport` — no
   socket, but genuine status codes, headers (`Retry-After`) and JSON bodies, so
   Agent B's status-code handling is actually exercised.
3. **No `sleep`-and-hope.** `wait_until()` polls a predicate with a timeout, so
   the suite is fast when things work and honest when they do not.

`app.config` reads the environment at import time, so `DATABASE_URL` is set in
`conftest` *module scope* — before pytest imports any test module, and therefore
before anything imports `app.*`.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

# --- Environment: must happen before any `app.*` import ----------------------

BACKEND_DIR = Path(__file__).resolve().parent.parent
TEST_DB = f"linkplease_test_{uuid.uuid4().hex[:10]}"
ADMIN_DSN = os.getenv("TEST_ADMIN_DSN", "postgresql://pinak@localhost:5432/postgres")
TEST_DSN = ADMIN_DSN.rsplit("/", 1)[0] + "/" + TEST_DB

TEST_API_KEY = "test_api_key_shhh"

os.environ["DATABASE_URL"] = TEST_DSN
os.environ["PSEUDOGRAM_API_KEY"] = TEST_API_KEY
os.environ["PSEUDOGRAM_BASE_URL"] = "http://fake-pseudogram.test"
# Signature verification defaults OFF so most tests can post plain payloads;
# test_signature.py turns it on explicitly via the `signatures_on` fixture.
os.environ.setdefault("VERIFY_SIGNATURES", "0")
# Compressed loop cadence so the pipeline tests finish in seconds, not minutes.
os.environ.setdefault("WORKER_IDLE_SLEEP", "0.05")
os.environ.setdefault("RECONCILER_INTERVAL", "0.1")
os.environ.setdefault("MATCHER_SWEEP_INTERVAL", "0.2")
os.environ.setdefault("BACKOFF_CAP_SECONDS", "0.2")
os.environ.setdefault("HTTP_TIMEOUT_SECONDS", "1.0")
os.environ.setdefault("SENDING_STALE_SECONDS", "60")

import asyncpg  # noqa: E402
import httpx  # noqa: E402

from tests import fake_pseudogram  # noqa: E402


# --- Skip helpers ------------------------------------------------------------
# Agent B's modules may not exist yet. Tests that need them import via these
# helpers so the suite still *collects and runs* (with skips) before their code
# lands, which is the whole point of writing tests first.

def require(module_name: str, *attrs: str):
    """Import `app.<module_name>` or skip the test, naming what is missing."""
    module = pytest.importorskip(
        f"app.{module_name}", reason=f"app/{module_name}.py not implemented yet"
    )
    for attr in attrs:
        if not hasattr(module, attr):
            pytest.skip(f"app.{module_name}.{attr} not implemented yet")
    return module


def modules_present(*names: str) -> bool:
    import importlib.util

    return all(importlib.util.find_spec(f"app.{n}") is not None for n in names)


# --- Database lifecycle ------------------------------------------------------

def _psql(sql: str) -> None:
    """Create/drop the throwaway database over psql, so we never need an open
    asyncpg connection to `postgres` competing with the pool."""
    subprocess.run(
        ["psql", ADMIN_DSN, "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session", autouse=True)
def test_database():
    """One throwaway database for the whole session; truncated between tests."""
    try:
        _psql(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        _psql(f'CREATE DATABASE "{TEST_DB}"')
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        detail = getattr(exc, "stderr", b"")
        pytest.skip(
            f"local Postgres unavailable ({ADMIN_DSN}): "
            f"{detail.decode(errors='replace') if detail else exc}"
        )
    yield TEST_DSN
    try:
        _psql(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
    except Exception:
        pass


@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.get_event_loop_policy()


@pytest.fixture(scope="session")
async def pool(test_database):
    """The pool `app.db` will use. Created once, schema applied once."""
    from app import db

    p = await db.connect()
    yield p
    await db.close()


@pytest.fixture(autouse=True)
async def clean_db(pool):
    """Fast reset between tests: TRUNCATE beats DROP/CREATE by ~100x, and
    RESTART IDENTITY keeps job_id predictable so idempotency-key assertions
    (`job:1:c0`) can be written literally."""
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE events, dm_jobs, rules, send_log, deleted_comments "
            "RESTART IDENTITY CASCADE"
        )
        await conn.execute("UPDATE counters SET value = 0")
    yield


# --- The fake PseudoGram -----------------------------------------------------

@pytest.fixture(autouse=True)
def fake_api():
    """Reset the stub's state and injection config before every test, so no
    test can be contaminated by the previous one's scripted failures."""
    fake_pseudogram.reset()
    yield fake_pseudogram
    fake_pseudogram.reset()


@pytest.fixture(autouse=True)
def wire_pseudogram(fake_api, monkeypatch):
    """Point `app.pseudogram`'s httpx client at the stub via ASGITransport.

    Prefer real-HTTP-through-ASGI over monkeypatching `send_dm`/`get_dm`: the
    202-vs-429-vs-500 branching, the `Retry-After` header parse and the
    `Idempotency-Key` header are all things we want genuinely exercised, and a
    function-level mock would test none of them.

    Falls back silently (leaving `app.pseudogram` untouched) if the module does
    not exist yet or does not expose a client hook — the affected tests skip on
    their own via `require()`.
    """
    if not modules_present("pseudogram"):
        yield None
        return

    import app.pseudogram as pg

    transport = httpx.ASGITransport(app=fake_api.app)
    client = httpx.AsyncClient(
        transport=transport,
        base_url="http://fake-pseudogram.test",
        timeout=float(os.environ.get("HTTP_TIMEOUT_SECONDS", "1.0")),
        headers={"X-API-Key": TEST_API_KEY},
    )

    # Agent B may expose the client as a module global, a factory, or a lazily
    # created singleton. Cover the common shapes; each is a no-op if absent.
    for attr in ("_client", "client", "CLIENT", "_CLIENT"):
        if hasattr(pg, attr):
            monkeypatch.setattr(pg, attr, client, raising=False)
    for attr in ("get_client", "_get_client", "http_client"):
        if hasattr(pg, attr) and callable(getattr(pg, attr)):
            original = getattr(pg, attr)
            if asyncio.iscoroutinefunction(original):
                async def _factory(_c=client):
                    return _c
            else:
                def _factory(_c=client):
                    return _c
            monkeypatch.setattr(pg, attr, _factory, raising=False)

    yield client

    async def _close():
        await client.aclose()

    try:
        asyncio.get_event_loop().run_until_complete(_close())
    except Exception:
        pass


# --- The app under test ------------------------------------------------------

@pytest.fixture
async def api(pool):
    """An httpx client bound to `app.main.app` over ASGI, WITHOUT running the
    lifespan — the background loops are started explicitly by the tests that
    want them, so a unit test of `/webhook` is not racing a send worker."""
    from app.main import app as fastapi_app

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client


@pytest.fixture
async def loops(pool, wire_pseudogram):
    """Start the background loops for a pipeline test and stop them after.

    Returns a small controller so a test can start only the loops it needs
    (e.g. crash-recovery wants the worker without the matcher sweep).
    """
    started: list[asyncio.Task] = []

    def start(*names: str) -> None:
        for name in names or ("worker", "reconciler", "matcher"):
            attr = {
                "worker": ("worker", "send_worker_loop"),
                "reconciler": ("reconciler", "reconciler_loop"),
                "matcher": ("matcher", "matcher_sweep_loop"),
            }[name]
            module = require(attr[0], attr[1])
            started.append(
                asyncio.create_task(getattr(module, attr[1])(), name=name)
            )

    yield start

    for task in started:
        task.cancel()
    for task in started:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# --- Polling helper ----------------------------------------------------------

async def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02,
                     message: str = "condition not met"):
    """Poll `predicate` (sync or async) until truthy or `timeout` elapses.

    Every wait in this suite goes through here. `time.sleep(2)` would make the
    suite slow when it passes and useless when it fails; this is fast on success
    and produces a real failure message on timeout.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        last = result
        if result:
            return result
        await asyncio.sleep(interval)
    raise AssertionError(f"wait_until timed out after {timeout}s: {message} (last={last!r})")


@pytest.fixture
def waiter():
    return wait_until


# --- Payload builders --------------------------------------------------------

_counter = {"n": 0}


def _next_id(prefix: str) -> str:
    _counter["n"] += 1
    return f"{prefix}_{_counter['n']:06d}"


def comment_event(
    text: str = "PRICE please 🙏",
    user_id: str = "usr_alice",
    username: str = "alice",
    comment_id: str | None = None,
    event_id: str | None = None,
    post_id: str = "post_1",
) -> dict:
    """A `comment.created` in exactly the shape ASSIGNMENT documents."""
    return {
        "event_id": event_id or _next_id("evt"),
        "event_type": "comment.created",
        "sent_at": "2026-08-10T09:14:22.481Z",
        "data": {
            "comment_id": comment_id or _next_id("cmt"),
            "post_id": post_id,
            "text": text,
            "created_at": "2026-08-10T09:14:21.900Z",
            "from": {"user_id": user_id, "username": username},
        },
    }


def deleted_event(comment_id: str, event_id: str | None = None) -> dict:
    """`comment.deleted`: same envelope, only `comment_id` populated."""
    return {
        "event_id": event_id or _next_id("evt"),
        "event_type": "comment.deleted",
        "sent_at": "2026-08-10T09:20:00.000Z",
        "data": {"comment_id": comment_id},
    }


def sign(raw: bytes, secret: str = TEST_API_KEY) -> str:
    """The header PseudoGram sends: HMAC-SHA256 of the raw bytes, hex, prefixed."""
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def encode(payload: dict) -> bytes:
    """Serialise once, so the bytes we sign are the bytes we send."""
    return json.dumps(payload).encode()


@pytest.fixture
def make_event():
    return comment_event


@pytest.fixture
def make_deleted():
    return deleted_event


# --- Direct DB helpers used by many tests ------------------------------------

@pytest.fixture
async def create_rule(pool):
    async def _create(keyword: str = "PRICE", message: str = "Here is the price list",
                      rule_id: str | None = None) -> str:
        rid = rule_id or _next_id("rule")
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO rules (rule_id, keyword, dm_message) VALUES ($1,$2,$3)",
                rid, keyword, message,
            )
        return rid

    return _create


@pytest.fixture
async def jobs(pool):
    """Read job rows back. Every pipeline assertion goes through the DB, because
    the DB is the system's source of truth (BLUEPRINT §1)."""

    async def _jobs(**where):
        sql = "SELECT * FROM dm_jobs"
        args = []
        if where:
            clauses = []
            for i, (key, value) in enumerate(where.items(), start=1):
                clauses.append(f"{key} = ${i}")
                args.append(value)
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY job_id"
        async with pool.acquire() as conn:
            return await conn.fetch(sql, *args)

    return _jobs


@pytest.fixture
async def counters(pool):
    async def _counter(name: str) -> int:
        async with pool.acquire() as conn:
            v = await conn.fetchval("SELECT value FROM counters WHERE name = $1", name)
        return int(v or 0)

    return _counter


@pytest.fixture
def signatures_on(monkeypatch):
    """Turn HMAC verification on for a test (Part B)."""
    from app import config

    monkeypatch.setattr(config, "VERIFY_SIGNATURES", True)
    monkeypatch.setattr(config, "PSEUDOGRAM_API_KEY", TEST_API_KEY)
    return TEST_API_KEY
