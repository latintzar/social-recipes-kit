"""ACP checkout tool tests — UUID-session edition.

Exercises the DB-backed (:mod:`checkout`) + state-machine-aware
(:mod:`checkout_handlers`) flow using the in-memory Supabase mock from
``fixtures/supabase_mock.py`` and a stub Stripe client. Covers the
legacy ``{shop_id, items}`` back-compat path (which mints a cart on the
fly), UUID session ids, stock race, state transitions including the
new ``requires_escalation`` step, and the ACP error envelope.

The HMAC round-trip tests from the pre-A3 stateless era are gone — the
session id is now a plain UUID pointing at a ``checkout_sessions``
row, and there is nothing to forge.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

_HERE = Path(__file__).resolve()
_TESTS_DIR = _HERE.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import checkout  # noqa: E402 — sys.path set up by conftest
import checkout_handlers  # noqa: E402
import snapshots  # noqa: E402
import tools  # noqa: E402
from fixtures import supabase_mock  # noqa: E402

ACP_VERSION = "2026-04-17"


class _FakeStripeSessions:
    def __init__(self, url: str = "https://checkout.stripe.com/test/pay/cs_test_123") -> None:
        self._url = url
        self.last_params: dict[str, Any] | None = None
        self.call_count = 0

    def create(self, params: dict[str, Any]) -> dict[str, Any]:
        self.last_params = params
        self.call_count += 1
        cid = f"cs_test_{self.call_count:03d}"
        out: dict[str, Any] = {"id": cid, "url": self._url}
        if params.get("ui_mode") == checkout.STRIPE_UI_MODE_EMBEDDED_PAGE:
            out["client_secret"] = f"{cid}_secret"
        return out


class _FakeStripeClient:
    def __init__(self) -> None:
        self.checkout = type("Ns", (), {"sessions": _FakeStripeSessions()})()


@pytest.fixture(autouse=True)
def _fresh_ledger() -> None:
    """Each test gets a clean snapshots ledger + Supabase mock."""
    snapshots.reset_for_tests()
    supabase_mock.reset()
    yield
    snapshots.reset_for_tests()
    supabase_mock.reset()


@pytest.fixture
def fake_sb(monkeypatch: pytest.MonkeyPatch) -> supabase_mock.FakeSupabase:
    """Wire the Supabase seam for :mod:`checkout` + seed a minimal shop row.

    :mod:`checkout` and :mod:`checkout_handlers` both call
    ``checkout._get_supabase``; monkeypatching that one function covers
    every DB hit.
    """
    fake = supabase_mock.FakeSupabase()
    monkeypatch.setattr(checkout, "_get_supabase", lambda: fake)
    return fake


@pytest.fixture
def fake_stripe(monkeypatch: pytest.MonkeyPatch) -> _FakeStripeClient:
    """Replace the Stripe SDK seam so no real HTTP fires."""
    fake = _FakeStripeClient()
    monkeypatch.setattr(checkout, "_get_stripe_client", lambda: fake)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    return fake


def _make_meta() -> dict[str, str]:
    return {"api_version": ACP_VERSION}


def _create_session(payload: dict[str, Any]) -> dict[str, Any]:
    return tools.dispatch(
        "create_checkout_session", {"meta": _make_meta(), "payload": payload}
    )


def _assert_is_uuid(value: str) -> None:
    uuid.UUID(value)  # raises ValueError if not a valid uuid


# ── legacy back-compat: shop_id + items ────────────────────────────────


def test_legacy_create_returns_uuid_session(fake_sb, fake_stripe) -> None:
    body = _create_session(
        {
            "shop_id": "mandra-rodopa",
            "items": [{"id": "MR-KASH-500", "quantity": 2}],
            "buyer": {"email": "shopper@example.com"},
        }
    )
    _assert_is_uuid(body["id"])
    # Status flipped to in_progress once stripe link was stamped.
    assert body["status"] == "in_progress"
    assert body["_acp_spec_version"] == ACP_VERSION
    assert body["currency"] == "EUR"
    assert len(body["fulfillment_options"]) == 1
    pickup = body["fulfillment_options"][0]
    assert pickup["type"] == "pickup"
    assert pickup["pickup_type"] == "in_store"
    assert len(pickup["collection_code"]) == 6
    assert fake_stripe.checkout.sessions.last_params is not None
    assert fake_stripe.checkout.sessions.last_params["ui_mode"] == checkout.STRIPE_UI_MODE_EMBEDDED_PAGE
    assert body["payment_link"]["mode"] == checkout.STRIPE_UI_MODE_EMBEDDED_PAGE
    assert body["payment_link"]["client_secret"]
    assert body["client_secret"] == body["payment_link"]["client_secret"]
    assert fake_stripe.checkout.sessions.last_params["metadata"]["vitrina_session_id"] == body["id"]
    # Cart row + line items were minted on the fly.
    carts = supabase_mock.dump("carts")
    assert len(carts) == 1
    lines = supabase_mock.dump("cart_line_items")
    assert len(lines) == 1
    assert lines[0]["product_id"] == "MR-KASH-500"
    assert lines[0]["quantity"] == 2


def test_legacy_complete_flips_status_and_decrements_stock(
    fake_sb, fake_stripe
) -> None:
    body = _create_session(
        {"shop_id": "mandra-rodopa", "items": [{"id": "MR-KASH-500", "quantity": 2}]}
    )
    sid = body["id"]
    completed = tools.dispatch(
        "complete_checkout_session", {"meta": _make_meta(), "id": sid}
    )
    assert completed["status"] == "completed"
    assert completed["_stock_before"]["MR-KASH-500"] == 18
    assert completed["_stock_after"]["MR-KASH-500"] == 16


def test_get_returns_same_view_with_uuid(fake_sb, fake_stripe) -> None:
    body = _create_session(
        {"shop_id": "mandra-rodopa", "items": [{"id": "MR-KASH-500", "quantity": 1}]}
    )
    sid = body["id"]
    _assert_is_uuid(sid)
    refetched = tools.dispatch(
        "get_checkout_session", {"meta": _make_meta(), "id": sid}
    )
    assert refetched["id"] == sid
    assert refetched["fulfillment_options"][0]["collection_code"] == (
        body["fulfillment_options"][0]["collection_code"]
    )


# ── stock race ─────────────────────────────────────────────────────────


def test_stock_race_on_complete_raises_minus_32000(fake_sb, fake_stripe) -> None:
    body = _create_session(
        {"shop_id": "mandra-rodopa", "items": [{"id": "MR-KASH-500", "quantity": 5}]}
    )
    sid = body["id"]
    snapshots.record_leo_sale(
        shop_id="mandra-rodopa", product_id="MR-KASH-500", quantity=18
    )
    with pytest.raises(checkout_handlers.ACPError) as exc:
        tools.dispatch(
            "complete_checkout_session", {"meta": _make_meta(), "id": sid}
        )
    assert exc.value.code == -32000
    assert "Stock changed" in exc.value.message
    assert exc.value.data == {"sku": "MR-KASH-500", "available": 0}


def test_complete_is_idempotent(fake_sb, fake_stripe) -> None:
    body = _create_session(
        {"shop_id": "mandra-rodopa", "items": [{"id": "MR-KASH-500", "quantity": 1}]}
    )
    sid = body["id"]
    tools.dispatch("complete_checkout_session", {"meta": _make_meta(), "id": sid})
    out2 = tools.dispatch(
        "complete_checkout_session", {"meta": _make_meta(), "id": sid}
    )
    assert out2["_idempotent"] is True
    # Only decremented once.
    assert snapshots.get_leo_delta("mandra-rodopa", "MR-KASH-500") == 1


# ── input validation ──────────────────────────────────────────────────


def test_unknown_sku_raises_minus_32602(fake_sb, fake_stripe) -> None:
    with pytest.raises(checkout_handlers.ACPError) as exc:
        _create_session(
            {"shop_id": "mandra-rodopa", "items": [{"id": "does-not-exist", "quantity": 1}]}
        )
    assert exc.value.code == -32602
    assert "unknown sku" in exc.value.message


def test_unknown_shop_raises_minus_32602(fake_sb, fake_stripe) -> None:
    with pytest.raises(checkout_handlers.ACPError) as exc:
        _create_session(
            {"shop_id": "no-such-shop", "items": [{"id": "MR-KASH-500", "quantity": 1}]}
        )
    assert exc.value.code == -32602
    assert "unknown shop_id" in exc.value.message


def test_wrong_api_version_raises_minus_32602(fake_sb, fake_stripe) -> None:
    with pytest.raises(checkout_handlers.ACPError) as exc:
        tools.dispatch(
            "create_checkout_session",
            {
                "meta": {"api_version": "1999-01-01"},
                "payload": {
                    "shop_id": "mandra-rodopa",
                    "items": [{"id": "MR-KASH-500", "quantity": 1}],
                },
            },
        )
    assert exc.value.code == -32602


def test_missing_meta_raises_minus_32602(fake_sb, fake_stripe) -> None:
    with pytest.raises(checkout_handlers.ACPError) as exc:
        tools.dispatch(
            "create_checkout_session",
            {
                "payload": {
                    "shop_id": "mandra-rodopa",
                    "items": [{"id": "MR-KASH-500", "quantity": 1}],
                }
            },
        )
    assert exc.value.code == -32602


def test_missing_session_id_raises_minus_32602(fake_sb, fake_stripe) -> None:
    with pytest.raises(checkout_handlers.ACPError) as exc:
        tools.dispatch(
            "get_checkout_session",
            {"meta": _make_meta(), "id": str(uuid.uuid4())},
        )
    assert exc.value.code == -32602
    assert "not found" in exc.value.message


# ── state transitions ────────────────────────────────────────────────


def test_cancel_flips_status_and_refuses_further_updates(fake_sb, fake_stripe) -> None:
    body = _create_session(
        {"shop_id": "mandra-rodopa", "items": [{"id": "MR-KASH-500", "quantity": 1}]}
    )
    sid = body["id"]
    canceled = tools.dispatch(
        "cancel_checkout_session", {"meta": _make_meta(), "id": sid}
    )
    assert canceled["status"] == "canceled"
    # Complete on a canceled row is a -32000.
    with pytest.raises(checkout_handlers.ACPError) as exc:
        tools.dispatch(
            "complete_checkout_session", {"meta": _make_meta(), "id": sid}
        )
    assert exc.value.code == -32000


def test_invalid_status_transition_raises(fake_sb, fake_stripe) -> None:
    body = _create_session(
        {"shop_id": "mandra-rodopa", "items": [{"id": "MR-KASH-500", "quantity": 1}]}
    )
    sid = body["id"]
    # in_progress → not_ready_for_payment is not allowed.
    with pytest.raises(checkout_handlers.ACPError) as exc:
        tools.dispatch(
            "update_checkout_session",
            {
                "meta": _make_meta(),
                "id": sid,
                "payload": {"status": "not_ready_for_payment"},
            },
        )
    assert exc.value.code == -32000
    assert "invalid_status_transition" in exc.value.message


def test_full_lifecycle_through_requires_escalation(fake_sb, fake_stripe) -> None:
    """in_progress → requires_escalation → in_progress → completed."""
    body = _create_session(
        {"shop_id": "mandra-rodopa", "items": [{"id": "MR-KASH-500", "quantity": 1}]}
    )
    sid = body["id"]
    # Flip to requires_escalation via update.
    out = tools.dispatch(
        "update_checkout_session",
        {
            "meta": _make_meta(),
            "id": sid,
            "payload": {
                "status": "requires_escalation",
                "requires_escalation_url": "https://hooks.stripe.com/3ds/ch_123",
            },
        },
    )
    assert out["status"] == "requires_escalation"
    # Shopper completes 3DS → back to in_progress.
    out = tools.dispatch(
        "update_checkout_session",
        {
            "meta": _make_meta(),
            "id": sid,
            "payload": {"status": "in_progress"},
        },
    )
    assert out["status"] == "in_progress"
    # Stripe webhook fires complete.
    completed = tools.dispatch(
        "complete_checkout_session", {"meta": _make_meta(), "id": sid}
    )
    assert completed["status"] == "completed"


def test_update_fulfillment_option_id(fake_sb, fake_stripe) -> None:
    body = _create_session(
        {"shop_id": "mandra-rodopa", "items": [{"id": "MR-KASH-500", "quantity": 1}]}
    )
    sid = body["id"]
    out = tools.dispatch(
        "update_checkout_session",
        {
            "meta": _make_meta(),
            "id": sid,
            "payload": {"fulfillment_option_id": "pickup_in_store"},
        },
    )
    assert out["fulfillment_option_id"] == "pickup_in_store"


# ── reset_demo_stock ──────────────────────────────────────────────────


def test_reset_demo_stock_clears_all_deltas(fake_sb, fake_stripe) -> None:
    snapshots.record_leo_sale(
        shop_id="mandra-rodopa", product_id="MR-KASH-500", quantity=3
    )
    assert snapshots.get_leo_delta("mandra-rodopa", "MR-KASH-500") == 3
    out = tools.dispatch("reset_demo_stock")
    assert out["ok"] is True
    assert "mandra-rodopa" in out["wiped"]
    assert snapshots.get_leo_delta("mandra-rodopa", "MR-KASH-500") == 0
