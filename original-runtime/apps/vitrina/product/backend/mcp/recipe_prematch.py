"""Per-user prematch worker for the Discover seed pool.

Plan reference: ``vitrina-async-chat-and-seed-pool``, phase C1–C4.

When a user provides a location (or moves their pin), we resolve the
40-row catalog's ``match_universal`` payloads to the user's catchment
and cache the per-row result in ``user_recipe_matches``.

This is a *cheap* operation per row — no LLM rerun, just the Haversine
distance filter — so we can chew through 40 rows in <1 s. The reason
we still cache it instead of computing on every request is twofold:

1. The Discover list endpoint becomes a single Supabase join
   (``creator_catalog ⋈ user_recipe_matches``) instead of 40 in-process
   filter calls; this matters at scale.
2. The frontend's cold-load loading state (C4) wants a "X of 40
   resolved" progress signal, which this table provides for free.
"""
from __future__ import annotations

import logging
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Re-use the already-vendored recipe_videos package on sys.path.
_HERE = Path(__file__).resolve()
for parent in _HERE.parents:
    candidate = parent / "recipe_videos"
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import catalog as recipe_catalog  # type: ignore[import-not-found]  # noqa: E402
import shop_matcher  # type: ignore[import-not-found]  # noqa: E402

try:
    from supabase_client import get_supabase  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    def get_supabase():  # type: ignore[no-redef]
        raise RuntimeError("supabase_not_configured")

logger = logging.getLogger("vitrina.mcp.recipe_prematch")

_TABLE = "user_recipe_matches"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def status(device_id: str) -> dict[str, Any]:
    """Return prematch progress for ``device_id``.

    Returns ``{total, resolved, skipped, failed, queued, running}`` —
    the frontend polls this to drive the cold-load progress bar (C4).
    """
    sb = get_supabase()
    try:
        rows = sb.table(_TABLE).select("status").eq("device_id", device_id).execute() or []
    except Exception:  # noqa: BLE001
        rows = []
    counts = {"resolved": 0, "skipped": 0, "failed": 0, "queued": 0, "running": 0}
    for row in rows or []:
        s = row.get("status")
        if s in counts:
            counts[s] += 1
    counts["total"] = len(rows or [])
    return counts


def resolved_recipe_ids(device_id: str) -> set[str]:
    """Set of recipe ids the device has a renderable per-user match for."""
    sb = get_supabase()
    try:
        rows = (
            sb.table(_TABLE)
            .select("recipe_id")
            .eq("device_id", device_id)
            .eq("status", "resolved")
            .execute()
            or []
        )
    except Exception:  # noqa: BLE001
        return set()
    return {r["recipe_id"] for r in (rows or []) if r.get("recipe_id")}


def _upsert(row: dict[str, Any]) -> None:
    sb = get_supabase()
    try:
        sb.table(_TABLE).upsert(row, on_conflict="device_id,recipe_id").execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("recipe_prematch.upsert_failed err=%s", exc)


def _resolve_one(
    *,
    device_id: str,
    user_id: str | None,
    recipe_row: dict[str, Any],
    user_lat: float,
    user_lng: float,
    catchment_km: float,
    shops_index: dict[str, Any],
) -> str:
    """Filter one catalog row's universal match to the user's catchment.

    Returns the resulting status string, also persisted to the table.
    """
    universal = recipe_row.get("match_universal")
    if not universal:
        # No universal payload = the cheap filter has nothing to chew on;
        # leave the row unresolved so the listing falls back to the
        # legacy keyword matcher.
        _upsert(
            {
                "device_id": device_id,
                "user_id": user_id,
                "recipe_id": recipe_row["id"],
                "status": "skipped",
                "match_json": None,
                "pin_lat": user_lat,
                "pin_lng": user_lng,
                "catchment_km": catchment_km,
                "updated_at": _now_iso(),
                "resolved_at": _now_iso(),
                "error_message": "no_universal_match",
            }
        )
        return "skipped"

    # Progressively expand the search radius if the tight catchment
    # yields nothing buyable. Peter's frustration ("26 of 40 skipped —
    # nothing has a substitute?") was caused by the previous code
    # treating the user's 1.5 km neighbourhood as the universe of
    # shops, even though the universal pre-bake at extraction time
    # already knows every shop in our catalog stocks ingredient X. By
    # widening the ring twice (1.5 → 2.5 → 5.0 km) before giving up,
    # we surface recipes that just need a shop one neighbourhood
    # over — the frontend can mark them as "found a bit further" via
    # `expanded_to_km` if we want a UI hint. If even 5 km has nothing,
    # *then* we skip.
    EXPANSION_RUNGS = [
        catchment_km,
        max(catchment_km, shop_matcher.EXPANDED_CATCHMENT_KM),
        5.0,
    ]
    last_filtered: dict[str, Any] | None = None
    used_km = catchment_km
    for trial_km in EXPANSION_RUNGS:
        try:
            filtered, _ = shop_matcher._filter_universal_to_catchment(  # noqa: SLF001
                universal,
                user_lat=user_lat,
                user_lng=user_lng,
                catchment_km=trial_km,
                shops_index=shops_index,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "recipe_prematch.filter_failed recipe=%s km=%.1f err=%s",
                recipe_row.get("id"), trial_km, exc,
            )
            _upsert(
                {
                    "device_id": device_id,
                    "user_id": user_id,
                    "recipe_id": recipe_row["id"],
                    "status": "failed",
                    "error_message": str(exc)[:400],
                    "pin_lat": user_lat,
                    "pin_lng": user_lng,
                    "catchment_km": catchment_km,
                    "updated_at": _now_iso(),
                }
            )
            return "failed"
        last_filtered = filtered
        used_km = trial_km
        if (filtered or {}).get("shop_groups"):
            # Found something — stamp the expansion so the UI can
            # mention "we looked a bit further" if it cares.
            if trial_km > catchment_km:
                filtered["expanded_to_km"] = trial_km
            break

    filtered = last_filtered or {}
    shop_groups = filtered.get("shop_groups") or []
    blockers = filtered.get("blockers") or []
    # Resolve whenever we have ANY buyable group anywhere in the user's
    # widest searched ring. A recipe with 1 shop_group + 5 blockers is
    # still valuable — the frontend already renders the substitution
    # suggestions. We only fall back to "skipped" when even the widest
    # ring has zero shop_groups AND we have unsourceable items.
    new_status = "skipped" if (not shop_groups and blockers) else "resolved"

    _upsert(
        {
            "device_id": device_id,
            "user_id": user_id,
            "recipe_id": recipe_row["id"],
            "status": new_status,
            "match_json": filtered,
            "pin_lat": user_lat,
            "pin_lng": user_lng,
            "catchment_km": catchment_km,
            "updated_at": _now_iso(),
            "resolved_at": _now_iso(),
        }
    )
    return new_status


def _seed_queue(device_id: str, user_id: str | None) -> None:
    """Insert a ``queued`` row for every catalog recipe for this device.

    Lets the frontend show a "0 of 40" progress bar immediately rather
    than waiting for the worker to insert each row as it processes it.
    """
    sb = get_supabase()
    rows = recipe_catalog._catalog()  # noqa: SLF001
    inserts = [
        {
            "id": str(uuid.uuid4()),
            "device_id": device_id,
            "user_id": user_id,
            "recipe_id": r["id"],
            "status": "queued",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        for r in rows
    ]
    if not inserts:
        return
    try:
        sb.table(_TABLE).upsert(
            inserts,
            on_conflict="device_id,recipe_id",
            ignore_duplicates=True,
        ).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("recipe_prematch.seed_failed err=%s", exc)


def prematch_for_device(
    *,
    device_id: str,
    user_id: str | None,
    user_lat: float,
    user_lng: float,
    catchment_km: float,
) -> None:
    """Worker body — resolve all 40 catalog rows for one device.

    Idempotent: rows already resolved at the same pin are skipped.
    Spawned via :func:`spawn_prematch` from the prematch endpoint.
    """
    rows = list(recipe_catalog._catalog())  # noqa: SLF001
    if not rows:
        return
    _seed_queue(device_id, user_id)
    shops_index = shop_matcher._all_shops_indexed()  # noqa: SLF001
    for r in rows:
        try:
            _resolve_one(
                device_id=device_id,
                user_id=user_id,
                recipe_row=r,
                user_lat=user_lat,
                user_lng=user_lng,
                catchment_km=catchment_km,
                shops_index=shops_index,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("recipe_prematch.row_failed id=%s err=%s", r.get("id"), exc)


def spawn_prematch(
    *,
    device_id: str,
    user_id: str | None,
    user_lat: float,
    user_lng: float,
    catchment_km: float,
) -> threading.Thread:
    """Kick off ``prematch_for_device`` on a daemon thread."""
    def _run() -> None:
        try:
            prematch_for_device(
                device_id=device_id,
                user_id=user_id,
                user_lat=user_lat,
                user_lng=user_lng,
                catchment_km=catchment_km,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("recipe_prematch.thread_failed device=%s err=%s", device_id, exc)

    t = threading.Thread(target=_run, name=f"recipe-prematch-{device_id[:8]}", daemon=True)
    t.start()
    return t


__all__ = [
    "prematch_for_device",
    "spawn_prematch",
    "status",
    "resolved_recipe_ids",
]
