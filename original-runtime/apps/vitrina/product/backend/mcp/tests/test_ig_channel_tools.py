"""Ig channel helpers — location magic-link mint (no network)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import ig_channel_tools


@pytest.fixture
def patch_meta(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr(
        "ig_channel_tools.chat_threads.patch_thread_metadata",
        mock,
    )
    return mock


def test_ig_request_location_requires_thread_and_cart(patch_meta: MagicMock) -> None:
    out = ig_channel_tools.ig_request_location(thread_id=None, cart_key="ig:x")
    assert out == {"ok": False, "error": "thread_id_and_cart_key_required"}
    patch_meta.assert_not_called()


def test_ig_request_location_returns_https_url_with_query(patch_meta: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VITRINA_PUBLIC_URL", raising=False)
    monkeypatch.delenv("VITRINA_PUBLIC_HOST", raising=False)
    monkeypatch.delenv("VITRINA_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr("ig_channel_tools.chat_threads.find_thread_by_ig_location_slug", lambda _s: None)
    tid = "550e8400-e29b-41d4-a716-446655440000"
    out = ig_channel_tools.ig_request_location(thread_id=tid, cart_key="ig:12345")
    assert out["ok"] is True
    assert out["expires_in"] == 600
    url = str(out["url"])
    assert url.startswith("https://vitrina.bg/ig/location?")
    assert "s=" in url
    assert out.get("short_slug")
    patch_meta.assert_called_once()
    assert patch_meta.call_args[0][0] == tid
    meta = patch_meta.call_args[0][1]
    assert "location_token" in meta
    assert "location_token_exp" in meta
    assert "location_short_slug" in meta
    assert patch_meta.call_args[1]["cart_key"] == "ig:12345"


def test_account_attach_email_valid(patch_meta: MagicMock) -> None:
    out = ig_channel_tools.account_attach_email(
        email="  Buyer@Example.COM ",
        thread_id="th1",
        cart_key="ig:x",
    )
    assert out["ok"] is True
    assert out["email"] == "buyer@example.com"
    patch_meta.assert_called()
