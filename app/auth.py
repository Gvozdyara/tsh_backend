"""Authentication, registration and access levels.

Accounts live in the ``users`` table of the application database and passwords
are stored as bcrypt hashes. Three roles form a hierarchy:

* ``user``      - read only access to the whole knowledge base
* ``moderator`` - additionally create, edit, move and reorder articles
* ``admin``     - everything, including delete, import/export and user roles

Sessions are opaque random tokens kept in memory and handed to the browser as
an HttpOnly cookie.  A restart of the container simply forces a new login,
which is acceptable for a single backend deployment.

``TSH_USERS`` is only a bootstrap: on start, accounts that do not exist yet are
inserted with a hashed password.  From then on the database is authoritative and
admins manage roles from the UI (or via ``PATCH /api/users/{username}``).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Cookie, Depends, Header, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import User
from .settings import USER_ROLES, normalize_role, settings

log = logging.getLogger("tsh.auth")

#: token -> AuthSession
SESSIONS: dict[str, "AuthSession"] = {}

#: bcrypt work factor; 12 is the current sane default.
BCRYPT_ROUNDS = 12

#: Dummy hash so a login for an unknown user takes as long as a real one.
_DUMMY_HASH: bytes | None = None


class UserExists(Exception):
    """A registration or rename collided with an existing account."""


class AccountNotApproved(Exception):
    """Correct credentials, but an admin has not approved the account yet."""


class AccountDisabled(Exception):
    """Correct credentials, but the account was disabled by an admin."""


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

    @property
    def is_moderator(self) -> bool:
        return self.role in ("admin", "moderator")


# ---------------------------------------------------------------- passwords
def hash_password(raw: str) -> str:
    """Return a salted bcrypt hash (ASCII, safe for the TEXT column)."""

    digest = bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS))
    return digest.decode("ascii")


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError, UnicodeError):  # pragma: no cover - defensive
        return False


def _dummy_hash() -> bytes:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = bcrypt.hashpw(
            secrets.token_bytes(32), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
        )
    return _DUMMY_HASH


# ----------------------------------------------------------------- account
def normalize_username(username: str) -> str:
    return username.strip()


async def find_user(db: AsyncSession, username: str) -> User | None:
    """Case-insensitive lookup (``Admin`` and ``admin`` are one account)."""

    candidate = normalize_username(username)
    if not candidate:
        return None
    result = await db.execute(
        select(User).where(func.lower(User.username) == candidate.lower())
    )
    return result.scalars().first()


async def login(db: AsyncSession, username: str, password: str) -> AuthSession | None:
    """Check the database and return a session, or ``None``."""

    user = await find_user(db, username)

    # Always run bcrypt, even for an unknown account, to avoid user enumeration
    # through response timing.
    if user is not None:
        valid = verify_password(password, user.password_hash)
    else:
        verify_password(password, _dummy_hash().decode("ascii"))
        valid = False

    if user is not None and valid:
        if not user.is_active:
            raise AccountDisabled(user.username)
        if not user.is_approved:
            raise AccountNotApproved(user.username)
        user.last_login_at = datetime.now(timezone.utc)
        return create_session(user.username, normalize_role(user.role))

    # Optional shared read-only password ("guest mode"). A real account that is
    # disabled or unapproved cannot bypass the gate through the shared password
    # either - the approval status wins over guest mode.
    if settings.readonly_password and secrets.compare_digest(
        password, settings.readonly_password
    ):
        if user is not None and not user.is_active:
            raise AccountDisabled(user.username)
        if user is not None and not user.is_approved:
            raise AccountNotApproved(user.username)
        return create_session(username.strip() or "guest", "user")

    return None


async def register(
    db: AsyncSession, username: str, password: str, role: str = "user"
) -> User:
    """Create an account. The caller decides whether self-registration is on."""

    candidate = normalize_username(username)
    if not candidate:
        raise ValueError("Username must not be empty")
    if normalize_role(role, default="") not in USER_ROLES:
        raise ValueError("Unknown role")

    if await find_user(db, candidate) is not None:
        raise UserExists(candidate)

    user = User(
        username=candidate,
        password_hash=hash_password(password),
        role=normalize_role(role),
        is_active=True,
        is_approved=False,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        # Concurrent registration with the same (case-insensitive) name.
        await db.rollback()
        raise UserExists(candidate) from None
    return user


async def bootstrap_users(db: AsyncSession) -> list[str]:
    """Insert missing ``TSH_USERS`` accounts; never overwrite an existing one."""

    existing_rows = (await db.execute(select(User.username))).scalars().all()
    existing = {name.lower() for name in existing_rows}
    created: list[str] = []
    for seed in settings.users:
        key = seed.username.lower()
        if key in existing:
            continue
        db.add(
            User(
                username=seed.username,
                password_hash=hash_password(seed.password),
                role=normalize_role(seed.role),
                is_active=True,
                is_approved=True,
            )
        )
        existing.add(key)
        created.append(seed.username)

    if created:
        try:
            await db.flush()
        except IntegrityError:  # pragma: no cover - racing backend instances
            await db.rollback()
            log.warning("bootstrap users collided with concurrent inserts")
            return []
        log.info("bootstrap users created: %s", ", ".join(created))

    active_admins = await db.scalar(
        select(func.count())
        .select_from(User)
        .where(
            User.role == "admin",
            User.is_active.is_(True),
            User.is_approved.is_(True),
        )
    )
    if not active_admins:
        log.warning(
            "no active admin account exists - set TSH_USERS with an admin role"
        )
    return created


def invalidate_user_sessions(
    username: str,
    role: str | None = None,
    active: bool = True,
    approved: bool = True,
) -> None:
    """Apply an admin's role/active/approval change to live sessions at once."""

    target = username.lower()
    for token, session in list(SESSIONS.items()):
        if session.username.lower() != target:
            continue
        if not active or not approved:
            SESSIONS.pop(token, None)
        elif role is not None:
            session.role = role


# ----------------------------------------------------------------- sessions
def prune_sessions(now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    expired = [token for token, s in SESSIONS.items() if s.expires_at <= now]
    for token in expired:
        SESSIONS.pop(token, None)


def create_session(username: str, role: str) -> AuthSession:
    now = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    session = AuthSession(
        token=token,
        username=username,
        role=normalize_role(role),
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


async def require_moderator(
    session: AuthSession = Depends(current_session),
) -> AuthSession:
    """Guard for authoring: moderators and admins may create/edit/move."""

    if not session.is_moderator:
        log.warning("user %s tried to edit without moderator rights", session.username)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Moderator rights are required to edit articles",
        )
    return session


async def require_admin(
    session: AuthSession = Depends(current_session),
) -> AuthSession:
    """Guard for destructive or administrative operations."""

    if not session.is_admin:
        log.warning("user %s tried an admin action", session.username)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin rights are required for this operation",
        )
    return session


__all__ = [
    "AccountDisabled",
    "AccountNotApproved",
    "AuthSession",
    "SESSIONS",
    "UserExists",
    "bootstrap_users",
    "clear_session_cookie",
    "create_session",
    "current_session",
    "find_user",
    "hash_password",
    "invalidate_user_sessions",
    "login",
    "normalize_username",
    "optional_session",
    "prune_sessions",
    "register",
    "require_admin",
    "require_moderator",
    "set_session_cookie",
    "verify_password",
]
