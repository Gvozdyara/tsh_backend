"""Application settings.

Everything is driven by environment variables so the same image can run in the
compose stack, behind the host nginx and in tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


LANGS: tuple[str, ...] = ("ru", "en", "es")

#: UI language code -> translation column prefix in the articles table.
LANG_COLUMNS: dict[str, str] = {"ru": "ru", "en": "en", "es": "es"}

#: PostgreSQL text search configuration per language (used for the generated
#: tsvector columns defined in tsh_manifest/db/initdb/01-schema.sql).
TS_CONFIG: dict[str, str] = {"ru": "russian", "en": "english", "es": "spanish"}


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass(frozen=True)
class User:
    username: str
    password: str
    role: str  # "admin" | "reader"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _parse_users(raw: str) -> list[User]:
    users: list[User] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) < 2:
            continue
        username = parts[0].strip()
        password = parts[1]
        role = (parts[2].strip().lower() if len(parts) > 2 else "reader")
        if role not in {"admin", "reader"}:
            role = "reader"
        if username:
            users.append(User(username=username, password=password, role=role))
    return users


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: _env(
            "DATABASE_URL",
            "postgresql+asyncpg://tsh:tsh@db:5432/tsh",
        )
    )
    db_echo: bool = field(default_factory=lambda: _env_bool("TSH_DB_ECHO", False))

    #: Only admins may mutate articles. Readers get a read-only knowledge base.
    users: tuple[User, ...] = field(
        default_factory=lambda: tuple(
            _parse_users(_env("TSH_USERS", "admin:admin:admin,guest:guest:reader"))
        )
    )
    readonly_password: str = field(
        default_factory=lambda: _env("TSH_READONLY_PASSWORD", "")
    )
    session_ttl_minutes: int = field(
        default_factory=lambda: _env_int("TSH_SESSION_TTL_MINUTES", 720)
    )
    cookie_name: str = field(
        default_factory=lambda: _env("TSH_COOKIE_NAME", "tsh_session")
    )
    cookie_secure: bool = field(
        default_factory=lambda: _env_bool("TSH_COOKIE_SECURE", False)
    )
    domain: str = field(default_factory=lambda: _env("TSH_DOMAIN", "tsh.local"))

    seed_file: str = field(
        default_factory=lambda: _env("TSH_SEED_FILE", "/seed/tsh_base_cloud.json")
    )
    endings_file: str = field(
        default_factory=lambda: _env("TSH_ENDINGS_FILE", "/seed/endings.txt")
    )
    auto_migrate: bool = field(
        default_factory=lambda: _env_bool("TSH_AUTO_MIGRATE", True)
    )
    root_id: str = field(default_factory=lambda: _env("TSH_ROOT_ID", "00000001"))

    allowed_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o.strip()
            for o in _env(
                "TSH_ALLOWED_ORIGINS",
                "http://127.0.0.1:8080,http://localhost:8080",
            ).split(",")
            if o.strip()
        )
    )

    @property
    def admin_username(self) -> str | None:
        for user in self.users:
            if user.is_admin:
                return user.username
        return None


settings = Settings()

#: Insecure default that must be changed in production.
INSECURE_DEFAULTS = {"admin:admin:admin", "guest:guest:reader"}
