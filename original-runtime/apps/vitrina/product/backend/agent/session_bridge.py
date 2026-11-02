"""Translate a Vitrina chat-stream request into Hermes-shaped turn inputs.

Post-rebuild rules (see plan ``stock-hermes vitrina rebuild``):

- Static ``system_message`` is **only** the SKILL.md body. It is the
  same every turn so the Anthropic / OpenRouter prompt cache hits.
- Dynamic per-turn context (channel block, anything we need to force
  inject) goes into ``ephemeral_system_prompt`` — NOT persisted to
  trajectories and not part of the cache prefix.
  Pass optional ``ephemeral_extra`` through ``channel.run_turn`` /
  ``build_turn`` for DM-only snippets (e.g. shopper IG @handle).
- The memory block is **not** built here. Hermes' configured memory
  provider (mem0, see ``agent_factory.ensure_hermes_config``) handles
  every fetch + injection on its own. The hand-rolled
  ``_memory_block`` we used to ship was racy (asyncio.run inside the
  event loop) and broke prompt caching.
- The basket block is **not** force-injected either. The model is
  free to call ``basket.snapshot`` whenever the SKILL.md tool-choice
  matrix tells it to. This removes a synchronous Supabase round-trip
  from the hot path of every turn.

Public surface — all stable across channels (web, IG DM, TikTok DM,
terminal repl):

- :class:`TurnInputs` — the dataclass passed to
  ``AIAgent.run_conversation``.
- :func:`build_turn` — turn a list of channel-shaped messages into
  :class:`TurnInputs`.
- :func:`build_session_metadata` — the dict every tool handler reads
  via ``_session_ctx`` to inject infra fields into MCP dispatch args.
"""

from __future__ import annotations

import re
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# DM user messages that suggest paying — inject pickup/checkout rehearsal before tools run.
_DM_CHECKOUT_INTENT = re.compile(
    r"(?i)(checkout|check\s*out|\bpay\b|payment|place\s+(?:my\s+)?order|"
    r"buy\s+it|purchase|card\s+ready|ready\s+to\s+pay|pay\s+now|"
    r"плащане|платя|поръчк|купувам|оплатя|завърш)",
)


# ── Skill loader (single source of truth for the system prompt) ──────


def _default_skill_path() -> Path:
    """Resolve ``vitrina-shopper/SKILL.md`` in both repo and Docker layouts."""
    explicit = os.getenv("SOCIAL_RECIPES_SKILL_PATH")
    if explicit:
        return Path(explicit)
    here = Path(__file__).resolve()
    candidates = [
        # Railway/Docker layout from the agent Dockerfile.
        Path("/app/skills/vitrina-shopper/SKILL.md"),
        # Monorepo layout: apps/vitrina/product/backend/agent/session_bridge.py
        # -> apps/vitrina/skills/vitrina-shopper/SKILL.md
        *[
            parent / "skills" / "vitrina-shopper" / "SKILL.md"
            for parent in here.parents
        ],
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


_SKILL_PATH = _default_skill_path()


def _strip_yaml_frontmatter(raw: str) -> str:
    """Remove optional YAML `--- … ---` wrapper from a markdown skill/soul file."""
    text = raw or ""
    if text.startswith("---"):
        end = text.find("\n---", 4)
        if end != -1:
            text = text[end + 4 :].lstrip()
    return text


def _load_skill() -> str:
    """Read ``SKILL.md`` + optional ``SOUL.md`` from the same directory.

    Hermes is constructed with ``skip_context_files=True`` (see
    ``agent_factory.build_agent``), so the stock ``$HERMES_HOME/SOUL.md``
    persona is **not** injected by Hermes. Vitrina instead merges an
    optional ``apps/vitrina/skills/vitrina-shopper/SOUL.md`` here after
    ``SKILL.md`` — same static prefix for prompt caching, but a real
    voice layer when the file exists.
    """
    text = ""
    if _SKILL_PATH.is_file():
        text = _strip_yaml_frontmatter(_SKILL_PATH.read_text(encoding="utf-8"))
    soul_path = _SKILL_PATH.parent / "SOUL.md"
    if soul_path.is_file():
        soul = _strip_yaml_frontmatter(soul_path.read_text(encoding="utf-8")).strip()
        if soul:
            text = (text.rstrip() + "\n\n---\n\n## Persona (SOUL)\n\n" + soul).strip()
    return text


def _has_pickup_location(user_location: dict[str, Any] | None) -> bool:
    """True when thread has usable lat/lng (same idea as IG magic-link metadata)."""
    if not user_location or not isinstance(user_location, dict):
        return False
    try:
        lat = float(user_location.get("lat", 0.0) or 0.0)
        lng = float(user_location.get("lng", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    # (0,0) means unset for Vitrina shoppers.
    return abs(lat) > 1e-6 or abs(lng) > 1e-6


def _dm_checkout_pickup_block(
    channel: str,
    *,
    user_location: dict[str, Any] | None,
    user_message: str,
) -> str:
    """Extra ephemeral hint for Instagram DM checkout — pickup must be explicit in-chat (no basket sheet)."""
    if channel != "instagram_dm":
        return ""
    if not _has_pickup_location(user_location):
        return ""
    if not user_message.strip() or not _DM_CHECKOUT_INTENT.search(user_message):
        return ""
    label = str((user_location or {}).get("label") or "").strip()
    area = f"**{label}**" if label else "their confirmed map pin"
    return (
        "## Checkout & pickup (THIS TURN — DM)\n"
        "<pickup_context_required>true</pickup_context_required>\n"
        f"The shopper already set a pickup area ({area}). Hosted Stripe does **not** replace "
        "chat: **before** `checkout.create_link`, restate **pickup / slot / shop hours** in "
        "plain language (per shop if the basket spans several stores). Confirm they are "
        "happy with timing. If they have not given an email on this thread, ask once OR say "
        "Stripe will collect email on the payment page — use `account.attach_email` if they "
        "reply with an address. Then send the checkout URL in its own bubble."
    )


def _session_identity_block(
    *,
    thread_id: str | None,
    user_id: str | None,
    cart_key: str | None,
) -> str:
    """Hard session identity for MCP tool args.

    Every Vitrina MCP tool that touches Supabase requires the *real*
    ``thread_id``, ``cart_key``, and ``user_id`` for this turn. The
    gateway already knows the truth — emit it verbatim so the model
    cannot fabricate values like ``thread_id="ig:967387775706018"``
    or omit ``cart_key`` and trip the ``thread_owner_present`` check
    constraint on ``mcp.chat_threads``.
    """
    tid = (thread_id or "").strip() or "null"
    uid = (user_id or "").strip() or "null"
    ck = (cart_key or "").strip() or "null"
    return (
        "## Session identity (USE VERBATIM)\n"
        "Whenever an MCP tool accepts `thread_id`, `cart_key`, or `user_id`, "
        "pass these exact values. Do **not** invent, guess, or omit them.\n"
        f"<thread_id>{tid}</thread_id>\n"
        f"<cart_key>{ck}</cart_key>\n"
        f"<user_id>{uid}</user_id>"
    )


def _channel_block(
    channel: str,
    *,
    user_location: dict[str, Any] | None,
    user_message: str = "",
    thread_id: str | None = None,
    user_id: str | None = None,
    cart_key: str | None = None,
) -> str:
    """Per-channel hint + hard session constraints. Ephemeral only (cache-safe)."""
    if os.getenv("SOCIAL_RECIPES_PROFILE") == "recipe":
        return f"## Channel\n<channel>{channel}</channel>\nReply in this Instagram conversation using the recipe extraction tool."
    parts = [
        f"## Channel\n<channel>{channel}</channel>",
        _session_identity_block(
            thread_id=thread_id, user_id=user_id, cart_key=cart_key
        ),
    ]
    if channel in ("instagram_dm", "tiktok_dm") and not _has_pickup_location(user_location):
        parts.append(
            "## Session constraint (THIS TURN)\n"
            "<no_pickup_location>true</no_pickup_location>\n"
            "The shopper has **not** confirmed a pickup area for this thread.\n"
            "Before **any** recipe tool (`recipes.extract_from_url*`, "
            "`recipes.discover_social`, `recipes.add_to_basket`, etc.), call "
            "`ig.request_location` and send the magic link URL in the same turn. "
            "Do not guess shops from vibes. On Instagram DM, avoid stacking "
            "multiple acknowledgement openers — one short line, then tools."
        )
    checkout_extra = _dm_checkout_pickup_block(
        channel, user_location=user_location, user_message=user_message
    )
    if checkout_extra:
        parts.append(checkout_extra)
    return "\n\n".join(parts)


# ── Turn shape ───────────────────────────────────────────────────────


@dataclass
class TurnInputs:
    """Everything Hermes needs to run one user→assistant turn."""

    user_message: str
    system_message: str  # Stable across turns → cacheable.
    ephemeral_system_prompt: str  # Per-turn live context (channel, etc.).
    conversation_history: list[dict[str, Any]] = field(default_factory=list)
    session_metadata: dict[str, Any] = field(default_factory=dict)


def latest_user_text(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return str(msg.get("content") or "")
    return ""


def build_conversation_history(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """Split incoming messages into history + the live user turn.

    Hermes wants ``conversation_history`` (everything before the last
    user turn) and ``user_message`` (the live turn) as separate args.
    """
    history: list[dict[str, Any]] = []
    user_text = ""
    seen_final_user = False
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if not seen_final_user and msg.get("role") == "user":
            user_text = str(msg.get("content") or "")
            history = [
                {"role": m.get("role"), "content": m.get("content", "")}
                for m in messages[:idx]
                if m.get("role") in {"user", "assistant"}
            ]
            seen_final_user = True
            break
    if not seen_final_user:
        history = [
            {"role": m.get("role"), "content": m.get("content", "")}
            for m in messages
            if m.get("role") in {"user", "assistant"}
        ]
    return history, user_text


def build_session_metadata(
    *,
    channel: str,
    thread_id: str | None,
    user_id: str | None,
    cart_key: str | None,
    cart_ids: list[str] | None,
    user_location: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "channel": channel,
        "thread_id": thread_id,
        "user_id": user_id,
        "cart_key": cart_key,
        "cart_ids": cart_ids or [],
        "user_location": user_location,
    }


def build_turn(
    *,
    messages: list[dict[str, Any]],
    channel: str,
    thread_id: str | None,
    user_id: str | None,
    cart_key: str | None,
    cart_ids: list[str] | None,
    user_location: dict[str, Any] | None,
    ephemeral_extra: str | None = None,
) -> TurnInputs:
    """Build the per-turn inputs for ``AIAgent.run_conversation``.

    The static ``system_message`` is the SKILL.md body verbatim — same
    bytes every turn so prompt caching fires. Anything we need to
    force-inject for *this* turn (currently just the channel hint) is
    routed through ``ephemeral_system_prompt`` instead.
    """
    history, user_text = build_conversation_history(messages)
    metadata = build_session_metadata(
        channel=channel,
        thread_id=thread_id,
        user_id=user_id,
        cart_key=cart_key,
        cart_ids=cart_ids,
        user_location=user_location,
    )
    ephemeral = _channel_block(
        channel,
        user_location=user_location,
        user_message=user_text,
        thread_id=thread_id,
        user_id=user_id,
        cart_key=cart_key,
    )
    extra = (ephemeral_extra or "").strip()
    if extra:
        ephemeral = f"{ephemeral}\n\n{extra}" if ephemeral else extra
    return TurnInputs(
        user_message=user_text,
        system_message=_load_skill(),
        ephemeral_system_prompt=ephemeral,
        conversation_history=history,
        session_metadata=metadata,
    )
