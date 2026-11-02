"""
core.text — Unicode / Spanish-aware text normalization
========================================================

Canonical implementation of accent stripping, whitespace collapse, and
URL slug generation. Any package or app that needs to canonicalise
user-visible strings — search index tokens, slugs, city/trade equality
checks — routes through here.

Tier: leaf. Depends on stdlib only.

Usage
-----
    from core.text import normalize_spanish, slugify

    normalize_spanish("ÁREAS Comunes  ")   → "areas comunes"
    slugify("Áreas comunes")               → "areas-comunes"

Phase 5 Wave 2 note
-------------------
Moved from the former ``packages/domain`` package to ``packages/core``.
Behaviour is byte-identical; only the import path changed from
``domain.text`` → ``core.text`` (also re-exported at the top level as
``from core import strip_accents, normalize_spanish, slugify``).
"""

from __future__ import annotations

import re
import unicodedata

_WS_RE = re.compile(r"\s+")
_SLUG_FILTER = re.compile(r"[^a-z0-9]+")


def strip_accents(text: str) -> str:
    """Remove diacritical marks.

    ``"ñ" → "n"``, ``"á" → "a"``. NFKD-decomposes then filters combining
    chars. Returns "" for falsy input.

    >>> strip_accents("Málaga")
    'Malaga'
    >>> strip_accents("coração")
    'coracao'
    """
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def normalize_spanish(text: str, *, lower: bool = True) -> str:
    """Canonical form for Spanish search / equality comparison.

    1. strip accents (``ñ → n``, ``á → a``)
    2. collapse any run of whitespace to a single space
    3. ``lower=True`` → lowercase (default)
    4. trim

    Used for trade / city / product-name matching where diacritics and
    casing differ between upstream data sources (Habitissimo vs
    Milanuncios).

    >>> normalize_spanish("ÁREAS  Comunes")
    'areas comunes'
    >>> normalize_spanish("Bilbao", lower=False)
    'Bilbao'
    """
    if not text:
        return ""
    out = strip_accents(text)
    if lower:
        out = out.lower()
    return _WS_RE.sub(" ", out).strip()


def slugify(text: str, *, sep: str = "-") -> str:
    """Produce a URL-safe slug.

    >>> slugify("Áreas comunes")
    'areas-comunes'
    >>> slugify("¡Hola, mundo!", sep="_")
    'hola_mundo'
    """
    normalized = normalize_spanish(text, lower=True)
    return _SLUG_FILTER.sub(sep, normalized).strip(sep)


__all__ = ["strip_accents", "normalize_spanish", "slugify"]
