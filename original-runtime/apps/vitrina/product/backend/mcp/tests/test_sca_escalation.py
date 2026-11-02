"""Strong-Customer-Authentication (3DS) escalation tests.

Covers the UCP 6-state lifecycle's ``requires_escalation`` branch:

* ``in_progress`` session receives a ``payment_intent.requires_action``
  webhook → session flips to ``requires_escalation`` with the
  redirect URL stashed.
* Shopper finishes 3DS → session moves back through ``in_progress`` →
  ``completed``.
* Alternative path: card declines after challenge → session resets to
  ``ready_for_payment`` and ``stripe_session_id`` is cleared (matches
  A8's ``payment_intent.payment_failed`` handler).

The Stripe webhook itself is owned by A8 (``api/routes/webhooks.py``);
here we simulate the state updates the webhook would perform, then
assert the state machine + ``build_session_view`` round-trip.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_HERE = Path(__file__).resolve()
_TESTS_DIR = _HERE.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import checkout  # noqa: E402
import checkout_handlers  # noqa: E402
import snapshots  # noqa: E402
import tools  # noqa: E402
from fixtures import supabase_mock  # noqa: E402

ACP_VERSION = "2026-04-17"


class _FakeStripeSessions:
    def create(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"id": "cs_test_sca", "url": "https://checkout.stripe.com/test/pay/cs_test_sca"}


class _FakeStripeClient:
    def __init__(self) -> None:
        self.checkout = type("Ns", (), {"sessions": _FakeStripeSessions()})()


@pytest.fixture(autouse=True)
def _setup(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshots.reset_for_tests()
    supabase_mock.reset()
    fake_sb = supabase_mock.FakeSupabase()
    monkeypatch.setattr(checkout, "_get_supabase", lambda: fake_sb)
    monkeypatch.setattr(checkout, "_get_stripe_client", lambda: _FakeStripeClient())
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    yield
    snapshots.reset_for_tests()
    supabase_mock.reset()


def _make_meta() -> dict[str, str]:
    return {"api_version": ACP_VERSION}


def _create_in_progress_session() -> str:
    body = tools.dispatch(
        "create_checkout_session",
        {
            "meta": _make_meta(),
            "payload": {
                "shop_id": "mandra-rodopa",
                "items": [{"id": "MR-KASH-500", "quantity": 1}],
            },
        },
    )
    assert body["status"] == "in_progress"
    return body["id"]


# ── requires_action → requires_escalation ───────────────────────────


def test_requires_action_webhook_flips_session_to_escalation() -> None:
    sid = _create_in_progress_session()
    redirect = "https://hooks.stripe.com/3d_secure_2/ch_abc"
    # Simulate the webhook patching the row.
    checkout.db_update_checkout_session(
        sid,
        {
            "status": "requires_escalation",
            "requires_escalation_url": redirect,
        },
    )
    view = tools.dispatch(
        "get_checkout_session", {"meta": _make_meta(), "id": sid}
    )
    assert view["status"] == "requires_escalation"
    assert view["requires_escalation_url"] == redirect


# ── requires_escalation → in_progress → completed ───────────────────


def test_resolve_escalation_can_complete_session() -> None:
    sid = _create_in_progress_session()
    checkout.db_update_checkout_session(
        sid,
        {"status": "requires_escalation",
         "requires_escalation_url": "https://hooks.stripe.com/3d_secure_2/ch_abc"},
    )
    # Shopper completes 3DS — resolve back to in_progress.
    tools.dispatch(
        "update_checkout_session",
        {"meta": _make_meta(), "id": sid, "payload": {"status": "in_progress"}},
    )
    completed = tools.dispatch(
        "complete_checkout_session", {"meta": _make_meta(), "id": sid}
    )
    assert completed["status"] == "completed"


# ── requires_escalation → ready_for_payment (card declined) ─────────


def test_payment_failed_webhook_resets_session() -> None:
    sid = _create_in_progress_session()
    checkout.db_update_checkout_session(
        sid,
        {"status": "requires_escalation"},
    )
    # Simulate A8's payment_failed handler: ready_for_payment + clear sid.
    checkout.db_update_checkout_session(
        sid,
        {"status": "ready_for_payment", "stripe_session_id": None},
    )
    row = checkout.db_get_checkout_session(sid)
    assert row is not None
    assert row["status"] == "ready_for_payment"
    assert row.get("stripe_session_id") in (None, "")
