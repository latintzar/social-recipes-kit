"""Tests for :mod:`chat_threads` — CRUD, auth, message append, cascade.

Uses the shared in-memory Supabase stand-in (``fixtures/supabase_mock.py``)
to exercise the full handler surface without real HTTP. The module-level
``_get_supabase`` seam is monkeypatched so every handler sees the same
fake backend.
"""
from __future__ import annotations

import pytest

import chat_threads
from fixtures import supabase_mock


@pytest.fixture(autouse=True)
def _fresh_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test gets a pristine dict-of-lists backend."""
    supabase_mock.reset()
    fake = supabase_mock.FakeSupabase()
    monkeypatch.setattr(chat_threads, "_get_supabase", lambda: fake)
    yield
    supabase_mock.reset()


# ── create ────────────────────────────────────────────────────────


def test_create_thread_authed() -> None:
    t = chat_threads.create_thread(user_id="u1", title="hello")
    assert t["user_id"] == "u1"
    assert t["cart_key"] is None
    assert t["title"] == "hello"
    assert t["pinned"] is False
    assert "id" in t and "created_at" in t and "updated_at" in t


def test_create_thread_anon() -> None:
    t = chat_threads.create_thread(cart_key="k-abc")
    assert t["cart_key"] == "k-abc"
    assert t["user_id"] is None


# ── list ──────────────────────────────────────────────────────────


def test_list_user_threads_returns_only_owned() -> None:
    chat_threads.create_thread(user_id="u1", title="a")
    chat_threads.create_thread(user_id="u1", title="b")
    chat_threads.create_thread(user_id="u2", title="nope")
    rows = chat_threads.list_user_threads("u1")
    assert {r["title"] for r in rows} == {"a", "b"}


def test_list_threads_by_key() -> None:
    chat_threads.create_thread(cart_key="k1", title="x")
    chat_threads.create_thread(cart_key="k2", title="y")
    rows = chat_threads.list_threads_by_key("k1")
    assert [r["title"] for r in rows] == ["x"]


# ── pin / rename ──────────────────────────────────────────────────


def test_pin_and_unpin() -> None:
    t = chat_threads.create_thread(user_id="u1")
    pinned = chat_threads.pin_thread(t["id"], True, user_id="u1")
    assert pinned["pinned"] is True
    unpinned = chat_threads.pin_thread(t["id"], False, user_id="u1")
    assert unpinned["pinned"] is False


def test_rename() -> None:
    t = chat_threads.create_thread(user_id="u1", title="old")
    renamed = chat_threads.update_thread_title(t["id"], "new", user_id="u1")
    assert renamed["title"] == "new"


# ── messages ──────────────────────────────────────────────────────


def test_append_message_role_validation() -> None:
    t = chat_threads.create_thread(user_id="u1")
    with pytest.raises(chat_threads.InvalidRole):
        chat_threads.append_message(t["id"], "banana", "hi", user_id="u1")


def test_append_and_list_messages_in_order() -> None:
    t = chat_threads.create_thread(user_id="u1")
    chat_threads.append_message(t["id"], "user", "first", user_id="u1")
    chat_threads.append_message(t["id"], "assistant", "second", user_id="u1")
    chat_threads.append_message(
        t["id"], "tool", {"kind": "tool_result", "x": 1}, user_id="u1"
    )
    msgs = chat_threads.list_messages(t["id"], user_id="u1")
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool"]
    # string content was wrapped
    assert msgs[0]["content"] == {"kind": "text", "text": "first"}
    assert msgs[2]["content"] == {"kind": "tool_result", "x": 1}


def test_append_message_bumps_thread_updated_at() -> None:
    t = chat_threads.create_thread(user_id="u1")
    before = t["updated_at"]
    import time

    time.sleep(0.005)
    chat_threads.append_message(t["id"], "user", "ping", user_id="u1")
    fresh = chat_threads.get_thread(t["id"], user_id="u1")
    assert fresh["updated_at"] > before


# ── delete cascade ────────────────────────────────────────────────


def test_delete_cascades_messages() -> None:
    t = chat_threads.create_thread(user_id="u1")
    chat_threads.append_message(t["id"], "user", "a", user_id="u1")
    chat_threads.append_message(t["id"], "user", "b", user_id="u1")
    assert len(supabase_mock.dump("chat_messages")) == 2

    chat_threads.delete_thread(t["id"], user_id="u1")
    assert supabase_mock.dump("chat_threads") == []
    assert supabase_mock.dump("chat_messages") == []

    with pytest.raises(chat_threads.ThreadNotFound):
        chat_threads.get_thread(t["id"], user_id="u1")


# ── auth boundary ─────────────────────────────────────────────────


def test_access_denied_wrong_user() -> None:
    t = chat_threads.create_thread(user_id="u1")
    with pytest.raises(chat_threads.ThreadAccessDenied):
        chat_threads.get_thread(t["id"], user_id="u2")


def test_access_denied_wrong_cart_key() -> None:
    t = chat_threads.create_thread(cart_key="k1")
    with pytest.raises(chat_threads.ThreadAccessDenied):
        chat_threads.list_messages(t["id"], cart_key="k2")


def test_access_denied_no_auth_on_protected() -> None:
    t = chat_threads.create_thread(user_id="u1")
    with pytest.raises(chat_threads.ThreadAccessDenied):
        chat_threads.append_message(t["id"], "user", "hi")


def test_anon_cart_key_can_access_own_thread() -> None:
    t = chat_threads.create_thread(cart_key="k1")
    chat_threads.append_message(t["id"], "user", "hi", cart_key="k1")
    msgs = chat_threads.list_messages(t["id"], cart_key="k1")
    assert len(msgs) == 1


def test_thread_not_found() -> None:
    with pytest.raises(chat_threads.ThreadNotFound):
        chat_threads.get_thread("00000000-0000-0000-0000-000000000000", user_id="u1")
