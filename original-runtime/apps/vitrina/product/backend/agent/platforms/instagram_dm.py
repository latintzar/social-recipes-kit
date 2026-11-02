"""Instagram DM channel adapter for the Vitrina shopper agent.

Wires Meta's Instagram Messaging webhook to Vitrina's Hermes runtime.

## Flow

    shopper DM → Meta webhook
    POST /v1/instagram/webhook  (X-Hub-Signature-256 verified)
        │
        ▼
    handle_webhook
        │  parse {sender_id, text}, transcribe voice attachments (same stack as
        │  ``POST /api/transcribe``), filter echoes
        ▼
    channel.run_turn(channel="instagram_dm",
                     user_message=text,
                     thread_id="ig:{sender_id}")
        │  Hermes loop + MCP tools → assistant_text
        ▼
    send_messages(sender_id, assistant_text)
        │  POST graph … one bubble per [BREAK] segment
        ▼
    shopper sees reply in the same DM thread

**Channel parity:** The Hermes + MCP tool loop is **identical** to the web app
and terminal repl — everything goes through :func:`platforms.channel.run_turn`
with the same ``session_bridge`` / ``session_source`` / ``agent_factory`` stack.
This module is **transport only**: Meta webhook parsing, optional burst batching
of rapid DMs, and Graph API outbound. It must not fork tool behaviour or
session rules; fixes belong in shared code unless they are strictly Instagram
Graph / payload quirks.

## Environment

- ``INSTAGRAM_APP_SECRET``        — HMAC-SHA256 key for X-Hub-Signature-256
- ``INSTAGRAM_PAGE_ACCESS_TOKEN`` — long-lived token for Send API **and** for
  ``GET /{media-id}?fields=permalink,shortcode`` (resolves DM ``reel_video_id``)
  and **``GET /{message-id}?fields=shares{url,…}``** (resolves the same share to
  public ``instagram.com/reel/…`` when media-id lookup returns 100/33 — no user
  action).
- ``INSTAGRAM_VERIFY_TOKEN``      — echoed back in GET subscription handshake
- ``INSTAGRAM_BUSINESS_ID``       — IG User ID used as path in Send API URL
- ``INSTAGRAM_BUSINESS_USERNAME`` — for logging only

## Threading

``thread_id`` is a deterministic UUID (see ``instagram_platform.ig_thread_id``);
``cart_key`` is ``ig:{sender_id}`` for anonymous carts. ``memory_user_id`` is
``ig:{sender_id}`` for Mem0 while ``user_id`` stays ``None``.
Instagram @handle (and display name when available) are fetched from Graph,
stored under ``chat_threads.metadata``, injected into the ephemeral system
prompt each turn, and written once to hosted mem0 via ``storage.remember``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import http_client_sync
from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse

try:
    from . import channel as channel_gateway
    from .ig_reel_permalink import GRAPH_FB_BASE, GRAPH_IG_BASE
    from .ig_webhook_inbound import iter_instagram_messaging_events as _iter_messages
    from .instagram_turn_context import (
        message_rejects_prior_reel_focus as _ig_message_rejects_prior_reel_focus,
        user_signals_staged_recipe_followup as _ig_user_signals_staged_recipe_followup,
    )
    from .instagram_platform import (
        extract_urls,
        fetch_instagram_user_profile,
        has_instagram_permalink_in_text,
        ig_graph_send_ok as _ig_send_ok,
        ig_memory_user_id,
        ig_thread_id,
        is_instagram_private_media_host,
        meta_webhook_json,
        plain_text_for_instagram_dm,
        split_reply,
    )
    from .. import tracing
except ImportError:  # uvicorn invoked from /app/vitrina_agent
    from platforms import channel as channel_gateway  # type: ignore
    from platforms.ig_reel_permalink import GRAPH_FB_BASE, GRAPH_IG_BASE  # type: ignore
    from platforms.ig_webhook_inbound import (  # type: ignore
        iter_instagram_messaging_events as _iter_messages,
    )
    from platforms.instagram_turn_context import (  # type: ignore
        message_rejects_prior_reel_focus as _ig_message_rejects_prior_reel_focus,
        user_signals_staged_recipe_followup as _ig_user_signals_staged_recipe_followup,
    )
    from platforms.instagram_platform import (  # type: ignore
        extract_urls,
        fetch_instagram_user_profile,
        has_instagram_permalink_in_text,
        ig_graph_send_ok as _ig_send_ok,
        ig_memory_user_id,
        ig_thread_id,
        is_instagram_private_media_host,
        meta_webhook_json,
        plain_text_for_instagram_dm,
        split_reply,
    )
    import tracing  # type: ignore


logger = logging.getLogger(__name__)

CHANNEL = "instagram_dm"

# In-memory dedupe of Meta message ids (mid). Meta retries the webhook
# if we don't 200 within ~10s; on retry it reuses the same `mid`. Without
# this guard a slow turn would be re-run and the shopper would see two
# instant-acks, two recipe extractions, two basket updates. Process-local
# is fine: the agent service is a single Railway replica today, and even
# at multi-replica Meta retries hit the same edge node within seconds.
_SEEN_MIDS: dict[str, float] = {}
_SEEN_MIDS_LOCK = threading.Lock()
_SEEN_MIDS_TTL_S = 600.0  # 10 minutes is well past Meta's retry window
_SEEN_MIDS_MAX = 10_000


def _is_duplicate_mid(mid: str | None) -> bool:
    if not mid:
        return False
    now = time.time()
    with _SEEN_MIDS_LOCK:
        # Cheap GC: when over the cap, drop everything older than TTL.
        if len(_SEEN_MIDS) >= _SEEN_MIDS_MAX:
            cutoff = now - _SEEN_MIDS_TTL_S
            stale = [k for k, ts in _SEEN_MIDS.items() if ts < cutoff]
            for k in stale:
                _SEEN_MIDS.pop(k, None)
        if mid in _SEEN_MIDS and (now - _SEEN_MIDS[mid]) < _SEEN_MIDS_TTL_S:
            return True
        _SEEN_MIDS[mid] = now
        return False


# ── burst queue (quiet window, 10s max from first) ─────────────────────

_IG_BURST_QUIET_S = 1.0
_IG_BURST_MAX_S = 10.0
_IG_BURST_LOCK = threading.Lock()
_IG_BURST_BUFFERS: dict[str, dict[str, Any]] = {}
_IG_BURST_TIMERS: dict[str, threading.Timer] = {}


def _ig_burst_drain(sender_id: str) -> None:
    with _IG_BURST_LOCK:
        entry = _IG_BURST_BUFFERS.pop(sender_id, None)
        _IG_BURST_TIMERS.pop(sender_id, None)
    if not entry:
        return
    msgs: list[dict[str, Any]] = entry["msgs"]
    if not msgs:
        return
    parts = [str(m.get("text") or "").strip() for m in msgs]
    combined_text = "\n".join(p for p in parts if p) or (str(msgs[-1].get("text") or ""))
    reel_cdn_only = any(bool(m.get("reel_cdn_only")) for m in msgs)
    synthetic = {
        "sender_id": sender_id,
        "text": combined_text,
        "mid": msgs[-1].get("mid"),
        "reel_cdn_only": reel_cdn_only,
    }
    threading.Thread(
        target=_handle_one_safe,
        args=(synthetic,),
        daemon=True,
        name=f"ig_turn_{sender_id[:8]}",
    ).start()


def _ig_burst_schedule(sender_id: str) -> None:
    with _IG_BURST_LOCK:
        buf = _IG_BURST_BUFFERS.get(sender_id)
        if not buf:
            return
        now = time.time()
        started = float(buf["started"])
        deadline = min(now + _IG_BURST_QUIET_S, started + _IG_BURST_MAX_S)
        delay = max(0.05, deadline - now)
        old = _IG_BURST_TIMERS.pop(sender_id, None)
        if old:
            old.cancel()

        def _fire() -> None:
            try:
                _ig_burst_drain(sender_id)
            except Exception:
                logger.exception("ig_dm: burst_drain failed sender=%s", sender_id)

        t = threading.Timer(delay, _fire)
        t.daemon = True
        _IG_BURST_TIMERS[sender_id] = t
        t.start()


def _ig_burst_enqueue(msg: dict[str, Any]) -> None:
    sender_id = str(msg.get("sender_id") or "")
    if not sender_id:
        return
    now = time.time()
    with _IG_BURST_LOCK:
        ent = _IG_BURST_BUFFERS.get(sender_id)
        if ent is None:
            _IG_BURST_BUFFERS[sender_id] = {"msgs": [msg], "started": now}
        else:
            ent["msgs"].append(msg)
    _ig_burst_schedule(sender_id)


def _graph_messages_endpoint(token: str, ig_user_id: str | None) -> str:
    """Pick the right Send API URL for the token flavour."""
    if token.startswith(("IGAA", "IGQ")):
        return f"{GRAPH_IG_BASE}/me/messages"
    return f"{GRAPH_FB_BASE}/{ig_user_id or 'me'}/messages"


_IG_REPLY_LIMIT = 1000  # Instagram hard cap per message
_MAX_IG_VOICE_BYTES = 10 * 1024 * 1024  # parity with /api/transcribe


def _download_ig_voice_attachment(url: str) -> tuple[bytes, str]:
    """Fetch bytes from Meta's time-limited CDN URL (same client as reel lookaside)."""
    with http_client_sync(timeout=120.0) as client:
        resp = client.get(url, follow_redirects=True)
    resp.raise_for_status()
    body = resp.content
    if len(body) > _MAX_IG_VOICE_BYTES:
        raise ValueError("ig voice attachment exceeds size cap")
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
    return body, (ctype or "application/octet-stream")


async def _enrich_ig_voice_notes(msg: dict[str, Any]) -> None:
    """Transcribe IG voice-message attachments before Hermes (web chat parity).

    Matches ``POST /api/transcribe``: ``provider=auto``, ``polish=False``.
    Removes ``voice_attachment_urls`` and appends plain transcript text to
    ``msg['text']`` (or sets it when the DM had no caption).
    """
    raw_urls = msg.pop("voice_attachment_urls", None)
    if not isinstance(raw_urls, list) or not raw_urls:
        return
    urls = [u for u in raw_urls if isinstance(u, str) and u.startswith(("http://", "https://"))]
    if not urls:
        return
    try:
        from voice import transcribe
    except ImportError:
        logger.error("ig_dm: voice package missing; cannot transcribe voice notes")
        if not (msg.get("text") or "").strip():
            msg["text"] = (
                "[Voice message — transcription unavailable in this deployment. "
                "Ask the shopper to type their message.]"
            )
        return

    transcripts: list[str] = []
    for url in urls:
        try:
            data, ctype = await asyncio.to_thread(_download_ig_voice_attachment, url)
            if not data:
                continue
            result = await transcribe(
                data,
                content_type=ctype,
                language=None,
                provider="auto",
                polish=False,
            )
            piece = (result.text or "").strip()
            if piece:
                transcripts.append(piece)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ig_dm: voice note transcription failed | mid=%s | err=%s",
                msg.get("mid"),
                exc,
            )

    prior = (msg.get("text") or "").strip()
    if transcripts:
        joined = "\n\n".join(transcripts)
        msg["text"] = f"{prior}\n\n{joined}".strip() if prior else joined
        tracing.trace(
            "ig.voice.transcribed",
            mid=msg.get("mid"),
            sender_id=msg.get("sender_id"),
            clips=len(urls),
            transcript_chars=len(joined),
        )
    elif not prior:
        msg["text"] = (
            "[Voice message — could not transcribe. Ask the shopper to repeat in text "
            "or try sending the note again.]"
        )


# ── webhook handlers ─────────────────────────────────────────────────


async def handle_verify(request: Request) -> JSONResponse:
    """Meta webhook subscription GET handshake — echoes hub.challenge."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    expected = os.getenv("INSTAGRAM_VERIFY_TOKEN")
    if mode == "subscribe" and expected and token == expected and challenge:
        return PlainTextResponse(content=challenge, status_code=200)  # type: ignore[return-value]
    logger.warning(
        "ig_dm: verify failed mode=%s token_match=%s challenge=%s",
        mode, token == expected, bool(challenge),
    )
    return JSONResponse({"error": "verification_failed"}, status_code=403)


async def handle_webhook(request: Request) -> JSONResponse:
    """Inbound DM POST from Meta.

    Verifies signature, parses messages, runs one Hermes turn per
    message, and replies via the Send API. Always returns 200 — Meta
    retries on anything else and we don't want duplicate turns.

    Kill-switch: when the env var ``VITRINA_IG_AUTOREPLY_DISABLED`` is
    truthy, the handler short-circuits after signature verification and
    returns 200 OK without invoking Hermes. Use to take the bot
    offline without unsubscribing the Meta webhook (which would lose
    Meta's queued events when re-enabled). Toggle on the Hetzner box:
    append ``VITRINA_IG_AUTOREPLY_DISABLED=1`` to ``/etc/vitrina/agent.env``
    and ``systemctl restart vitrina-agent`` (or remove the line + restart
    to re-enable).
    """
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 1_048_576:
            return JSONResponse({"error": "payload_too_large"}, status_code=413)
    raw = bytes(raw)

    if not _verify_signature(request.headers.get("x-hub-signature-256"), raw):
        return JSONResponse({"error": "bad_signature"}, status_code=403)

    if os.getenv("VITRINA_IG_AUTOREPLY_DISABLED", "").strip() not in ("", "0", "false", "False"):
        # Peter manages Instagram manually — bot is muted at his request.
        # Still parse minimally so the trace shows we received the event,
        # then drop it on the floor.
        logger.info("ig_dm: VITRINA_IG_AUTOREPLY_DISABLED=1, dropping inbound DM")
        tracing.trace("ig.webhook.muted_by_kill_switch")
        return JSONResponse({"status": "ok", "muted": True})

    try:
        payload = meta_webhook_json(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("ig_dm: invalid JSON body: %s", exc)
        return JSONResponse({"status": "ok", "note": "unparseable_payload"})

    if os.getenv("SOCIAL_RECIPES_RUNTIME") == "1":
        expected = os.getenv("INSTAGRAM_BUSINESS_ID")
        payload["entry"] = [entry for entry in payload.get("entry", []) if str(entry.get("id") or "") == expected]

    handled = 0
    skipped = 0
    for msg in _iter_messages(payload):
        if os.getenv("SOCIAL_RECIPES_PROFILE") == "recipe" and msg.get("voice_attachment_urls"):
            if not msg.get("is_echo") and not _is_duplicate_mid(msg.get("mid")):
                send_message(str(msg["sender_id"]), "Send a cooking Reel or a text message.")
            skipped += 1
            continue
        await _enrich_ig_voice_notes(msg)
        tracing.trace(
            "ig.webhook.event",
            sender_id=msg.get("sender_id"),
            mid=msg.get("mid"),
            is_echo=bool(msg.get("is_echo")),
            text_chars=len(msg.get("text") or ""),
            reel_cdn_only=bool(msg.get("reel_cdn_only")),
        )
        if msg.get("is_echo"):
            skipped += 1
            continue
        if _is_duplicate_mid(msg.get("mid")):
            tracing.trace("ig.webhook.duplicate_mid", mid=msg.get("mid"), sender_id=msg.get("sender_id"))
            skipped += 1
            continue
        # Queue for burst (quiet window, max 10s from first) so double-text becomes one turn.
        _ig_burst_enqueue(msg)
        handled += 1

    logger.info("ig_dm: webhook handled=%d skipped=%d", handled, skipped)
    return JSONResponse({"status": "ok", "handled": handled, "skipped": skipped})


def _handle_one_safe(msg: dict[str, Any]) -> None:
    sid = str(msg.get("sender_id") or "")
    try:
        _handle_one(msg)
    except Exception:
        logger.exception("ig_dm: turn failed sender=%s", sid)
        if sid:
            fb = _ig_send_ok(
                send_message(
                    sid,
                    "Something went wrong on our side — try sending that again in a second.",
                )
            )
            if not fb:
                logger.error("ig_dm: could not deliver error fallback DM recipient=%s", sid)


# ── outbound send ────────────────────────────────────────────────────


def send_message(recipient_id: str, text: str) -> dict[str, Any]:
    """Post a DM reply via the Instagram Graph Send API.

    Returns the parsed JSON response on success, or a ``{"error": ...}``
    dict on failure. Never raises — callers are inbound webhook handlers
    where raising would swallow the 200 ack and cause Meta to retry.
    """
    token = os.getenv("INSTAGRAM_PAGE_ACCESS_TOKEN")
    ig_user_id = os.getenv("INSTAGRAM_BUSINESS_ID")
    if not token:
        logger.error("ig_dm: send_message missing INSTAGRAM_PAGE_ACCESS_TOKEN")
        return {"error": "missing_credentials"}

    url = _graph_messages_endpoint(token, ig_user_id)
    body = {
        "recipient": {"id": recipient_id},
        "message": {"text": text[:_IG_REPLY_LIMIT]},
        "messaging_type": "RESPONSE",
    }
    try:
        with http_client_sync(timeout=10.0) as client:
            resp = client.post(
                url,
                params={"access_token": token},
                json=body,
            )
        if resp.status_code >= 400:
            logger.error(
                "ig_dm: send HTTP %d body=%s recipient=%s",
                resp.status_code, resp.text[:400], recipient_id,
            )
            return {"error": f"http_{resp.status_code}", "detail": resp.text[:400]}
        data = resp.json()
        logger.info("ig_dm: sent recipient=%s mid=%s", recipient_id, data.get("message_id"))
        return data
    except Exception as exc:  # noqa: BLE001
        logger.exception("ig_dm: send_message network error recipient=%s", recipient_id)
        return {"error": "send_failed", "detail": str(exc)}


def notify_shopper_text(recipient_id: str, text: str) -> None:
    """Plain-text line from background workers (async recipe extract) — not a full turn."""
    t = plain_text_for_instagram_dm(text)
    if t:
        res = send_message(recipient_id, t)
        if not _ig_send_ok(res):
            logger.error("ig_dm: notify_shopper_text Graph failure recipient=%s res=%s", recipient_id, res)


# ── helpers ──────────────────────────────────────────────────────────


def _verify_signature(header: str | None, raw_body: bytes) -> bool:
    """Verify X-Hub-Signature-256: sha256=<hmac> against INSTAGRAM_APP_SECRET.

    Requires an app secret and exactly 64 ASCII hexadecimal digest characters.
    """
    secret = os.getenv("INSTAGRAM_APP_SECRET")
    if not secret:
        logger.error("ig_dm: configure your Instagram app secret")
        return False
    if not header or not header.startswith("sha256="):
        logger.warning("ig_dm: missing/malformed x-hub-signature-256")
        return False
    expected = header.removeprefix("sha256=")
    if len(expected) != 64 or any(char not in "0123456789abcdefABCDEF" for char in expected):
        return False
    expected = expected.lower()
    computed = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, computed):
        logger.warning("ig_dm: signature mismatch — possible replay/spoofing")
        return False
    return True


def _load_staged_recipe(cart_key: str) -> dict[str, Any] | None:
    """Most recent successfully-extracted recipe for ``cart_key`` (≤ 60 min).

    The IG flow is asynchronous: turn N extracts a reel into a recipe row
    via ``recipes.extract_from_url_async`` (writes ``chat_jobs.result`` with
    ``recipe_id`` + ``recipe_title``); turn N+1 the shopper says "yes add
    it". Without context bridging, the model has no idea what "it" is —
    it sees plain "yes" and either re-asks or fabricates.

    This helper reads the most recent succeeded job for the same
    ``cart_key`` so the ephemeral system prompt can remind the model
    *exactly* which recipe to pass to ``recipes.add_to_basket``.

    Returns ``None`` when no recent extract is staged or Supabase is
    unreachable — degrades silently, never blocks the turn.
    """
    if not cart_key:
        return None
    mcp_dir = Path(__file__).resolve().parents[2] / "mcp"
    mcp_str = str(mcp_dir)
    added = mcp_str not in sys.path
    if added:
        sys.path.insert(0, mcp_str)
    try:
        import supabase_client as sc  # noqa: PLC0415
        from datetime import datetime, timedelta, timezone  # noqa: PLC0415

        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        sb = sc.get_supabase()
        rows = (
            sb.table("chat_jobs")
            .select("result,created_at")
            .eq("cart_key", cart_key)
            .eq("status", "succeeded")
            .gte("created_at", cutoff)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001
        return None
    finally:
        if added and mcp_str in sys.path:
            sys.path.remove(mcp_str)
    if not rows:
        return None
    result = rows[0].get("result") or {}
    if not isinstance(result, dict):
        return None
    recipe_id = result.get("recipe_id")
    if not recipe_id:
        return None
    return {
        "recipe_id": str(recipe_id),
        "title": str(result.get("recipe_title") or "").strip(),
        "url": str(result.get("url") or "").strip(),
        "created_at": str(rows[0].get("created_at") or ""),
    }


def _load_thread_user_location(thread_id: str, cart_key: str) -> dict[str, Any] | None:
    """Use persisted ``metadata.location`` (IG magic link) for subsequent DMs."""
    mcp_dir = Path(__file__).resolve().parents[2] / "mcp"
    mcp_str = str(mcp_dir)
    added = mcp_str not in sys.path
    if added:
        sys.path.insert(0, mcp_str)
    row: dict[str, Any] | None = None
    try:
        import chat_threads as ct  # noqa: PLC0415

        row = ct.get_thread(thread_id, cart_key=cart_key)
    except Exception:  # noqa: BLE001
        return None
    finally:
        if added and mcp_str in sys.path:
            sys.path.remove(mcp_str)
    if not row:
        return None
    loc = (row.get("metadata") or {}).get("location")
    if not isinstance(loc, dict):
        return None
    if "lat" not in loc or "lng" not in loc:
        return None
    return {
        "lat": float(loc["lat"]),
        "lng": float(loc["lng"]),
        "label": str(loc.get("label") or ""),
        "source": str(loc.get("source") or "ig_link"),
        "precision": str(loc.get("precision") or "approximate"),
    }


def _ensure_thread_row(thread_id: str, cart_key: str, sender_psid: str) -> None:
    """Create ``chat_threads`` row on first DM if missing."""
    mcp_dir = Path(__file__).resolve().parents[2] / "mcp"
    mcp_str = str(mcp_dir)
    added = mcp_str not in sys.path
    if added:
        sys.path.insert(0, mcp_str)
    try:
        import chat_threads as ct  # noqa: PLC0415

        try:
            ct.get_thread(thread_id, cart_key=cart_key)
        except ct.ThreadNotFound:
            ct.create_thread(
                id=thread_id,
                cart_key=cart_key,
                metadata={
                    "instagram_psid": sender_psid,
                    "channel": CHANNEL,
                },
            )
        except ct.ThreadAccessDenied:
            if os.getenv("SOCIAL_RECIPES_RUNTIME") == "1":
                raise
            logger.warning("ig_dm: thread access denied thread=%s", thread_id)
    except Exception:
        if os.getenv("SOCIAL_RECIPES_RUNTIME") == "1":
            raise
        logger.exception("ig_dm: ensure_thread failed thread=%s", thread_id)
    finally:
        if added and mcp_str in sys.path:
            sys.path.remove(mcp_str)


def _format_instagram_identity_ephemeral(identity: dict[str, str | None]) -> str:
    """Ephemeral system snippet so the model sees @handle (never cached with SKILL)."""
    username = (identity.get("username") or "").strip().lstrip("@")
    name = (identity.get("name") or "").strip()
    if not username and not name:
        return ""
    parts = ["## Shopper Instagram identity"]
    if username:
        parts.append(f"<instagram_username>@{username}</instagram_username>")
    if name:
        parts.append(f"<instagram_display_name>{name}</instagram_display_name>")
    parts.append(
        "Use their handle naturally when it helps (not every sentence). "
        "Do not claim you \"looked up\" private account details beyond this."
    )
    return "\n".join(parts)


def _mem0_remember_ig_identity(mem_uid: str, username: str | None, name: str | None) -> bool:
    """One-shot semantic memory so hosted mem0 can recall @handle like other facts."""
    try:
        from storage import is_mem0_configured, remember  # noqa: PLC0415
    except Exception:
        return False
    if not is_mem0_configured():
        return False
    u = (username or "").strip().lstrip("@")
    nm = (name or "").strip()
    if u:
        user_line = f"My Instagram username is @{u}." + (f" Display name: {nm}." if nm else "")
    elif nm:
        user_line = f"My Instagram display name is {nm}."
    else:
        return False
    asst_line = (
        "Recorded the shopper's Instagram identity for this DM channel — "
        "prefer @username when addressing them in IG."
    )

    class _Turn:
        __slots__ = ("role", "text")

        def __init__(self, role: str, text: str) -> None:
            self.role = role
            self.text = text

    async def _go() -> bool:
        return await remember(
            mem_uid,
            [_Turn("user", user_line), _Turn("assistant", asst_line)],
            metadata={
                "app": "vitrina",
                "kind": "instagram_identity",
                "instagram_username": u or None,
            },
        )

    try:
        return bool(asyncio.run(_go()))
    except RuntimeError as exc:
        if "cannot be called from a running event loop" not in str(exc).lower():
            logger.warning("ig_dm: mem0 ig identity unexpected RuntimeError: %s", exc)
        return False
    except Exception as exc:
        logger.warning("ig_dm: mem0 ig identity failed: %s", exc)
        return False


def _sync_instagram_profile_metadata(
    thread_id: str,
    cart_key: str,
    sender_psid: str,
) -> dict[str, str | None]:
    """Fetch IG username/name once, persist to ``chat_threads.metadata``, optional Mem0."""
    mcp_dir = Path(__file__).resolve().parents[2] / "mcp"
    mcp_str = str(mcp_dir)
    added = mcp_str not in sys.path
    if added:
        sys.path.insert(0, mcp_str)
    try:
        import chat_threads as ct  # noqa: PLC0415

        row = ct.get_thread(thread_id, cart_key=cart_key)
    except Exception:
        return {}
    finally:
        if added and mcp_str in sys.path:
            sys.path.remove(mcp_str)

    if not isinstance(row, dict):
        return {}
    meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    had_mem0 = bool(meta.get("instagram_identity_mem0_at"))

    existing_u = (meta.get("instagram_username") or "").strip().lstrip("@")
    existing_n = (meta.get("instagram_name") or "").strip()
    if existing_u:
        return {"username": existing_u, "name": existing_n or None}

    prof = fetch_instagram_user_profile(sender_psid)
    ru, rn = prof.get("username"), prof.get("name")
    username = str(ru).strip().lstrip("@") if isinstance(ru, str) and ru.strip() else ""
    name = str(rn).strip() if isinstance(rn, str) and rn.strip() else ""
    if not username and not name:
        return {}

    now_iso = datetime.now(timezone.utc).isoformat()
    patch: dict[str, Any] = {
        "instagram_profile_synced_at": now_iso,
    }
    if username:
        patch["instagram_username"] = username
    if name:
        patch["instagram_name"] = name

    added = mcp_str not in sys.path
    if added:
        sys.path.insert(0, mcp_str)
    try:
        import chat_threads as ct  # noqa: PLC0415

        ct.patch_thread_metadata(thread_id, patch, cart_key=cart_key)
    except Exception:
        logger.exception("ig_dm: patch instagram profile metadata failed thread=%s", thread_id)
        return {}
    finally:
        if added and mcp_str in sys.path:
            sys.path.remove(mcp_str)

    logger.info(
        "ig_dm: instagram profile stored thread=%s username=%s",
        thread_id,
        username or "(none)",
    )

    mem_uid = ig_memory_user_id(sender_psid)
    if not had_mem0 and (username or name):
        ok = _mem0_remember_ig_identity(mem_uid, username or None, name or None)
        if ok:
            added = mcp_str not in sys.path
            if added:
                sys.path.insert(0, mcp_str)
            try:
                import chat_threads as ct  # noqa: PLC0415

                ct.patch_thread_metadata(
                    thread_id,
                    {"instagram_identity_mem0_at": datetime.now(timezone.utc).isoformat()},
                    cart_key=cart_key,
                )
            except Exception:
                logger.debug("ig_dm: mem0 timestamp patch failed thread=%s", thread_id)
            finally:
                if added and mcp_str in sys.path:
                    sys.path.remove(mcp_str)

    return {"username": username or None, "name": name or None}


def _send_proactive_location_link(
    thread_id: str, cart_key: str, recipient_psid: str
) -> bool:
    """Mint the magic-link URL and send it in DM without relying on the model.

    Tool results are queued in :class:`InstagramStreamAdapter` until the next
    text bubble flushes them — but unlike the web SSE path we never called
    ``adapter.close()`` after ``run_turn``, so checkout/location URLs could
    stay buffered forever when the model emitted little or no ``[BREAK]``
    text. Proactive send bypasses that for location setup specifically.
    """
    mcp_dir = Path(__file__).resolve().parents[2] / "mcp"
    mcp_str = str(mcp_dir)
    added = mcp_str not in sys.path
    if added:
        sys.path.insert(0, mcp_str)
    try:
        from ig_channel_tools import location_create_link  # noqa: PLC0415

        out = location_create_link(thread_id=thread_id, cart_key=cart_key)
    except Exception:
        logger.exception("ig_dm: proactive location link exception thread=%s", thread_id)
        return False
    finally:
        if added and mcp_str in sys.path:
            sys.path.remove(mcp_str)
    if out.get("ok") and isinstance(out.get("url"), str):
        url_str = str(out["url"])
        # #region debug instrumentation — magic-link URL audit (session 1d70ef)
        logger.info(
            "MAGIC_LINK_AUDIT-1d70ef location url=%s thread=%s "
            "VITRINA_PUBLIC_HOST=%r VITRINA_PUBLIC_URL=%r VITRINA_PUBLIC_BASE_URL=%r",
            url_str, thread_id,
            os.environ.get("VITRINA_PUBLIC_HOST"),
            os.environ.get("VITRINA_PUBLIC_URL"),
            os.environ.get("VITRINA_PUBLIC_BASE_URL"),
        )
        # #endregion
        res = send_message(recipient_psid, url_str)
        if _ig_send_ok(res):
            logger.info("ig_dm: proactive location link sent thread=%s url=%s", thread_id, url_str)
            return True
        logger.error(
            "ig_dm: proactive location link Graph failure thread=%s res=%s",
            thread_id,
            res,
        )
        return False
    logger.warning(
        "ig_dm: proactive location link not sent thread=%s payload=%s",
        thread_id,
        out,
    )
    return False


def send_messages(recipient_id: str, text: str) -> bool:
    """Send assistant output as one Graph API message per ``[BREAK]`` segment.

    Returns True if at least one segment was accepted by Graph. On partial
    failure, logs and returns False so callers can retry or send a short fallback.
    """
    text = plain_text_for_instagram_dm(text)
    if not text.strip():
        return False
    parts = split_reply(text)
    if not parts:
        return False
    tracing.trace(
        "ig.send.bubbles",
        recipient_id=recipient_id,
        bubble_count=len(parts),
        total_chars=sum(len(p) for p in parts),
    )
    any_ok = False
    for i, chunk in enumerate(parts):
        res = send_message(recipient_id, chunk)
        ok = _ig_send_ok(res)
        tracing.trace(
            "ig.send.bubble",
            recipient_id=recipient_id,
            idx=i,
            chars=len(chunk),
            ok=ok,
            error=(str(res)[:200] if not ok else None),
        )
        if ok:
            any_ok = True
        else:
            logger.error(
                "ig_dm: send_messages chunk failed recipient=%s idx=%s res=%s",
                recipient_id,
                i,
                res,
            )
        if i + 1 < len(parts):
            time.sleep(0.35)
    return any_ok


def send_sender_action(recipient_id: str, action: str) -> None:
    """Best-effort ``sender_action`` ping (``typing_on`` / ``typing_off`` / ``mark_seen``).

    Instagram typing is driven from :class:`InstagramStreamAdapter` Hermes
    callbacks (thinking, tool start/progress, stream deltas) so the bubble
    tracks real model/tool work instead of a timed keepalive. Failures are
    swallowed.
    """
    token = os.getenv("INSTAGRAM_PAGE_ACCESS_TOKEN")
    ig_user_id = os.getenv("INSTAGRAM_BUSINESS_ID")
    if not token:
        return
    url = _graph_messages_endpoint(token, ig_user_id)
    body = {"recipient": {"id": recipient_id}, "sender_action": action}
    try:
        with http_client_sync(timeout=5.0) as client:
            client.post(url, params={"access_token": token}, json=body)
    except Exception:  # noqa: BLE001
        return


# ── Hermes streaming adapter ─────────────────────────────────────────


_BREAK_TOKEN = "[BREAK]"


def _partial_break_suffix(buf: str) -> int:
    """How many trailing chars of *buf* could still complete ``[BREAK]``."""
    if not buf:
        return 0
    tok = _BREAK_TOKEN
    for i in range(min(len(buf), len(tok) - 1), 0, -1):
        if buf.endswith(tok[:i]):
            return i
    return 0


# Meta clears each ``typing_on`` bubble after ~10s; throttle refreshes.
_IG_TYPING_REFRESH_S = 7.5


class InstagramStreamAdapter:
    """Hermes adapter that flushes one Graph DM per ``[BREAK]`` chunk in real time.

    Mirrors the contract of ``HermesSSEAdapter`` (same callbacks Hermes calls
    during ``run_conversation``) so the model can interleave bubbles + tool
    calls naturally — exactly like the web side. Bubbles fire as soon as
    they're complete, not at the end of the turn.
    """

    def __init__(self, recipient_id: str, *, max_chars: int = 990, gap_s: float = 0.35) -> None:
        self.recipient_id = recipient_id
        self.max_chars = max_chars
        self.gap_s = gap_s
        self._raw_chunks: list[str] = []
        self._pending = ""
        self._sent_any = False
        self._collected_widgets: list[dict[str, Any]] = []
        self._pending_share_urls: list[str] = []
        self._tool_call_count = 0
        self._lock = threading.Lock()
        self.dispatched_location_magic_link = False
        self._last_ig_typing = 0.0

    def _typing_busy(self) -> None:
        """Refresh IG typing while Hermes is doing real work (LLM / tools).

        Meta auto-clears each ``typing_on`` after ~10s; throttle so we do not
        hammer the Send API on every stream token.
        """
        now = time.monotonic()
        if now - self._last_ig_typing < _IG_TYPING_REFRESH_S:
            return
        self._last_ig_typing = now
        send_sender_action(self.recipient_id, "typing_on")

    def _typing_clear(self) -> None:
        send_sender_action(self.recipient_id, "typing_off")

    # ── Hermes callbacks (subset; the rest are noops) ──────────────

    def on_stream_delta(self, delta: str | None) -> None:
        if not delta:
            # End-of-segment from Hermes — flush any complete chunk
            # immediately so a bubble lands while the model decides
            # to call a tool.
            self._flush_segment()
            return
        self._typing_busy()
        with self._lock:
            self._raw_chunks.append(delta)
            self._pending += delta
            while _BREAK_TOKEN in self._pending:
                before, _, after = self._pending.partition(_BREAK_TOKEN)
                self._pending = after
                if before.strip():
                    self._dispatch(before.strip())

    def on_tool_start(self, *_args: Any, **_kw: Any) -> None:
        self._typing_busy()
        self._tool_call_count += 1
        # A tool call is about to fire — flush any partially-complete
        # bubble so the user sees the lead-in BEFORE the tool latency.
        self._flush_segment(tail_only=True)

    def on_tool_complete(self, _tcid: Any, _name: str, _args: Any, result: Any) -> None:
        widget = _extract_widget(result)
        if widget:
            self._collected_widgets.append(widget)
        # If the tool returned a sharable URL (checkout link, Reel,
        # location-magic-link), surface it as its own bubble AFTER any
        # explanatory text the model writes next. Buffer here; the
        # next ``[BREAK]`` flush emits it.
        url = _extract_share_url(result)
        if url:
            self._pending_share_urls.append(url)

    def on_thinking(self, message: str | None = None, **_: Any) -> None:
        self._typing_busy()

    def on_reasoning(self, *_a: Any, **_kw: Any) -> None:
        return None

    def on_step(self, *args: Any, **kwargs: Any) -> None:
        label = kwargs.get("label") or kwargs.get("description")
        if not label:
            for arg in args:
                if isinstance(arg, str) and arg.strip():
                    label = arg
                    break
        if label:
            self._typing_busy()

    def on_tool_progress(
        self,
        event_type: str = "",
        tool_name: str | None = None,
        preview: str | None = None,
        args: Any = None,
        *,
        duration: float | None = None,
        is_error: bool = False,
        **_: Any,
    ) -> None:
        if event_type == "tool.started":
            self._typing_busy()

    def on_status(self, *_a: Any, **_kw: Any) -> None:
        return None

    def on_clarify(self, *_a: Any, **_kw: Any) -> None:
        return None

    # ── lifecycle parity with HermesSSEAdapter ─────────────────────

    def push_text(self, text: str) -> None:
        if not text:
            return
        self._raw_chunks.append(text)
        self._dispatch(text.strip())

    def push_event(self, _event: dict[str, Any]) -> None:
        return None

    def close(self, _reason: str = "stop") -> None:
        with self._lock:
            tail = self._pending
            self._pending = ""
        if tail.strip():
            self._dispatch(tail.strip())
        tracing.trace(
            "ig.adapter.close",
            reason=_reason,
            sent_any=self._sent_any,
            widgets=len(self._collected_widgets),
            pending_share_urls=len(self._pending_share_urls),
            raw_chunks=len(self._raw_chunks),
        )
        self._flush_share_urls()
        self._typing_clear()

    def fail(self, _exc: BaseException) -> None:
        tracing.trace(
            "ig.adapter.fail",
            exc_type=type(_exc).__name__,
            exc=str(_exc)[:200],
            sent_any=self._sent_any,
        )
        return self.close("error")

    @property
    def assistant_text(self) -> str:
        raw = "".join(self._raw_chunks)
        return raw.replace(_BREAK_TOKEN, "\n\n")

    @property
    def widgets(self) -> list[dict[str, Any]]:
        return list(self._collected_widgets)

    @property
    def assistant_bubbles(self) -> list[str]:
        """Per-bubble messages, mirroring :class:`HermesSSEAdapter`.

        The IG adapter dispatches each ``[BREAK]``-segment as its own
        Graph send, so ``_raw_chunks`` already contains one entry per
        bubble. We just trim and drop empties for parity with the web
        adapter — `channel.run_turn` reads this for both persistence
        and tracing, and `assistant_bubbles` missing here was the
        AttributeError that tanked IG turns mid-conversation.
        """
        out: list[str] = []
        for chunk in self._raw_chunks:
            text = chunk.replace(_BREAK_TOKEN, "").strip()
            if text:
                out.append(text)
        return out

    @property
    def tool_call_count(self) -> int:
        return self._tool_call_count

    # ── internals ──────────────────────────────────────────────────

    def _flush_segment(self, *, tail_only: bool = False) -> None:
        """Emit any bubble whose end is unambiguous.

        ``tail_only=True`` is used right before a tool call so we send
        whatever's pending even if the model didn't end with ``[BREAK]``.
        """
        with self._lock:
            buf = self._pending
            if not buf:
                return
            hold = 0 if tail_only else _partial_break_suffix(buf)
            safe = buf if hold == 0 else buf[: len(buf) - hold]
            self._pending = buf[len(buf) - hold :] if hold else ""
        text = safe.strip()
        if text:
            self._dispatch(text)

    def _dispatch(self, text: str) -> None:
        text = plain_text_for_instagram_dm(text)
        if not text:
            self._flush_share_urls()
            self._typing_clear()
            return
        chunks = [text[i : i + self.max_chars] for i in range(0, len(text), self.max_chars)] or [text]
        for i, c in enumerate(chunks):
            res = send_message(self.recipient_id, c)
            if _ig_send_ok(res):
                self._sent_any = True
            else:
                logger.error(
                    "ig_dm: _dispatch Graph failure recipient=%s chunk=%s res=%s",
                    self.recipient_id,
                    i,
                    res,
                )
            if i + 1 < len(chunks):
                time.sleep(self.gap_s)
        time.sleep(self.gap_s)
        # Now that an explanatory bubble has landed, release any
        # tool-driven URLs (checkout link, Reel, etc.) as their own
        # bubble — IG auto-renders Reel/TikTok/Shorts URLs as inline
        # cards, and Stripe links become tappable.
        self._flush_share_urls()
        self._typing_clear()

    def _flush_share_urls(self) -> None:
        urls: list[str] = []
        with self._lock:
            urls = self._pending_share_urls
            self._pending_share_urls = []
        for url in urls:
            res = send_message(self.recipient_id, url)
            if _ig_send_ok(res):
                self._sent_any = True
            else:
                logger.error(
                    "ig_dm: share URL send failed recipient=%s res=%s url=%s…",
                    self.recipient_id,
                    res,
                    url[:80],
                )
            low = url.lower()
            if "/ig/location" in low or "?s=" in low:
                self.dispatched_location_magic_link = True
            time.sleep(self.gap_s)


def _extract_widget(result: Any) -> dict[str, Any] | None:
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (TypeError, ValueError):
            return None
    if isinstance(result, dict):
        w = result.get("_widget")
        if isinstance(w, dict) and "type" in w:
            return w
    return None


def _extract_share_url(result: Any) -> str | None:
    """Pick a tappable URL out of a tool result for IG to auto-render."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (TypeError, ValueError):
            return None
    if not isinstance(result, dict):
        return None
    candidates = [
        result.get("url"),
        (result.get("payment_link") or {}).get("url") if isinstance(result.get("payment_link"), dict) else None,
        (result.get("checkout") or {}).get("url") if isinstance(result.get("checkout"), dict) else None,
        result.get("video_url"),
        result.get("permalink"),
    ]
    for u in candidates:
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            return u
    return None


def fire_location_confirmed_turn(
    *,
    thread_id: str,
    cart_key: str | None,
    metadata: dict[str, Any],
) -> None:
    """Hermes turn after the shopper confirms location on vitrina.app (web UI).

    Sends the assistant reply back to Instagram using the same multi-bubble
    splitter as normal DM turns.
    """
    loc = metadata.get("location") if isinstance(metadata.get("location"), dict) else {}
    label = str(loc.get("label") or "your area")
    lat = float(loc.get("lat") or 0.0)
    lng = float(loc.get("lng") or 0.0)
    psid = str(metadata.get("instagram_psid") or "")
    if not psid and cart_key and str(cart_key).startswith("ig:"):
        psid = str(cart_key).removeprefix("ig:")
    if not psid:
        logger.warning("ig_dm: location callback missing psid thread=%s", thread_id)
        return

    mem_uid = ig_memory_user_id(psid)
    ck = cart_key if cart_key else f"ig:{psid}"
    ig_identity = _sync_instagram_profile_metadata(thread_id, ck, psid)
    ig_identity_extra = _format_instagram_identity_ephemeral(ig_identity)
    user_location = {
        "lat": lat,
        "lng": lng,
        "label": label,
        "precision": str(loc.get("precision") or "approximate"),
        "source": str(loc.get("source") or "ig_link"),
    }
    msg = (
        f"[system] The shopper confirmed their pickup area: {label}. "
        f"Coordinates: {lat:.5f},{lng:.5f}. Continue helping them shop."
    )
    adapter = InstagramStreamAdapter(psid)
    try:
        result = channel_gateway.run_turn(
            channel=CHANNEL,
            user_message=msg,
            thread_id=thread_id,
            user_id=None,
            memory_user_id=mem_uid,
            cart_key=ck,
            cart_ids=None,
            user_location=user_location,
            adapter=adapter,
            ephemeral_extra=ig_identity_extra or None,
        )
        adapter.close("stop")
        if not adapter._sent_any:
            reply = plain_text_for_instagram_dm((result.assistant_text or "").strip())
            if reply:
                if not send_messages(psid, reply):
                    fb = send_message(psid, "Got it — what next?")
                    if not _ig_send_ok(fb):
                        logger.error("ig_dm: location-turn fallback send failed psid=%s", psid)
            else:
                fb = send_message(psid, "Got it — what next?")
                if not _ig_send_ok(fb):
                    logger.error("ig_dm: location-turn empty assistant fallback failed psid=%s", psid)
    finally:
        send_sender_action(psid, "typing_off")


def _handle_one(msg: dict[str, Any]) -> None:
    """Run one Hermes turn and post the reply back."""
    sender_id = msg["sender_id"]
    text = msg["text"]
    cart_key = f"ig:{sender_id}"
    thread_id = ig_thread_id(sender_id)
    mem_uid = ig_memory_user_id(sender_id)

    # IG webhook is the entry point — start the turn here so the
    # IG-specific pre-work (location magic link, identity sync) shares
    # the same turn_id as the Hermes loop and outbound DMs.
    turn_id = tracing.start_turn(
        channel=CHANNEL,
        thread_id=thread_id,
        user_id=None,
        cart_key=cart_key,
        user_message=text,
        source_msg_id=msg.get("mid"),
    )

    # Instant feedback BEFORE the slow turn (no extra text bubble — the
    # model's first line + ``recipes.extract_from_url_async`` ack_text were
    # stacking with this and felt like spam: "Got your reel" + "Spotted the
    # reel" + "On it…". Read receipt only; typing tracks Hermes callbacks.
    send_sender_action(sender_id, "mark_seen")

    try:
        urls = extract_urls(text)
        # A "reel link" for our purposes is anything the extractor can ingest:
        # public ``instagram.com/reel/…`` URLs (yt-dlp) OR Meta IG Messaging
        # CDN ``lookaside.fbsbx.com/ig_messaging_cdn`` MP4 URLs (httpx direct).
        def _is_extractable_reel(s: str) -> bool:
            low = (s or "").lower()
            return has_instagram_permalink_in_text(s) or "lookaside.fbsbx.com" in low
        has_reel_link = _is_extractable_reel(text) or any(_is_extractable_reel(u) for u in urls)
        user_message = text
        if urls:
            user_message = f"{text}\n\n[User shared links: {', '.join(urls)}]"

        reel_cdn_only = bool(msg.get("reel_cdn_only"))
        _ensure_thread_row(thread_id, cart_key, sender_id)

        recipe_only = os.getenv("SOCIAL_RECIPES_PROFILE") == "recipe"
        ig_identity = {} if recipe_only else _sync_instagram_profile_metadata(thread_id, cart_key, sender_id)
        ig_identity_extra = _format_instagram_identity_ephemeral(ig_identity)

        user_location = None if recipe_only else _load_thread_user_location(thread_id, cart_key)

        proactive_location_before_turn = not user_location and not has_reel_link
        extra_blocks: list[str] = []
        if not recipe_only and not user_location:
            if proactive_location_before_turn:
                link_sent = _send_proactive_location_link(thread_id, cart_key, sender_id)
                if link_sent:
                    extra_blocks.append(
                        "[Session: no pickup area saved yet. A tap-to-set-area link was "
                        "already sent to the shopper in its own DM bubble just now. "
                        "Do NOT call `ig.request_location` again this turn (that would "
                        "invalidate the link). Continue with guidance until they confirm "
                        "an area on a later message.]"
                    )
                else:
                    extra_blocks.append(
                        "[Session: no pickup area saved for this chat yet. If the shopper "
                        "is trying to shop or extract a recipe, call `ig.request_location` "
                        "before any recipe tools (`recipes.extract_from_url*`, "
                        "`recipes.discover_social`, etc.) and send the returned magic link.]"
                    )
            else:
                extra_blocks.append(
                    "[Session: No pickup area yet — a location link will follow this turn "
                    "after reel handling. Do NOT call `ig.request_location` this turn "
                    "(would duplicate / invalidate).]"
                )
        if urls:
            extra_blocks.append(
                "[System: Current-turn shared URLs are authoritative for this turn. "
                "When calling recipe tools for this message, use one of these URLs "
                f"verbatim and ignore older links in chat history: {', '.join(urls)}]"
            )
        elif not recipe_only and not has_reel_link:
            staged = _load_staged_recipe(cart_key)
            if staged and _ig_user_signals_staged_recipe_followup(text):
                title_part = (
                    f" ({staged['title']})" if staged.get("title") else ""
                )
                extra_blocks.append(
                    "[System: A recipe was recently extracted for this shopper "
                    f"and is **staged but not yet in the basket**: "
                    f"recipe_id=`{staged['recipe_id']}`{title_part}. "
                    "If this turn confirms they want **that** recipe in the basket, "
                    "call `recipes.add_to_basket` with this recipe_id (do NOT "
                    "re-extract, do NOT ask 'which recipe'). "
                    "If they are on a new topic, ignore this block entirely.]"
                )
        has_lookaside = any("lookaside.fbsbx.com" in (u or "").lower() for u in urls)
        logger.info(
            "ig_dm: lookaside_check thread=%s urls=%d has_lookaside=%s",
            thread_id,
            len(urls),
            has_lookaside,
        )
        if reel_cdn_only:
            # Rare: non-lookaside CDN attachment (e.g. cdninstagram.com photo
            # preview) — not consumable by the extractor.  Lookaside MP4 shares
            # are handled by the reel-first system preamble when present.
            logger.warning(
                "ig_dm: reel_cdn_only thread=%s sender=%s — non-extractable CDN host",
                thread_id,
                sender_id,
            )
            extra_blocks.append(
                "[System: No usable URL could be resolved from Meta for this "
                "attachment (non-extractable CDN host) — do NOT pass these URLs "
                "to `recipes.extract_from_url*`. Offer similar recipes via "
                "`recipes.discover_social` from any caption/hint in the thread, or "
                "`recipes.synthesize_from_brief`, but never ask the shopper to paste a link.]"
            )
        if extra_blocks:
            user_message = f"{user_message}\n\n" + "\n\n".join(extra_blocks)

        if has_reel_link:
            la_note = (
                " A Meta CDN link (lookaside…?asset_id=…) is fine — pass that URL "
                "verbatim to `recipes.extract_from_url_async`."
                if has_lookaside
                else ""
            )
            user_message = (
                "[System: The shopper included a social video link this turn. "
                "You cannot see inside the clip without running extraction tools — "
                "but if their text or thread already says it is not food, **do not** "
                "force those tools; acknowledge and move on like a person. "
                "If it is plausibly food or cooking, prioritise it (extract, describe, "
                "or offer to add to the basket) before unrelated small talk. "
                "If it is clearly **not** food (music, drumming, sports, random meme, "
                "pets with no dish, etc.), one short honest line — **no** recipe extraction "
                "or grocery steering — then answer what they actually asked. "
                "Do not sound stuck on an earlier reel unless they bring it back."
                + la_note
                + "]\n\n"
                + user_message
            )

        if recipe_only:
            user_message = text
            if urls:
                user_message += "\n\n[Current shared URLs: " + ", ".join(urls) + "]"
        t0 = time.perf_counter()
        logger.info(
            "ig_dm: turn sender=%s thread=%s text_chars=%s",
            sender_id,
            thread_id,
            len(text),
        )

        adapter = InstagramStreamAdapter(sender_id)
        tracing.trace(
            "ig.run_turn.pre",
            sender_id=sender_id,
            thread_id=thread_id,
            cart_key=cart_key,
            mem_uid=mem_uid,
            has_user_location=bool(user_location),
            has_identity_extra=bool(ig_identity_extra),
            shared_urls=len(urls),
            user_message_chars=len(user_message),
            reel_cdn_only=reel_cdn_only,
        )
        result = channel_gateway.run_turn(
            channel=CHANNEL,
            user_message=user_message,
            thread_id=thread_id,
            user_id=None,
            memory_user_id=mem_uid,
            cart_key=cart_key,
            cart_ids=None,
            user_location=user_location,
            adapter=adapter,
            ephemeral_extra=ig_identity_extra or None,
        )
        tracing.trace(
            "ig.run_turn.post",
            sender_id=sender_id,
            sent_any=adapter._sent_any,
            widget_types=[w.get("type") for w in adapter._collected_widgets],
            text_chars=len(adapter.assistant_text or ""),
            error=(result.error or "")[:200] if result and result.error else None,
        )
        # Flush buffered tool URLs (Stripe, location from tools, etc.). Web SSE does
        # this via adapter.close after run_turn; DM path must too or URLs never send.
        adapter.close("stop")

        # Reel shares: location magic link comes *after* the model turn so it does not
        # jump ahead of "on it" / extract tooling.
        user_location_after = None if recipe_only else _load_thread_user_location(thread_id, cart_key)
        if (
            not recipe_only
            and not user_location_after
            and has_reel_link
            and not adapter.dispatched_location_magic_link
        ):
            _send_proactive_location_link(thread_id, cart_key, sender_id)

        logger.info(
            "ig_dm: turn finished sender=%s elapsed_s=%.2f sent_any=%s",
            sender_id,
            time.perf_counter() - t0,
            adapter._sent_any,
        )

        if not adapter._sent_any:
            # Streaming adapter never sent a bubble (e.g. pure tool-only
            # turn, or model produced empty text). Fall back to whatever
            # final text Hermes accumulated so the user always gets a reply.
            reply = plain_text_for_instagram_dm((result.assistant_text or "").strip())
            if reply:
                if not send_messages(sender_id, reply):
                    fb = send_message(sender_id, "Got it — what next?")
                    if not _ig_send_ok(fb):
                        logger.error(
                            "ig_dm: fallback DM delivery failed sender=%s err=%s",
                            sender_id,
                            result.error,
                        )
            else:
                fb = send_message(sender_id, "Got it — what next?")
                if not _ig_send_ok(fb):
                    logger.error(
                        "ig_dm: empty assistant_text and fallback send failed sender=%s err=%s",
                        sender_id,
                        result.error,
                    )
                else:
                    logger.warning(
                        "ig_dm: empty assistant_text sender=%s err=%s — sent fallback",
                        sender_id,
                        result.error,
                    )
    finally:
        send_sender_action(sender_id, "typing_off")
        # Finish the turn we started at the top of this function. Status
        # mirrors what the shopper actually saw in their DM thread — not
        # internal "did the LLM emit text" semantics — because that's
        # the question we want the trace to answer at a glance.
        try:
            _adapter_text = locals().get("adapter")
            _final_text = (_adapter_text.assistant_text if _adapter_text else "") or ""
            _widgets = _adapter_text._collected_widgets if _adapter_text else []
            _sent = bool(getattr(_adapter_text, "_sent_any", False))
            _err = (locals().get("result").error if locals().get("result") else None)
            if _err:
                _status = "error"
            elif not _sent and not _final_text.strip():
                _status = "silent"
            else:
                _status = "ok"
            tracing.finish_turn(
                turn_id,
                status=_status,
                error=_err,
                assistant_text=_final_text,
                widgets=_widgets,
                bubbles=len(getattr(_adapter_text, "_raw_chunks", []) or []),
                tool_calls=getattr(locals().get("result"), "tool_calls", 0)
                or getattr(_adapter_text, "tool_call_count", 0),
            )
        except Exception:
            tracing.finish_turn(turn_id, status="error", error="finish_turn_exception")

