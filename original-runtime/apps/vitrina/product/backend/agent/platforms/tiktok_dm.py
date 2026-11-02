"""TikTok DM channel adapter for the Vitrina shopper agent.

**Status:** scaffold. Same shape as :mod:`.instagram_dm` — webhook
endpoints wired in :mod:`agent.server`, outbound send raises until
credentials arrive.

## Required env (set when credentials arrive)

- ``TIKTOK_CLIENT_KEY`` / ``TIKTOK_CLIENT_SECRET`` — TikTok for
  Developers app credentials.
- ``TIKTOK_VERIFY_TOKEN`` — verify token for the webhook handshake.
- ``TIKTOK_ACCESS_TOKEN`` — long-lived access token used to send
  business messages.

## Wiring in

1. Implement :func:`send_message` against TikTok's Business Messaging
   API once we're approved (it's an opt-in beta as of writing).
2. Same :mod:`agent.session_bridge` + :mod:`agent.agent_factory`
   pattern as the web channel and the IG adapter — pass
   ``channel="tiktok_dm"`` so the system prompt knows widgets won't
   render.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse


CHANNEL = "tiktok_dm"


async def handle_verify(request: Request) -> JSONResponse:
    """TikTok webhook subscription handshake.

    The exact handshake shape depends on which Business Messaging
    endpoint we end up using; this stub mirrors Meta's pattern (echo
    back ``hub.challenge`` when ``hub.verify_token`` matches) so the
    plumbing is in place for whichever shape ships.
    """
    params = request.query_params
    token = params.get("hub.verify_token") or params.get("verify_token")
    challenge = params.get("hub.challenge") or params.get("challenge")
    expected = os.getenv("TIKTOK_VERIFY_TOKEN")
    if expected and token == expected and challenge:
        return PlainTextResponse(content=challenge, status_code=200)  # type: ignore[return-value]
    return JSONResponse({"error": "verification_failed"}, status_code=403)


async def handle_webhook(request: Request) -> JSONResponse:
    if not _credentials_present():
        return JSONResponse(
            {"status": "ok", "note": "tiktok credentials not configured"}
        )
    payload = await request.json()
    sent = 0
    try:
        from . import channel as channel_gateway  # type: ignore[import-not-found]
    except ImportError:
        from platforms import channel as channel_gateway  # type: ignore
    for ev in _iter_messages(payload):
        try:
            result = channel_gateway.run_turn(
                channel=CHANNEL,
                user_message=ev["text"],
                thread_id=f"tt:{ev['sender_id']}",
                user_id=ev["sender_id"],
            )
            if result.assistant_text:
                send_message(ev["sender_id"], result.assistant_text)
                sent += 1
        except NotImplementedError:
            pass
    return JSONResponse({"status": "ok", "received": sent})


def send_message(recipient_id: str, text: str) -> None:
    raise NotImplementedError(
        "TikTok DM outbound send not implemented. Set TIKTOK_CLIENT_KEY, "
        "TIKTOK_CLIENT_SECRET, TIKTOK_ACCESS_TOKEN, and TIKTOK_VERIFY_TOKEN, "
        "then implement against TikTok Business Messaging."
    )


def _credentials_present() -> bool:
    return all(
        os.getenv(k) for k in ("TIKTOK_ACCESS_TOKEN", "TIKTOK_VERIFY_TOKEN")
    )


def _iter_messages(payload: dict[str, Any]):
    """Yield ``{sender_id, text, mid}`` dicts from the TikTok payload.

    The exact shape is documented in TikTok's Business Messaging spec;
    this stub assumes a Meta-like ``entry[].messaging[]`` envelope so
    the dispatch loop doesn't change shape when credentials arrive.
    """
    for entry in payload.get("entry", []) or []:
        for ev in entry.get("messaging", []) or []:
            sender = ev.get("sender", {}).get("id")
            msg = ev.get("message") or {}
            text = msg.get("text")
            mid = msg.get("mid")
            if sender and text:
                yield {"sender_id": sender, "text": text, "mid": mid}
