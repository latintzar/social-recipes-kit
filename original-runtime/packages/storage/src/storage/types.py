"""storage.types — dataclasses for the storage engines.

Engine types (mem0 + pgvector) live here. Scope-specific record shapes
(``Lead``, ``Proposal``, ``Contractor``, ``Message``) moved to
``apps/scope/product/backend/records/types.py`` in Phase 5 Wave 3 — they describe
scope-app Supabase schemas, not shared engine output, so they belong with
the scope app.

Design:
- ``@dataclass`` (not frozen) because callers often build these from
  raw dicts and the optional fields get filled incrementally.
- Field types use ``Optional[...]`` + ``default=None`` rather than
  ``Union`` so the public API stays ergonomic from typed callers.
- ``created_at`` / ``updated_at`` are ``datetime`` objects — the mem0
  HTTP layer returns ISO-8601 strings; each engine module coerces them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional, Protocol, runtime_checkable

__all__ = [
    "ChatTurn",
    "Memory",
    "MigrationResult",
    "Embedding",
    "EmbeddingMatch",
]


@runtime_checkable
class ChatTurn(Protocol):
    """Structural type accepted by :func:`storage.remember`.

    The mem0 engine only needs ``role`` (``"user"`` / ``"assistant"`` /
    ``"system"``) and ``text`` from each conversation turn. Any app can
    pass its own Message-like object — scope's ``records.Message`` fits
    naturally, but so would a bare dataclass defined elsewhere. This
    protocol keeps the engine scope-free while staying ergonomic.
    """

    role: str
    text: str


@dataclass
class Memory:
    """A semantic-memory item returned by :func:`storage.recall`.

    ``score`` is populated on search results (cosine similarity) and
    ``None`` on :func:`recall` without query (``get_all``).
    """

    id: str
    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    score: Optional[float] = None
    user_id: str = ""
    created_at: Optional[datetime] = None


@dataclass
class MigrationResult:
    """Outcome of :func:`storage.migrate_user`.

    Non-atomic: ``migrated_count`` + ``failed_count`` can both be >0
    on partial failure. ``error`` is the first exception message when
    the whole operation aborts.
    """

    ok: bool
    migrated_count: int = 0
    failed_count: int = 0
    error: Optional[str] = None


@dataclass
class Embedding:
    """A row from ``knowledge_embeddings`` (minus the raw vector).

    ``content_hash`` is the sha256 of ``content`` — used by
    :func:`index_text` for idempotent re-indexing.
    """

    id: str
    chunk_id: str
    content: str
    content_hash: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class EmbeddingMatch:
    """A :func:`search_similar` result: an embedding row plus its cosine score."""

    embedding: Embedding
    score: float
