"""Graph profile fetch for Instagram DM senders (username / name)."""

from __future__ import annotations

import pytest

from platforms import instagram_platform as igp


def test_fetch_instagram_user_profile_uses_instagram_graph_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Resp:
        status_code = 200

        def json(self) -> dict:
            return {"username": "shopper_test", "name": "Test User"}

    class Client:
        def __enter__(self) -> Client:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def get(self, url: str, params: dict | None = None) -> Resp:
            assert "graph.instagram.com" in url
            assert params and "access_token" in params
            return Resp()

    def fake_http(timeout: float = 0):  # noqa: ARG001
        return Client()

    monkeypatch.setenv("INSTAGRAM_PAGE_ACCESS_TOKEN", "EAAB-fake")
    monkeypatch.setattr(igp, "http_client_sync", fake_http)
    out = igp.fetch_instagram_user_profile("123456")
    assert out["username"] == "shopper_test"
    assert out["name"] == "Test User"


def test_fetch_instagram_user_profile_falls_back_to_facebook_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Bad:
        status_code = 400
        text = "no"

        def json(self) -> dict:
            return {"error": {"message": "nope"}}

    class Good:
        status_code = 200

        def json(self) -> dict:
            return {"username": "from_fb", "name": ""}

    calls: list[str] = []

    class Client:
        def __enter__(self) -> Client:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def get(self, url: str, params: dict | None = None) -> Bad | Good:
            calls.append(url)
            if "graph.instagram.com" in url:
                return Bad()
            assert "graph.facebook.com" in url
            return Good()

    def fake_http(timeout: float = 0):  # noqa: ARG001
        return Client()

    monkeypatch.setenv("INSTAGRAM_PAGE_ACCESS_TOKEN", "EAAB-fake")
    monkeypatch.setattr(igp, "http_client_sync", fake_http)
    out = igp.fetch_instagram_user_profile("789")
    assert out.get("username") == "from_fb"
    assert len(calls) == 2
