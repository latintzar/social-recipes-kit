"""Lightweight Instagram DM turn heuristics (no Hermes / FastAPI imports).

Used by :mod:`platforms.instagram_dm` to avoid nagging shoppers about a prior
reel or staged recipe on every unrelated message.
"""

from __future__ import annotations

import re


def message_rejects_prior_reel_focus(raw: str) -> bool:
    """True when the shopper is changing topic or frustrated — skip staged reel nags."""
    t = (raw or "").strip().lower()
    if not t:
        return False
    return bool(
        re.search(
            r"\b(stop (talking|mentioning)|why (do|are) you|not food|unrelated|"
            r"different topic|forget that|ignore (that|the) reel|not interested|"
            r"quit (talking )?about|obsessed|stuck (on|with)|something else|"
            r"new question|no more|not a recipe|i did not ask)\b",
            t,
        )
    )


def user_signals_staged_recipe_followup(raw: str) -> bool:
    """Only surface staged-recipe context when the DM plausibly refers to adding food."""
    if message_rejects_prior_reel_focus(raw):
        return False
    t = (raw or "").strip().lower()
    if not t:
        return False
    if re.search(
        r"\b(yes|yeah|yep|sure|please|add (it|that|this)|do it|sounds good|"
        r"go ahead|put it|to (the )?basket|to cart|the recipe|that recipe|add to|"
        r"ring it up|bill me|checkout)\b",
        t,
    ):
        return True
    compact = re.sub(r"[^\w\s]", "", t).strip()
    if len(compact) <= 4 and compact in {"ok", "yes", "yep", "sure", "pls", "add", "k", "kk"}:
        return True
    return False
