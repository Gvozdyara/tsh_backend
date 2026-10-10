"""Pydantic request/response models.

Fields are declared snake_case and serialised camelCase so the vanilla JS
frontend can consume them directly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .utils import new_id

Lang = Literal["ru", "en", "es"]


def to_camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.title() for part in tail)


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


# --------------------------------------------------------------------- auth
class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class SessionOut(ApiModel):
    username: str
    role: Literal["admin", "reader"]
    admin: bool
    expires_at: datetime
    server_time: datetime
    languages: tuple[str, ...] = ("ru", "en", "es")
    domain: str = ""


# ------------------------------------------------------------------ content
class TranslationOut(ApiModel):
    lang: str
    name: str = ""
    content: str = ""
    name_edited_at: datetime | None = None
    content_edited_at: datetime | None = None
    name_author: str | None = None
    content_author: str | None = None
    name_missing: bool = False
    content_missing: bool = False


class ArticleMeta(ApiModel):
    """Lightweight article row used by the tree/list views."""

    id: str
    parent_id: str | None = None
    name: str
    parent_name: str | None = None
    path: str = ""
    has_children: bool = False
    child_count: int = 0
    sort_index: int | None = None
    updated_at: datetime | None = None
    created_at: datetime | None = None


class BreadcrumbItem(ApiModel):
    id: str
    name: str


class TocItem(ApiModel):
    id: str
    name: str
    position: int
    has_children: bool = False
    updated_at: datetime | None = None


class LinkItem(ApiModel):
    id: str | None = None
    label: str
    title: str | None = None
    raw: str
    resolved: bool = False
    unresolved_reason: str | None = None
    inline: str | None = None


class ChildRef(ApiModel):
    id: str
    name: str


class ArticleDetail(ApiModel):
    id: str
    lang: str
    name: str
    content: str
    parent_id: str | None = None
    parent_name: str | None = None
    path: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    breadcrumbs: list[BreadcrumbItem] = Field(default_factory=list)
    children: list[TocItem] = Field(default_factory=list)
    links: list[LinkItem] = Field(default_factory=list)
    translations: list[TranslationOut] = Field(default_factory=list)
    available_languages: list[str] = Field(default_factory=list)
    is_root: bool = False


class ArticleListOut(ApiModel):
    lang: str
    total: int
    articles: list[ArticleMeta]


# ------------------------------------------------------------------- search
class SearchHit(ApiModel):
    id: str
    name: str
    title: str | None = None
    parent_id: str | None = None
    path: str = ""
    snippet: str = ""
    score: float = 0.0
    matched_terms: list[str] = Field(default_factory=list)
    matched_in: list[str] = Field(default_factory=list)


class SearchOut(ApiModel):
    query: str
    scope: Literal["name", "content"]
    lang: str
    exact: list[SearchHit] = Field(default_factory=list)
    partial: list[SearchHit] = Field(default_factory=list)
    took_ms: int = 0
    truncated: bool = False


# ----------------------------------------------------------------- mutation
class ArticleCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=1, max_length=300)
    parent_id: str = Field(default="00000001", alias="parentId")
    content: str = ""
    lang: Lang = "ru"
    id: str | None = None
    sort_index: int | None = Field(default=None, alias="sortIndex")

    def resolved_id(self) -> str:
        return (self.id or new_id()).upper()


class TranslationPatch(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str | None = None
    content: str | None = None
    delete: bool = False


class ArticleUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    translations: dict[str, TranslationPatch] = Field(default_factory=dict)
    parent_id: str | None = Field(default=None, alias="parentId")
    sort_index: int | None = Field(default=None, alias="sortIndex")


class MoveRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    new_parent_id: str = Field(min_length=1, alias="newParentId")


class MutationResult(ApiModel):
    ok: bool = True
    message: str = ""
    article: ArticleDetail | None = None
    moved_ids: list[str] = Field(default_factory=list)


class UpdateItem(ApiModel):
    id: str
    name: str
    action: str
    actor: str | None = None
    at: datetime | None = None


class UpdatesOut(ApiModel):
    total: int
    unseen: int
    last_seen_at: datetime | None = None
    items: list[UpdateItem] = Field(default_factory=list)


class TreeItem(ApiModel):
    id: str
    name: str
    parent_id: str | None = None
    child_count: int = 0
    children: list["TreeItem"] = Field(default_factory=list)


class HealthOut(ApiModel):
    status: str
    database: str
    articles: int
    languages: tuple[str, ...]
    version: str
    detail: dict[str, Any] = Field(default_factory=dict)


TreeItem.model_rebuild()
