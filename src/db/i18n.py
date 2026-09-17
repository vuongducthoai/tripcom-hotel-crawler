"""Language helpers shared by database loaders and exporters."""
from __future__ import annotations


def language_key(locale: str | None) -> str:
    """Map a request locale (vi-VN/en-US) to the DB language key (vi/en)."""
    value = (locale or "vi").strip().replace("_", "-")
    return value.split("-", 1)[0].lower()

