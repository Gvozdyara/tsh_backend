"""HTTP API.

URL map (the SPA and the host nginx only ever need ``/api/...``)::

    GET    /api/health                     liveness + db + index stats
    GET    /api/version                    build info
    GET    /api/auth/config                public auth feature flags
    POST   /api/auth/register              self registration -> pending account
    POST   /api/auth/login                 -> session cookie (approved, active only)
    POST   /api/auth/logout
    GET    /api/auth/session               current user + access level
    GET    /api/users                      list accounts       (admin)
    PATCH  /api/users/{username}           role/approval/active/password (admin)
    GET    /api/langs                      supported languages
    GET    /api/tree                       nested knowledge tree
    GET    /api/articles                   all articles (most edited first)
    GET    /api/articles/{id}              article + toc + links
    GET    /api/articles/{id}/children     inner layer listing
    POST   /api/articles                   create          (moderator)
    PATCH  /api/articles/{id}              update          (moderator)
    DELETE /api/articles/{id}              delete subtree  (admin)
    POST   /api/articles/{id}/move         move            (moderator)
    POST   /api/articles/{id}/reorder      order children  (moderator)
    GET    /api/search?q=&scope=&lang=     exact + NLP partial search
    GET    /api/search/suggest             title autocomplete
    GET    /api/admin/updates              audit feed
    POST   /api/admin/updates/read         mark updates as read
    POST   /api/admin/import               import legacy JSON (admin)
    GET    /api/admin/export               dump the whole base as JSON (admin)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import (
    APIRouter,
    Body,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from . import auth, importer, search as search_module, service
from .db import SessionFactory, get_session
from .models import Article, ArticleTranslation, User
from .schemas import (
    ArticleCreate,
    ArticleDetail,
    ArticleListOut,
    ArticleMeta,
    ArticleUpdate,
    AuthConfigOut,
    HealthOut,
    LoginRequest,
    MoveRequest,
    MutationResult,
    RegisterRequest,
    RegistrationOut,
    SearchOut,
    SessionOut,
    TreeItem,
    UpdatesOut,
    UserListOut,
    UserOut,
    UserUpdate,
)
from .settings import LANGS, settings

log = logging.getLogger("tsh.api")

router = APIRouter(prefix="/api")

VERSION = "1.0.0"
LANG_LABELS = {
    "ru": {"code": "ru", "label": "Рус", "nameMapping": "name", "field": "note"},
    "en": {"code": "en", "label": "Eng", "nameMapping": "name_en", "field": "eng"},
    "es": {"code": "es", "label": "Esp", "nameMapping": "name_esp", "field": "esp"},
}


def normalize_lang(lang: str | None) -> str:
    candidate = (lang or "ru").lower()
    return candidate if candidate in LANGS else "ru"


# --------------------------------------------------------------------- meta
@router.get("/health", response_model=HealthOut)
async def health() -> dict[str, Any]:
    detail: dict[str, Any] = {}
    database = "ok"
    articles = 0
    try:
        async with SessionFactory() as session:
            articles = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Article)
                    .where(Article.deleted_at.is_(None))
                )
                or 0
            )
            detail = await search_module.search_stats(session)
    except Exception as exc:  # pragma: no cover - reported to the operator
        log.exception("health check failed")
        database = f"error: {exc.__class__.__name__}"
    return {
        "status": "ok" if database == "ok" else "degraded",
        "database": database,
        "articles": articles,
        "languages": LANGS,
        "version": VERSION,
        "detail": detail,
    }


@router.get("/version")
async def version() -> dict[str, str]:
    return {
        "version": VERSION,
        "domain": settings.domain,
        "serverTime": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/langs")
async def langs() -> dict[str, Any]:
    return {"languages": list(LANGS), "labels": LANG_LABELS}


# --------------------------------------------------------------------- auth
@router.get("/auth/config", response_model=AuthConfigOut)
async def auth_config() -> dict[str, Any]:
    """Public feature flags needed to render the sign-in page."""

    return {
        "registration_enabled": settings.registration_enabled,
        "approval_required": True,
        "roles": ("admin", "moderator", "user"),
        "min_password_length": 8,
    }


@router.post(
    "/auth/register",
    response_model=RegistrationOut,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    payload: RegisterRequest,
    db: Any = Depends(get_session),
) -> dict[str, Any]:
    """Self-service registration. New accounts are unapproved and get no session."""

    if not settings.registration_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registration is disabled",
        )
    try:
        user = await auth.register(db, payload.username, payload.password, role="user")
    except auth.UserExists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This username is already taken",
        ) from None
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from None

    await service.audit(
        db,
        None,
        "user_register",
        user.username,
        {"role": user.role, "approved": False},
    )
    log.info("new user registered (pending approval): %s", user.username)
    return {
        "username": user.username,
        "role": user.role,
        "approved": False,
        "message": "Account created - an administrator must approve it",
    }


@router.post("/auth/login", response_model=SessionOut)
async def login(
    payload: LoginRequest,
    response: Response,
    db: Any = Depends(get_session),
) -> dict[str, Any]:
    try:
        session = await auth.login(db, payload.username.strip(), payload.password)
    except auth.AccountNotApproved:
        log.warning("unapproved login attempt for %r", payload.username)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is awaiting administrator approval",
        ) from None
    except auth.AccountDisabled:
        log.warning("disabled login attempt for %r", payload.username)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled",
        ) from None
    if session is None:
        log.warning("failed login for %r", payload.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )
    auth.set_session_cookie(response, session)
    return _session_payload(session)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response) -> Response:
    token = request.cookies.get(settings.cookie_name)
    auth.logout(token)
    auth.clear_session_cookie(response)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/auth/session", response_model=SessionOut)
async def session_info(
    session: auth.AuthSession = Depends(auth.current_session),
) -> dict[str, Any]:
    return _session_payload(session)


def _session_payload(session: auth.AuthSession) -> dict[str, Any]:
    return {
        "username": session.username,
        "role": session.role,
        "admin": session.is_admin,
        "moderator": session.is_moderator,
        "can_edit": session.is_moderator,
        "expires_at": session.expires_at,
        "server_time": datetime.now(timezone.utc),
        "languages": LANGS,
        "domain": settings.domain,
    }


# -------------------------------------------------------------------- users
def _user_payload(user: User) -> dict[str, Any]:
    return {
        "username": user.username,
        "role": user.role,
        "active": user.is_active,
        "approved": user.is_approved,
        "created_at": user.created_at,
        "last_login_at": user.last_login_at,
    }


@router.get("/users", response_model=UserListOut)
async def list_users(
    db: Any = Depends(get_session),
    _admin: auth.AuthSession = Depends(auth.require_admin),
) -> dict[str, Any]:
    rows = (
        await db.execute(select(User).order_by(func.lower(User.username)))
    ).scalars()
    users = list(rows)
    return {"total": len(users), "users": [_user_payload(user) for user in users]}


@router.patch("/users/{username}", response_model=UserOut)
async def update_user(
    username: str,
    payload: UserUpdate,
    db: Any = Depends(get_session),
    admin: auth.AuthSession = Depends(auth.require_admin),
) -> dict[str, Any]:
    """Change a role, (de)activate an account or reset its password."""

    user = await auth.find_user(db, username)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )

    new_role = payload.role or user.role
    new_active = user.is_active if payload.active is None else payload.active
    new_approved = (
        user.is_approved if payload.approved is None else payload.approved
    )

    # Never lock the application out of all administrative accounts.
    if user.is_admin and (new_role != "admin" or not new_active or not new_approved):
        other_admins = await db.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.role == "admin",
                User.is_active.is_(True),
                User.is_approved.is_(True),
                func.lower(User.username) != user.username.lower(),
            )
        )
        if not other_admins:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="At least one approved, active admin must remain",
            )

    user.role = new_role
    user.is_active = new_active
    user.is_approved = new_approved
    if payload.password is not None:
        user.password_hash = auth.hash_password(payload.password)
    await db.flush()

    if (
        payload.password is not None
        or not user.is_active
        or not user.is_approved
    ):
        # A password reset, a disabled or an unapproved account ends sessions.
        auth.invalidate_user_sessions(user.username, active=False)
    else:
        auth.invalidate_user_sessions(user.username, role=user.role)
    await service.audit(
        db,
        None,
        "user_update",
        admin.username,
        {
            "username": user.username,
            "role": user.role,
            "active": user.is_active,
            "approved": user.is_approved,
            "password_reset": payload.password is not None,
        },
    )
    log.info(
        "user %s updated by %s (role=%s active=%s approved=%s)",
        user.username,
        admin.username,
        user.role,
        user.is_active,
        user.is_approved,
    )
    return _user_payload(user)


# ------------------------------------------------------------------ reading
@router.get("/tree", response_model=TreeItem)
async def get_tree(
    lang: str | None = Query(default=None),
    root: str | None = Query(default=None),
    session: Any = Depends(get_session),
    _user: auth.AuthSession = Depends(auth.current_session),
) -> dict[str, Any]:
    return await service.tree(session, normalize_lang(lang), root)


@router.get("/articles", response_model=ArticleListOut)
async def list_articles(
    lang: str | None = Query(default=None),
    limit: int = Query(default=2000, ge=1, le=10000),
    session: Any = Depends(get_session),
    _user: auth.AuthSession = Depends(auth.current_session),
) -> dict[str, Any]:
    language = normalize_lang(lang)
    articles = await service.list_all(session, language, limit=limit)
    return {"lang": language, "total": len(articles), "articles": articles}


@router.get("/articles/{article_id}", response_model=ArticleDetail)
async def get_article(
    article_id: str,
    lang: str | None = Query(default=None),
    inline_single_link: bool = Query(default=True, alias="inlineSingleLink"),
    session: Any = Depends(get_session),
    _user: auth.AuthSession = Depends(auth.current_session),
) -> dict[str, Any]:
    try:
        return await service.article_detail(
            session,
            article_id.upper(),
            normalize_lang(lang),
            inline_single_link=inline_single_link,
        )
    except service.NotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get("/articles/{article_id}/children", response_model=list[ArticleMeta])
async def get_children(
    article_id: str,
    lang: str | None = Query(default=None),
    session: Any = Depends(get_session),
    _user: auth.AuthSession = Depends(auth.current_session),
) -> list[dict[str, Any]]:
    try:
        return await service.list_children(session, article_id.upper(), normalize_lang(lang))
    except service.NotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


# ----------------------------------------------------------------- mutation
@router.post(
    "/articles",
    response_model=MutationResult,
    status_code=status.HTTP_201_CREATED,
)
async def create_article(
    payload: ArticleCreate,
    lang: str | None = Query(default=None),
    session: Any = Depends(get_session),
    user: auth.AuthSession = Depends(auth.require_moderator),
) -> dict[str, Any]:
    language = payload.lang or normalize_lang(lang)
    try:
        article_id = await service.create_article(
            session,
            article_id=payload.resolved_id(),
            name=payload.name,
            parent_id=payload.parent_id,
            content=payload.content,
            lang=language,
            sort_index=payload.sort_index,
            actor=user.username,
        )
    except service.NotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except service.Conflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    detail = await service.article_detail(session, article_id, language)
    return {"ok": True, "message": f"Article {article_id} created", "article": detail}


@router.patch("/articles/{article_id}", response_model=MutationResult)
async def update_article(
    article_id: str,
    payload: ArticleUpdate,
    lang: str | None = Query(default=None),
    session: Any = Depends(get_session),
    user: auth.AuthSession = Depends(auth.require_moderator),
) -> dict[str, Any]:
    translations = {
        code: patch.model_dump(exclude_none=True)
        for code, patch in payload.translations.items()
    }
    if not translations and payload.parent_id is None and payload.sort_index is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Nothing to update"
        )
    try:
        detail = await service.update_article(
            session,
            article_id.upper(),
            translations,
            parent_id=payload.parent_id.upper() if payload.parent_id else None,
            sort_index=payload.sort_index,
            actor=user.username,
        )
    except service.NotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except service.Conflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    preferred = normalize_lang(lang)
    if detail["lang"] != preferred:
        detail = await service.article_detail(session, article_id.upper(), preferred)
    return {"ok": True, "message": "Article updated", "article": detail}


@router.post("/articles/{article_id}/move", response_model=MutationResult)
async def move_article(
    article_id: str,
    payload: MoveRequest,
    lang: str | None = Query(default=None),
    session: Any = Depends(get_session),
    user: auth.AuthSession = Depends(auth.require_moderator),
) -> dict[str, Any]:
    try:
        moved = await service.move_article(
            session,
            article_id.upper(),
            payload.new_parent_id.upper(),
            actor=user.username,
        )
    except service.NotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except service.Conflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    detail = await service.article_detail(session, article_id.upper(), normalize_lang(lang))
    return {
        "ok": True,
        "message": f"Moved {article_id.upper()}",
        "article": detail,
        "moved_ids": moved,
    }


@router.delete("/articles/{article_id}", response_model=MutationResult)
async def delete_article(
    article_id: str,
    session: Any = Depends(get_session),
    user: auth.AuthSession = Depends(auth.require_admin),
) -> dict[str, Any]:
    try:
        deleted = await service.delete_article(
            session, article_id.upper(), actor=user.username
        )
    except service.NotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except service.Conflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return {
        "ok": True,
        "message": f"Deleted {len(deleted)} article(s)",
        "moved_ids": deleted,
    }


@router.post("/articles/{article_id}/reorder", response_model=MutationResult)
async def reorder_children(
    article_id: str,
    ordered_ids: list[str] = Body(embed=True, alias="orderedIds"),
    session: Any = Depends(get_session),
    user: auth.AuthSession = Depends(auth.require_moderator),
) -> dict[str, Any]:
    await service.reorder_children(
        session,
        article_id.upper(),
        [item.upper() for item in ordered_ids],
        actor=user.username,
    )
    return {"ok": True, "message": "Order updated"}


# ------------------------------------------------------------------- search
@router.get("/search", response_model=SearchOut)
async def search(
    q: str = Query(default="", max_length=256),
    scope: Literal["name", "content"] = Query(default="content"),
    lang: str | None = Query(default=None),
    session: Any = Depends(get_session),
    _user: auth.AuthSession = Depends(auth.current_session),
) -> dict[str, Any]:
    return await search_module.search(
        session, q, lang=normalize_lang(lang), scope=scope
    )


@router.get("/search/suggest")
async def suggest(
    q: str = Query(default="", max_length=128),
    lang: str | None = Query(default=None),
    session: Any = Depends(get_session),
    _user: auth.AuthSession = Depends(auth.current_session),
) -> dict[str, Any]:
    items = await search_module.suggest(session, q, normalize_lang(lang))
    return {"items": items}


# -------------------------------------------------------------------- admin
@router.get("/admin/updates", response_model=UpdatesOut)
async def updates(
    lang: str | None = Query(default=None),
    session: Any = Depends(get_session),
    user: auth.AuthSession = Depends(auth.current_session),
) -> dict[str, Any]:
    return await service.updates_feed(session, user.username, normalize_lang(lang))


@router.post("/admin/updates/read")
async def mark_updates_read(
    session: Any = Depends(get_session),
    user: auth.AuthSession = Depends(auth.current_session),
) -> dict[str, Any]:
    seen_at = await service.mark_read(session, user.username)
    return {"ok": True, "lastSeenAt": seen_at}


@router.post("/admin/import")
async def import_backup(
    file: UploadFile = File(...),
    session: Any = Depends(get_session),
    user: auth.AuthSession = Depends(auth.require_admin),
) -> dict[str, Any]:
    raw = await file.read()
    if len(raw) > 64 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Backup is larger than 64 MiB",
        )
    import json

    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid JSON: {exc}"
        )
    try:
        result = await importer.import_from_upload(session, payload)
    except Exception as exc:
        log.exception("backup import failed")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        )
    log.info("backup imported by %s: %s", user.username, result)
    return {"ok": True, "message": "Backup imported", "result": result}


@router.get("/admin/export")
async def export_backup(
    session: Any = Depends(get_session),
    user: auth.AuthSession = Depends(auth.require_admin),
) -> JSONResponse:
    """Legacy-shaped JSON dump so old clients/backups stay compatible."""

    rows = await session.execute(select(Article))
    articles = list(rows.scalars())
    translations = await session.execute(select(ArticleTranslation))
    by_article: dict[str, dict[str, ArticleTranslation]] = {}
    for translation in translations.scalars():
        by_article.setdefault(translation.article_id, {})[translation.lang] = translation

    children: dict[str, list[str]] = {}
    for article in articles:
        if article.parent_id:
            children.setdefault(article.parent_id, []).append(article.id)

    payload: dict[str, Any] = {}
    for article in articles:
        if article.deleted_at is not None:
            continue
        record: dict[str, Any] = {
            "name": None,
            "note": None,
            "parent": article.parent_id or "TSH",
            "children": children.get(article.id, []),
            "index": article.sort_index,
            "created": article.created_at.isoformat() if article.created_at else None,
            "note_edited": None,
            "name_edited": None,
            "parent_edited": None,
            "children_edited": None,
        }
        mapping = {"ru": ("name", "note"), "en": ("name_eng", "eng"), "es": ("name_esp", "esp")}
        for lang, (name_field, content_field) in mapping.items():
            translation = by_article.get(article.id, {}).get(lang)
            if translation is None:
                record[name_field] = None
                record[content_field] = None
                continue
            record[name_field] = translation.name
            record[content_field] = translation.content
            if translation.name_edited_at:
                record[f"{name_field}_edited"] = translation.name_edited_at.isoformat()
            if translation.content_edited_at:
                record[f"{content_field}_edited"] = translation.content_edited_at.isoformat()
        payload[article.id] = record

    return JSONResponse(
        content=payload,
        headers={
            "Content-Disposition": 'attachment; filename="tsh_base_export.json"'
        },
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title="TSH knowledge base",
        version=VERSION,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    # Safety net: if an upstream proxy strips the /api prefix the bare routes
    # keep working (identical paths, hidden from the OpenAPI schema).
    app.include_router(router, include_in_schema=False)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "error": exc.__class__.__name__},
        )

    return app


__all__ = ["create_app", "router", "VERSION"]
