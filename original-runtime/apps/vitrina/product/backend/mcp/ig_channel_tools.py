"""Channel-agnostic magic-link helpers (MCP tools).

Previously contained IG-specific logic; the ``location.create_link`` tool
is now channel-agnostic — it works for Instagram DMs, ChatGPT, Claude
Desktop, and any future MCP client.  The IG-only naming has been retired.

Anonymous shoppers use ``cart_key=ig:{psid}``; thread rows use a
deterministic UUID.  Location and email state stash in ``chat_threads.metadata``
(see migration ``005_chat_threads_metadata``).
"""
from __future__ import annotations

import os
from typing import Any

import chat_threads
import location_tokens

logger = __import__("logging").getLogger(__name__)


def _public_base_url() -> str:
    raw = (
        os.getenv("VITRINA_PUBLIC_URL")
        or os.getenv("VITRINA_PUBLIC_HOST")
        or os.getenv("VITRINA_PUBLIC_BASE_URL")
        or "https://vitrina.bg"
    )
    return raw.rstrip("/")


def location_create_link(
    *,
    thread_id: str | None = None,
    cart_key: str | None = None,
) -> dict[str, Any]:
    """Mint a short-lived token and return a channel-agnostic vitrina.bg/location URL.

    The URL uses a 6-character ``s=`` slug that maps to a row in
    ``public.location_tokens``. The web page resolves the slug via
    ``GET /v1/magic/location/lookup``, shows the location modal, and
    posts back to ``POST /v1/magic/location/confirm`` to stamp the thread.
    """
    if not thread_id or not cart_key:
        return {"ok": False, "error": "thread_id_and_cart_key_required"}
    try:
        slug = location_tokens.mint(thread_id=thread_id, cart_key=cart_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("location_create_link mint failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    base = _public_base_url()
    url = f"{base}/location?s={slug}"
    return {"ok": True, "url": url, "expires_in": 600}


def account_attach_email(
    *,
    email: str,
    thread_id: str | None = None,
    cart_key: str | None = None,
) -> dict[str, Any]:
    """Store shopper email on the thread for later linking to a web signup."""
    if not email or "@" not in email:
        return {"ok": False, "error": "invalid_email"}
    if not thread_id or not cart_key:
        return {"ok": False, "error": "thread_id_and_cart_key_required"}
    email_n = email.strip().lower()[:320]
    try:
        chat_threads.patch_thread_metadata(
            thread_id,
            {"email": email_n},
            cart_key=cart_key,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "email": email_n}


HANDLERS: dict[str, Any] = {
    "location.create_link": location_create_link,
    "account.attach_email": account_attach_email,
}
