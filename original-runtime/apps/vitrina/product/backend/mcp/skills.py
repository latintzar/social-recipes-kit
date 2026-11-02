"""Vitrina skill register — Hermes-style descriptors injected into the prompt.

Why this exists
---------------
The shopper agent has ~15 tools. Without a structured skill register
the model treats every tool as ad-hoc and forgets when to compose them
("user named a recipe → search catalog first, then offer modify or
bundle"). Hermes solves this with `tools/registry.py` + per-skill
descriptor blocks; we adopt the *pattern* (no runtime swap) — see the
chat answer at `apps/vitrina/product/backend/agent/session.py`
docstring for the rationale on not importing hermes-agent itself.

Each skill is a short paragraph describing **when** to reach for which
tools, plus one worked example. The block is appended to the system
prompt at session start (cache-stable; never per-turn, matching Hermes'
prompt-caching expectations).

To add a skill: append to `SKILLS` below. Keep each entry under ~6
lines so the full block stays cheap to cache.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    tools: tuple[str, ...]
    example: str


SKILLS: tuple[Skill, ...] = (
    Skill(
        name="recipe-discovery",
        description=(
            "Find a recipe the shopper named, browse leftovers, or pull a "
            "creator video. The catalog is the source of truth — never "
            "assume something only exists in the basket."
        ),
        tools=(
            "recipes.search",
            "recipes.get",
            "recipes.discover_social",
            "recipes.extract_from_url_async",
            "recipes.synthesize_from_brief",
        ),
        example=(
            'User: "modify Noodle Soup with Pork and Egg" → '
            "recipes.search(query='noodle soup pork egg') → "
            "recipes.get(recipe_id=…) → propose ingredient swap."
        ),
    ),
    Skill(
        name="basket-edit",
        description=(
            "Add, replace, or substitute ingredients inside a recipe group. "
            "Always trust `## Current basket` over conversation history."
        ),
        tools=(
            "recipes.add_to_basket",
            "recipes.replace_recipe_group",
            "recipes.substitute",
            "basket.snapshot",
        ),
        example=(
            'User: "make it vegetarian" → recipes.substitute(group_id, '
            "intent='vegetarian') for each animal ingredient."
        ),
    ),
    Skill(
        name="bundling",
        description=(
            "After every basket addition, propose a sibling recipe that "
            "consumes the leftover grams. `recipes.add_to_basket` returns a "
            "`bundle_hint` with the top 2 candidates pre-computed — emit a "
            "`stretch_card` widget for the best one."
        ),
        tools=("recipes.stretch_basket",),
        example=(
            'After adding "BLP sandwich" → leftover bacon → emit '
            "stretch_card for the top bundle_hint candidate."
        ),
    ),
    Skill(
        name="checkout",
        description=(
            "Drive ACP checkout once the shopper says pay. Verify shop "
            "hours first; if any shop is closed, run the re-plan flow "
            "instead of sending them to Stripe."
        ),
        tools=(
            "create_checkout_session",
            "update_checkout_session",
            "complete_checkout_session",
            "complete_checkout_group",
            "cancel_checkout_session",
        ),
        example=(
            'User: "pay" → create_checkout_session → emit PayButton with '
            "the Stripe URL."
        ),
    ),
)


def render_skills_block() -> str:
    """Render the skills section appended to the system prompt.

    Cache-stable: the output depends only on `SKILLS`, so the model's
    prompt cache survives across turns. Anything turn-specific (basket
    snapshot, memory) MUST be appended *after* this block, not woven
    into it.
    """
    lines = ["## Skill register", ""]
    for skill in SKILLS:
        tool_list = ", ".join(f"`{t}`" for t in skill.tools)
        lines.append(f"### {skill.name}")
        lines.append(skill.description)
        lines.append(f"Tools: {tool_list}")
        lines.append(f"Example: {skill.example}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
