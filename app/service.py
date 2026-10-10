"""Article tree operations: read models, mutations, moves, link resolution.

All tree semantics of the Kivy application are kept:

* one root article (``TSH`` / ``00000001``) holds the whole knowledge base
* every article has a parent and an ordered child list
* the article content is always rendered together with a table of contents of
  its direct children ("inner layer")
* ``sort_index`` pulls an article to the top of its parent listing
* articles can be moved between parents, but never into their own subtree
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .links import ParsedLink, inline_body, looks_like_id, parse_links
from .models import Article, ArticleTranslation, AuditLog, ReadMark, ROOT_PARENT, new_id
from .settings import LANGS, settings
from .utils import squeeze

log = logging.getLogger("tsh.service")

MAX_ANCESTOR_DEPTH = 64
SEARCH_TEXT_LIMIT = 400_000


class NotFound(Exception):
    """Raised when an article does not exist (or is soft deleted)."""


class Conflict(Exception):
    """Raised when a mutation is invalid (cycle, duplicated id, ...)."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------- paths
async def adjacency(
    session: AsyncSession,
) -> dict[str, tuple[str | None, int | None]]:
    """id -> (parent_id, sort_index) for every live article."""

    rows = await session.execute(
        select(Article.id, Article.parent_id, Article.sort_index).where(
            Article.deleted_at.is_(None)
        )
    )
    return {row.id: (row.parent_id, row.sort_index) for row in rows}


def children_index(
    adjacency_map: dict[str, tuple[str | None, int | None]], root_id: str
) -> dict[str, list[str]]:
    """parent_id -> [child_id]; the root and tree orphans are top level.

    A child is only attached to its parent when that parent really exists,
    otherwise it is treated as a top level node so it can never be mistaken for
    a descendant of the root.
    """

    children: dict[str, list[str]] = {}
    for node_id, (parent_id, _) in adjacency_map.items():
        if node_id == root_id:
            continue
        if parent_id and parent_id in adjacency_map:
            children.setdefault(parent_id, []).append(node_id)
        else:
            children.setdefault(root_id, []).append(node_id)
    return children


def compute_paths(
    adjacency_map: dict[str, tuple[str | None, int | None]],
    root_id: str,
    names: dict[str, str] | None = None,
) -> dict[str, str]:
    """Materialised path for every article, e.g. ``MAIN / GSM / STATYS GSM``.

    The root article keeps an empty path, exactly like the legacy UI which
    printed only the parent names above the article body.
    """

    names = names or {}
    children = children_index(adjacency_map, root_id)
    paths: dict[str, str] = {}
    queue: list[str] = [root_id]
    if root_id not in adjacency_map:
        queue = [node for node, (parent, _) in adjacency_map.items() if parent not in adjacency_map]

    seen: set[str] = set()
    while queue:
        node_id = queue.pop(0)
        if node_id in seen:
            continue
        seen.add(node_id)
        children_of = children.get(node_id, [])
        parent_path = paths.get(node_id, "")
        for child_id in children_of:
            own_name = names.get(child_id) or child_id
            paths[child_id] = f"{parent_path} / {own_name}".strip(" /") if parent_path else own_name
            queue.append(child_id)
        paths.setdefault(node_id, parent_path)

    # cycle leftovers still get a path so they remain reachable and searchable
    for node_id in adjacency_map:
        if node_id != root_id and node_id not in paths:
            paths[node_id] = names.get(node_id) or node_id
    paths[root_id] = ""
    return paths


def descendant_ids(
    adjacency_map: dict[str, tuple[str | None, int | None]], root: str
) -> set[str]:
    """Every id strictly below ``root`` by real parent edges (cycle safe)."""

    children: dict[str, list[str]] = {}
    for node_id, (parent_id, _) in adjacency_map.items():
        if parent_id and parent_id in adjacency_map:
            children.setdefault(parent_id, []).append(node_id)

    found: set[str] = set()
    stack = list(children.get(root, []))
    while stack:
        node_id = stack.pop()
        if node_id in found or node_id == root:
            continue
        found.add(node_id)
        stack.extend(children.get(node_id, []))
    return found


def topological_order(
    adjacency_map: dict[str, tuple[str | None, int | None]], root_id: str
) -> list[str]:
    """Breadth first order from the root, cycle safe."""

    children = children_index(adjacency_map, root_id)
    order: list[str] = []
    seen: set[str] = set()
    queue = [root_id] if root_id in adjacency_map else []
    queue.extend(
        node_id
        for node_id, (parent, _) in adjacency_map.items()
        if parent not in adjacency_map
    )
    while queue:
        node_id = queue.pop(0)
        if node_id in seen:
            continue
        seen.add(node_id)
        order.append(node_id)
        queue.extend(children.get(node_id, []))
    order.extend(node_id for node_id in adjacency_map if node_id not in seen)
    return order


# ------------------------------------------------------------- projections
def searchable_text(
    *,
    path: str,
    names: Sequence[str],
    contents: Sequence[str],
) -> str:
    parts: list[str] = [path, *names, *contents]
    text_value = " ".join(part for part in parts if part)
    return text_value[:SEARCH_TEXT_LIMIT]


async def refresh_projection(
    session: AsyncSession, article_ids: Iterable[str]
) -> None:
    """Recompute ``path`` and ``search_text`` for the given articles.

    ``search_text`` is what the tsvector trigger turns into the per language NLP
    index, so it must always contain the full translation set plus the parent
    tree ("search in the titles including parent tree").
    """

    ids = list({i for i in article_ids if i})
    if not ids:
        return

    adjacency_map = await adjacency(session)
    names = await name_map(session, "ru")
    paths = compute_paths(adjacency_map, settings.root_id, names)

    rows = await session.execute(
        select(ArticleTranslation).where(ArticleTranslation.article_id.in_(ids))
    )
    translations: dict[str, list[ArticleTranslation]] = {}
    for translation in rows.scalars():
        translations.setdefault(translation.article_id, []).append(translation)

    for article_id in ids:
        if article_id not in adjacency_map:
            continue
        items = translations.get(article_id, [])
        item_names = [item.name for item in items if item.name]
        contents = [item.content for item in items if item.content]
        await session.execute(
            update(Article)
            .where(Article.id == article_id)
            .values(
                path=paths.get(article_id, ""),
                search_text=searchable_text(
                    path=paths.get(article_id, ""),
                    names=item_names,
                    contents=contents,
                ),
            )
        )
    await session.flush()


async def refresh_subtree(session: AsyncSession, article_id: str) -> list[str]:
    """Refresh a whole subtree (a rename/move changes every descendant path)."""

    adjacency_map = await adjacency(session)
    subtree = [article_id, *sorted(descendant_ids(adjacency_map, article_id))]
    await refresh_projection(session, subtree)
    return subtree


async def refresh_ancestor_paths(session: AsyncSession, article_id: str) -> None:
    """After a rename/move, the paths of descendants change as well."""

    await refresh_subtree(session, article_id)


# ------------------------------------------------------------------- reading
async def get_article(session: AsyncSession, article_id: str) -> Article:
    article = await session.get(Article, article_id)
    if article is None or article.deleted_at is not None:
        raise NotFound(f"Article {article_id} does not exist")
    return article


async def name_map(session: AsyncSession, lang: str) -> dict[str, str]:
    """id -> display name for every article, with russian as fallback."""

    rows = await session.execute(select(ArticleTranslation))
    names: dict[str, dict[str, str]] = {}
    for translation in rows.scalars():
        names.setdefault(translation.article_id, {})[translation.lang] = (
            translation.name or ""
        )

    resolved: dict[str, str] = {}
    for article_id, by_lang in names.items():
        for candidate in (lang, "ru", "en", "es"):
            value = by_lang.get(candidate)
            if value:
                resolved[article_id] = value
                break
        else:
            resolved[article_id] = ""
    return resolved


async def children_map(
    session: AsyncSession,
) -> dict[str | None, list[tuple[str, int | None, datetime | None]]]:
    """parent_id -> [(child_id, sort_index, updated_at)] sorted for display."""

    rows = await session.execute(
        select(
            Article.id, Article.parent_id, Article.sort_index, Article.updated_at
        ).where(Article.deleted_at.is_(None))
    )
    grouped: dict[str | None, list[tuple[str, int | None, datetime | None]]] = {}
    for row in rows:
        grouped.setdefault(row.parent_id, []).append(
            (row.id, row.sort_index, row.updated_at)
        )
    for values in grouped.values():
        values.sort(
            key=lambda item: (
                1 if item[1] is None else 0,
                item[1] if item[1] is not None else 0,
                -(item[2].timestamp() if item[2] else 0),
            )
        )
    return grouped


async def breadcrumbs(
    session: AsyncSession, article_id: str, lang: str
) -> list[dict[str, str]]:
    """Root -> article chain (the ``parent / current`` directory label)."""

    names = await name_map(session, lang)
    rows = await session.execute(select(Article.id, Article.parent_id))
    parents = {row.id: row.parent_id for row in rows}

    chain: list[dict[str, str]] = []
    current: str | None = article_id
    depth = 0
    while current and depth < MAX_ANCESTOR_DEPTH:
        if current not in parents:
            break
        chain.append({"id": current, "name": names.get(current) or current})
        current = parents.get(current)
        depth += 1
    chain.reverse()
    return chain


def toc_from_children(
    children: list[tuple[str, int | None, datetime | None]],
    names: dict[str, str],
    child_counts: dict[str, int],
) -> list[dict[str, Any]]:
    toc: list[dict[str, Any]] = []
    for position, (child_id, _sort_index, updated_at) in enumerate(children, start=1):
        toc.append(
            {
                "id": child_id,
                "name": names.get(child_id) or child_id,
                "position": position,
                "has_children": child_counts.get(child_id, 0) > 0,
                "updated_at": updated_at,
            }
        )
    return toc


async def resolve_links(
    session: AsyncSession,
    parsed: list[ParsedLink],
    lang: str,
    names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Resolve ``ищи X`` references to article ids (legacy ``find_sections``).

    A reference may be an article id or an article name; names are matched
    against every translation of every article so a russian article can link to
    an english one.
    """

    if not parsed:
        return []

    if names is None:
        names = await name_map(session, lang)

    rows = await session.execute(
        select(
            Article.id,
            Article.path,
            ArticleTranslation.lang,
            ArticleTranslation.name,
        ).join(ArticleTranslation, ArticleTranslation.article_id == Article.id)
    )
    by_id: set[str] = set()
    by_name: dict[str, dict[str, str]] = {}
    by_path: dict[str, str] = {}
    for row in rows:
        by_id.add(row.id)
        by_path.setdefault(row.path or "", row.id)
        if row.name:
            by_name.setdefault(row.name.strip().casefold(), {})[row.lang] = row.id

    resolved: list[dict[str, Any]] = []
    for link in parsed:
        token = link.token.strip()
        target_id: str | None = None
        if looks_like_id(token) and token.upper() in by_id:
            target_id = token.upper()
        else:
            candidates = by_name.get(token.casefold())
            if candidates:
                target_id = (
                    candidates.get(lang)
                    or candidates.get("ru")
                    or next(iter(candidates.values()))
                )
            elif token in by_path:
                target_id = by_path[token]

        item: dict[str, Any] = {
            "id": target_id,
            "label": token,
            "raw": link.raw,
            "resolved": target_id is not None,
            "unresolved_reason": None,
            "title": None,
        }
        if target_id is None:
            item["unresolved_reason"] = "no article with this name or id"
        else:
            title = names.get(target_id, "")
            item["label"] = squeeze(title, 27) or token
            item["title"] = title
        resolved.append(item)
    return resolved


async def article_detail(
    session: AsyncSession, article_id: str, lang: str, inline_single_link: bool = True
) -> dict[str, Any]:
    article = await get_article(session, article_id)
    names = await name_map(session, lang)
    grouped = await children_map(session)
    child_counts = {key: len(value) for key, value in grouped.items() if key}

    translations = {t.lang: t for t in article.translations}
    selected = translations.get(lang)
    content = selected.content if selected and selected.content else ""
    if not content:
        for fallback in LANGS:
            other = translations.get(fallback)
            if other and other.content:
                content = other.content
                break

    parsed = parse_links(content)
    links = await resolve_links(session, parsed, lang, names)

    # legacy behaviour: a single link inlines the linked body under the comment
    if inline_single_link and len(links) == 1 and links[0]["resolved"]:
        target_id = links[0]["id"]
        target = await session.get(Article, target_id)
        target_name = names.get(target_id, "")
        target_content = ""
        if target is not None:
            target_translation = target.translation(lang) or target.translation("ru")
            target_content = target_translation.content if target_translation else ""
        content = _inline_single_link(content, links[0]["raw"], target_name, target_content)
        links[0]["inline"] = inline_body(target_name, target_content)

    children = grouped.get(article_id, [])
    chain = await breadcrumbs(session, article_id, lang)

    return {
        "id": article.id,
        "lang": lang,
        "name": names.get(article.id) or article.id,
        "content": content,
        "parent_id": article.parent_id,
        "parent_name": names.get(article.parent_id or "", None),
        "path": " / ".join(item["name"] for item in chain),
        "created_at": article.created_at,
        "updated_at": article.updated_at,
        "breadcrumbs": chain,
        "children": toc_from_children(children, names, child_counts),
        "links": links,
        "translations": [
            translation_out(article, candidate) for candidate in LANGS
        ],
        "available_languages": [
            candidate
            for candidate in LANGS
            if (translations.get(candidate) and translations[candidate].content)
        ],
        "is_root": article.id == settings.root_id,
    }


def _inline_single_link(
    content: str, raw_link: str, target_name: str, target_content: str
) -> str:
    replacement = f"// отображение ссылки {target_name}"
    replaced = content.replace(raw_link, replacement, 1)
    marker = "\nСодержание"
    head, sep, tail = replaced.partition(marker)
    body = inline_body(target_name, target_content)
    if sep:
        return f"{head}\n\n{body}\n{sep}{tail}"
    return f"{replaced}\n\n{body}"


def translation_out(article: Article, lang: str) -> dict[str, Any]:
    translation = article.translation(lang)
    if translation is None:
        return {
            "lang": lang,
            "name": "",
            "content": "",
            "name_edited_at": None,
            "content_edited_at": None,
            "name_author": None,
            "content_author": None,
            "name_missing": True,
            "content_missing": True,
        }
    return {
        "lang": lang,
        "name": translation.name or "",
        "content": translation.content or "",
        "name_edited_at": translation.name_edited_at,
        "content_edited_at": translation.content_edited_at,
        "name_author": translation.name_author,
        "content_author": translation.content_author,
        "name_missing": not bool(translation.name),
        "content_missing": not bool(translation.content),
    }


async def article_meta(
    session: AsyncSession, article_id: str, lang: str
) -> dict[str, Any]:
    names = await name_map(session, lang)
    article = await get_article(session, article_id)
    return {
        "id": article.id,
        "parent_id": article.parent_id,
        "name": names.get(article.id) or article.id,
        "parent_name": names.get(article.parent_id or ""),
        "path": article.path,
        "has_children": False,
        "child_count": 0,
        "sort_index": article.sort_index,
        "updated_at": article.updated_at,
        "created_at": article.created_at,
    }


async def list_children(
    session: AsyncSession, parent_id: str, lang: str
) -> list[dict[str, Any]]:
    """Direct children of an article, sorted (sort_index first, then edited)."""

    names = await name_map(session, lang)
    grouped = await children_map(session)
    child_counts = {key: len(value) for key, value in grouped.items() if key}
    children = grouped.get(parent_id, [])
    result: list[dict[str, Any]] = []
    for child_id, sort_index, updated_at in children:
        child = await session.get(Article, child_id)
        result.append(
            {
                "id": child_id,
                "parent_id": parent_id,
                "name": names.get(child_id) or child_id,
                "parent_name": names.get(parent_id),
                "path": child.path if child else "",
                "has_children": child_counts.get(child_id, 0) > 0,
                "child_count": child_counts.get(child_id, 0),
                "sort_index": sort_index,
                "updated_at": updated_at,
                "created_at": child.created_at if child else None,
            }
        )
    return result


async def list_all(session: AsyncSession, lang: str, limit: int = 2000) -> list[dict[str, Any]]:
    """All articles, most recently edited first (legacy "Show all")."""

    names = await name_map(session, lang)
    rows = await session.execute(
        select(
            Article.id,
            Article.parent_id,
            Article.path,
            Article.sort_index,
            Article.updated_at,
            Article.created_at,
        )
        .where(Article.deleted_at.is_(None))
        .order_by(Article.updated_at.desc())
        .limit(limit)
    )
    grouped = await children_map(session)
    child_counts = {key: len(value) for key, value in grouped.items() if key}
    result = []
    for row in rows:
        result.append(
            {
                "id": row.id,
                "parent_id": row.parent_id,
                "name": names.get(row.id) or row.id,
                "parent_name": names.get(row.parent_id or ""),
                "path": row.path,
                "has_children": child_counts.get(row.id, 0) > 0,
                "child_count": child_counts.get(row.id, 0),
                "sort_index": row.sort_index,
                "updated_at": row.updated_at,
                "created_at": row.created_at,
            }
        )
    return result


async def tree(session: AsyncSession, lang: str, root_id: str | None = None) -> dict[str, Any]:
    """Nested tree of the whole knowledge base (used by the tree navigator)."""

    names = await name_map(session, lang)
    grouped = await children_map(session)

    def build(node_id: str, depth: int = 0) -> dict[str, Any]:
        kids = grouped.get(node_id, [])
        return {
            "id": node_id,
            "name": names.get(node_id) or node_id,
            "parent_id": None if depth == 0 else None,
            "child_count": len(kids),
            "children": [build(child[0], depth + 1) for child in kids]
            if depth < MAX_ANCESTOR_DEPTH
            else [],
        }

    root = root_id or settings.root_id
    node = build(root)
    node["parent_id"] = None
    return node


# ------------------------------------------------------------------ mutation
async def _set_translation(
    session: AsyncSession,
    article_id: str,
    lang: str,
    *,
    name: str | None,
    content: str | None,
    actor: str,
) -> None:
    statement = pg_insert(ArticleTranslation).values(
        article_id=article_id,
        lang=lang,
        name=name or "",
        content=content or "",
        name_edited_at=utcnow() if name is not None else None,
        content_edited_at=utcnow() if content is not None else None,
        name_author=actor if name is not None else None,
        content_author=actor if content is not None else None,
    )
    update_values: dict[str, Any] = {}
    if name is not None:
        update_values.update(
            name=name,
            name_edited_at=utcnow(),
            name_author=actor,
        )
    if content is not None:
        update_values.update(
            content=content,
            content_edited_at=utcnow(),
            content_author=actor,
        )
    statement = statement.on_conflict_do_update(
        index_elements=[ArticleTranslation.article_id, ArticleTranslation.lang],
        set_=update_values,
    )
    await session.execute(statement)


async def create_article(
    session: AsyncSession,
    *,
    article_id: str,
    name: str,
    parent_id: str,
    content: str = "",
    lang: str = "ru",
    sort_index: int | None = None,
    actor: str = "system",
) -> str:
    article_id = article_id.strip().upper()
    if not article_id:
        raise Conflict("Article id must not be empty")
    if len(article_id) > 32:
        raise Conflict("Article id must be at most 32 characters")

    existing = await session.get(Article, article_id)
    if existing is not None and existing.deleted_at is None:
        raise Conflict(f"Article {article_id} already exists")

    if parent_id:
        parent = await session.get(Article, parent_id)
        if parent is None or parent.deleted_at is not None:
            raise NotFound(f"Parent article {parent_id} does not exist")

    article = Article(
        id=article_id,
        parent_id=parent_id or None,
        sort_index=sort_index,
        created_at=utcnow(),
        inserted_at=utcnow(),
        updated_at=utcnow(),
        path="",
        search_text="",
    )
    session.add(article)
    await session.flush()
    await _set_translation(
        session, article_id, lang, name=name.strip(), content=content, actor=actor
    )
    await refresh_projection(session, [parent_id or settings.root_id, article_id])
    await refresh_ancestor_paths(session, article_id)
    await audit(session, article_id, "create", actor, {"parent": parent_id, "lang": lang})
    return article_id


async def update_article(
    session: AsyncSession,
    article_id: str,
    translations: dict[str, dict[str, Any]],
    *,
    parent_id: str | None = None,
    sort_index: int | None = None,
    actor: str = "system",
) -> dict[str, Any]:
    article = await get_article(session, article_id)
    touched_langs: list[str] = []

    for lang, patch in translations.items():
        if lang not in LANGS:
            raise Conflict(f"Unsupported language {lang}")
        if patch.get("delete"):
            translation = article.translation(lang)
            if translation is not None:
                await session.delete(translation)
                touched_langs.append(lang)
            continue
        name = patch.get("name")
        content = patch.get("content")
        if name is None and content is None:
            continue
        if name is not None:
            name = name.strip()
            if not name and lang == "ru":
                raise Conflict("The russian article name must not be empty")
        await _set_translation(
            session,
            article_id,
            lang,
            name=name,
            content=content,
            actor=actor,
        )
        touched_langs.append(lang)

    changed = bool(touched_langs)
    if parent_id is not None and parent_id != article.parent_id:
        await _move(session, article_id, parent_id, actor=actor)
        changed = True
    if sort_index is not None and sort_index != article.sort_index:
        article.sort_index = sort_index
        changed = True

    if not changed:
        raise Conflict("Nothing to update")

    article.updated_at = utcnow()
    await session.flush()
    if touched_langs:
        await refresh_projection(session, [article_id])
        await refresh_ancestor_paths(session, article_id)
    elif parent_id is not None:
        await refresh_ancestor_paths(session, article_id)
    await audit(
        session,
        article_id,
        "update",
        actor,
        {"languages": touched_langs, "parent": parent_id, "sort_index": sort_index},
    )
    return await article_detail(session, article_id, touched_langs[0] if touched_langs else "ru", inline_single_link=False)


async def _move(
    session: AsyncSession, article_id: str, new_parent_id: str, actor: str
) -> list[str]:
    if article_id == new_parent_id:
        raise Conflict("An article cannot be its own parent")

    adjacency_map = await adjacency(session)
    if article_id not in adjacency_map:
        raise NotFound(f"Article {article_id} does not exist")
    if new_parent_id not in adjacency_map:
        raise NotFound(f"Target parent {new_parent_id} does not exist")

    forbidden = descendant_ids(adjacency_map, article_id)
    forbidden.add(article_id)
    if new_parent_id in forbidden:
        raise Conflict("An article cannot be moved into its own subtree")

    article = await session.get(Article, article_id)
    article.parent_id = new_parent_id
    article.updated_at = utcnow()
    await session.flush()
    await refresh_ancestor_paths(session, article_id)
    await audit(session, article_id, "move", actor, {"new_parent": new_parent_id})
    return [article_id, *sorted(forbidden - {article_id})]


async def move_article(
    session: AsyncSession, article_id: str, new_parent_id: str, actor: str = "system"
) -> list[str]:
    moved = await _move(session, article_id, new_parent_id, actor=actor)
    return moved


async def delete_article(
    session: AsyncSession, article_id: str, actor: str = "system"
) -> list[str]:
    """Delete an article and the whole nested subtree (legacy behaviour)."""

    if article_id == settings.root_id:
        raise Conflict("The main article cannot be deleted")

    adjacency_map = await adjacency(session)
    if article_id not in adjacency_map:
        raise NotFound(f"Article {article_id} does not exist")

    targets = [article_id, *sorted(descendant_ids(adjacency_map, article_id))]
    now = utcnow()
    await session.execute(
        update(Article).where(Article.id.in_(targets)).values(deleted_at=now)
    )
    # keep the data but release the ids from the tree
    await session.execute(
        update(Article).where(Article.id.in_(targets)).values(parent_id=None)
    )
    parent_id = adjacency_map[article_id][0]
    if parent_id:
        await refresh_projection(session, [parent_id])
        await refresh_ancestor_paths(session, parent_id)
    # the deleted rows are no longer part of the tree, so drop their stale
    # parent tree paths / search projections as well
    await refresh_projection(session, targets)
    await audit(
        session,
        article_id,
        "delete",
        actor,
        {"deleted": targets},
    )
    return targets


async def reorder_children(
    session: AsyncSession,
    parent_id: str,
    ordered_ids: list[str],
    actor: str = "system",
) -> None:
    """Set explicit sort_index values 0..n-1 for the given children."""

    for position, child_id in enumerate(ordered_ids):
        await session.execute(
            update(Article)
            .where(Article.id == child_id, Article.parent_id == parent_id)
            .values(sort_index=position)
        )
    await session.execute(
        update(Article).where(Article.id == parent_id).values(updated_at=utcnow())
    )
    await audit(session, parent_id, "reorder", actor, {"order": ordered_ids})
    await refresh_projection(session, [parent_id])


# -------------------------------------------------------------------- audit
async def audit(
    session: AsyncSession,
    article_id: str | None,
    action: str,
    actor: str,
    payload: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditLog(
            article_id=article_id,
            actor=actor,
            action=action,
            payload=payload or {},
        )
    )


async def updates_feed(
    session: AsyncSession, username: str, lang: str, limit: int = 100
) -> dict[str, Any]:
    """Recent changes + unread counter ("Last added" / "Mark updates as read")."""

    mark = await session.get(ReadMark, username)
    last_seen = mark.last_seen_at if mark else None

    rows = await session.execute(
        select(AuditLog)
        .where(AuditLog.article_id.is_not(None))
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    names = await name_map(session, lang)
    items: list[dict[str, Any]] = []
    unseen = 0
    for entry in rows.scalars():
        is_new = last_seen is None or (
            entry.created_at is not None and entry.created_at > last_seen
        )
        if is_new and entry.actor != username:
            unseen += 1
        items.append(
            {
                "id": entry.article_id,
                "name": names.get(entry.article_id or "") or entry.article_id or "",
                "action": entry.action,
                "actor": entry.actor,
                "at": entry.created_at,
            }
        )

    total = await session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.article_id.is_not(None))
    )
    return {
        "total": int(total or 0),
        "unseen": unseen,
        "last_seen_at": last_seen,
        "items": items,
    }


async def mark_read(session: AsyncSession, username: str) -> datetime:
    now = utcnow()
    statement = pg_insert(ReadMark).values(username=username, last_seen_at=now)
    statement = statement.on_conflict_do_update(
        index_elements=[ReadMark.username], set_={"last_seen_at": now}
    )
    await session.execute(statement)
    return now


async def statistics(session: AsyncSession) -> dict[str, Any]:
    total = await session.scalar(
        select(func.count()).select_from(Article).where(Article.deleted_at.is_(None))
    )
    per_lang: dict[str, int] = {}
    rows = await session.execute(
        select(ArticleTranslation.lang, func.count())
        .where(ArticleTranslation.content != "")
        .group_by(ArticleTranslation.lang)
    )
    for lang, count in rows.all():
        per_lang[lang] = int(count)
    return {
        "articles": int(total or 0),
        "with_content": per_lang,
    }
