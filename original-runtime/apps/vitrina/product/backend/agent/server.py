"""Vitrina shopper-agent FastAPI surface — Hermes-backed.

Endpoints:

- ``POST /v1/chat/stream`` — Vercel AI-SDK SSE stream (web channel).
- ``POST /v1/instagram/webhook`` — Meta Graph DM webhook (scaffold).
- ``POST /v1/tiktok/webhook`` — TikTok DM webhook (scaffold).
- ``POST /v1/ig/location`` — Instagram magic-link location confirm (→ proactive DM).

Every channel funnels through :func:`platforms.channel.run_turn`, so
the web SSE route here is just "spawn a thread that calls run_turn,
yield the adapter's queue as SSE bytes". DMs and the terminal repl
use the same entry point.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """So Railway shows app log lines (e.g. ig_dm) at INFO, not only uvicorn access."""
    level_name = (os.getenv("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(levelname)s [%(name)s] %(message)s",
            stream=sys.stdout,
            force=True,
        )
    else:
        root.setLevel(level)
    for log_name in ("platforms.instagram_dm", "platforms.channel", "agent_factory", "session_source"):
        logging.getLogger(log_name).setLevel(level)


_configure_logging()


def _user_id_from_bearer(authorization: str | None) -> str | None:
    """Extract ``sub`` from a Supabase JWT without verifying the signature.

    The agent trusts the JWT was issued by Supabase (verified upstream by
    the Vercel edge / client SDK). We only need the identity so we can
    route cart & memory operations to the right user.  If the header is
    absent, malformed, or the payload is unparseable we return ``None``
    and the turn continues anonymously.
    """
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        # JWT payload is base64url without padding
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        sub = payload.get("sub")
        return str(sub) if sub else None
    except Exception:
        return None

try:
    from . import agent_factory, sse_adapter
    from .platforms import channel as channel_gateway, instagram_dm, tiktok_dm
except ImportError:  # uvicorn invoked from /app/vitrina_agent
    import agent_factory  # type: ignore
    import sse_adapter  # type: ignore
    from platforms import channel as channel_gateway, instagram_dm, tiktok_dm  # type: ignore


app = FastAPI(title="Vitrina agent (Hermes)", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tightened in prod via env, same pattern as api/main.py
    allow_methods=["*"],
    allow_headers=["*"],
)


class StreamChatMessage(BaseModel):
    role: str
    content: str


class StreamChatRequest(BaseModel):
    """``POST /v1/chat/stream`` payload — kept identical to the legacy."""

    messages: list[StreamChatMessage]
    thread_id: str | None = None
    user_id: str | None = None
    cart_key: str | None = None
    cart_ids: list[str] | None = None
    user_location: dict[str, Any] | None = None


class MagicLocationConfirm(BaseModel):
    """``POST /v1/magic/location/confirm`` body."""

    slug: str
    lat: float
    lng: float
    label: str


class MagicCheckoutAuth(BaseModel):
    """``POST /v1/magic/checkout/auth`` body — guest email stamping."""

    slug: str
    email: str


class MagicCheckoutSession(BaseModel):
    """``POST /v1/magic/checkout/session`` body — create embedded Stripe session."""

    slug: str
    email: str | None = None
    slot: dict[str, Any] | None = None


@app.get("/health")
def health() -> dict[str, Any]:
    profile = agent_factory._resolve_provider()
    return {
        "status": "ok",
        "service": "vitrina-agent",
        "live": profile is not None,
        "provider": profile.name if profile else "stub",
        "model": profile.model if profile else "stub",
    }


# ── /v1/chat/stream ───────────────────────────────────────────────────


@app.post("/v1/chat/stream")
async def chat_stream(
    req: StreamChatRequest,
    authorization: str | None = Header(None),
) -> StreamingResponse:
    # Prefer the explicit body field; fall back to the JWT sub so
    # anonymous-cart tools always have an identity to inject.
    user_id = req.user_id or _user_id_from_bearer(authorization)

    adapter = sse_adapter.HermesSSEAdapter()

    def _wrap() -> None:
        try:
            channel_gateway.run_turn(
                channel="web",
                messages=[m.model_dump() for m in req.messages],
                thread_id=req.thread_id,
                user_id=user_id,
                cart_key=req.cart_key,
                cart_ids=req.cart_ids,
                user_location=req.user_location,
                adapter=adapter,
            )
        except Exception as exc:  # noqa: BLE001
            adapter.fail(exc)
        else:
            adapter.close("stop")

    threading.Thread(target=_wrap, daemon=True).start()

    return StreamingResponse(
        adapter.stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Magic-link endpoints (/v1/magic/…) ───────────────────────────────
#
# Channel-agnostic short links minted by ``location.create_link`` and
# ``checkout.create_link`` MCP tools.  Used by vitrina.bg/location and
# vitrina.bg/checkout pages — same wire contract for IG, ChatGPT, Claude, etc.


def _mcp_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "mcp"


def _ensure_mcp_in_path() -> bool:
    mcp_str = str(_mcp_dir())
    if mcp_str not in sys.path:
        sys.path.insert(0, mcp_str)
        return True
    return False


# ── Location ──────────────────────────────────────────────────────────

@app.get("/v1/magic/location/lookup")
async def magic_location_lookup(s: str) -> JSONResponse:
    """Resolve ``?s=`` slug → thread_id + cart_key for LocationPage."""
    added = _ensure_mcp_in_path()
    try:
        import location_tokens as lt  # noqa: PLC0415

        row = lt.lookup(s.strip())
    except ImportError as exc:  # pragma: no cover
        logger.error("magic_location_lookup: import error: %s", exc)
        return JSONResponse({"ok": False, "error": "server_misconfigured"}, status_code=500)
    finally:
        if added:
            mcp_str = str(_mcp_dir())
            if mcp_str in sys.path:
                sys.path.remove(mcp_str)
    if not row:
        return JSONResponse({"ok": False, "error": "not_found_or_expired"}, status_code=404)
    return JSONResponse({"ok": True, "thread_id": row["thread_id"], "cart_key": row["cart_key"]})


@app.post("/v1/magic/location/confirm")
async def magic_location_confirm(body: MagicLocationConfirm) -> JSONResponse:
    """Stamp location on thread + mark token redeemed, then fire proactive DM."""
    added = _ensure_mcp_in_path()
    thread_id: str | None = None
    cart_key: str | None = None
    try:
        import location_tokens as lt  # noqa: PLC0415
        import chat_threads as ct  # noqa: PLC0415

        row = lt.lookup(body.slug)
        if not row:
            return JSONResponse({"ok": False, "error": "not_found_or_expired"}, status_code=404)
        thread_id = row["thread_id"]
        cart_key = row["cart_key"]
        loc = {
            "lat": float(body.lat),
            "lng": float(body.lng),
            "label": str(body.label)[:500],
            "source": "magic_link",
            "precision": "approximate",
        }
        ct.patch_thread_metadata(thread_id, {"location": loc}, cart_key=cart_key)
        lt.mark_redeemed(body.slug)
    except ImportError as exc:  # pragma: no cover
        logger.error("magic_location_confirm: import error: %s", exc)
        return JSONResponse({"ok": False, "error": "server_misconfigured"}, status_code=500)
    except Exception as exc:  # noqa: BLE001
        logger.exception("magic_location_confirm failed: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    finally:
        if added:
            mcp_str = str(_mcp_dir())
            if mcp_str in sys.path:
                sys.path.remove(mcp_str)

    def _bg() -> None:
        try:
            instagram_dm.fire_location_confirmed_turn(
                thread_id=thread_id,  # type: ignore[arg-type]
                cart_key=cart_key,
                metadata={"location": {"lat": body.lat, "lng": body.lng, "label": body.label}},
            )
        except Exception:  # noqa: BLE001
            logger.exception("magic_location_confirm: proactive turn failed thread=%s", thread_id)

    threading.Thread(target=_bg, daemon=True).start()
    return JSONResponse({"ok": True})


# ── Checkout ──────────────────────────────────────────────────────────

@app.get("/v1/magic/checkout/lookup")
async def magic_checkout_lookup(s: str) -> JSONResponse:
    """Resolve ``?s=`` slug → basket summary for CheckoutPage."""
    added = _ensure_mcp_in_path()
    try:
        import checkout_tokens as ckt  # noqa: PLC0415
        import cart as cart_mod  # noqa: PLC0415

        row = ckt.lookup(s.strip())
        if not row:
            return JSONResponse({"ok": False, "error": "not_found_or_expired"}, status_code=404)

        # Build a basic per-shop summary from the cart rows.
        cart_ids: list[str] = list(row["cart_ids"] or [])
        shops: list[dict] = []
        total_eur = 0.0
        for cid in cart_ids:
            try:
                c = cart_mod.get_cart(cart_key=row["cart_key"], cart_id=cid)
                if c:
                    item_count = sum(int(ln.get("quantity", 1)) for ln in (c.get("lines") or []))
                    shop_name = c.get("shop_name") or c.get("shop_id") or cid
                    subtotal = float(c.get("subtotal_eur") or 0)
                    total_eur += subtotal
                    shops.append({"id": cid, "name": shop_name, "item_count": item_count})
            except Exception:  # noqa: BLE001
                shops.append({"id": cid, "name": cid, "item_count": 0})

    except ImportError as exc:  # pragma: no cover
        logger.error("magic_checkout_lookup: import error: %s", exc)
        return JSONResponse({"ok": False, "error": "server_misconfigured"}, status_code=500)
    finally:
        if added:
            mcp_str = str(_mcp_dir())
            if mcp_str in sys.path:
                sys.path.remove(mcp_str)

    return JSONResponse({
        "ok": True,
        "cart_ids": cart_ids,
        "cart_key": row["cart_key"],
        "thread_id": row["thread_id"],
        "slot": row.get("slot_payload"),
        "shops": shops,
        "total_eur": round(total_eur, 2),
    })


@app.post("/v1/magic/checkout/auth")
async def magic_checkout_auth(body: MagicCheckoutAuth) -> JSONResponse:
    """Stamp guest email on thread for later cart transfer."""
    added = _ensure_mcp_in_path()
    try:
        import checkout_tokens as ckt  # noqa: PLC0415
        import chat_threads as ct  # noqa: PLC0415

        row = ckt.lookup(body.slug)
        if not row:
            return JSONResponse({"ok": False, "error": "not_found_or_expired"}, status_code=404)
        email_n = body.email.strip().lower()[:320]
        ct.patch_thread_metadata(
            row["thread_id"],
            {"email": email_n},
            cart_key=row["cart_key"],
        )
    except ImportError as exc:  # pragma: no cover
        logger.error("magic_checkout_auth: import error: %s", exc)
        return JSONResponse({"ok": False, "error": "server_misconfigured"}, status_code=500)
    except Exception as exc:  # noqa: BLE001
        logger.exception("magic_checkout_auth failed: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    finally:
        if added:
            mcp_str = str(_mcp_dir())
            if mcp_str in sys.path:
                sys.path.remove(mcp_str)
    return JSONResponse({"ok": True})


@app.post("/v1/magic/checkout/session")
async def magic_checkout_session(body: MagicCheckoutSession) -> JSONResponse:
    """Create an embedded Stripe session for the checkout page."""
    added = _ensure_mcp_in_path()
    try:
        import checkout_tokens as ckt  # noqa: PLC0415
        import checkout as checkout_mod  # noqa: PLC0415

        row = ckt.lookup(body.slug)
        if not row:
            return JSONResponse({"ok": False, "error": "not_found_or_expired"}, status_code=404)

        buyer_email = body.email
        if not buyer_email:
            # Try email stored on the thread from previous auth step.
            try:
                import chat_threads as ct  # noqa: PLC0415
                thread = ct.get_thread(row["thread_id"], cart_key=row["cart_key"])
                meta = thread.get("metadata") if isinstance(thread.get("metadata"), dict) else {}
                buyer_email = meta.get("email") if isinstance(meta.get("email"), str) else None
            except Exception:  # noqa: BLE001
                pass

        slot_arg = body.slot or (row.get("slot_payload") if isinstance(row.get("slot_payload"), dict) else None)

        # Create the multishop group + embedded Stripe session.
        try:
            group = checkout_mod.create_multishop_group(
                list(row["cart_ids"] or []),
                slot=slot_arg,
            )
        except checkout_mod.ShopClosedError as exc:
            return JSONResponse(
                {"ok": False, "error": "shop_closed", "closed_carts": exc.closed_carts},
                status_code=409,
            )

        stripe_result = checkout_mod.create_single_stripe_session_for_group(
            group["group_id"],
            buyer_email=buyer_email,
            return_origin="https://vitrina.bg",
            extra_metadata={
                "checkout_slug": body.slug,
                "cart_key": row["cart_key"],
                "thread_id": row["thread_id"],
            },
        )
        if not stripe_result.get("ok"):
            return JSONResponse(
                {"ok": False, "error": stripe_result.get("error", "stripe_failed")},
                status_code=500,
            )

    except ImportError as exc:  # pragma: no cover
        logger.error("magic_checkout_session: import error: %s", exc)
        return JSONResponse({"ok": False, "error": "server_misconfigured"}, status_code=500)
    except Exception as exc:  # noqa: BLE001
        logger.exception("magic_checkout_session failed slug=%s", body.slug)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    finally:
        if added:
            mcp_str = str(_mcp_dir())
            if mcp_str in sys.path:
                sys.path.remove(mcp_str)

    return JSONResponse({
        "ok": True,
        "client_secret": stripe_result["client_secret"],
        "group_id": group["group_id"],
    })


# ── DM webhooks (scaffolds) ───────────────────────────────────────────


@app.get("/v1/instagram/webhook")
async def instagram_verify(request: Request) -> JSONResponse:
    return await instagram_dm.handle_verify(request)


@app.post("/v1/instagram/webhook")
async def instagram_webhook(request: Request) -> JSONResponse:
    return await instagram_dm.handle_webhook(request)


@app.get("/v1/tiktok/webhook")
async def tiktok_verify(request: Request) -> JSONResponse:
    return await tiktok_dm.handle_verify(request)


@app.post("/v1/tiktok/webhook")
async def tiktok_webhook(request: Request) -> JSONResponse:
    return await tiktok_dm.handle_webhook(request)


# ── Backwards-compat ``app`` symbol for `uvicorn main:app` callers ────


# Older deploy configs might have ``CMD ["uvicorn", "main:app"]`` baked
# into the Dockerfile. New Dockerfile uses ``server:app`` but we expose
# both during the cutover so a stale image still boots.
main_app = app
