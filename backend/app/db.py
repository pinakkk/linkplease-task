# Copyright (c) 2026 Pinak Kundu. All rights reserved.
# Licensed under the Business Source License 1.1 (see LICENSE).
# No use, copying, or modification without written permission.
"""Database access: one asyncpg pool, created at startup, shared by the HTTP
handlers and the background loops. No ORM — every query is visible SQL."""
import logging
import pathlib

import asyncpg

from . import config

log = logging.getLogger("linkplease.db")

_pool: asyncpg.Pool | None = None

SCHEMA_PATH = pathlib.Path(__file__).with_name("schema.sql")


async def connect() -> asyncpg.Pool:
    """Create the pool and run the idempotent migration. Called once, from the
    FastAPI lifespan."""
    global _pool
    if _pool is not None:
        return _pool
    if not config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    _pool = await asyncpg.create_pool(
        dsn=_normalise_dsn(config.DATABASE_URL),
        min_size=config.DB_POOL_MIN,
        max_size=config.DB_POOL_MAX,
        command_timeout=30,
    )
    await migrate(_pool)
    return _pool


def _normalise_dsn(dsn: str) -> str:
    """Fly hands out `postgres://`, which asyncpg accepts; some tools emit
    `postgresql+asyncpg://`, which it does not. Normalise both."""
    if dsn.startswith("postgresql+asyncpg://"):
        return "postgresql://" + dsn.split("://", 1)[1]
    return dsn


async def migrate(pool: asyncpg.Pool) -> None:
    """Run schema.sql. Every statement is IF NOT EXISTS, so this is safe on
    every boot and we need no migration framework for five tables."""
    sql = SCHEMA_PATH.read_text()
    async with pool.acquire() as conn:
        await conn.execute(sql)
    log.info("schema applied")


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("database pool not initialised")
    return _pool


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# --- Small helpers used everywhere -------------------------------------------

async def fetch(query: str, *args) -> list[asyncpg.Record]:
    async with pool().acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args) -> asyncpg.Record | None:
    async with pool().acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetchval(query: str, *args):
    async with pool().acquire() as conn:
        return await conn.fetchval(query, *args)


async def execute(query: str, *args) -> str:
    async with pool().acquire() as conn:
        return await conn.execute(query, *args)


async def bump_counter(name: str, delta: int = 1, conn: asyncpg.Connection | None = None) -> None:
    """Increment a monotonic counter. Accepts an existing connection so it can
    join a caller's transaction (the matcher does this so a counter bump and the
    job insert that caused it commit together)."""
    sql = """
        INSERT INTO counters (name, value) VALUES ($1, $2)
        ON CONFLICT (name) DO UPDATE SET value = counters.value + EXCLUDED.value
    """
    if conn is not None:
        await conn.execute(sql, name, delta)
    else:
        await execute(sql, name, delta)


async def counter(name: str) -> int:
    value = await fetchval("SELECT value FROM counters WHERE name = $1", name)
    return int(value or 0)
