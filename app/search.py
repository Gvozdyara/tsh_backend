"""Search: exact substring matching + per language NLP index search.

Replaces the legacy natasha/pymorphy2 index (``Index``/``Finder`` classes) with
PostgreSQL full text search, keeping the same two-stage behaviour:

* *exact* match   - the raw request occurs as a substring of a name or of the
                    content (legacy ``search_for_exact_match``)
* *partial* match - the request is turned into a language specific tsquery
                    (stemming/lemmatisation) and documents are ranked by
                    relevance (legacy ``search_in_index``) - this is the "NLP
                    index" that auto-search while typing uses.

Two scopes mirror the two buttons of the Kivy UI:

* ``name``    - article titles only. The parent tree is part of the index
                (``articles.tsv_<lang>`` also holds ``path``) and the title
                fallback chain is applied, exactly like the legacy index which
                searched the title plus every ancestor title.
* ``content`` - article bodies (with the same fallback chain).
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Literal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Article, ArticleTranslation
from .settings import LANGS, TS_CONFIG

log = logging.getLogger("tsh.search")

Scope = Literal["name", "content"]
MAX_EXACT = 40
MAX_PARTIAL = 60
MIN_TOKEN_LENGTH = 2
MAX_TOKEN_LENGTH = 64
MAX_TOKENS = 12
EMPTY_TSVECTOR = "''::tsvector"

_TOKEN_RE = re.compile(r"[\w\d]+", re.UNICODE)

JOIN_TEMPLATE = (
    "LEFT JOIN article_translations t_{lang} "
    "ON t_{lang}.article_id = a.id AND t_{lang}.lang = '{lang}'"
)
JOIN_SQL = " ".join(JOIN_TEMPLATE.format(lang=lang) for lang in LANGS)


def normalize_query(query: str) -> str:
    return re.sub(r"\s+", " ", (query or "").strip())


def query_tokens(query: str) -> list[str]:
    """Safe tokens for the tsquery (PostgreSQL drops stop words by itself)."""

    tokens: list[str] = []
    seen: set[str] = set()
    for raw in _TOKEN_RE.findall(query):
        if not (MIN_TOKEN_LENGTH < len(raw) <= MAX_TOKEN_LENGTH):
            continue
        lowered = raw.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        tokens.append(raw)
        if len(tokens) >= MAX_TOKENS:
            break
    return tokens


def _langs_for(lang: str) -> list[str]:
    return [lang, *[candidate for candidate in LANGS if candidate != lang]]


def _name_expression(lang: str) -> str:
    """SQL: coalesce of the requested language title and its fallbacks."""

    parts = ", ".join(f"t_{candidate}.name" for candidate in _langs_for(lang))
    return f"coalesce({parts}, a.id)"


def _value_expression(scope: Scope, lang: str) -> str:
    """SQL: the text an exact (substring) match is performed against."""

    columns: list[str] = []
    if scope == "content":
        columns.extend(
            f"t_{candidate}.content" for candidate in _langs_for(lang)
        )
    columns.extend(f"t_{candidate}.name" for candidate in _langs_for(lang))
    return f"coalesce({', '.join(columns)}, '')"


def _tsvector_expression(lang: str, scope: Scope) -> str:
    candidates = [lang, "ru"] if scope == "name" else _langs_for(lang)
    unique = list(dict.fromkeys(candidates))
    vectors = ", ".join(f"a.tsv_{candidate}" for candidate in unique)
    return f"coalesce({vectors}, {EMPTY_TSVECTOR})"


def _tsquery_expression(lang: str) -> str:
    return f"websearch_to_tsquery('{TS_CONFIG.get(lang, 'simple')}', :tsq)"


async def search(
    session: AsyncSession,
    query: str,
    *,
    lang: str = "ru",
    scope: Scope = "content",
    limit_exact: int = MAX_EXACT,
    limit_partial: int = MAX_PARTIAL,
) -> dict[str, Any]:
    started = time.perf_counter()
    normalized = normalize_query(query)
    if not normalized:
        return {
            "query": "",
            "scope": scope,
            "lang": lang,
            "exact": [],
            "partial": [],
            "took_ms": 0,
            "truncated": False,
        }

    tokens = query_tokens(normalized)
    tsq = " ".join(tokens) if tokens else normalized

    exact = await _search_exact(session, normalized, lang, scope, limit_exact)
    partial = await _search_partial(session, normalized, tsq, lang, scope, limit_partial)

    exact_ids = {hit["id"] for hit in exact}
    partial = [hit for hit in partial if hit["id"] not in exact_ids]

    return {
        "query": normalized,
        "scope": scope,
        "lang": lang,
        "exact": exact,
        "partial": partial,
        "took_ms": int((time.perf_counter() - started) * 1000),
        "truncated": len(partial) >= limit_partial,
    }


# --------------------------------------------------------------------- exact
async def _search_exact(
    session: AsyncSession,
    query: str,
    lang: str,
    scope: Scope,
    limit: int,
) -> list[dict[str, Any]]:
    value_sql = _value_expression(scope, lang)
    name_sql = _name_expression(lang)
    sql = f"""
        SELECT a.id,
               {name_sql} AS name,
               a.parent_id,
               a.path,
               {value_sql} AS value,
               (upper(a.id) = upper(:raw)) AS id_match
        FROM articles a
        {JOIN_SQL}
        WHERE a.deleted_at IS NULL
          AND (
                position(lower(:raw) in lower({value_sql})) > 0
             OR upper(a.id) = upper(:raw)
          )
        ORDER BY id_match DESC,
                 NULLIF(position(lower(:raw) in lower({value_sql})), 0) ASC NULLS LAST,
                 a.updated_at DESC
        LIMIT :limit
    """
    rows = await session.execute(text(sql), {"raw": query, "limit": limit})
    hits: list[dict[str, Any]] = []
    for row in rows.mappings():
        value = row["value"] or ""
        hits.append(
            {
                "id": row["id"],
                "name": row["name"],
                "parent_id": row["parent_id"],
                "path": row["path"] or "",
                "snippet": _exact_snippet(value, query),
                "score": 1.0 if row["id_match"] else 0.9,
                "matched_terms": [query],
                "matched_in": ["id"] if row["id_match"] else [scope],
            }
        )
    return hits


def _exact_snippet(value: str, query: str, window: int = 240) -> str:
    if not value:
        return ""
    position = value.lower().find(query.lower())
    if position < 0:
        return value[:window].replace("\n", " ")
    start = max(0, position - window // 3)
    end = min(len(value), start + window)
    snippet = value[start:end].replace("\n", " ")
    return f"{'…' if start > 0 else ''}{snippet}{'…' if end < len(value) else ''}"


# ------------------------------------------------------------------- partial
async def _search_partial(
    session: AsyncSession,
    query: str,
    tsq: str,
    lang: str,
    scope: Scope,
    limit: int,
) -> list[dict[str, Any]]:
    if not tsq.strip():
        return []

    vector = _tsvector_expression(lang, scope)
    tsquery = _tsquery_expression(lang)
    name_sql = _name_expression(lang)
    body_sql = f"coalesce(t_{lang}.content, '')"
    highlight_source = name_sql if scope == "name" else body_sql
    similarity_source = name_sql if scope == "name" else f"{name_sql} || ' ' || {body_sql}"
    config = TS_CONFIG.get(lang, "simple")

    sql = f"""
        WITH q AS (SELECT {tsquery} AS tsq)
        SELECT a.id,
               {name_sql} AS name,
               a.parent_id,
               a.path,
               ts_headline('{config}', {highlight_source}, q.tsq,
                           'StartSel=[[,StopSel=]],MaxWords=22,MinWords=8,MaxFragments=2'
               ) AS snippet,
               ts_rank_cd({vector}, q.tsq) AS rank,
               word_similarity(:raw, {similarity_source}) AS similarity
        FROM articles a
        {JOIN_SQL}
        CROSS JOIN q
        WHERE a.deleted_at IS NULL
          AND {vector} @@ q.tsq
        ORDER BY rank DESC, similarity DESC, a.updated_at DESC
        LIMIT :limit
    """
    rows = await session.execute(text(sql), {"raw": query, "tsq": tsq, "limit": limit})
    hits: list[dict[str, Any]] = []
    for row in rows.mappings():
        snippet = (row["snippet"] or "").strip()
        hits.append(
            {
                "id": row["id"],
                "name": row["name"],
                "parent_id": row["parent_id"],
                "path": row["path"] or "",
                "snippet": snippet,
                "score": round(
                    float(row["rank"] or 0) + 0.05 * float(row["similarity"] or 0), 6
                ),
                "matched_terms": _highlighted_terms(snippet),
                "matched_in": [scope],
            }
        )
    log.debug(
        "search %r scope=%s lang=%s -> %s partial hits", query, scope, lang, len(hits)
    )
    return hits


def _highlighted_terms(snippet: str) -> list[str]:
    return re.findall(r"\[\[(.+?)\]\]", snippet)[:12]


async def suggest(
    session: AsyncSession, query: str, lang: str = "ru", limit: int = 12
) -> list[dict[str, str]]:
    """Lightweight title autocomplete used by the SPA search field."""

    normalized = normalize_query(query)
    if len(normalized) < MIN_TOKEN_LENGTH:
        return []
    name_sql = _name_expression(lang)
    sql = f"""
        SELECT a.id, {name_sql} AS name, a.path
        FROM articles a
        {JOIN_SQL}
        WHERE a.deleted_at IS NULL
          AND position(lower(:raw) in lower({name_sql})) > 0
        ORDER BY length({name_sql}) ASC
        LIMIT :limit
    """
    rows = await session.execute(text(sql), {"raw": normalized, "limit": limit})
    return [
        {"id": row["id"], "name": row["name"], "path": row["path"] or ""}
        for row in rows.mappings()
    ]


async def search_stats(session: AsyncSession) -> dict[str, int]:
    total = await session.scalar(
        select(func.count()).select_from(Article).where(Article.deleted_at.is_(None))
    )
    indexed = await session.scalar(
        select(func.count())
        .select_from(Article)
        .where(Article.deleted_at.is_(None), Article.search_text != "")
    )
    translations = await session.scalar(
        select(func.count()).select_from(ArticleTranslation)
    )
    return {
        "articles": int(total or 0),
        "indexed": int(indexed or 0),
        "translations": int(translations or 0),
    }


__all__ = [
    "search",
    "suggest",
    "search_stats",
    "normalize_query",
    "query_tokens",
    "Scope",
]
