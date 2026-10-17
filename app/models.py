"""SQLAlchemy 2.0 models mirroring the legacy Kivy JSON database.

Legacy shape (data/tsh_base_cloud.json), one dict per article id::

    {name, note, name_eng, eng, name_esp, esp, parent, children,
     index, created, <field>_edited...}

New shape: one row per article plus a translation row per language.  The
generated tsvector columns are declared in the initdb schema, the ORM maps them
as read-only attributes used by the search queries.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


ROOT_PARENT = "TSH"


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("articles.id", ondelete="RESTRICT"), nullable=True
    )
    sort_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: materialised tree path, e.g. "MAIN / GSM / СТАТУС GSM"
    path: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    #: searchable projection of every translation + path; the DB trigger turns
    #: it into the per language tsvectors (tsv_ru / tsv_en / tsv_es).
    search_text: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    inserted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: tsvector columns are written by the database trigger only; they are never
    #: set from Python, so they stay out of every INSERT/UPDATE statement.
    tsv_ru: Mapped[str | None] = mapped_column(
        Text, server_default=text("''::tsvector")
    )
    tsv_en: Mapped[str | None] = mapped_column(
        Text, server_default=text("''::tsvector")
    )
    tsv_es: Mapped[str | None] = mapped_column(
        Text, server_default=text("''::tsvector")
    )

    translations: Mapped[list["ArticleTranslation"]] = relationship(
        back_populates="article",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ArticleTranslation.lang",
    )
    #: parent/children is a single adjacency relationship; children rows are
    #: fetched by query (see app.repository) to keep the mapper simple.
    parent: Mapped["Article | None"] = relationship(
        remote_side="Article.id", viewonly=True
    )

    __table_args__ = (
        CheckConstraint("id <> parent_id", name="articles_no_self_parent"),
        Index("ix_articles_parent", "parent_id"),
        Index("ix_articles_parent_sort", "parent_id", "sort_index", "updated_at"),
    )

    # ------------------------------------------------------------------ utils
    def translation(self, lang: str) -> "ArticleTranslation | None":
        for translation in self.translations:
            if translation.lang == lang:
                return translation
        return None

    def display_name(self, lang: str) -> str:
        translation = self.translation(lang)
        if translation and translation.name:
            return translation.name
        for fallback in ("ru", "en", "es"):
            other = self.translation(fallback)
            if other and other.name:
                return other.name
        return "(no name)"

    def content(self, lang: str) -> str:
        translation = self.translation(lang)
        if translation and translation.content:
            return translation.content
        return ""

    @property
    def is_root(self) -> bool:
        return self.parent_id in (None, ROOT_PARENT)


class ArticleTranslation(Base):
    __tablename__ = "article_translations"

    article_id: Mapped[str] = mapped_column(
        Text, ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True
    )
    lang: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    name_edited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    content_edited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    name_author: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_author: Mapped[str | None] = mapped_column(Text, nullable=True)

    article: Mapped[Article] = relationship(
        back_populates="translations", lazy="joined"
    )

    __table_args__ = (
        CheckConstraint("lang IN ('ru', 'en', 'es')", name="translations_lang_check"),
        Index("ix_translations_lang", "lang"),
    )


class AuditLog(Base):
    """Small audit trail so 'last added' and 'mark updates as read' keep working."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    article_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_audit_created", "created_at"),)


class User(Base):
    """Application account.

    Passwords are stored as bcrypt hashes (see ``app.auth.hash_password``).
    Roles form a hierarchy: ``user`` (read only) < ``moderator`` (may author
    articles) < ``admin`` (everything, including user management).
    """

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(Text, primary_key=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="user")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    #: Self-registered accounts must be approved by an admin before login.
    is_approved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('admin', 'moderator', 'user')", name="users_role_check"
        ),
        CheckConstraint("btrim(username) <> ''", name="users_username_not_blank"),
        CheckConstraint(
            "password_hash <> ''", name="users_password_hash_not_blank"
        ),
        Index("ux_users_username_lower", func.lower(username), unique=True),
        Index("ix_users_role", "role"),
    )

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_moderator(self) -> bool:
        return self.role in ("admin", "moderator")


class ReadMark(Base):
    """Per-user read cursor for the legacy 'updates' counter."""

    __tablename__ = "read_marks"

    username: Mapped[str] = mapped_column(Text, primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("username", name="read_marks_username_key"),)


def new_id() -> str:
    """Uppercase 8 char id, same style as the legacy ``gen_rnd_str``."""

    return uuid.uuid4().hex[:8].upper()


__all__ = [
    "Article",
    "ArticleTranslation",
    "AuditLog",
    "Base",
    "ReadMark",
    "ROOT_PARENT",
    "User",
    "new_id",
    "text",
]
