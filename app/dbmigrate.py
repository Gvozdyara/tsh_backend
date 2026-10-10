"""Idempotent schema bootstrap.

The postgres image already runs backend/schema/01-schema.sql when the data
volume is created for the first time (it is bind mounted into
/docker-entrypoint-initdb.d). This module re-applies the very same file from the
backend on every start so an existing volume is always up to date.

asyncpg is used directly here: it executes a multi statement script in one round
trip (SQLAlchemy would prepare the script as a single statement and PostgreSQL
rejects that).
"""

from __future__ import annotations

import logging
from pathlib import Path

import asyncpg

from .settings import settings

log = logging.getLogger("tsh.dbmigrate")

SCHEMA_CANDIDATES = (
    Path("/app/schema/01-schema.sql"),
    Path(__file__).resolve().parents[1] / "schema" / "01-schema.sql",
)


def schema_sql() -> str:
    for candidate in SCHEMA_CANDIDATES:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise FileNotFoundError(
        "01-schema.sql not found, looked in: "
        + ", ".join(str(path) for path in SCHEMA_CANDIDATES)
    )


def dsn() -> str:
    """SQLAlchemy URL -> plain libpq style DSN for asyncpg."""

    url = settings.database_url
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url


async def apply_schema() -> None:
    sql = schema_sql()
    connection = await asyncpg.connect(dsn())
    try:
        await connection.execute(sql)
    finally:
        await connection.close()
    log.info("schema applied (%s statements/%s bytes)", sql.count(";"), len(sql))
