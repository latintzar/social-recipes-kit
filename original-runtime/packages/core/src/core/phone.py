"""
core.phone — Phone normalization (Spanish-primary, generic-capable)
===================================================================

Single source of truth for phone handling across scrapers, adapters,
routes, and agent tools. The scope app parses "+34 612 345 678",
stores "612345678", displays "+34612345678", dedups by 9-digit key —
every step goes through this module so the representation never
drifts.

The implementation is Spanish-tuned (short-circuit for 6/7/8/9 mobile
prefixes, fallback path assumes ``+34``) but still works for any
E.164-shaped number via the ``phonenumbers`` library. Callers that
need strict regional validation pass ``require_region`` explicitly.

Tier: leaf. Depends on stdlib + ``phonenumbers`` for E.164 quality.

Two normalization forms
-----------------------
- :func:`normalize` → 9-digit Spanish dedup key (no prefix)
- :func:`to_e164`   → "+CCNNNNNNNNN" display / API form

The dedup key is lossy on purpose: "+34600000001" and "34600000001" and
"0034 600 000 001" all collapse to "600000001" for de-duplication.

Phase 5 Wave 2 note
-------------------
Moved from the former ``packages/domain/domain/phone.py``. Behaviour
is byte-identical; only the import path changed from ``domain.phone``
→ ``core.phone`` (top-level re-exports continue to mirror
``from domain import normalize, to_e164, is_valid_spanish,
extract_from_html, dedup_by_phone``).
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, List, Protocol, TypeVar, runtime_checkable

logger = logging.getLogger(__name__)

T = TypeVar("T")

_STRIP_CHARS = re.compile(r"[\s.\-()]")
_SPAIN_PREFIX = re.compile(r"^(\+34|0034|34)(?=[6789])")
_TEL_HREF = re.compile(r'href=["\']tel:([0-9 +]+)["\']')
_SPANISH_MOBILE = re.compile(r"^[6789]\d{8}$")
_DIGIT_ONLY = re.compile(r"\D")


def to_digits(s: str) -> str:
    """Strip every non-digit char.

    >>> to_digits("+34 (612) 345-678")
    '34612345678'
    """
    if not s:
        return ""
    return _DIGIT_ONLY.sub("", str(s))


def normalize(raw: str) -> str:
    """Normalize a Spanish phone number to its 9-digit dedup key.

    Steps: strip whitespace/dots/dashes/parens, then drop the
    ``+34|0034|34`` country prefix **only when followed by a valid
    Spanish first digit (6/7/8/9)**. Anything else passes through as-is.

    Returns "" for falsy input.

    >>> normalize("+34 612 345 678")
    '612345678'
    >>> normalize("0034-912-345-678")
    '912345678'
    >>> normalize("+447578244644")   # non-Spanish passes through cleaned
    '+447578244644'
    >>> normalize("")
    ''
    """
    if not raw:
        return ""
    cleaned = _STRIP_CHARS.sub("", str(raw))
    return _SPAIN_PREFIX.sub("", cleaned)


def extract_from_html(html: str) -> List[str]:
    """Extract + normalize phones from ``tel:`` links in HTML.

    Empty results filtered. Used by Habitissimo/Milanuncios scrapers.

    >>> extract_from_html('<a href="tel:+34612345678">call</a>')
    ['612345678']
    """
    raw_phones = _TEL_HREF.findall(html)
    return [normalize(p) for p in raw_phones if normalize(p)]


def is_valid_spanish(phone: str) -> bool:
    """Check if a *normalized* phone looks like a valid Spanish number.

    Accepts raw input — normalizes first. A valid Spanish number is
    exactly 9 digits starting with 6/7/8/9. Returns False on bad input.

    >>> is_valid_spanish("612345678")
    True
    >>> is_valid_spanish("+34 612 345 678")
    True
    >>> is_valid_spanish("123")
    False
    """
    return bool(_SPANISH_MOBILE.match(normalize(phone)))


def to_e164(
    raw: str,
    *,
    default_region: str = "ES",
    require_region: str | None = None,
) -> str:
    """Return E.164 form ("+CCNNNNNNNNN").

    Uses ``phonenumbers`` when available (handles every country, not
    just Spain). Falls back to the Spanish-only regex path if the
    library is missing or the parse fails.

    Parameters
    ----------
    default_region:
        Region code used when *raw* has no country prefix. Default
        ``"ES"``.
    require_region:
        If set (e.g. ``"ES"``), reject numbers that resolve to a
        different region. Returns ``""`` when rejected.

    >>> to_e164("612 345 678")
    '+34612345678'
    >>> to_e164("+44 7578 244644")
    '+447578244644'
    """
    if not raw:
        return ""
    text = str(raw).strip()

    # ES-only pre-promote: a bare 9-digit mobile gets a +34 prefix
    # *before* phonenumbers sees it. Otherwise the library guesses UK.
    if default_region == "ES" and not text.startswith("+"):
        digits = to_digits(text)
        if len(digits) == 9 and digits[0] in "6789":
            text = "+34" + digits

    try:
        import phonenumbers

        parsed = phonenumbers.parse(text, default_region)
        if not phonenumbers.is_valid_number(parsed):
            return ""
        if require_region:
            region = phonenumbers.region_code_for_number(parsed)
            if region != require_region:
                logger.info(
                    "Phone rejected | region=%s required=%s",
                    region,
                    require_region,
                )
                return ""
        return phonenumbers.format_number(
            parsed, phonenumbers.PhoneNumberFormat.E164,
        )
    except Exception as exc:
        logger.debug("phonenumbers parse failed for %r: %s", raw, exc)

    # Spanish-only regex fallback.
    norm = normalize(raw)
    if _SPANISH_MOBILE.match(norm):
        return f"+34{norm}"
    if require_region == "ES":
        return ""
    # Only reflect inputs that already LOOK like a phone (digit-only
    # after normalize OR already E.164). Refuse gibberish like "abc" —
    # mirrors ``phonenumbers.is_valid_number == False`` by returning "".
    if not norm:
        return ""
    if norm.startswith("+") and norm[1:].isdigit():
        return norm
    if norm.isdigit():
        return f"+{norm}"
    return ""


def dedup_by_phone(
    items: Iterable[T],
    *,
    key: str = "phone",
) -> List[T]:
    """Drop duplicates from *items* where two records share a normalized phone.

    Preserves first-seen order. Records with empty / unparseable phones
    are ALL kept (we can't dedup what we can't key). ``key`` is the
    dict-key or attribute to read from each item.

    >>> dedup_by_phone([
    ...     {"phone": "+34612345678"},
    ...     {"phone": "612 345 678"},
    ...     {"phone": "699999999"},
    ... ])
    [{'phone': '+34612345678'}, {'phone': '699999999'}]
    """
    seen: set[str] = set()
    out: list[T] = []
    for item in items:
        if isinstance(item, dict):
            phone_raw = str(item.get(key) or "")
        else:
            phone_raw = str(getattr(item, key, "") or "")
        norm = normalize(phone_raw)
        if not norm:
            out.append(item)
            continue
        if norm in seen:
            continue
        seen.add(norm)
        out.append(item)
    return out


@runtime_checkable
class PhoneNormalizer(Protocol):
    """Structural-type contract for mock / fake phone normalizers in tests."""

    def normalize(self, raw: str) -> str: ...
    def to_e164(self, raw: str) -> str: ...
    def is_valid_spanish(self, phone: str) -> bool: ...


__all__ = [
    "normalize",
    "to_e164",
    "is_valid_spanish",
    "extract_from_html",
    "dedup_by_phone",
    "to_digits",
    "PhoneNormalizer",
]
