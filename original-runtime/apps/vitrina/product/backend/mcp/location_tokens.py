"""Location magic-link token management.

Replaces the ad-hoc ``chat_threads.metadata.location_short_slug`` +
``find_thread_by_ig_location_slug`` approach with a clean ``mcp.location_tokens``
table that has explicit expiry and redemption tracking.

Table schema (see migration 012_magic_link_tokens.sql):
    slug        TEXT PK
    thread_id   TEXT
    cart_key    TEXT
    expires_at  TIMESTAMPTZ
    redeemed_at TIMESTAMPTZ
    created_at  TIMESTAMPTZ
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import supabase_client
from _magic_link import mint_unique_slug

_TABLE = "location_tokens"


def _exists(slug: str) -> bool:
    sb = supabase_client.get_supabase()
    rows = sb.table(_TABLE).select("slug").eq("slug", slug).limit(1).execute()
    return bool(rows)


def mint(*, thread_id: str, cart_key: str) -> str:
    """Insert a new location token and return its slug."""
    slug = mint_unique_slug(_exists)
    supabase_client.get_supabase().table(_TABLE).insert(
        {"slug": slug, "thread_id": thread_id, "cart_key": cart_key}
    ).execute()
    return slug


def lookup(slug: str) -> dict[str, Any] | None:
    """Return the token row if it is valid (not expired, not redeemed), else None."""
    sb = supabase_client.get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    rows = (
        sb.table(_TABLE)
        .select("slug,thread_id,cart_key,expires_at,redeemed_at")
        .eq("slug", slug)
        .gt("expires_at", now)
        .is_("redeemed_at", None)
        .limit(1)
        .execute()
    )
    if not rows:
        return None
    row = rows[0] if isinstance(rows, list) else rows
    if isinstance(row, dict):
        return row
    return None


def mark_redeemed(slug: str) -> None:
    """Stamp ``redeemed_at`` so the slug cannot be replayed."""
    now = datetime.now(timezone.utc).isoformat()
    supabase_client.get_supabase().table(_TABLE).update(
        {"redeemed_at": now}
    ).eq("slug", slug).execute()
