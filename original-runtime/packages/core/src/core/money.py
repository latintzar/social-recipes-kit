"""
core.money — Decimal-backed currency arithmetic
=================================================

Every monetary value entering / leaving the system rides through this
module. Cents are always ``int``; majors are always ``Decimal`` rounded
``ROUND_HALF_UP`` (matches Spanish legal convention, and is the right
default for any EU VAT regime). Float is the enemy: no operation in
this module accepts or returns ``float``.

VAT helpers (``add_iva``, ``strip_iva``, ``iva_amount``) are rate-
parameterised and jurisdiction-agnostic — callers pass the rate. The
actual rate constants (``IVA_GENERAL = 0.21`` etc.) are Spanish policy
and live in ``apps/scope/product/backend/constants.py`` because they encode a
business decision specific to that app.

The functions keep the ``iva`` name because (a) the only in-repo caller
today is scope-Spanish and (b) the mental model "tax on top" is
universal; an apps/<other-app> caller that hits UK VAT can still write
``add_iva(amount, Decimal('0.20'))`` and get the right number.

Tier: leaf. Depends on stdlib only.

Phase 5 Wave 2 note
-------------------
Moved from the former ``packages/domain/domain/money.py``. Behaviour is
byte-identical; import paths change ``domain`` → ``core``. Top-level
re-exports (``from core import Money, to_cents, from_cents, add_iva,
strip_iva, iva_amount, format_eur``) continue to mirror the previous
``from domain import ...`` surface.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal, Union

# Callers hand us ``int``, ``float``, ``str`` or ``Decimal``. We coerce
# everything to ``Decimal`` internally. ``Money`` is the accepted input
# alias; outputs are always either ``Decimal`` or ``int`` (cents) — never
# float, never a mixed union.
Money = Union[int, float, str, Decimal]

# Two decimal places for EUR. Cents are integer; this drives the
# quantize step inside every public API.
_TWO_PLACES = Decimal("0.01")


def _as_decimal(value: Money) -> Decimal:
    """Coerce *value* to ``Decimal`` via ``str(value)`` to dodge float noise.

    Accepts the ``Money`` alias. Raises ``InvalidOperation`` (stdlib
    ``decimal`` exception) for unparseable strings — callers typically
    wrap with their own 400 BadRequest.
    """
    if isinstance(value, Decimal):
        return value
    # str(float) is the canonical bridge: Decimal("0.1") vs Decimal(0.1)
    # — the latter smuggles in binary-float garbage.
    return Decimal(str(value))


def _quantize(value: Decimal) -> Decimal:
    """Round *value* to 2 dp, ROUND_HALF_UP (Spanish legal convention)."""
    return value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Cent <-> euro
# ---------------------------------------------------------------------------


def to_cents(eur: Money) -> int:
    """Convert EUR amount to integer cents (HALF_UP at 2 dp).

    Examples
    --------
    >>> to_cents(10)
    1000
    >>> to_cents("10.567")
    1057
    >>> to_cents(Decimal("0.005"))  # Half-up rounds up
    1
    """
    dec = _as_decimal(eur)
    return int(_quantize(dec) * 100)


def from_cents(cents: int) -> Decimal:
    """Convert integer cents to ``Decimal`` EUR (2 dp).

    Raises ``TypeError`` if *cents* isn't ``int``. We refuse ``float``
    and ``Decimal`` here on purpose — mixing representations is the #1
    source of rounding drift in the old codebase.

    >>> from_cents(1057)
    Decimal('10.57')
    """
    if not isinstance(cents, int):
        raise TypeError(f"cents must be int, got {type(cents).__name__}")
    return (Decimal(cents) / 100).quantize(_TWO_PLACES)


# ---------------------------------------------------------------------------
# IVA — Spanish VAT
# ---------------------------------------------------------------------------


def add_iva(amount: Money, rate: Money) -> Decimal:
    """Return ``amount * (1 + rate)`` rounded 2 dp HALF_UP.

    *rate* is a fraction: 0.21 for 21%. Accepts int/float/str/Decimal.
    A rate of 21 (percent-integer form) multiplies 22×, which is almost
    always a caller bug — use :func:`add_iva_percent` for the
    percent-integer path.

    >>> add_iva(100, 0.21)
    Decimal('121.00')
    """
    dec_amount = _as_decimal(amount)
    dec_rate = _as_decimal(rate)
    return _quantize(dec_amount * (Decimal(1) + dec_rate))


def strip_iva(gross: Money, rate: Money) -> Decimal:
    """Inverse of ``add_iva``: extract net from gross.

    ``strip_iva(121, 0.21) → Decimal('100.00')``.

    >>> strip_iva(121, 0.21)
    Decimal('100.00')
    """
    dec_gross = _as_decimal(gross)
    dec_rate = _as_decimal(rate)
    return _quantize(dec_gross / (Decimal(1) + dec_rate))


def iva_amount(amount: Money, rate: Money) -> Decimal:
    """Return only the VAT component: ``amount * rate`` rounded 2 dp.

    >>> iva_amount(100, 0.21)
    Decimal('21.00')
    """
    dec_amount = _as_decimal(amount)
    dec_rate = _as_decimal(rate)
    return _quantize(dec_amount * dec_rate)


# ---------------------------------------------------------------------------
# Percent-integer convenience wrappers
# ---------------------------------------------------------------------------
# Some data sources (contracts, CSV imports) store IVA as "21" rather
# than "0.21". These three wrappers make that explicit and eliminate the
# ``0.21 vs 21`` ambiguity at the call site.


def add_iva_percent(amount: Money, percent: Money) -> Decimal:
    """Like :func:`add_iva` but *percent* is an integer percentage.

    >>> add_iva_percent(100, 21)
    Decimal('121.00')
    """
    return add_iva(amount, _as_decimal(percent) / Decimal(100))


def strip_iva_percent(gross: Money, percent: Money) -> Decimal:
    """Like :func:`strip_iva` but *percent* is an integer percentage."""
    return strip_iva(gross, _as_decimal(percent) / Decimal(100))


def iva_amount_percent(amount: Money, percent: Money) -> Decimal:
    """Like :func:`iva_amount` but *percent* is an integer percentage."""
    return iva_amount(amount, _as_decimal(percent) / Decimal(100))


# ---------------------------------------------------------------------------
# Human formatting
# ---------------------------------------------------------------------------
# ``convention="es"`` → "1.234,56 €"   (thousand-dot, comma-decimal, trailing €)
# ``convention="en"`` → "€1,234.56"    (leading €, comma-thousand, dot-decimal)


def format_eur(
    amount: Money,
    *,
    convention: Literal["es", "en"] = "es",
) -> str:
    """Format *amount* as EUR string.

    Always renders 2 decimal places (``20 → "20,00 €"``) — there is no
    opt-out. Design doc D5 calls this ``cents_always`` behaviour: mixed
    precision confuses both humans and the LLM.

    >>> format_eur(1234.56)
    '1.234,56 €'
    >>> format_eur(1234.56, convention='en')
    '€1,234.56'
    """
    dec = _quantize(_as_decimal(amount))
    # Render absolute value with plain format, then re-attach sign after
    # convention swap so "(-)1.234,56 €" reads right in both.
    sign = "-" if dec < 0 else ""
    abs_str = f"{abs(dec):,.2f}"  # "1,234.56" — always en-US format here.

    if convention == "es":
        # swap "," ↔ "." using a placeholder to avoid double-replace
        swapped = abs_str.replace(",", "∎").replace(".", ",").replace("∎", ".")
        return f"{sign}{swapped} €"

    return f"{sign}€{abs_str}"


__all__ = [
    "Money",
    "to_cents",
    "from_cents",
    "add_iva",
    "strip_iva",
    "iva_amount",
    "add_iva_percent",
    "strip_iva_percent",
    "iva_amount_percent",
    "format_eur",
]
