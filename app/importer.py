"""One-off importer for the legacy Kivy database (``data/tsh_base_cloud.json``).

The legacy record looks like::

    "3DOI7BHN": {
        "name": "СТАТУС GSM", "note": "…",
        "name_eng": "GSM STATUS", "eng": "…",
        "name_esp": null, "esp": null,
        "parent": "GH8AYPL5", "children": ["…", "…"],
        "index": null, "created": "2022-04-26T13:27:13.529972",
        "name_edited": "…", "note_edited": "…", "parent_edited": "…", …
    }

Field mapping:

============================  ==========================================
legacy                        new
============================  ==========================================
name / note                   article_translations(lang='ru')
name_eng / eng                article_translations(lang='en')
name_esp / esp                article_translations(lang='es')
name_edited / note_edited     name_edited_at / content_edited_at
author                        name_author / content_author
parent                        articles.parent_id
children                      (validation only, the tree uses parent_id)
index                         articles.sort_index
created                       articles.created_at
============================  ==========================================

``author_edited`` has no column in the new schema and is ignored. The same
mapping is implemented by ``scripts/import_json.py``; that CLI is idempotent and
surgical (it updates an existing database without a volume wipe), while this
module is the one-off first-boot importer for an empty database.

Articles whose name is ``!deleted`` (or which have no name at all and no
children) are imported as soft deleted rows so that ids stay reserved.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Article, ArticleTranslation

article_table = Article.__table__
from .settings import LANGS, settings

log = logging.getLogger("tsh.importer")

NAME_FIELDS = {"ru": "name", "en": "name_eng", "es": "name_esp"}
CONTENT_FIELDS = {"ru": "note", "en": "eng", "es": "esp"}
DELETED_NAME = "!deleted"


def parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # legacy files were written with local time (UTC+3); treat as UTC to
        # keep ordering stable, the absolute value does not matter here.
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def clean(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value)
    return value


def load_legacy(path: str | Path) -> dict[str, dict]:
    with open(path, encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a legacy database object")
    return data


def legacy_order(data: dict[str, dict], root_id: str) -> list[str]:
    """Parents before children; unknown/cyclic parents fall back to the root."""

    order: list[str] = []
    seen: set[str] = set()
    pending = dict(data)

    def emit(node_id: str, guard: set[str]) -> None:
        if node_id in seen or node_id not in pending:
            return
        if node_id in guard:
            log.warning("cycle detected at %s, attaching to root", node_id)
            seen.add(node_id)
            order.append(node_id)
            return
        record = pending[node_id]
        parent = record.get("parent")
        if parent and parent != "TSH" and parent in pending and parent not in seen:
            emit(parent, guard | {node_id})
        if node_id in seen:
            return
        seen.add(node_id)
        order.append(node_id)

    if root_id in pending:
        seen.add(root_id)
        order.append(root_id)
    for node_id in pending:
        emit(node_id, set())
    for node_id in pending:
        if node_id not in seen:
            order.append(node_id)
    return order


def to_utc(value: Any, default: datetime) -> datetime:
    parsed = parse_dt(value)
    return parsed or default


async def import_legacy(
    session: AsyncSession,
    *,
    path: str | Path | None = None,
    root_id: str | None = None,
    force: bool = False,
) -> dict[str, int]:
    """Import the legacy JSON file into an empty database."""

    source = Path(path or settings.seed_file)
    root = root_id or settings.root_id

    existing = await session.scalar(
        select(func.count()).select_from(Article)
    )
    if existing and not force:
        log.info("database already holds %s articles, import skipped", existing)
        return {"imported": 0, "skipped": 1, "translations": 0}

    if not source.is_file():
        log.warning("seed file %s not found, creating an empty root article", source)
        return await create_empty_root(session, root)

    data = load_legacy(source)
    log.info("importing %s legacy records from %s", len(data), source)
    epoch = datetime.fromtimestamp(0, tz=timezone.utc)
    now = datetime.now(timezone.utc)

    order = legacy_order(data, root)
    if root not in data:
        data[root] = {
            "name": "main",
            "note": "Main folder",
            "parent": "TSH",
            "children": [],
        }
        order.insert(0, root)

    imported = 0
    deleted = 0
    translations = 0

    # pass 1: rows without parents (self referenced foreign key)
    for node_id in order:
        record = data[node_id]
        names = {lang: clean(record.get(field)).strip() for lang, field in NAME_FIELDS.items()}
        contents = {lang: clean(record.get(field)) for lang, field in CONTENT_FIELDS.items()}
        # no name and no content in any language: an empty stub, keep the id reserved
        is_deleted = any(name == DELETED_NAME for name in names.values()) or (
            not any(names.values()) and not any(content.strip() for content in contents.values())
        )
        author = clean(record.get("author")).strip() or None

        created = to_utc(record.get("created"), epoch)
        edited_dates = [
            parse_dt(record.get(f"{field}_edited"))
            for field in (NAME_FIELDS["ru"], CONTENT_FIELDS["ru"])
        ]
        updated = max([x for x in edited_dates if x] or [created, now])

        article = Article(
            id=node_id,
            parent_id=None,
            sort_index=record.get("index") if isinstance(record.get("index"), int) else None,
            path="",
            search_text="",
            created_at=created,
            inserted_at=now,
            updated_at=updated,
            deleted_at=now if is_deleted else None,
        )
        session.add(article)
        imported += 1
        if is_deleted:
            deleted += 1

        for lang in LANGS:
            name = names[lang]
            content = contents[lang]
            if not name and not content:
                continue
            if name == DELETED_NAME:
                name = ""
            session.add(
                ArticleTranslation(
                    article_id=node_id,
                    lang=lang,
                    name=name,
                    content=content,
                    name_edited_at=parse_dt(record.get(f"{NAME_FIELDS[lang]}_edited")) or created,
                    content_edited_at=parse_dt(record.get(f"{CONTENT_FIELDS[lang]}_edited")) or created,
                    name_author=author if name else None,
                    content_author=author if content else None,
                )
            )
            translations += 1

    await session.flush()

    # pass 2: parents and paths
    for node_id in order:
        record = data[node_id]
        parent = record.get("parent")
        if not parent or parent == "TSH" or parent not in data:
            parent = None if node_id == root else root
        await session.execute(
            article_table.update()
            .where(article_table.c.id == node_id)
            .values(parent_id=parent)
        )
    await session.flush()

    from .service import refresh_projection  # local import to avoid a cycle

    # Every record: refresh_projection ignores soft deleted rows (they are absent
    # from the live adjacency map) and must also see the top level articles whose
    # parent is None / "TSH", otherwise their path and search_text stay empty and
    # they never show up in full text search.
    await refresh_projection(session, list(order))

    await session.flush()
    log.info(
        "legacy import finished: %s articles (%s soft deleted), %s translations",
        imported,
        deleted,
        translations,
    )
    return {
        "imported": imported,
        "deleted": deleted,
        "translations": translations,
        "skipped": 0,
    }


async def create_empty_root(session: AsyncSession, root_id: str) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    session.add(
        Article(
            id=root_id,
            parent_id=None,
            path="",
            search_text="",
            created_at=now,
            inserted_at=now,
            updated_at=now,
        )
    )
    await session.flush()
    session.add(
        ArticleTranslation(
            article_id=root_id,
            lang="ru",
            name="MAIN",
            content="Main folder",
            name_edited_at=now,
            content_edited_at=now,
        )
    )
    await session.flush()
    return {"imported": 1, "deleted": 0, "translations": 1, "skipped": 0}


async def import_from_upload(
    session: AsyncSession, payload: dict[str, Any], root_id: str | None = None
) -> dict[str, int]:
    """Import a legacy JSON payload uploaded through the admin menu."""

    from .service import refresh_projection

    root = root_id or settings.root_id
    if not isinstance(payload, dict):
        raise ValueError("Backup must be a JSON object")

    epoch = datetime.fromtimestamp(0, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    order = legacy_order(payload, root)
    count = 0

    for node_id in order:
        record = payload[node_id]
        if not isinstance(record, dict):
            continue
        names = {lang: clean(record.get(field)).strip() for lang, field in NAME_FIELDS.items()}
        if all(not name or name == DELETED_NAME for name in names.values()):
            continue
        contents = {lang: clean(record.get(field)) for lang, field in CONTENT_FIELDS.items()}
        created = to_utc(record.get("created"), epoch)

        article = await session.get(Article, node_id)
        if article is None:
            article = Article(
                id=node_id,
                parent_id=None,
                created_at=created,
                inserted_at=now,
                updated_at=now,
                path="",
                search_text="",
            )
            session.add(article)
            await session.flush()
        article.deleted_at = None
        count += 1

        for lang in LANGS:
            name = names[lang]
            content = contents[lang]
            if name == DELETED_NAME:
                name = ""
            if not name and not content:
                continue
            existing_translation = await session.get(
                ArticleTranslation, {"article_id": node_id, "lang": lang}
            )
            if existing_translation is None:
                existing_translation = ArticleTranslation(
                    article_id=node_id, lang=lang, name=name, content=content
                )
                session.add(existing_translation)
            else:
                existing_translation.name = name or existing_translation.name
                existing_translation.content = content or existing_translation.content
            existing_translation.name_edited_at = (
                parse_dt(record.get(f"{NAME_FIELDS[lang]}_edited")) or created
            )
            existing_translation.content_edited_at = (
                parse_dt(record.get(f"{CONTENT_FIELDS[lang]}_edited")) or created
            )

    await session.flush()
    for node_id in order:
        record = payload[node_id]
        if not isinstance(record, dict):
            continue
        parent = record.get("parent")
        if not parent or parent == "TSH" or parent not in payload:
            parent = None if node_id == root else root
        await session.execute(
            article_table.update()
            .where(article_table.c.id == node_id)
            .values(parent_id=parent)
        )
    await session.flush()
    await refresh_projection(session, list(order))
    return {"imported": count, "skipped": 0, "translations": 0, "deleted": 0}


def read_endings(path: str | Path | None = None) -> Iterable[str]:
    """Legacy ``data/endings.txt`` is only kept for parity/diagnostics."""

    source = Path(path or settings.endings_file)
    if not source.is_file():
        return []
    text_value = source.read_text(encoding="utf-8-sig")
    return [chunk.strip() for chunk in text_value.split(",") if chunk.strip()]


__all__ = [
    "import_legacy",
    "import_from_upload",
    "create_empty_root",
    "load_legacy",
    "read_endings",
]
