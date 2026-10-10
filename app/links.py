"""Text encoded article links.

Legacy format, unchanged from the Kivy application: any line of an article
content that starts with ``ищи`` points at another article::

    ищи СТАТУС GSM        -> resolved by name (or by id when the token is an id)
    ищи GH8AYPL5          -> resolved by id

The API resolves those references to article ids and returns them together with
the article, so the SPA can render a row of link buttons exactly like the Kivy
``LinksLayout`` did (and inline-expand the body when there is exactly one link).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

LINK_PATTERN = re.compile(r"ищи([ \t]+)([^\r\n]*)", re.IGNORECASE | re.MULTILINE)
ID_PATTERN = re.compile(r"^[0-9A-Z]{8}$")


@dataclass(frozen=True)
class ParsedLink:
    raw: str
    token: str


def parse_links(text: str | None) -> list[ParsedLink]:
    """Return every ``ищи <target>`` reference found in ``text``."""

    if not text:
        return []
    found: list[ParsedLink] = []
    seen: set[str] = set()
    for match in LINK_PATTERN.finditer(text):
        token = match.group(2).strip()
        raw = match.group(0).strip()
        if not token or token in seen:
            continue
        seen.add(token)
        found.append(ParsedLink(raw=raw, token=token))
    return found


def looks_like_id(token: str) -> bool:
    return bool(ID_PATTERN.match(token.strip().upper()))


def inline_replacement(target: "object", highlight: bool = True) -> str:
    """Comment used when the app inlines the linked article body."""

    name = getattr(target, "name", "") or ""
    if highlight:
        return f"// отображение ссылки {name}"
    return f"// {name}"


def inline_body(name: str, content: str) -> str:
    return f"{name}\n\n{content}\n\n// конец ссылки //"
