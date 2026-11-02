"""Session bridge — translates the frontend payload shape into Hermes
``run_conversation`` inputs (system message, conversation history,
session metadata) without touching the network.

Post-rebuild (plan ``stock-hermes vitrina rebuild``):

- ``system_message`` is the raw SKILL.md body — same every turn so the
  prompt cache hits.
- The channel hint and any other dynamic state live in
  ``ephemeral_system_prompt`` instead.
- Memory and basket blocks are gone — Hermes' configured memory
  provider handles recall, and the model fetches ``basket.snapshot``
  on demand.
"""

from __future__ import annotations

import session_bridge


def test_build_conversation_history_splits_off_live_user_turn() -> None:
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "live"},
    ]
    history, user_text = session_bridge.build_conversation_history(msgs)
    assert user_text == "live"
    assert history == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
    ]


def test_session_metadata_includes_all_fields() -> None:
    meta = session_bridge.build_session_metadata(
        channel="web",
        thread_id="th_1",
        user_id="u_1",
        cart_key="ck",
        cart_ids=["c1"],
        user_location={"lat": 42.0, "lng": 23.3},
    )
    assert meta == {
        "channel": "web",
        "thread_id": "th_1",
        "user_id": "u_1",
        "cart_key": "ck",
        "cart_ids": ["c1"],
        "user_location": {"lat": 42.0, "lng": 23.3},
    }


def test_build_turn_separates_static_and_ephemeral() -> None:
    turn = session_bridge.build_turn(
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hey"},
            {"role": "user", "content": "what's in my basket?"},
        ],
        channel="web",
        thread_id="th_1",
        user_id=None,
        cart_key=None,
        cart_ids=None,
        user_location=None,
        ephemeral_extra="## Extra\n<foo>bar</foo>",
    )
    assert turn.user_message == "what's in my basket?"
    assert "<foo>bar</foo>" in turn.ephemeral_system_prompt
    assert len(turn.conversation_history) == 2
    # Static system message is the SKILL.md body — cache-stable, no
    # per-turn substrings allowed. The literal ``<channel>web</channel>``
    # token must NOT live in the static prompt (it would change every
    # turn and bust the cache); it lives in the ephemeral prompt.
    assert "Vitrina" in turn.system_message
    assert "<channel>web</channel>" not in turn.system_message
    assert "<channel>web</channel>" in turn.ephemeral_system_prompt
    assert turn.session_metadata["thread_id"] == "th_1"


def test_build_turn_injects_dm_checkout_pickup_hint_when_paying() -> None:
    turn = session_bridge.build_turn(
        messages=[{"role": "user", "content": "I want to pay now"}],
        channel="instagram_dm",
        thread_id="th1",
        user_id=None,
        cart_key="ig:1",
        cart_ids=None,
        user_location={"lat": 42.0, "lng": 23.3, "label": "Pirotska"},
    )
    assert "<pickup_context_required>true</pickup_context_required>" in turn.ephemeral_system_prompt
    assert "checkout.create_link" in turn.ephemeral_system_prompt


def test_build_turn_no_checkout_hint_without_location() -> None:
    turn = session_bridge.build_turn(
        messages=[{"role": "user", "content": "I want to pay now"}],
        channel="instagram_dm",
        thread_id="th1",
        user_id=None,
        cart_key="ig:1",
        cart_ids=None,
        user_location=None,
    )
    assert "<pickup_context_required" not in turn.ephemeral_system_prompt


def test_build_turn_caches_skill_across_channels() -> None:
    """Two different-channel turns must share the same system_message bytes.

    This is the prompt-cache invariant: the model provider's cache key
    is the static system prompt. If the channel sneaks into it, every
    new channel busts the cache.
    """
    web = session_bridge.build_turn(
        messages=[{"role": "user", "content": "hi"}],
        channel="web",
        thread_id=None,
        user_id=None,
        cart_key=None,
        cart_ids=None,
        user_location=None,
    )
    ig = session_bridge.build_turn(
        messages=[{"role": "user", "content": "hi"}],
        channel="instagram_dm",
        thread_id=None,
        user_id=None,
        cart_key=None,
        cart_ids=None,
        user_location=None,
    )
    assert web.system_message == ig.system_message
    assert web.ephemeral_system_prompt != ig.ephemeral_system_prompt
