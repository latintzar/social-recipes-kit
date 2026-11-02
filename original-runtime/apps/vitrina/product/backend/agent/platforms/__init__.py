"""Channel adapters for the Vitrina agent.

Each module implements a thin gateway between an external messaging
surface (Instagram DM, TikTok DM) and the same Hermes-backed runtime
that powers ``/v1/chat/stream`` for the web. The web channel runs
in-process via :mod:`agent.server`'s SSE route — no separate adapter
is needed.

Milestone 1 ships **scaffolds only** for IG/TT: webhook verification,
payload parsing, and a clear ``NotImplementedError`` on the outbound
``send_message`` path. Adding credentials later is a single PR (see
each module's docstring for the env vars).
"""
