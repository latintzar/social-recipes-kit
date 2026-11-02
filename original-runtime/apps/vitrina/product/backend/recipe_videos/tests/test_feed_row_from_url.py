"""Regression: feed_row_from_url must not collapse IG Messaging CDN URLs."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extract_social_recipe import feed_row_from_url  # noqa: E402


def test_lookaside_cdn_distinct_ids_per_asset_id():
    a = feed_row_from_url(
        "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=17841431963463132"
    )
    b = feed_row_from_url(
        "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=99988877766655544"
    )
    assert a["id"] == "instagram-cdn-17841431963463132"
    assert b["id"] == "instagram-cdn-99988877766655544"
    assert a["id"] != b["id"]
    assert a["source"] == "Instagram" and b["source"] == "Instagram"
    assert "asset_id=17841431963463132" in a["url"]
    assert "asset_id=99988877766655544" in b["url"]


def test_instagram_reel_unchanged():
    r = feed_row_from_url("https://www.instagram.com/reel/DWhNsduiQkj/")
    assert r["id"] == "instagram-DWhNsduiQkj"


def test_lookaside_without_asset_id_raises():
    with pytest.raises(ValueError, match="asset_id"):
        feed_row_from_url("https://lookaside.fbsbx.com/ig_messaging_cdn/")
