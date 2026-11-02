"""storage.semantic — mem0 hosted SaaS wrapper.

Per-user fact extraction + retrieval. The mem0 platform automatically
distills conversation turns into short factual statements ("User lives
in Madrid", "User's budget is under 3000 EUR") keyed by ``user_id``.
Memory simply wraps the platform: the dedupe, extraction, and embedding
all run server-side.

Backend: mem0 SaaS (https://app.mem0.ai). API key in ``MEM0_API_KEY``.

Six public functions plus :func:`is_mem0_configured`:

- :func:`remember` — store a batch of conversation turns for a user.
- :func:`recall` — retrieve memories (``get_all`` or semantic ``search``).
- :func:`forget` — delete one memory by id.
- :func:`migrate_user` — transfer memories from ``old_id`` to ``new_id``
  (non-atomic; see D16).
- :func:`format_memories_for_prompt` — render a list of :class:`Memory`
  as "Known facts about this user:\\n- ..." text.
- :func:`get_user_context` — convenience: :func:`recall` + format, for
  injecting into an ephemeral prompt.
- :func:`is_mem0_configured` — public guard replacing the old
  ``_get_api_key`` alias leak (design.md D15).

Legacy invariants preserved:

- **mem0 client v1.0.7 bug workaround**: ``client.get_all()`` and
  ``client.search()`` don't wrap ``user_id`` in the ``filters`` dict
  required by the v2 API, so this module falls back to raw ``httpx``
  for reads. Writes (``add`` / ``delete``) work on the client. See
  design.md D17.
- **``infer=False`` on re-add during migration** — stops the SaaS
  from re-extracting facts from what are already extracted facts.
- **User ID normalization** — digit-only strings get a ``+`` prefix so
  WhatsApp phone numbers and E.164 share a namespace. UUID-shaped ids
  (web-chat tokens) pass through unchanged.

State model (refactored per D15): explicit two-field state replaces
the tri-state sentinel. ``_mem0_client`` is ``None`` until first use,
then holds a ``MemoryClient`` instance. ``_mem0_init_failed`` flips to
``True`` once init fails and stays there for the process lifetime —
preventing retry storms against a misconfigured / import-broken SDK.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from core import http_client

from .types import ChatTurn, Memory, MigrationResult

__all__ = [
    "is_mem0_configured",
    "remember",
    "recall",
    "forget",
    "migrate_user",
    "format_memories_for_prompt",
    "get_user_context",
]

logger = logging.getLogger(__name__)

_MEM0_API_BASE = "https://api.mem0.ai"
_READ_TIMEOUT_SECONDS = 10.0


# Explicit state: None ≡ "never initialized", _mem0_init_failed=True ≡
# "initialization raised; do not retry". Drop-in replacement for the
# audit §File 3/3 tri-state sentinel (None | MemoryClient | False).
_mem0_client: Any = None
_mem0_init_failed: bool = False


# --------------------------------- config -------------------------------------

def _get_api_key() -> str:
    return os.getenv("MEM0_API_KEY", "")


def is_mem0_configured() -> bool:
    """True iff ``MEM0_API_KEY`` is set in the environment.

    Replaces the three callsites that used to alias ``_get_api_key`` under
    an underscore prefix (audit §File 3/3 smells). Callers gate their own
    work on this predicate::

        from storage import is_mem0_configured, recall

        if is_mem0_configured():
            facts = await recall(user_id)
    """

    return bool(_get_api_key())


def _get_client() -> Any:
    """Lazy-initialize the mem0 client. Returns ``None`` on failure.

    Used only for write paths (``add`` / ``delete``) — reads go through
    :func:`_api_get_all` / :func:`_api_search` because the client v1.0.7
    has a v2-API bug. See module docstring.
    """

    global _mem0_client, _mem0_init_failed

    if _mem0_init_failed:
        return None

    api_key = _get_api_key()
    if not api_key:
        return None

    if _mem0_client is None:
        try:
            from mem0 import MemoryClient  # type: ignore[import-not-found]
            _mem0_client = MemoryClient(api_key=api_key)
            logger.info("mem0 client initialized")
        except Exception as exc:
            logger.warning("mem0 client init failed: %s", exc)
            _mem0_init_failed = True
            return None

    return _mem0_client


def _auth_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Token {_get_api_key()}",
        "Content-Type": "application/json",
    }


def _normalize_user_id(user_id: str) -> str:
    """Stable per-user namespace key.

    WhatsApp phone numbers arrive as ``"+34612345678"`` or plain digits.
    Web-chat anon tokens arrive as UUID-ish strings (``"web-abc123..."``)
    or raw UUIDs. Normalize so both channels partition the same mem0
    namespace when they belong to the same human:

    - Whitespace and hyphens stripped.
    - Digit-only / ``+``-prefixed-digit strings get a ``+`` prefix if
      missing (so ``34612345678`` ≡ ``+34612345678``).
    - Everything else (UUIDs, tokens) passes through unchanged.
    """

    if not user_id:
        return ""
    uid = user_id.strip().replace(" ", "").replace("-", "")
    if uid.isdigit() or (uid.startswith("+") and uid[1:].isdigit()):
        if not uid.startswith("+"):
            uid = "+" + uid
    return uid


# --------------------------------- typing -------------------------------------


def _parse_created_at(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _row_to_memory(row: Dict[str, Any], *, user_id: str) -> Memory:
    """Coerce a raw mem0 API row to a :class:`Memory` dataclass.

    The mem0 v2 API returns rows like
    ``{"id": "...", "memory": "...", "metadata": {...}, "score": 0.87,
       "created_at": "2026-01-14T..."}``. We map ``memory`` → ``text``
    and denormalize ``user_id`` for caller convenience (mem0 doesn't
    echo the user_id back per-row).
    """

    return Memory(
        id=str(row.get("id", "")),
        text=str(row.get("memory", "")),
        metadata=dict(row.get("metadata") or {}),
        score=row.get("score") if isinstance(row.get("score"), (int, float)) else None,
        user_id=user_id,
        created_at=_parse_created_at(row.get("created_at")),
    )


# --------------------------------- reads --------------------------------------


async def _api_get_all(uid: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Raw v2 API call. The client's ``get_all`` is broken in v1.0.7."""

    async with http_client(timeout=_READ_TIMEOUT_SECONDS) as http:
        resp = await http.post(
            f"{_MEM0_API_BASE}/v2/memories/",
            json={"filters": {"user_id": uid}, "page_size": limit},
            headers=_auth_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("results", [])


async def _api_search(uid: str, query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Raw v2 API call. The client's ``search`` is broken in v1.0.7."""

    async with http_client(timeout=_READ_TIMEOUT_SECONDS) as http:
        resp = await http.post(
            f"{_MEM0_API_BASE}/v2/memories/search/",
            json={"filters": {"user_id": uid}, "query": query, "top_k": limit},
            headers=_auth_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("results", [])


async def recall(
    user_id: str,
    query: str = "",
    *,
    limit: int = 10,
) -> List[Memory]:
    """Retrieve memories for a user.

    - ``query=""`` → ``get_all`` (most recent ``limit`` memories).
    - ``query="..."`` → semantic search (top-k by cosine similarity).

    Returns ``[]`` if ``MEM0_API_KEY`` is unset, the user_id is empty,
    or the SaaS call fails. Never raises — upstream callers gate
    Leo's response composition on memory availability and must not
    fall over when memory is down.
    """

    if not is_mem0_configured():
        return []

    uid = _normalize_user_id(user_id)
    if not uid:
        return []

    try:
        rows = await _api_search(uid, query, limit) if query else await _api_get_all(uid, limit)
        out = [_row_to_memory(r, user_id=uid) for r in rows]
        logger.debug("mem0 recalled %d memories for user %s", len(out), uid[:8] + "***")
        return out
    except Exception as exc:
        logger.warning("mem0 recall failed for %s: %s", uid[:8] + "***", exc)
        return []


# --------------------------------- writes -------------------------------------


async def remember(
    user_id: str,
    messages: List[ChatTurn],
    *,
    metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """Store conversation turns in mem0. The SaaS extracts facts server-side.

    Only ``user`` and ``assistant`` roles are forwarded; ``system`` turns
    are silently dropped (mem0 doesn't accept them). Empty-text messages
    are also dropped.

    Returns ``False`` if mem0 is not configured, the client failed to
    initialize, the user_id is empty, no valid messages remain after
    filtering, or the SaaS call raises. Never raises.
    """

    client = _get_client()
    if not client:
        return False

    uid = _normalize_user_id(user_id)
    if not uid or not messages:
        return False

    formatted: List[Dict[str, str]] = []
    for m in messages:
        if m.role in ("user", "assistant") and m.text:
            formatted.append({"role": m.role, "content": m.text})

    if not formatted:
        return False

    try:
        await asyncio.to_thread(
            client.add,
            formatted,
            user_id=uid,
            metadata=metadata or {},
        )
        logger.info("mem0 remembered %d messages for user %s", len(formatted), uid[:8] + "***")
        return True
    except Exception as exc:
        logger.warning("mem0 remember failed for %s: %s", uid[:8] + "***", exc)
        return False


async def forget(user_id: str, memory_id: str) -> bool:
    """Delete one memory by id. Returns True on success, False otherwise.

    ``user_id`` is not currently required by mem0's delete endpoint but
    we keep the parameter so the public API parallels ``remember`` and
    ``recall`` — if mem0 ever splits per-user delete permissions, we
    won't have to re-break callers.
    """

    del user_id
    client = _get_client()
    if not client:
        return False

    if not memory_id:
        return False

    try:
        await asyncio.to_thread(client.delete, memory_id)
        return True
    except Exception as exc:
        logger.warning("mem0 forget failed for memory %s: %s", memory_id, exc)
        return False


# --------------------------------- migration ----------------------------------


async def migrate_user(old_id: str, new_id: str) -> MigrationResult:
    """Transfer all memories from ``old_id`` to ``new_id``. Non-atomic.

    Process:

    1. Fetch up to 100 memories from ``old_id`` via ``get_all``.
    2. Re-add each one under ``new_id`` with ``infer=False``
       (skip server-side re-extraction — we already have the fact).
    3. Delete each from ``old_id``.

    Failures during the re-add step are counted in
    :attr:`MigrationResult.failed_count` but don't abort the whole
    migration. Failures during the delete step are swallowed — the
    re-add has already succeeded so the new namespace is authoritative;
    an orphaned row in the old namespace is cosmetic.

    **Non-atomicity** (see design.md D16): on partial failure, memories
    may exist under both user_ids. The caller can retry safely —
    re-running is idempotent because mem0 dedupes via text similarity
    and ``infer=False`` skips extraction. The only bad steady state is
    the window between the re-add and the delete, which is accepted.
    """

    client = _get_client()
    if not client:
        return MigrationResult(ok=False, error="mem0_not_configured")

    old_uid = _normalize_user_id(old_id)
    new_uid = _normalize_user_id(new_id)

    if not old_uid or not new_uid:
        return MigrationResult(ok=False, error="empty_user_id")
    if old_uid == new_uid:
        return MigrationResult(ok=True)

    try:
        rows = await _api_get_all(old_uid, limit=100)
    except Exception as exc:
        logger.warning("mem0 migrate_user: fetch failed (%s → %s): %s",
                       old_uid[:8] + "***", new_uid[:8] + "***", exc)
        return MigrationResult(ok=False, error=f"fetch_failed: {exc}")

    if not rows:
        logger.debug("mem0 migrate_user: no memories to move from %s", old_uid[:8] + "***")
        return MigrationResult(ok=True, migrated_count=0)

    migrated = 0
    failed = 0

    for row in rows:
        text = row.get("memory", "")
        if not text:
            continue
        try:
            await asyncio.to_thread(
                client.add,
                [{"role": "user", "content": text}],
                user_id=new_uid,
                infer=False,
            )
            migrated += 1
        except Exception as exc:
            failed += 1
            logger.warning("mem0 migrate_user: re-add failed for %s: %s",
                           new_uid[:8] + "***", exc)

    for row in rows:
        mem_id = row.get("id")
        if not mem_id:
            continue
        try:
            await asyncio.to_thread(client.delete, mem_id)
        except Exception:
            pass

    logger.info("mem0 migrate_user: %d migrated / %d failed (%s → %s)",
                migrated, failed, old_uid[:8] + "***", new_uid[:8] + "***")

    return MigrationResult(
        ok=failed == 0,
        migrated_count=migrated,
        failed_count=failed,
    )


# --------------------------------- formatting ---------------------------------


def format_memories_for_prompt(memories: List[Memory]) -> str:
    """Render memories as ``"Known facts about this user:\\n- ..."`` text.

    The framing is English-only by design (audit §File 3/3 invariant 8) —
    the facts themselves are in whatever language the user wrote in; the
    instruction wrapper is for the LLM. At most 10 facts are rendered;
    extras are dropped silently.
    """

    if not memories:
        return ""

    bullets: List[str] = []
    for mem in memories[:10]:
        if mem.text:
            bullets.append(f"- {mem.text}")

    if not bullets:
        return ""

    return "Known facts about this user:\n" + "\n".join(bullets)


async def get_user_context(user_id: str, current_message: str = "") -> str:
    """Convenience: :func:`recall` + :func:`format_memories_for_prompt`.

    Returns ``""`` if mem0 is unconfigured or the recall returned
    nothing. Never raises. This is the single call-site most Leo agent
    turns make when building the ephemeral prompt — it's deliberately
    a one-liner wrapper so the prompt-builder doesn't juggle two
    imports.
    """

    if not user_id or not is_mem0_configured():
        return ""

    memories = await recall(user_id, query=current_message, limit=10)
    return format_memories_for_prompt(memories)
