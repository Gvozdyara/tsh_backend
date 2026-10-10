"""Small helpers shared by the API layer."""

from __future__ import annotations

import uuid


def new_id() -> str:
    """Uppercase 8 character id, same shape as the legacy ``gen_rnd_str``."""

    return uuid.uuid4().hex[:8].upper()


def squeeze(name: str | None, limit: int = 27) -> str:
    """Shorten a long article name the same way the Kivy UI did."""

    if not name:
        return ""
    if len(name) <= limit:
        return name
    half = (limit - 3) // 2
    return f"{name[:half]}...{name[-half:]}"
