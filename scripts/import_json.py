#!/usr/bin/env python3
# encoding: utf-8
"""Parse a legacy TSH JSON database and fill the PostgreSQL database.

Usage
-----
Inside the running stack (recommended, no local Python needed)::

    docker compose -f tsh_manifest/compose.yaml cp mybase.json backend:/tmp/mybase.json
    docker compose -f tsh_manifest/compose.yaml exec backend python -m scripts.import_json /tmp/mybase.json --dry-run
    docker compose -f tsh_manifest/compose.yaml exec backend python -m scripts.import_json /tmp/mybase.json

On a host checkout of the backend (``pip install -r requirements.txt`` plus a
reachable ``DATABASE_URL``)::

    cd tsh_manifest/backend
    PYTHONPATH=. python -m scripts.import_json ../data/tsh_base_cloud.json --check
    PYTHONPATH=. python -m scripts.import_json ../data/tsh_base_cloud.json --update

Input format
------------
One object per article id, exactly like ``data/tsh_base_cloud.json``::

    {
      "3DOI7BHN": {
        "name": "СТАТУС GSM",          "note": "…",           # russian
        "name_eng": "GSM STATUS",      "eng": "…",            # english
        "name_esp": null,              "esp": null,           # spanish
        "parent": "GH8AYPL5",
        "children": ["…", "…"],
        "index": 2,
        "author": "наиль",
        "created": "2022-04-26T13:27:13.529972",
        "name_edited": "…", "note_edited": "…",
        "name_eng_edited": "…", "eng_edited": "…",
        "name_esp_edited": "…", "esp_edited": "…"
      },
      …
    }

Conventions
-----------
* ``name == "!deleted"`` (or a record with no name and no content at all) is
  imported as a **soft deleted** article: the row and its id are kept, the
  article disappears from the tree. If such an article already exists live it
  gets ``deleted_at`` set, and if it comes back later it is restored.
* The ``children`` array is used for validation only (child ids missing from the
  file are reported); the tree itself is stored through ``parent_id``. Sibling
  order comes from the legacy ``index`` field (``articles.sort_index``) when it
  is an integer, and falls back to ``updated_at`` in the API otherwise.
* ``author`` is stored as ``name_author`` / ``content_author`` on the
  translation, but only where the database has no author yet so a real editor is
  never overwritten. The legacy ``author_edited`` stamp has no column in the new
  schema and is ignored.
* Missing dates are taken from ``created`` (or the current time as last resort),
  and a timestamp in the file never overwrites a *newer* value in the database
  unless ``--force`` is given, so a stale export cannot undo newer edits.
* Running the script twice changes nothing: only rows whose values actually
  differ are written (that is what ``--dry-run`` reports).

Exit codes: ``0`` success, ``1`` usage/IO error, ``2`` validation problems
(``--check`` or ``--strict``), ``3`` database error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

try:  # the script is meant to run from the backend checkout
    from sqlalchemy import func, select, text, update
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
except ModuleNotFoundError:  # pragma: no cover - friendly message
    print(
        "SQLAlchemy is not installed. Run this inside the backend container\n"
        "  docker compose exec backend python -m scripts.import_json <file>\n"
        "or install the backend requirements first:\n"
        "  pip install -r tsh_manifest/backend/requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(1)

try:
    from app.models import Article, ArticleTranslation
    from app.settings import settings
except ModuleNotFoundError:  # pragma: no cover
    print(
        "The 'app' package is not importable. Run from the backend directory:\n"
        "  cd tsh_manifest/backend && PYTHONPATH=. python -m scripts.import_json <file>",
        file=sys.stderr,
    )
    raise SystemExit(1)

LANGS: tuple[str, ...] = ("ru", "en", "es")
NAME_FIELDS = {"ru": "name", "en": "name_eng", "es": "name_esp"}
CONTENT_FIELDS = {"ru": "note", "en": "eng", "es": "esp"}
DELETED_NAME = "!deleted"
ROOT_PARENT = "TSH"
EPOCH = datetime.fromtimestamp(0, tz=timezone.utc)
SEARCH_TEXT_LIMIT = 400_000

#: fields compared for the "updated" report (legacy field -> translation lang)
FIELD_LABELS = {
    "ru": ("name", "note"),
    "en": ("name_eng", "eng"),
    "es": ("name_esp", "esp"),
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def clean(value: Any) -> str:
    """Legacy values are ``str | None``; a few records contain numbers."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def parse_dt(value: Any) -> datetime | None:
    """Parse a legacy ISO timestamp into an aware UTC datetime."""

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip().replace("Z", "+00:00")
    for parser in (
        datetime.fromisoformat,
        lambda v: datetime.strptime(v, "%Y-%m-%d %H:%M:%S"),
        lambda v: datetime.strptime(v, "%Y-%m-%d"),
    ):
        try:
            parsed = parser(candidate)
        except (ValueError, TypeError):
            continue
        # legacy files were written in local time (UTC+3) without a zone marker;
        # treat them as UTC so ordering stays stable and reproducible
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def normalize_id(value: Any) -> str | None:
    if value is None:
        return None
    token = str(value).strip().upper()
    return token or None


def is_deleted_record(record: dict[str, Any]) -> bool:
    """A record is soft deleted when it is marked ``!deleted`` or is an empty stub.

    Empty stubs (only ``*_edited`` stamps, no name and no content in any
    language) exist in the legacy files, for example ``PRIVATE0``; they must not
    become live articles in the tree.
    """

    names = [clean(record.get(field)).strip() for field in NAME_FIELDS.values()]
    if any(name == DELETED_NAME for name in names):
        return True
    contents = [clean(record.get(field)) for field in CONTENT_FIELDS.values()]
    return not any(names) and not any(content.strip() for content in contents)


def field_edited(record: dict[str, Any], lang: str, kind: str) -> datetime | None:
    """``name_edited`` / ``note_edited`` / ``name_eng_edited`` …"""

    record_field = NAME_FIELDS[lang] if kind == "name" else CONTENT_FIELDS[lang]
    return parse_dt(record.get(f"{record_field}_edited"))


def build_search_text(path: str, names: Sequence[str], contents: Sequence[str]) -> str:
    joined = " ".join(part for part in [path, *names, *contents] if part)
    return joined[:SEARCH_TEXT_LIMIT]


# --------------------------------------------------------------------------- #
# parsing / validation
# --------------------------------------------------------------------------- #
@dataclass
class Incoming:
    id: str
    record: dict[str, Any]
    names: dict[str, str]
    contents: dict[str, str]
    name_edited: dict[str, datetime | None]
    content_edited: dict[str, datetime | None]
    parent: str | None
    child_order: list[str]
    sort_index: int | None
    created: datetime | None
    deleted: bool
    author: str | None = None
    author_edited: datetime | None = None
    path: str = ""

    @property
    def updated_at(self) -> datetime:
        stamps = [value for value in (*self.name_edited.values(), *self.content_edited.values()) if value]
        stamps.append(self.created or EPOCH)
        return max(stamps)


@dataclass
class Report:
    records: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    restored: int = 0
    soft_deleted: int = 0
    skipped_empty: int = 0
    missing_parent: int = 0
    cycles: int = 0
    languages: Counter = field(default_factory=Counter)
    changes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unknown_children: list[str] = field(default_factory=list)

    def note_change(self, message: str, limit: int = 40) -> None:
        if len(self.changes) < limit:
            self.changes.append(message)


def load_json(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"{path} is not a file")
    with path.open(encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object keyed by article id")
    records: dict[str, dict[str, Any]] = {}
    for key, value in payload.items():
        if not isinstance(value, dict):
            raise ValueError(f"record {key!r} is {type(value).__name__}, expected an object")
        records[str(key)] = value
    return records


def parse_records(
    records: dict[str, dict[str, Any]],
    langs: Sequence[str],
    report: Report,
) -> dict[str, Incoming]:
    parsed: dict[str, Incoming] = {}
    for article_id, record in records.items():
        names = {lang: clean(record.get(NAME_FIELDS[lang])).strip() for lang in langs}
        contents = {lang: clean(record.get(CONTENT_FIELDS[lang])) for lang in langs}
        for lang in langs:
            if names[lang] == DELETED_NAME:
                names[lang] = ""
        deleted = is_deleted_record(record)
        children = record.get("children")
        child_order = [
            normalize_id(child) or ""
            for child in (children if isinstance(children, list) else [])
        ]
        index = record.get("index")
        sort_index = index if isinstance(index, int) else None
        parsed[article_id.upper()] = Incoming(
            id=article_id.upper(),
            record=record,
            names=names,
            contents=contents,
            name_edited={lang: field_edited(record, lang, "name") for lang in langs},
            content_edited={lang: field_edited(record, lang, "content") for lang in langs},
            parent=normalize_id(record.get("parent")),
            child_order=[child for child in child_order if child],
            sort_index=sort_index,
            created=parse_dt(record.get("created")),
            # is_deleted_record() inspects every language; re-deriving it from the
            # selected ``langs`` only would mark an English-only article deleted
            # when running with --langs ru.
            deleted=deleted,
            author=clean(record.get("author")).strip() or None,
            author_edited=parse_dt(record.get("author_edited")),
        )
    return parsed


def order_parents_first(parsed: dict[str, Incoming], root_id: str, report: Report) -> list[str]:
    """Insertion order: every parent before its children (cycle safe)."""

    order: list[str] = []
    seen: set[str] = set()
    in_progress: set[str] = set()

    def visit(article_id: str) -> None:
        if article_id in seen:
            return
        if article_id in in_progress:
            report.cycles += 1
            report.problems.append(f"cycle detected around {article_id}, parent link kept as is")
            seen.add(article_id)
            order.append(article_id)
            return
        in_progress.add(article_id)
        incoming = parsed[article_id]
        parent = incoming.parent
        if parent and parent not in (ROOT_PARENT, root_id) and parent in parsed:
            visit(parent)
        in_progress.discard(article_id)
        if article_id not in seen:
            seen.add(article_id)
            order.append(article_id)

    if root_id in parsed:
        seen.add(root_id)
        order.append(root_id)
    for article_id in parsed:
        visit(article_id)
    return order


def display_name(incoming: Incoming) -> str:
    """First non-empty name, mirroring ``app.service.name_map`` (ru, then en, es).

    ``refresh_projection`` builds the stored path with that fallback, so the
    importer must use the same rule or it would write paths the API immediately
    rewrites on the next edit.
    """

    for lang in ("ru", "en", "es"):
        value = incoming.names.get(lang)
        if value:
            return value
    return incoming.id


def compute_incoming_paths(
    parsed: dict[str, Incoming], order: Sequence[str], root_id: str
) -> None:
    """Fill ``Incoming.path`` (legacy parent tree) for the incoming records."""

    paths: dict[str, str] = {root_id: ""}
    for article_id in order:
        incoming = parsed[article_id]
        parent = incoming.parent
        if article_id == root_id or incoming.deleted:
            incoming.path = ""
            paths[article_id] = ""
            continue
        if parent in (None, ROOT_PARENT):
            incoming.path = display_name(incoming)
            paths[article_id] = incoming.path
            continue
        parent_path = paths.get(parent, "")
        own = display_name(incoming)
        incoming.path = f"{parent_path} / {own}".strip(" /") if parent_path else own
        paths[article_id] = incoming.path


# --------------------------------------------------------------------------- #
# database work
# --------------------------------------------------------------------------- #
async def load_existing(
    session: AsyncSession, ids: Sequence[str]
) -> dict[str, tuple[Article, dict[str, ArticleTranslation]]]:
    if not ids:
        return {}
    existing: dict[str, tuple[Article, dict[str, ArticleTranslation]]] = {}
    result = await session.execute(select(Article).where(Article.id.in_(list(ids))))
    for article in result.scalars():
        existing[article.id] = (article, {t.lang: t for t in article.translations})
    return existing


def pick_value(new_value: str, old_value: str | None, force: bool, stamp_new, stamp_old) -> str | None:
    """Return the value to store, or None when nothing should change."""

    if new_value == (old_value or ""):
        return None
    if force:
        return new_value
    if stamp_new and stamp_old and stamp_new < stamp_old and old_value:
        return None  # the database holds a newer edit, keep it
    return new_value


async def apply_translations(
    session: AsyncSession,
    incoming: Incoming,
    existing: dict[str, ArticleTranslation],
    langs: Sequence[str],
    force: bool,
    report: Report,
    dry_run: bool,
) -> list[str]:
    changed: list[str] = []
    for lang in langs:
        name = incoming.names[lang]
        content = incoming.contents[lang]
        current = existing.get(lang)
        if current is None and not name and not content:
            continue
        current_name = current.name if current else ""
        current_content = current.content if current else ""
        current_name_author = current.name_author if current else None
        current_content_author = current.content_author if current else None

        name_value = pick_value(
            name, current_name, force,
            incoming.name_edited[lang], current.name_edited_at if current else None,
        )
        content_value = pick_value(
            content, current_content, force,
            incoming.content_edited[lang], current.content_edited_at if current else None,
        )
        # The legacy ``author`` is the only source for the author columns (a
        # record without one stays NULL, exactly like the boot importer). It is
        # written only where the database has no author yet, so a real editor is
        # never overwritten and a second run is a no-op.
        name_author_value = (
            incoming.author if name and not (current_name_author or "") else None
        )
        content_author_value = (
            incoming.author if content and not (current_content_author or "") else None
        )
        if (
            name_value is None
            and content_value is None
            and name_author_value is None
            and content_author_value is None
        ):
            continue

        if name_value is None and content_value is None:
            changed.append(f"{lang}:author")
        else:
            changed.append(f"{lang}:{'name' if name_value is not None else ''}"
                           f"{'content' if content_value is not None else ''}")
        report.languages[lang] += 1
        if dry_run:
            continue

        # The INSERT branch must already carry the final values: without a
        # conflict ``on_conflict_do_update`` never runs, so inserting the *old*
        # values made every newly created article/translation come out empty.
        final_name_author = (
            name_author_value if name_author_value is not None else current_name_author
        )
        final_content_author = (
            content_author_value if content_author_value is not None else current_content_author
        )
        statement = pg_insert(ArticleTranslation).values(
            article_id=incoming.id,
            lang=lang,
            name=name_value if name_value is not None else current_name,
            content=content_value if content_value is not None else current_content,
            name_edited_at=incoming.name_edited[lang] or incoming.updated_at,
            content_edited_at=incoming.content_edited[lang] or incoming.updated_at,
            name_author=final_name_author,
            content_author=final_content_author,
        )
        update_values: dict[str, Any] = {}
        if name_value is not None:
            update_values["name"] = name_value
            update_values["name_edited_at"] = incoming.name_edited[lang] or incoming.updated_at
        if name_author_value is not None:
            update_values["name_author"] = name_author_value
        if content_value is not None:
            update_values["content"] = content_value
            update_values["content_edited_at"] = incoming.content_edited[lang] or incoming.updated_at
        if content_author_value is not None:
            update_values["content_author"] = content_author_value
        statement = statement.on_conflict_do_update(
            index_elements=[ArticleTranslation.article_id, ArticleTranslation.lang],
            set_=update_values,
        )
        await session.execute(statement)
    return changed


async def import_records(
    session: AsyncSession,
    records: dict[str, dict[str, Any]],
    *,
    langs: Sequence[str],
    root_id: str,
    dry_run: bool,
    force: bool,
    check_only: bool,
    update_only: bool,
    prune: bool,
    adopt_orphans: bool,
    fill_paths: bool,
    report: Report,
) -> Report:
    parsed = parse_records(records, langs, report)
    if root_id not in parsed:
        report.problems.append(
            f"the root article {root_id} is missing from the file; "
            "orphans will keep their current parent"
        )

    order = order_parents_first(parsed, root_id, report)
    compute_incoming_paths(parsed, order, root_id)

    # ``path``/``search_text`` are a projection of *all* translations plus the
    # ancestor names. A language subset cannot rebuild them: writing them from
    # the selected languages alone would drop the other languages from the search
    # index, so a subset import leaves the projection to --fill-paths.
    full_langs = set(langs) == set(LANGS)

    # A partial file cannot rebuild a path that starts above it. Without
    # --fill-paths such a record would be stored with only its own name and lose
    # the ancestor chain, so say so loudly instead of corrupting the projection.
    if not fill_paths:
        outside = [
            article_id
            for article_id, incoming in parsed.items()
            if incoming.parent
            and incoming.parent not in (ROOT_PARENT, root_id)
            and incoming.parent not in parsed
        ]
        if outside:
            report.warnings.append(
                f"{len(outside)} record(s) have a parent outside the file "
                f"(e.g. {outside[0]}): their path/search text would be rebuilt from the "
                "file alone and lose the ancestors. Add --fill-paths to recompute the "
                "whole tree (recommended whenever the file is not complete)."
            )

    existing = await load_existing(session, list(parsed))

    for article_id in order:
        incoming = parsed[article_id]
        row = existing.get(article_id)
        parent = incoming.parent
        if parent == ROOT_PARENT:
            parent = None
        if parent is not None and parent not in parsed:
            # is the parent at least in the database?
            known = await session.get(Article, parent)
            if known is None or known.deleted_at is not None:
                report.missing_parent += 1
                report.problems.append(
                    f"{article_id}: parent {parent} does not exist (neither in the file nor in the database)"
                )
                if incoming.deleted:
                    # a deleted record may keep a dangling parent pointer
                    parent = None
                elif adopt_orphans:
                    parent = root_id
                    report.note_change(
                        f"ADOPT {article_id}: parent {incoming.parent} unknown, attached to {root_id}"
                    )
                else:
                    report.note_change(
                        f"SKIP  {article_id}: parent {incoming.parent} unknown "
                        f"(use --adopt-orphans to attach it to {root_id})"
                    )
                    continue
        if article_id == root_id:
            parent = None
        elif parent is None and not incoming.deleted:
            # never leave a live article outside the tree: the root is its home
            parent = root_id

        if check_only:
            continue

        if row is None:
            if update_only:
                report.unchanged += 1
                report.problems.append(f"{article_id}: not in the database, skipped (--mode update)")
                continue
            # A soft deleted record still reserves its id: create the row exactly
            # like the first boot importer does, but keep it hidden (no parent, no
            # search projection). Skipping the insert entirely lost the id and made
            # a from-scratch import produce fewer rows than the legacy file.
            article = Article(
                id=article_id,
                parent_id=None if incoming.deleted else parent,
                sort_index=incoming.sort_index,
                path=incoming.path if full_langs else "",
                search_text="",
                created_at=incoming.created or EPOCH,
                inserted_at=datetime.now(timezone.utc),
                updated_at=incoming.updated_at,
                deleted_at=datetime.now(timezone.utc) if incoming.deleted else None,
            )
            if not dry_run:
                session.add(article)
                await session.flush()
                # translations of a !deleted record survive (hidden) like the boot
                # importer keeps them; only live articles get a search projection
                await apply_translations(
                    session, incoming, {}, langs, force, report, dry_run
                )
                if not incoming.deleted and full_langs:
                    await session.execute(
                        update(Article)
                        .where(Article.id == article_id)
                        .values(search_text=build_search_text(
                            incoming.path,
                            [incoming.names[lang] for lang in LANGS],
                            [incoming.contents[lang] for lang in LANGS],
                        ))
                    )
            if incoming.deleted:
                report.soft_deleted += 1
                report.note_change(f"KEEP  {article_id}: marked !deleted (row reserved)")
            else:
                report.created += 1
                report.note_change(f"NEW   {article_id}: {incoming.names.get('ru') or incoming.id}")
            continue

        article, translations = row
        if incoming.deleted:
            if article.deleted_at is None:
                report.soft_deleted += 1
                report.note_change(f"DEL   {article_id}: marked !deleted")
                if not dry_run:
                    article.deleted_at = datetime.now(timezone.utc)
                    article.parent_id = None
                    await session.execute(
                        update(Article)
                        .where(Article.id == article_id)
                        .values(search_text=build_search_text("", [], []))
                    )
            else:
                report.unchanged += 1
            continue

        if article.deleted_at is not None:
            report.restored += 1
            report.note_change(f"BACK  {article_id}: was deleted, restored")
            if not dry_run:
                article.deleted_at = None

        changed = await apply_translations(
            session, incoming, translations, langs, force, report, dry_run
        )
        if article.parent_id != parent and not (parent is None and article.parent_id in (None, ROOT_PARENT)):
            changed.append(f"parent {article.parent_id} -> {parent}")
        if incoming.sort_index is not None and article.sort_index != incoming.sort_index:
            changed.append(f"index {article.sort_index} -> {incoming.sort_index}")
        if full_langs and incoming.path != (article.path or ""):
            changed.append("path")

        if changed:
            report.updated += 1
            report.note_change(f"UPD   {article_id}: {', '.join(changed)}")
            if not dry_run:
                article.parent_id = parent
                if incoming.sort_index is not None:
                    article.sort_index = incoming.sort_index
                if article.created_at is None and incoming.created:
                    article.created_at = incoming.created
                article.updated_at = incoming.updated_at
                if full_langs:
                    # only a full language set may rebuild the projection
                    article.path = incoming.path or article.path
                    await session.execute(
                        update(Article)
                        .where(Article.id == article_id)
                        .values(
                            path=incoming.path or article.path,
                            search_text=build_search_text(
                                incoming.path or article.path,
                                [incoming.names[lang] for lang in LANGS],
                                [incoming.contents[lang] for lang in LANGS],
                            ),
                        )
                    )
        else:
            report.unchanged += 1

        # a child listed in the file but absent from it (and from the database)
        # is only reported: the importer never invents articles
        for child in incoming.child_order:
            if child in parsed or child in existing:
                continue
            known_child = await session.get(Article, child)
            if known_child is None:
                report.unknown_children.append(f"{article_id} -> {child}")

    if not full_langs and report.updated and not fill_paths:
        report.warnings.append(
            f"only {', '.join(langs)} selected: path/search text were left untouched "
            "(a language subset cannot produce the full projection). Run without "
            "--langs or add --fill-paths to refresh the search index."
        )

    if check_only:
        return report

    if fill_paths:
        from app.service import refresh_projection

        all_ids = [
            row_id
            for row_id in (
                await session.execute(
                    select(Article.id).where(Article.deleted_at.is_(None))
                )
            ).scalars()
        ]
        if not dry_run:
            await refresh_projection(session, all_ids)
            report.note_change(f"paths/search text recomputed for {len(all_ids)} articles")
            if len(report.changes) == 40:
                report.changes.append(f"... and {len(all_ids)} paths recomputed")

    if prune:
        incoming_ids = set(parsed)
        rows = await session.execute(
            select(Article.id).where(Article.deleted_at.is_(None))
        )
        removed = 0
        for (article_id,) in rows.all():
            if article_id in incoming_ids:
                continue
            removed += 1
            if dry_run:
                continue
            await session.execute(
                update(Article)
                .where(Article.id == article_id)
                .values(deleted_at=datetime.now(timezone.utc), parent_id=None)
            )
        report.soft_deleted += removed
        if removed:
            report.note_change(f"PRUNE {removed} article(s) not present in the file")

    return report


# --------------------------------------------------------------------------- #
# validation report
# --------------------------------------------------------------------------- #
def print_validation(
    records: dict[str, dict[str, Any]],
    parsed: dict[str, Incoming],
    report: Report,
    root_id: str,
    out=sys.stdout,
) -> int:
    problems = 0
    ids = set(parsed)
    for article_id, incoming in parsed.items():
        if incoming.parent and incoming.parent not in (ROOT_PARENT, root_id) and incoming.parent not in ids:
            problems += 1
            print(f"  error  {article_id}: parent {incoming.parent} is not in the file", file=out)
        for child in incoming.child_order:
            if child not in ids:
                problems += 1
                print(f"  warn   {article_id}: child {child} is not in the file", file=out)
    if root_id not in parsed:
        print(f"  error  root article {root_id} is missing", file=out)
        problems += 1
    for problem in report.problems:
        if "cycle" in problem:
            print(f"  error  {problem}", file=out)
            problems += 1
    for article_id, record in records.items():
        keys = set(record)
        # "null" appears in three legacy records as a literal junk key, and the
        # duplicate *_edited_edited stamps are ignored on purpose
        unknown = keys - {
            "name", "note", "parent", "children", "index", "created", "author", "null",
            "name_eng", "eng", "name_esp", "esp",
        } - {key for key in keys if key.endswith("_edited") or key.endswith("_edited_edited")}
        if unknown:
            print(f"  warn   {article_id}: unexpected field(s) {sorted(unknown)}", file=out)
    print(f"  {len(parsed)} record(s) parsed, {problems} structural problem(s)", file=out)
    return problems


def print_report(report: Report, dry_run: bool, out=sys.stdout) -> None:
    verb = "would be" if dry_run else ""
    print("", file=out)
    print("summary", file=out)
    print(f"  records parsed .......... {report.records}", file=out)
    print(f"  created ................. {report.created} {verb}".rstrip(), file=out)
    print(f"  updated ................. {report.updated} {verb}".rstrip(), file=out)
    print(f"  unchanged ............... {report.unchanged}", file=out)
    print(f"  restored (was deleted) .. {report.restored} {verb}".rstrip(), file=out)
    print(f"  soft deleted ............ {report.soft_deleted} {verb}".rstrip(), file=out)
    if report.skipped_empty:
        print(f"  empty records skipped ... {report.skipped_empty}", file=out)
    if report.languages:
        detail = ", ".join(f"{lang}={report.languages[lang]}" for lang in LANGS if report.languages[lang])
        print(f"  translations written .... {detail}", file=out)
    if report.missing_parent:
        print(f"  records with a missing parent ... {report.missing_parent}", file=out)
    if report.unknown_children:
        print(f"  child ids unknown to the file ... {len(report.unknown_children)}", file=out)
    if report.warnings:
        print("", file=out)
        print("warnings", file=out)
        for line in report.warnings:
            print(f"  {line}", file=out)
    if report.changes:
        print("", file=out)
        print("changes", file=out)
        for line in report.changes:
            print(f"  {line}", file=out)
        if len(report.changes) >= 40:
            print("  …", file=out)
    if report.problems:
        print("", file=out)
        print("problems", file=out)
        for line in report.problems[:40]:
            print(f"  {line}", file=out)
        if len(report.problems) > 40:
            print(f"  … and {len(report.problems) - 40} more", file=out)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="import_json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Parse a legacy TSH JSON database and fill the PostgreSQL database.",
        epilog=(
            "examples:\n"
            "  python -m scripts.import_json ../data/tsh_base_cloud.json --check\n"
            "  python -m scripts.import_json ../data/tsh_base_cloud.json --dry-run\n"
            "  python -m scripts.import_json ../data/tsh_base_cloud.json --update\n"
            "  python -m scripts.import_json base.json --update --fill-paths\n"
        ),
    )
    parser.add_argument("json_file", type=Path, help="legacy JSON database to read")
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", settings.database_url),
        help="SQLAlchemy async URL (default: $DATABASE_URL)",
    )
    parser.add_argument("--root", default=settings.root_id, help="root article id (default: 00000001)")
    parser.add_argument(
        "--langs",
        default=",".join(LANGS),
        help="comma separated languages to import (default: ru,en,es)",
    )
    parser.add_argument(
        "--mode",
        choices=("merge", "update"),
        default="merge",
        help="merge: insert new + update existing (default); update: update existing only",
    )
    parser.add_argument(
        "--update",
        dest="mode",
        action="store_const",
        const="update",
        help="alias for --mode update (never creates articles, only updates existing ones)",
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    parser.add_argument("--check", action="store_true", help="validate the file structure and exit")
    parser.add_argument("--force", action="store_true", help="overwrite even newer database values")
    parser.add_argument(
        "--adopt-orphans",
        action="store_true",
        help="attach records whose parent is unknown to the root instead of skipping them",
    )
    parser.add_argument("--prune", action="store_true", help="soft delete live articles missing from the file")
    parser.add_argument(
        "--fill-paths",
        action="store_true",
        help="recompute path/search text for every article afterwards (needed for partial files)",
    )
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit with code 2 when validation problems were found",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    langs = tuple(lang.strip().lower() for lang in args.langs.split(",") if lang.strip())
    unknown = [lang for lang in langs if lang not in LANGS]
    if unknown:
        print(f"unsupported language(s): {', '.join(unknown)} (supported: {', '.join(LANGS)})", file=sys.stderr)
        return 1
    if not langs:
        print("no language selected", file=sys.stderr)
        return 1

    print(f"reading {args.json_file}")
    records = load_json(args.json_file)
    print(f"  {len(records)} record(s) found, languages: {', '.join(langs)}")

    report = Report()
    parsed = parse_records(records, langs, report)
    report.records = len(parsed)
    order = order_parents_first(parsed, args.root.upper(), report)
    compute_incoming_paths(parsed, order, args.root.upper())

    if args.check:
        problems = print_validation(records, parsed, report, args.root.upper())
        return 2 if (problems and args.strict) else 0

    engine = create_async_engine(args.database_url, echo=False, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            try:
                await session.execute(text("SELECT 1 FROM articles LIMIT 1"))
            except Exception as error:  # schema missing / wrong credentials
                print(
                    f"cannot query the 'articles' table: {error}\n"
                    "Is the stack up and did the backend apply the schema?",
                    file=sys.stderr,
                )
                return 3

            await import_records(
                session,
                records,
                langs=langs,
                root_id=args.root.upper(),
                dry_run=args.dry_run,
                force=args.force,
                check_only=False,
                update_only=args.mode == "update",
                prune=args.prune,
                adopt_orphans=args.adopt_orphans,
                fill_paths=args.fill_paths,
                report=report,
            )
            if args.dry_run:
                await session.rollback()
            else:
                await session.commit()
    finally:
        await engine.dispose()

    if not args.quiet:
        print_report(report, args.dry_run)
    else:
        print(
            f"created={report.created} updated={report.updated} unchanged={report.unchanged} "
            f"restored={report.restored} deleted={report.soft_deleted}"
        )

    if args.dry_run:
        print("\ndry run: nothing was written")
    if report.problems and args.strict:
        return 2
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 1
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        print("interrupted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
