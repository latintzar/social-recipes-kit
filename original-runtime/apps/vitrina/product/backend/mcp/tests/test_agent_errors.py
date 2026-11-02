"""Tests for the agent_errors ticket queue + the capture_tool_errors decorator.

Uses the shared :class:`fixtures.supabase_mock.FakeSupabase` backend so
no HTTP is issued. The ``agent_errors`` table isn't in
``_DEFAULT_COLS``, but insert-defaults (``id``/``created_at``) are
applied for every table by the mock, which is all we need.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the sibling ``agent/`` package importable (the mcp conftest adds
# ``mcp/`` to sys.path; we need the backend root for ``agent.*``).
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

import agent_errors  # noqa: E402 — added by conftest
from fixtures.supabase_mock import FakeSupabase, reset  # noqa: E402

from agent import error_handling  # noqa: E402


@pytest.fixture(autouse=True)
def _fake_supabase(monkeypatch: pytest.MonkeyPatch) -> FakeSupabase:
    """Swap the real supabase client for the in-memory fake in both modules."""
    reset()
    fake = FakeSupabase()
    monkeypatch.setattr(agent_errors, "get_supabase", lambda: fake)
    return fake


# ── create_error / get_error ───────────────────────────────────────


def test_create_error_persists_row() -> None:
    row = agent_errors.create_error(
        tool_name="add_item",
        args={"sku": "abc", "qty": 2},
        error_message="stock race",
        stack="Traceback (most recent call last):\n  ...\nValueError: x",
        thread_id="11111111-1111-1111-1111-111111111111",
        user_id="22222222-2222-2222-2222-222222222222",
        cart_key="cart_xyz",
        cart_id="33333333-3333-3333-3333-333333333333",
        recoverable=True,
    )
    assert row["id"]
    assert row["tool_name"] == "add_item"
    assert row["args"] == {"sku": "abc", "qty": 2}
    assert row["recoverable"] is True
    # And it round-trips through get_error.
    fetched = agent_errors.get_error(row["id"])
    assert fetched["error_message"] == "stock race"


def test_get_error_missing_raises() -> None:
    with pytest.raises(LookupError):
        agent_errors.get_error("00000000-0000-0000-0000-000000000000")


# ── list_user_errors / list_unresolved ─────────────────────────────


def test_list_user_errors_filters_by_resolved_flag(_fake_supabase: FakeSupabase) -> None:
    user_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    open1 = agent_errors.create_error(
        tool_name="add_item", args={}, error_message="boom1", user_id=user_id
    )
    open2 = agent_errors.create_error(
        tool_name="checkout", args={}, error_message="boom2", user_id=user_id
    )
    closed = agent_errors.create_error(
        tool_name="add_item", args={}, error_message="boom3", user_id=user_id
    )
    agent_errors.mark_resolved(
        closed["id"], resolved_by=user_id, resolution_note="ok"
    )

    open_only = agent_errors.list_user_errors(user_id, resolved=False)
    open_ids = {r["id"] for r in open_only}
    assert open_ids == {open1["id"], open2["id"]}

    closed_only = agent_errors.list_user_errors(user_id, resolved=True)
    assert [r["id"] for r in closed_only] == [closed["id"]]

    all_rows = agent_errors.list_user_errors(user_id, resolved=None)
    assert len(all_rows) == 3


def test_list_unresolved_excludes_resolved() -> None:
    a = agent_errors.create_error(
        tool_name="add_item", args={}, error_message="a"
    )
    b = agent_errors.create_error(
        tool_name="add_item", args={}, error_message="b"
    )
    agent_errors.mark_resolved(
        a["id"],
        resolved_by="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        resolution_note="fixed",
    )
    out = agent_errors.list_unresolved()
    assert [r["id"] for r in out] == [b["id"]]


# ── mark_resolved ──────────────────────────────────────────────────


def test_mark_resolved_sets_all_fields() -> None:
    row = agent_errors.create_error(
        tool_name="add_item", args={}, error_message="boom"
    )
    operator = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    closed = agent_errors.mark_resolved(
        row["id"], resolved_by=operator, resolution_note="fixed race"
    )
    assert closed["resolved_by"] == operator
    assert closed["resolution_note"] == "fixed race"
    assert closed["resolved_at"] is not None


# ── capture_tool_errors decorator ──────────────────────────────────


def test_decorator_success_passthrough() -> None:
    @error_handling.capture_tool_errors("add_item")
    def add_item(*, sku: str, qty: int) -> dict:
        return {"ok": True, "sku": sku, "qty": qty}

    assert add_item(sku="x", qty=3) == {"ok": True, "sku": "x", "qty": 3}


def test_decorator_failure_files_ticket_and_raises() -> None:
    @error_handling.capture_tool_errors("add_item")
    def add_item(*, sku: str, user_id: str, thread_id: str) -> dict:
        raise ValueError("stock race")

    with pytest.raises(error_handling.ToolCallFailed) as ei:
        add_item(
            sku="x",
            user_id="22222222-2222-2222-2222-222222222222",
            thread_id="11111111-1111-1111-1111-111111111111",
        )

    failure = ei.value
    assert failure.recoverable is True
    assert failure.ticket_id
    assert failure.ticket_id in failure.friendly_message
    assert "Couldn't add that item" in failure.friendly_message

    row = agent_errors.get_error(failure.ticket_id)
    assert row["tool_name"] == "add_item"
    assert row["error_message"] == "stock race"
    assert row["user_id"] == "22222222-2222-2222-2222-222222222222"
    assert row["thread_id"] == "11111111-1111-1111-1111-111111111111"
    assert row["args"]["sku"] == "x"
    assert "ValueError: stock race" in (row.get("stack") or "")


def test_decorator_recoverable_override_via_kwarg() -> None:
    @error_handling.capture_tool_errors("complete_checkout", recoverable=True)
    def complete(*, cart_id: str) -> dict:
        raise RuntimeError("stripe down")

    with pytest.raises(error_handling.ToolCallFailed) as ei:
        complete(
            cart_id="33333333-3333-3333-3333-333333333333",
            _recoverable=False,
        )
    assert ei.value.recoverable is False
    row = agent_errors.get_error(ei.value.ticket_id)
    assert row["recoverable"] is False


def test_decorator_does_not_double_wrap_toolcallfailed() -> None:
    inner = error_handling.ToolCallFailed("fixed-id", "already logged", True)

    @error_handling.capture_tool_errors("add_item")
    def reraise() -> None:
        raise inner

    with pytest.raises(error_handling.ToolCallFailed) as ei:
        reraise()
    assert ei.value is inner


def test_friendly_message_fallback() -> None:
    msg = error_handling.friendly_message_for("unknown_tool", RuntimeError("x"))
    assert "{id}" in msg
    assert "logged" in msg


# ── Slack trigger contract ─────────────────────────────────────────
#
# The actual Slack webhook fires from a Postgres trigger
# (``trg_agent_errors_slack``) defined in the 001 migration. We can't
# exercise it in-process, but we *can* guard against someone silently
# dropping it from the SQL — a regression that would turn off Peter's
# pager.


def test_slack_trigger_requires_live_pg() -> None:
    """Migration 001 must declare ``create trigger trg_agent_errors_slack``.

    This is an offline grep against the SQL file — not a Postgres round
    trip. The real trigger behaviour (pg_net POST to the Slack
    incoming-webhook URL stored in ``app_settings``) is only testable
    against a live Postgres with pg_net installed; that lives in the
    integration suite, not here.
    """
    migration = (
        Path(__file__).resolve().parents[2]
        / "api"
        / "migrations"
        / "001_commerce_tables.sql"
    )
    assert migration.exists(), f"missing migration: {migration}"
    sql = migration.read_text(encoding="utf-8")
    assert "create trigger trg_agent_errors_slack" in sql
    assert "slack_agent_errors_webhook" in sql


# ── MCP tool descriptors ───────────────────────────────────────────


def test_agent_error_tools_descriptors_have_required_shape() -> None:
    names = {t["name"] for t in agent_errors.AGENT_ERROR_TOOLS}
    assert names == {
        "agent_errors.create",
        "agent_errors.get",
        "agent_errors.list_user",
        "agent_errors.list_unresolved",
        "agent_errors.resolve",
    }
    for tool in agent_errors.AGENT_ERROR_TOOLS:
        assert "description" in tool and tool["description"]
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert "properties" in schema


# silence unused-import warnings for the autouse fixture
_ = os
