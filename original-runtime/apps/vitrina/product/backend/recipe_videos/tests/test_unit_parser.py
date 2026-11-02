"""Tests for the deterministic pack-size parser."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from unit_parser import parse_pack_grams  # noqa: E402


def test_bulgarian_grams():
    assert parse_pack_grams("pack", "500г").grams == 500.0
    assert parse_pack_grams("pack", "200 г").grams == 200.0


def test_bulgarian_kg():
    assert parse_pack_grams("kg", "1кг").grams == 1000.0
    assert parse_pack_grams("kg", "1.5 кг").grams == 1500.0


def test_english_grams_and_kg():
    assert parse_pack_grams("pack", "500g").grams == 500.0
    assert parse_pack_grams("pack", "1.5 kg").grams == 1500.0


def test_millilitres_treated_as_grams():
    assert parse_pack_grams("pack", "500ml").grams == 500.0
    assert parse_pack_grams("pack", "1 L").grams == 1000.0
    assert parse_pack_grams("pack", "200мл").grams == 200.0


def test_pieces():
    p = parse_pack_grams("pack", "4 pcs")
    assert p.pieces == 4 and p.grams is None
    p = parse_pack_grams("pc", "1 pc")
    assert p.pieces == 1
    p = parse_pack_grams("pack", "6 бр")
    assert p.pieces == 6


def test_unit_only_fallback():
    assert parse_pack_grams("kg", None).grams == 1000.0
    assert parse_pack_grams("pc", "").pieces == 1


def test_unknown():
    p = parse_pack_grams(None, None)
    assert p.grams is None and p.pieces is None
