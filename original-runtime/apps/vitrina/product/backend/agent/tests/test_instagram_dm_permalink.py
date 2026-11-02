"""Instagram CDN reel path — unit tests (no Hermes).

Peter’s production shares often arrive as ``lookaside.fbsbx.com`` + ``reel_video_id``
(e.g. ``18431245783184544``). Graph message nodes return ``shares.data[].link`` (not
``url``); CDN links carry ``asset_id`` which we resolve to ``instagram.com/reel/…``
via the media node when permissions allow.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from platforms import ig_reel_permalink
from platforms import ig_webhook_inbound


# ── Parser (pure) ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "shares": {
                    "data": [
                        {
                            "type": "ig_reel",
                            "url": "https://www.instagram.com/reel/ABC123xyz/?utm_source=ig",
                        }
                    ]
                }
            },
            "https://www.instagram.com/reel/ABC123xyz/",
        ),
        (
            {
                "shares": {
                    "data": [
                        {
                            "type": "post",
                            "url": "https://instagram.com/p/XXXX/",
                        }
                    ]
                }
            },
            "https://instagram.com/p/XXXX/",
        ),
        (
            {
                "shares": {
                    "data": [
                        {
                            "url": "https://l.instagram.com/?u=https%3A%2F%2Fwww.instagram.com%2Freel%2FZ%2F",
                        }
                    ]
                }
            },
            None,
        ),
        ({}, None),
        ({"shares": {}}, None),
        (
            {
                "shares": {
                    "data": [
                        {"url": "https://example.com/reel/x/", "type": "reel"},
                    ]
                }
            },
            None,
        ),
    ],
)
def test_public_instagram_url_from_message_graph_response(
    payload: dict,
    expected: str | None,
) -> None:
    assert ig_reel_permalink.public_instagram_url_from_message_graph_response(payload) == expected


def test_public_instagram_prefers_first_matching_share() -> None:
    data = {
        "shares": {
            "data": [
                {"url": "https://cdninstagram.com/foo", "type": "bad"},
                {"url": "https://www.instagram.com/reel/GOOD/", "type": "ig_reel"},
            ]
        }
    }
    assert (
        ig_reel_permalink.public_instagram_url_from_message_graph_response(data)
        == "https://www.instagram.com/reel/GOOD/"
    )


def test_public_instagram_accepts_link_not_only_url() -> None:
    data = {
        "shares": {
            "data": [
                {
                    "link": "https://www.instagram.com/reel/LINKKEY/",
                    "type": "ig_reel",
                }
            ]
        }
    }
    assert (
        ig_reel_permalink.public_instagram_url_from_message_graph_response(data)
        == "https://www.instagram.com/reel/LINKKEY/"
    )


@patch("platforms.ig_reel_permalink.resolve_ig_reel_video_id_to_permalink")
def test_public_instagram_resolves_lookaside_share_link(mock_perm: MagicMock) -> None:
    mock_perm.return_value = "https://www.instagram.com/reel/FromAsset/"
    data = {
        "shares": {
            "data": [
                {
                    "link": (
                        "https://lookaside.fbsbx.com/ig_messaging_cdn/"
                        "?asset_id=18431245783184544&signature=x"
                    ),
                }
            ]
        }
    }
    assert (
        ig_reel_permalink.public_instagram_url_from_message_graph_response(data)
        == "https://www.instagram.com/reel/FromAsset/"
    )
    mock_perm.assert_called_once_with("18431245783184544")


# ── HTTP resolver (mocked Graph) ───────────────────────────────────────


class _FakeResp:
    def __init__(self, status_code: int, body: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self) -> dict:
        assert self._body is not None
        return self._body


def _setup_http_mock(mock_http: MagicMock, responses: list[_FakeResp]) -> None:
    client = MagicMock()
    client.get.side_effect = responses
    cm = MagicMock()
    cm.__enter__.return_value = client
    cm.__exit__.return_value = None
    mock_http.return_value = cm


@patch.dict(os.environ, {"INSTAGRAM_PAGE_ACCESS_TOKEN": "IGAAxxxxxxxx"}, clear=False)
@patch("platforms.ig_reel_permalink.http_client_sync")
def test_resolve_mid_success_first_host(mock_http: MagicMock) -> None:
    graph_json = {
        "shares": {
            "data": [
                {
                    "type": "ig_reel",
                    "url": "https://www.instagram.com/reel/FromMid123/",
                }
            ]
        }
    }
    _setup_http_mock(mock_http, [_FakeResp(200, graph_json)])

    out = ig_reel_permalink.resolve_ig_share_url_from_message_mid("m_mid_test")
    assert out == "https://www.instagram.com/reel/FromMid123/"
    client = mock_http.return_value.__enter__.return_value
    assert client.get.call_count == 1
    args, _ = client.get.call_args
    assert "m_mid_test" in args[0]
    assert ig_reel_permalink.GRAPH_IG_BASE in args[0]


@patch.dict(os.environ, {"INSTAGRAM_PAGE_ACCESS_TOKEN": "IGAAxxxxxxxx"}, clear=False)
@patch("platforms.ig_reel_permalink.http_client_sync")
def test_resolve_mid_falls_back_to_second_graph_host(mock_http: MagicMock) -> None:
    ok_body = {
        "shares": {
            "data": [{"type": "ig_reel", "url": "https://www.instagram.com/reel/Fallback/"}]
        }
    }
    _setup_http_mock(
        mock_http,
        [
            _FakeResp(400, text='{"error":{"message":"fail"}}'),
            _FakeResp(200, ok_body),
        ],
    )

    out = ig_reel_permalink.resolve_ig_share_url_from_message_mid("m_retry")
    assert out == "https://www.instagram.com/reel/Fallback/"
    client = mock_http.return_value.__enter__.return_value
    assert client.get.call_count == 2


@patch.dict(os.environ, {"INSTAGRAM_PAGE_ACCESS_TOKEN": "EAABxxxxxxxx"}, clear=False)
@patch("platforms.ig_reel_permalink.http_client_sync")
def test_resolve_mid_eaab_starts_with_facebook_graph(mock_http: MagicMock) -> None:
    graph_json = {
        "shares": {
            "data": [{"url": "https://www.instagram.com/p/POSTONLY/", "type": "ig_post"}]
        }
    }
    _setup_http_mock(mock_http, [_FakeResp(200, graph_json)])

    out = ig_reel_permalink.resolve_ig_share_url_from_message_mid("mid_eaab")
    assert out == "https://www.instagram.com/p/POSTONLY/"
    client = mock_http.return_value.__enter__.return_value
    args, _ = client.get.call_args
    assert ig_reel_permalink.GRAPH_FB_BASE in args[0]


@patch.dict(os.environ, {"INSTAGRAM_PAGE_ACCESS_TOKEN": ""}, clear=False)
def test_resolve_mid_no_token_returns_none() -> None:
    with patch.dict(os.environ, {"INSTAGRAM_PAGE_ACCESS_TOKEN": ""}):
        assert ig_reel_permalink.resolve_ig_share_url_from_message_mid("m_x") is None


@patch.dict(os.environ, {"INSTAGRAM_PAGE_ACCESS_TOKEN": "IGAAx"}, clear=False)
@patch("platforms.ig_reel_permalink.http_client_sync")
def test_resolve_mid_skips_graph_error_object(mock_http: MagicMock) -> None:
    err_then_ok = [
        _FakeResp(200, {"error": {"message": "OAuth", "code": 190}}),
        _FakeResp(
            200,
            {
                "shares": {
                    "data": [{"url": "https://www.instagram.com/reel/AfterErr/", "type": "reel"}]
                }
            },
        ),
    ]
    _setup_http_mock(mock_http, err_then_ok)

    out = ig_reel_permalink.resolve_ig_share_url_from_message_mid("m_two")
    assert out == "https://www.instagram.com/reel/AfterErr/"


# ── CDN-shaped webhook (production DB snippets) ───────────────────────

_LOOKASIDE_CDN = (
    "https://lookaside.fbsbx.com/ig_messaging_cdn/"
    "?asset_id=18431245783184544&signature=Ab3_ZWDERtN2Cq9AI1cEnDqKDBbKw9povdDpExDoneO0mXgXGSeQOaXa2EeLX_aQmME7b4Er13J_XvcRwkuWUJGW5PdVCCTFY9LJPGqTrG7ffPMS_Uu4HXfNgPDi433Gp61RmblDPZOZ39uGntKvqFcAGZ9phYmaZg35yx6dvD7tnc7j5Sc8vMskn8hsi3nKou_lZI_aZ0cIglb-P0PgenIpDKV8P5a6"
)


def _cdn_ig_reel_payload(*, mid: str, reel_video_id: str = "18431245783184544") -> dict:
    return {
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "967387775706018"},
                        "message": {
                            "mid": mid,
                            "attachments": [
                                {
                                    "type": "ig_reel",
                                    "payload": {
                                        "url": _LOOKASIDE_CDN,
                                        "reel_video_id": reel_video_id,
                                    },
                                }
                            ],
                        },
                    }
                ]
            }
        ]
    }


@patch("platforms.ig_webhook_inbound.resolve_ig_share_url_from_message_mid")
@patch("platforms.ig_webhook_inbound.resolve_ig_reel_video_id_to_permalink")
def test_cdn_reel_mid_shares_resolves_when_graph_returns_public_url(
    mock_media: MagicMock,
    mock_mid: MagicMock,
) -> None:
    mock_media.return_value = None
    mock_mid.return_value = "https://www.instagram.com/reel/DWhNsduiQkj/"

    payload = _cdn_ig_reel_payload(mid="m_mid_from_webhook")
    msgs = list(ig_webhook_inbound.iter_instagram_messaging_events(payload))
    assert len(msgs) == 1
    assert msgs[0]["reel_cdn_only"] is False
    assert "https://www.instagram.com/reel/DWhNsduiQkj/" in msgs[0]["text"]
    assert "lookaside.fbsbx.com" not in msgs[0]["text"]
    mock_mid.assert_called_once_with("m_mid_from_webhook")


@patch("platforms.ig_webhook_inbound.resolve_ig_share_url_from_message_mid")
@patch("platforms.ig_webhook_inbound.resolve_ig_reel_video_id_to_permalink")
def test_lookaside_cdn_reel_is_extractable_not_flagged_cdn_only(
    mock_media: MagicMock,
    mock_mid: MagicMock,
) -> None:
    """Lookaside MP4 CDN URLs are now consumed directly by the extractor
    (httpx download in ``recipe_videos.extract_social_recipe``), so we no
    longer flag them as ``reel_cdn_only`` (which would have told the agent
    to refuse the URL)."""
    mock_media.return_value = None
    mock_mid.return_value = None

    payload = _cdn_ig_reel_payload(mid="m_fail")
    msgs = list(ig_webhook_inbound.iter_instagram_messaging_events(payload))
    assert msgs[0]["reel_cdn_only"] is False
    assert "lookaside" in msgs[0]["text"].lower()


@patch("platforms.ig_webhook_inbound.resolve_ig_share_url_from_message_mid")
@patch("platforms.ig_webhook_inbound.resolve_ig_reel_video_id_to_permalink")
def test_cdn_reel_prefers_media_permalink_over_mid(
    mock_media: MagicMock,
    mock_mid: MagicMock,
) -> None:
    mock_media.return_value = "https://www.instagram.com/reel/FROM_MEDIA/"
    mock_mid.return_value = "https://www.instagram.com/reel/FROM_MID/"

    payload = _cdn_ig_reel_payload(mid="m_pref")
    msgs = list(ig_webhook_inbound.iter_instagram_messaging_events(payload))
    assert "FROM_MEDIA" in msgs[0]["text"]
    assert "FROM_MID" not in msgs[0]["text"]
    mock_mid.assert_not_called()


def test_voice_only_message_yields_voice_urls() -> None:
    audio_url = "https://lookaside.fbsbx.com/ig_messaging_cdn/foo.m4a"
    payload = {
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "12345"},
                        "message": {
                            "mid": "m_voice",
                            "attachments": [
                                {
                                    "type": "audio",
                                    "payload": {"url": audio_url},
                                }
                            ],
                        },
                    }
                ]
            }
        ]
    }
    msgs = list(ig_webhook_inbound.iter_instagram_messaging_events(payload))
    assert len(msgs) == 1
    assert msgs[0]["text"] == ""
    assert msgs[0]["voice_attachment_urls"] == [audio_url]
    assert msgs[0]["reel_cdn_only"] is False


@patch("platforms.ig_webhook_inbound.resolve_ig_share_url_from_message_mid")
@patch("platforms.ig_webhook_inbound.resolve_ig_reel_video_id_to_permalink")
def test_voice_note_plus_caption_and_reel_share_split_urls(
    mock_media: MagicMock,
    mock_mid: MagicMock,
) -> None:
    """Audio URLs must not appear in the reel/share appendix (STT path only)."""
    mock_media.return_value = None
    mock_mid.return_value = None
    audio_url = "https://cdn.example/audio.m4a"
    reel_url = "https://lookaside.fbsbx.com/ig_messaging_cdn/reel.mp4"
    payload = {
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "999"},
                        "message": {
                            "mid": "m_mix",
                            "text": "Check this",
                            "attachments": [
                                {"type": "audio", "payload": {"url": audio_url}},
                                {
                                    "type": "ig_reel",
                                    "payload": {"url": reel_url, "reel_video_id": "1"},
                                },
                            ],
                        },
                    }
                ]
            }
        ]
    }
    msgs = list(ig_webhook_inbound.iter_instagram_messaging_events(payload))
    assert len(msgs) == 1
    assert msgs[0]["voice_attachment_urls"] == [audio_url]
    assert audio_url not in (msgs[0]["text"] or "")
    assert reel_url in (msgs[0]["text"] or "")


# ── Live: Graph message id for a CDN share often has **no** shares field ─


LIVE_TOKEN = os.getenv("INSTAGRAM_PAGE_ACCESS_TOKEN")


@pytest.mark.skipif(
    not (os.getenv("IG_LIVE_TEST_MESSAGE_MID") and LIVE_TOKEN),
    reason="Set IG_LIVE_TEST_MESSAGE_MID (webhook mid) + token if testing webhook-shaped id",
)
def test_live_webhook_mid_returns_public_url_if_meta_cooperates() -> None:
    url = ig_reel_permalink.resolve_ig_share_url_from_message_mid(os.getenv("IG_LIVE_TEST_MESSAGE_MID") or "")
    assert url is not None
    assert "instagram.com" in url.lower()
