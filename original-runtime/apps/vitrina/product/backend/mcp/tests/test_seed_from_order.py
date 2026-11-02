"""Tests for :func:`chat_threads.seed_thread_from_order`.

Seeds an orders row in the fake Supabase backend, calls
``seed_thread_from_order``, and asserts the new thread carries the
expected seed messages (one user prompt + one tool_result with line_items).
"""
from __future__ import annotations

import pytest

import chat_threads
from fixtures import supabase_mock


@pytest.fixture(autouse=True)
def _fresh_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    supabase_mock.reset()
    fake = supabase_mock.FakeSupabase()
    monkeypatch.setattr(chat_threads, "_get_supabase", lambda: fake)
    yield
    supabase_mock.reset()


ORDER_ID = "11111111-1111-1111-1111-111111111111"
SHOP_ID = "shop-sofia-01"
LINE_ITEMS = [
    {"id": "sku-apple", "quantity": 3, "total": 750},
    {"id": "sku-bread", "quantity": 1, "total": 290},
]


def _seed_order() -> None:
    supabase_mock.seed(
        "orders",
        [
            {
                "id": ORDER_ID,
                "shop_id": SHOP_ID,
                "line_items": LINE_ITEMS,
            }
        ],
    )
    supabase_mock.seed(
        "shops",
        [{"id": SHOP_ID, "name_en": "Corner Market", "name_bg": "Бакалия"}],
    )


def test_seed_creates_thread_with_source_order() -> None:
    _seed_order()
    thread = chat_threads.seed_thread_from_order(ORDER_ID, user_id="u1")

    assert thread["seeded_from_order_id"] == ORDER_ID
    assert thread["user_id"] == "u1"
    assert thread["title"].startswith("Repeat order")


def test_seed_plants_two_seed_messages() -> None:
    _seed_order()
    thread = chat_threads.seed_thread_from_order(ORDER_ID, user_id="u1")

    msgs = chat_threads.list_messages(thread["id"], user_id="u1")
    assert len(msgs) == 2

    user_msg, tool_msg = msgs
    assert user_msg["role"] == "user"
    assert user_msg["content"]["kind"] == "text"
    assert "Corner Market" in user_msg["content"]["text"]
    assert "2 items" in user_msg["content"]["text"]

    assert tool_msg["role"] == "tool"
    assert tool_msg["content"]["kind"] == "tool_result"
    assert tool_msg["content"]["tool"] == "seed_from_order"
    assert tool_msg["content"]["order_id"] == ORDER_ID
    assert tool_msg["content"]["line_items"] == LINE_ITEMS


def test_seed_with_anon_cart_key() -> None:
    _seed_order()
    thread = chat_threads.seed_thread_from_order(ORDER_ID, cart_key="k-anon")
    assert thread["cart_key"] == "k-anon"
    assert thread["user_id"] is None
    # access via cart_key works
    msgs = chat_threads.list_messages(thread["id"], cart_key="k-anon")
    assert len(msgs) == 2


def test_seed_missing_order_raises() -> None:
    with pytest.raises(chat_threads.ThreadNotFound):
        chat_threads.seed_thread_from_order("does-not-exist", user_id="u1")


def test_seed_singular_item_grammar() -> None:
    supabase_mock.seed(
        "orders",
        [{"id": ORDER_ID, "shop_id": SHOP_ID, "line_items": [{"id": "x", "quantity": 1}]}],
    )
    supabase_mock.seed("shops", [{"id": SHOP_ID, "name_en": "Corner Market"}])
    thread = chat_threads.seed_thread_from_order(ORDER_ID, user_id="u1")
    msgs = chat_threads.list_messages(thread["id"], user_id="u1")
    assert "1 item from" in msgs[0]["content"]["text"]
