"""Multi-shop checkout group tests.

Covers the Phase 1 UCP extension where one Stripe Checkout Session
carries line items from N independent ``checkout_sessions`` rows
sharing a ``group_id``:

* create with ``{cart_ids: [...]}`` → group_id + N session rows + 1
  Stripe URL.
* any cart's shop closed → ``shop_closed`` error, no rows written.
* complete-group with a stock-race on any session → atomic rollback
  (every session canceled, no orders minted).
* complete-group all-good → every session flips to ``completed`` with
  the shared ``stripe_session_id``.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_HERE = Path(__file__).resolve()
_TESTS_DIR = _HERE.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import checkout  # noqa: E402
import checkout_handlers  # noqa: E402
import shop_hours  # noqa: E402
import snapshots  # noqa: E402
import tools  # noqa: E402
from fixtures import supabase_mock  # noqa: E402

ACP_VERSION = "2026-04-17"


ALWAYS_OPEN = {
    "mon": [["00:00", "23:59"]],
    "tue": [["00:00", "23:59"]],
    "wed": [["00:00", "23:59"]],
    "thu": [["00:00", "23:59"]],
    "fri": [["00:00", "23:59"]],
    "sat": [["00:00", "23:59"]],
    "sun": [["00:00", "23:59"]],
}
ALWAYS_CLOSED = {d: [] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


class _FakeStripeSessions:
    def __init__(self) -> None:
        self.last_params: dict[str, Any] | None = None
        self.calls = 0

    def create(self, params: dict[str, Any]) -> dict[str, Any]:
        self.last_params = params
        self.calls += 1
        cid = f"cs_test_{self.calls:03d}"
        out: dict[str, Any] = {
            "id": cid,
            "url": f"https://checkout.stripe.com/test/pay/{cid}",
        }
        if params.get("ui_mode") == checkout.STRIPE_UI_MODE_EMBEDDED_PAGE:
            out["client_secret"] = f"{cid}_secret"
        return out


class _FakeStripePaymentIntents:
    def retrieve(self, payment_intent_id: str) -> dict[str, Any]:
        return {"id": payment_intent_id, "latest_charge": "ch_test_123"}


class _FakeStripeTransfers:
    def __init__(self) -> None:
        self.last_params: dict[str, Any] | None = None
        self.calls = 0

    def create(self, params: dict[str, Any]) -> dict[str, Any]:
        self.last_params = params
        self.calls += 1
        return {"id": f"tr_test_{self.calls:03d}"}


class _FakeStripeClient:
    def __init__(self) -> None:
        self.checkout = type("Ns", (), {"sessions": _FakeStripeSessions()})()
        self.payment_intents = _FakeStripePaymentIntents()
        self.transfers = _FakeStripeTransfers()


@pytest.fixture(autouse=True)
def _reset_all(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshots.reset_for_tests()
    supabase_mock.reset()
    fake = supabase_mock.FakeSupabase()
    # checkout + shop_hours both reach for supabase; same mock backend.
    monkeypatch.setattr(checkout, "_get_supabase", lambda: fake)
    monkeypatch.setattr(shop_hours, "get_supabase", lambda: fake)
    fake_stripe = _FakeStripeClient()
    monkeypatch.setattr(checkout, "_get_stripe_client", lambda: fake_stripe)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    pytest.__fake_stripe__ = fake_stripe  # type: ignore[attr-defined]
    yield
    snapshots.reset_for_tests()
    supabase_mock.reset()


def _seed_cart(
    *,
    shop_id: str,
    cart_id: str,
    product_id: str,
    quantity: int,
    price_eur: float,
) -> None:
    supabase_mock.seed(
        "carts",
        [
            {
                "id": cart_id,
                "shop_id": shop_id,
                "cart_key": "k",
                "user_id": None,
                "currency": "EUR",
                "status": "open",
            }
        ],
    )
    unit_minor = int(round(price_eur * 100))
    supabase_mock.seed(
        "cart_line_items",
        [
            {
                "cart_id": cart_id,
                "product_id": product_id,
                "quantity": quantity,
                "base_amount_minor": unit_minor * quantity,
                "subtotal_minor": unit_minor * quantity,
                "total_minor": unit_minor * quantity,
                "tax_minor": 0,
                "product_snapshot": {
                    "shop_id": shop_id,
                    "name": product_id,
                    "price_eur": price_eur,
                    "price_minor": unit_minor,
                },
            }
        ],
    )


def _seed_shop(shop_id: str, opening_hours: dict) -> None:
    supabase_mock.seed(
        "shops",
        [
            {
                "id": shop_id,
                "name": shop_id,
                "timezone": "UTC",
                "opening_hours": opening_hours,
            }
        ],
    )


def _make_meta() -> dict[str, str]:
    return {"api_version": ACP_VERSION}


# ── happy path: 2 open shops → group + 2 sessions + 1 Stripe URL ──────


def test_multishop_create_returns_group_and_one_stripe_url() -> None:
    _seed_shop("mandra-rodopa", ALWAYS_OPEN)
    _seed_shop("mesarnitsa-diado-ivan", ALWAYS_OPEN)
    _seed_cart(
        shop_id="mandra-rodopa",
        cart_id="c1",
        product_id="MR-KASH-500",
        quantity=1,
        price_eur=12.0,
    )
    _seed_cart(
        shop_id="mesarnitsa-diado-ivan",
        cart_id="c2",
        product_id="BDI-LUK-250",
        quantity=1,
        price_eur=8.0,
    )

    body = tools.dispatch(
        "create_checkout_session",
        {"meta": _make_meta(), "payload": {"cart_ids": ["c1", "c2"]}},
    )
    assert "group_id" in body
    assert len(body["sessions"]) == 2
    pl = body.get("payment_link") or {}
    assert pl.get("stripe_session_id") == "cs_test_001"
    assert pl.get("mode") == checkout.STRIPE_UI_MODE_EMBEDDED_PAGE
    # One Stripe session across both shops.
    assert pytest.__fake_stripe__.checkout.sessions.calls == 1  # type: ignore[attr-defined]
    params = pytest.__fake_stripe__.checkout.sessions.last_params  # type: ignore[attr-defined]
    assert params["ui_mode"] == checkout.STRIPE_UI_MODE_EMBEDDED_PAGE
    assert params["payment_intent_data"]["transfer_group"].startswith("vitrina_group_")
    assert params["metadata"]["transfer_group"] == params["payment_intent_data"]["transfer_group"]
    assert len(params["line_items"]) == 2
    for sess in body["sessions"]:
        assert sess["status"] == "in_progress"
        assert sess["group_id"] == body["group_id"]
    rows = supabase_mock.dump("checkout_sessions")
    assert len(rows) == 2
    assert {r["group_id"] for r in rows} == {body["group_id"]}
    assert {r["stripe_session_id"] for r in rows} == {"cs_test_001"}


def test_store_transfer_sends_net_to_connected_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VITRINA_PLATFORM_FEE_BPS", "1000")
    _seed_shop("mandra-rodopa", ALWAYS_OPEN)
    supabase_mock.seed(
        "shops",
        [
            {
                "id": "connected-shop",
                "name": "Connected Shop",
                "timezone": "UTC",
                "opening_hours": ALWAYS_OPEN,
                "stripe_connect_account_id": "acct_store_123",
                "platform_fee_bps": None,
            }
        ],
    )
    _seed_cart(
        shop_id="connected-shop",
        cart_id="c-connect",
        product_id="SKU-1",
        quantity=2,
        price_eur=10.0,
    )
    order = {
        "id": "order-1",
        "checkout_session_id": "session-1",
        "group_id": "group-1",
        "cart_id": "c-connect",
        "shop_id": "connected-shop",
    }

    payout = checkout.create_store_transfer_for_order(
        order,
        payment_intent_id="pi_test_123",
        transfer_group="vitrina_group_group-1",
    )

    assert payout["payout_status"] == "transferred"
    assert payout["stripe_transfer_id"] == "tr_test_001"
    # 2 × 10 EUR = 20 EUR gross → 10% platform fee → 18 EUR net to the shop.
    # (Pre-2026 the BGN→EUR peg halved the gross at this step; obsolete now
    # that all shops are priced in EUR end-to-end.)
    assert payout["gross_amount_minor"] == 2000
    assert payout["platform_fee_minor"] == 200
    assert payout["payout_amount_minor"] == 1800
    params = pytest.__fake_stripe__.transfers.last_params  # type: ignore[attr-defined]
    assert params["destination"] == "acct_store_123"
    assert params["source_transaction"] == "ch_test_123"
    assert params["transfer_group"] == "vitrina_group_group-1"


# ── closed shop → shop_closed error ──────────────────────────────────


def test_multishop_refuses_when_any_shop_closed() -> None:
    _seed_shop("mandra-rodopa", ALWAYS_OPEN)
    _seed_shop("mesarnitsa-diado-ivan", ALWAYS_CLOSED)
    _seed_cart(
        shop_id="mandra-rodopa",
        cart_id="c1",
        product_id="MR-KASH-500",
        quantity=1,
        price_eur=12.0,
    )
    _seed_cart(
        shop_id="mesarnitsa-diado-ivan",
        cart_id="c2",
        product_id="BDI-LUK-250",
        quantity=1,
        price_eur=8.0,
    )

    with pytest.raises(checkout_handlers.ACPError) as exc:
        tools.dispatch(
            "create_checkout_session",
            {"meta": _make_meta(), "payload": {"cart_ids": ["c1", "c2"]}},
        )
    assert exc.value.code == -32000
    assert exc.value.message == "shop_closed"
    closed = (exc.value.data or {}).get("closed_carts") or []
    assert len(closed) == 1
    assert closed[0]["cart_id"] == "c2"
    # No sessions should have been written.
    assert supabase_mock.dump("checkout_sessions") == []


# ── group complete: stock race → atomic rollback ────────────────────


def test_group_complete_rolls_back_on_stock_race() -> None:
    _seed_shop("mandra-rodopa", ALWAYS_OPEN)
    _seed_shop("mesarnitsa-diado-ivan", ALWAYS_OPEN)
    _seed_cart(
        shop_id="mandra-rodopa",
        cart_id="c1",
        product_id="MR-KASH-500",
        quantity=1,
        price_eur=12.0,
    )
    _seed_cart(
        shop_id="mesarnitsa-diado-ivan",
        cart_id="c2",
        product_id="BDI-LUK-250",
        quantity=1,
        price_eur=8.0,
    )
    body = tools.dispatch(
        "create_checkout_session",
        {"meta": _make_meta(), "payload": {"cart_ids": ["c1", "c2"]}},
    )
    group_id = body["group_id"]
    # Drain the shelf from under c2.
    snapshots.record_leo_sale(
        shop_id="mesarnitsa-diado-ivan",
        product_id="BDI-LUK-250",
        quantity=99,
    )

    with pytest.raises(checkout_handlers.ACPError) as exc:
        checkout_handlers.handle_complete_checkout_group(
            meta=_make_meta(), group_id=group_id
        )
    assert exc.value.code == -32000
    assert exc.value.message == "stock_race_rolled_back"

    # Both sessions canceled, none completed.
    rows = supabase_mock.dump("checkout_sessions")
    assert len(rows) == 2
    assert {r["status"] for r in rows} == {"canceled"}
    # Stock for c1's sku untouched — atomic rollback means no decrement.
    assert snapshots.get_leo_delta("mandra-rodopa", "MR-KASH-500") == 0


# ── group complete: happy path ───────────────────────────────────────


def test_group_complete_success_flips_all_sessions() -> None:
    _seed_shop("mandra-rodopa", ALWAYS_OPEN)
    _seed_shop("mesarnitsa-diado-ivan", ALWAYS_OPEN)
    _seed_cart(
        shop_id="mandra-rodopa",
        cart_id="c1",
        product_id="MR-KASH-500",
        quantity=1,
        price_eur=12.0,
    )
    _seed_cart(
        shop_id="mesarnitsa-diado-ivan",
        cart_id="c2",
        product_id="BDI-LUK-250",
        quantity=1,
        price_eur=8.0,
    )
    body = tools.dispatch(
        "create_checkout_session",
        {"meta": _make_meta(), "payload": {"cart_ids": ["c1", "c2"]}},
    )
    group_id = body["group_id"]
    stripe_sid = {r["stripe_session_id"] for r in supabase_mock.dump("checkout_sessions")}
    assert len(stripe_sid) == 1  # shared across both

    out = checkout_handlers.handle_complete_checkout_group(
        meta=_make_meta(), group_id=group_id
    )
    assert out["group_id"] == group_id
    assert len(out["results"]) == 2
    assert {r["result"] for r in out["results"]} == {"completed"}
    rows = supabase_mock.dump("checkout_sessions")
    assert {r["status"] for r in rows} == {"completed"}
    # Stock decremented for both.
    assert snapshots.get_leo_delta("mandra-rodopa", "MR-KASH-500") == 1
    assert snapshots.get_leo_delta("mesarnitsa-diado-ivan", "BDI-LUK-250") == 1


# ── hosted checkout link + thread email ───────────────────────────────


def test_checkout_create_link_prefills_stripe_email_from_thread_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Magic-link path: mints checkout_tokens row + vitrina.bg/checkout URL (Stripe deferred to web)."""
    import chat_threads  # noqa: PLC0415
    import checkout_tokens  # noqa: PLC0415
    import supabase_client  # noqa: PLC0415

    snapshots.reset_for_tests()
    supabase_mock.reset()
    fake = supabase_mock.FakeSupabase()
    monkeypatch.setattr(checkout, "_get_supabase", lambda: fake)
    monkeypatch.setattr(shop_hours, "get_supabase", lambda: fake)
    monkeypatch.setattr(chat_threads, "_get_supabase", lambda: fake)
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: fake)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)

    _seed_shop("mandra-rodopa", ALWAYS_OPEN)
    _seed_cart(
        shop_id="mandra-rodopa",
        cart_id="c1",
        product_id="MR-KASH-500",
        quantity=1,
        price_eur=12.0,
    )
    t = chat_threads.create_thread(cart_key="ig:psid1")
    chat_threads.patch_thread_metadata(
        t["id"],
        {"email": "IG_SHOPPER@EXAMPLE.ORG "},
        cart_key="ig:psid1",
    )

    out = tools.dispatch(
        "checkout.create_link",
        {
            "meta": _make_meta(),
            "payload": {"cart_ids": ["c1"]},
            "thread_id": t["id"],
            "cart_key": "ig:psid1",
        },
    )
    assert out["ok"] is True
    assert "/checkout?s=" in out["url"]
    assert out["url"].startswith("https://")
    assert out["expires_in"] == int(checkout_tokens.CHECKOUT_TOKEN_TTL.total_seconds())
    rows = supabase_mock.dump("checkout_tokens")
    assert len(rows) == 1
    assert rows[0]["cart_ids"] == ["c1"]
    assert rows[0]["thread_id"] == t["id"]
