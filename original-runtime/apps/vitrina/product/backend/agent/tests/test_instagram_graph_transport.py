"""Pure helpers for Instagram Graph + Meta webhook parsing (no Hermes import).

Regression coverage for silent-DM bugs: we must only count Graph responses
without an ``error`` key as delivered messages.
"""

from __future__ import annotations

import json

import pytest

from platforms import instagram_platform


@pytest.mark.parametrize(
    ("payload", "expected_ok"),
    [
        ({"message_id": "m", "recipient_id": "x"}, True),
        ({"error": "http_400"}, False),
        ({"error": "missing_credentials"}, False),
    ],
)
def test_ig_graph_send_ok(payload: dict, expected_ok: bool) -> None:
    assert instagram_platform.ig_graph_send_ok(payload) is expected_ok


def test_meta_webhook_json_roundtrip() -> None:
    raw = b'{"object":"instagram","entry":[{"id":"1"}]}'
    assert instagram_platform.meta_webhook_json(raw) == {
        "object": "instagram",
        "entry": [{"id": "1"}],
    }


def test_meta_webhook_json_invalid() -> None:
    with pytest.raises(json.JSONDecodeError):
        instagram_platform.meta_webhook_json(b"not json")
