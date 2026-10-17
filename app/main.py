"""FastAPI application entrypoint (``uvicorn app.main:app``)."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import auth
from .db import SessionFactory
from .dbmigrate import apply_schema
from .importer import import_legacy
from .routes import create_app
from .settings import INSECURE_DEFAULTS, settings

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"


def configure_logging() -> None:
    level = os.getenv("TSH_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format=LOG_FORMAT)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.db_echo else logging.WARNING
    )


log = logging.getLogger("tsh")


def warn_about_defaults() -> None:
    rendered = {f"{u.username}:{u.password}:{u.role}" for u in settings.users}
    if rendered & INSECURE_DEFAULTS:
        log.warning(
            "default credentials are in use (%s) - set TSH_USERS before exposing "
            "this container to the network",
            ", ".join(sorted(rendered & INSECURE_DEFAULTS)),
        )
    if settings.readonly_password:
        log.info("shared read-only password is enabled")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log.info("starting TSH API version %s (domain=%s)", "1.0.0", settings.domain)
    warn_about_defaults()

    await apply_schema()
    try:
        async with SessionFactory() as session:
            await auth.bootstrap_users(session)
            await session.commit()
    except Exception:
        log.exception("bootstrap users failed (existing accounts still work)")

    if settings.auto_migrate:
        try:
            async with SessionFactory() as session:
                result = await import_legacy(session)
                await session.commit()
                if result.get("imported"):
                    log.info("legacy import: %s", result)
        except Exception:
            log.exception("legacy import failed (continuing with the current data)")
    try:
        yield
    finally:
        from .db import engine

        await engine.dispose()
        log.info("TSH API stopped")


app = create_app()
app.router.lifespan_context = lifespan


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.getenv("TSH_BIND_HOST", "0.0.0.0"),
        port=int(os.getenv("TSH_BACKEND_PORT", "37296")),
        log_level=os.getenv("TSH_LOG_LEVEL", "info").lower(),
    )
