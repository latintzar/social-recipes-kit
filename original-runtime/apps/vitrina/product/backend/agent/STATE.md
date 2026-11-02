> Retained April–May 2026 implementation record. [Current public setup](../../../../../../docs/agent-runtime.md).

# STATE: dormant — paused 2026-05-07

> **Scoping note.** This subtree contains both the chat-first agent
> (paused) and the Instagram webhook plumbing (paused since the IG-DM
> shopper flow isn't being driven today either). Treat the entire
> `agent/` subtree as dormant. The waitlist surface
> (`product/public/bg/live/` + `product/public/bg/waitlist/` +
> `product/backend/api/routes/waitlist.py`) does **not** depend on
> anything in here.

## Why paused

Vitrina pivoted from a chat-first multi-shop commerce app (Hermes-driven
agent that talks to shoppers in IG DMs / web chat, calls into the MCP,
and assembles multi-shop baskets) to a phone-first waitlist for
personalised farm-drops. The agent + MCP + Discover tab were complete
enough to demo, but the *demand* signal was too weak — too much friction
between "I have a recipe" and "I'd pay for this basket today." Pivoted to
prove demand on the simpler waitlist surface first. See
`apps/vitrina/company/decisions/2026-05-02-pivot-to-farm-drops.md` (`../../../company/decisions/2026-05-02-pivot-to-farm-drops.md`, historical internal reference).

## Last known good

- Commit at pause: `1c8212904fa762f30a2ede84e721f5062f675f82`
- Tests: `pytest apps/vitrina/product/backend/agent/tests/`
- Deployed where: the `vitrina-agent` systemd unit on Hetzner (`:8002`)
  is still running, but its only live job today is the
  `/v1/instagram/webhook` health endpoint and the IG signature check
  used for sandbox testing. No active conversations.
- Tracing: `[TRACE]` JSON lines in `journalctl -u vitrina-agent` —
  the format described at `tracing.py`'s docstring.

## What it does

Hermes-based agent runtime that:
- Receives Instagram DMs / web-chat messages → builds a turn → calls
  into the MCP → emits bubbles back to the user.
- Uses `agent_factory.py` to bootstrap Hermes with the MCP tool catalog
  via the stdio-subprocess pattern.
- Persists chat threads + messages in Supabase (`chat_threads`,
  `chat_messages`).
- Has framework adapters for IG DMs, web chat, and a planned WhatsApp
  surface (never wired).

## What it would take to revive

1. Re-source `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`,
   `INSTAGRAM_APP_SECRET`, `INSTAGRAM_PAGE_ACCESS_TOKEN`,
   `INSTAGRAM_BUSINESS_ID`, `VITRINA_MCP_*`, `SUPABASE_*` in
   `/etc/vitrina/agent.env` on the Hetzner box.
2. Confirm Supabase migrations 001–011 (commerce tables, chat threads,
   agent traces) are applied.
3. If reviving the IG flow: Meta App Dashboard → webhook URL points at
   `https://5-161-178-146-agent.sslip.io/webhooks/instagram`.
4. Smoke: `python3 scripts/ops/vitrina-debug/health.py` should be all
   green; `python3 scripts/ops/vitrina-debug/ig_replay.py --text "what
   cheese do you have?"` should return a non-empty assistant reply
   visible in `chat_messages`.
5. Update this file's "Deployed where" section.

Expected gotchas:
- The MCP runs as a stdio subprocess of this agent (since 2026-04-29);
  `VITRINA_MCP_URL` is normally unset. If you set it, point at the
  internal port the subprocess actually listens on, not 8787.
- `[TRACE]` events use `turn_id` as the join key — `trace.py` does the
  joining for you, don't grep raw logs.
- Hermes config bootstrap order matters: `agent_factory.py` docstring
  documents the right sequence.

## Don't iterate here unless reviving

This subtree is dormant. Cosmetic edits, refactors, and "while I'm
passing through" cleanups are wasted effort. If something is genuinely
broken, fix it and add a line here. Otherwise, wait for a revive
decision.

## Review by

Q3 2026 — re-evaluate after the waitlist signal lands. The chat-first
flow is the natural home for "you've collected your drop, here's a
recipe with one of the producers' cheeses" upsells, but only if
waitlist demand crosses the threshold first.
