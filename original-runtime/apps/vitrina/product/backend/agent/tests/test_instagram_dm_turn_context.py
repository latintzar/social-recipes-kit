"""Instagram DM turn shaping — staged recipe hint + topic-change heuristics."""

from __future__ import annotations

from platforms import instagram_turn_context as tc


def test_reject_prior_reel_focus_detects_frustration() -> None:
    assert tc.message_rejects_prior_reel_focus(
        "Why do you keep talking about that drum reel"
    )
    assert tc.message_rejects_prior_reel_focus("Not food — stop mentioning it")
    assert not tc.message_rejects_prior_reel_focus("yes add it please")


def test_staged_followup_only_when_plausible() -> None:
    assert tc.user_signals_staged_recipe_followup("yeah add that to the basket")
    assert tc.user_signals_staged_recipe_followup("ok")
    assert tc.user_signals_staged_recipe_followup("yep!")
    assert not tc.user_signals_staged_recipe_followup(
        "Can we talk about delivery instead?"
    )
    assert not tc.user_signals_staged_recipe_followup(
        "Why are you obsessed with that reel"
    )
