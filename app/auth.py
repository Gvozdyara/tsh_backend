"""Authentication and access levels.

Two roles exist:

* ``admin``  - may create, edit, move and delete articles
* ``reader`` - read only access to the whole knowledge base

Sessions are opaque random tokens kept in memory and handed to the browser as
an HttpOnly cookie.  A restart of the container simply forces a new login,
which is acceptable for a single backend deployment.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Cookie, Depends, Header, HTTPException, Response, status

from .settings import User, settings

log = logging.getLogger("tsh.auth")

#: token -> AuthSession
SESSIONS: dict[str, "AuthSession"] = {}


@dataclass
class AuthSession:
    token: str
    username: str
    role: str
    created_at: datetime
    expires_at: datetime
    last_seen_at: datetime

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# Cache of bcrypt hashes for the configured users (plain text stays in env).
_HASH_CACHE: dict[str, bytes] = {}


def _hash_of(raw: str) -> bytes:
    cached = _HASH_CACHE.get(raw)
    if cached is None:
        cached = bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt(rounds=10))
        _HASH_CACHE[raw] = cached
    return cached


def _find_user(username: str) -> User | None:
    for user in settings.users:
        if hmac.compare_digest(user.username, username):
            return user
    return None


def _verify(user: User, password: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), _hash_of(user.password))
    except ValueError:  # pragma: no cover - defensive
        return False


def prune_sessions(now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    expired = [token for token, s in SESSIONS.items() if s.expires_at <= now]
    for token in expired:
        SESSIONS.pop(token, None)


def login(username: str, password: str) -> AuthSession | None:
    user = _find_user(username)
    if user is not None and _verify(user, password):
        return _create_session(user.username, user.role)

    # Optional shared read-only password ("guest mode").
    if settings.readonly_password and secrets.compare_digest(
        password, settings.readonly_password
    ):
        return _create_session(username or "guest", "reader")

    return None


def _create_session(username: str, role: str) -> AuthSession:
    now = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    session = AuthSession(
        token=token,
        username=username,
        role=role,
        created_at=now,
        expires_at=now + timedelta(minutes=settings.session_ttl_minutes),
        last_seen_at=now,
    )
    SESSIONS[token] = session
    return session


def logout(token: str | None) -> None:
    if token:
        SESSIONS.pop(token, None)


def set_session_cookie(response: Response, session: AuthSession) -> None:
    max_age = int((session.expires_at - datetime.now(timezone.utc)).total_seconds())
    response.set_cookie(
        key=settings.cookie_name,
        value=session.token,
        max_age=max(0, max_age),
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(settings.cookie_name, path="/")


async def current_session(
    tsh_session: str | None = Cookie(default=None, alias=settings.cookie_name),
    authorization: str | None = Header(default=None),
) -> AuthSession:
    """Resolve the caller from the session cookie (or a Bearer token)."""

    token = tsh_session
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Cookie"},
        )

    prune_sessions()
    session = SESSIONS.get(token)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired"
        )
    session.last_seen_at = datetime.now(timezone.utc)
    return session


async def optional_session(
    tsh_session: str | None = Cookie(default=None, alias=settings.cookie_name),
    authorization: str | None = Header(default=None),
) -> AuthSession | None:
    try:
        return await current_session(tsh_session, authorization)
    except HTTPException:
        return None


async def require_admin(
    session: AuthSession = Depends(current_session),
) -> AuthSession:
    """Guard for every article mutation: only admin mode may edit."""

    if not session.is_admin:
        log.warning("user %s tried to edit without admin rights", session.username)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin mode is required to edit articles",
        )
    return session
