"""
storage — durable engines (mem0 semantic memory + pgvector embeddings)
=======================================================================

Generic, scope-free storage engines. Every Leo app can use these without
any scope schema leaking through:

- **Semantic** — mem0 hosted fact extraction + per-user retrieval.
- **Embeddings** — pgvector-backed knowledge index (1536-dim, OpenAI).

Scope-specific record schemas (Leads, Proposals, Contractors, Messages)
live in ``apps/scope/product/backend/records/`` — they describe scope's Supabase
tables, not shared engine behaviour. Importing this package should never
load scope-specific code.

Top-level import surface
------------------------

Callers write::

    from storage import (
        # semantic / mem0
        remember, recall, forget, migrate_user,
        is_mem0_configured, format_memories_for_prompt, get_user_context,
        Memory, MigrationResult,
        # embeddings / pgvector
        index_text, index_batch, search_similar,
        delete_chunk, prune_embeddings,
        EMBED_DIM, EMBED_MODEL,
        Embedding, EmbeddingMatch,
    )

Submodules remain addressable for advanced usage (``from storage.semantic
import _api_get_all``) but are not the blessed surface.

Design + per-symbol rationale: ``internal/refactor/packages/memory/design.md``
(historical path; the package was renamed from ``memory`` → ``storage`` in
Phase 5 Wave 1 to reflect that it owns *engines*, not the mem0 "memory"
concept specifically; scope record schemas split out in Phase 5 Wave 3).
"""

from __future__ import annotations

from .types import (
    Embedding,
    EmbeddingMatch,
    Memory,
    MigrationResult,
)

from .semantic import (
    forget,
    format_memories_for_prompt,
    get_user_context,
    is_mem0_configured,
    migrate_user,
    recall,
    remember,
)

from .embeddings import (
    EMBED_DIM,
    EMBED_MODEL,
    delete_chunk,
    index_batch,
    index_text,
    prune_embeddings,
    search_similar,
)

__all__ = [
    # --- types (4) ---
    "Memory",
    "MigrationResult",
    "Embedding",
    "EmbeddingMatch",
    # --- semantic (7) ---
    "remember",
    "recall",
    "forget",
    "migrate_user",
    "format_memories_for_prompt",
    "get_user_context",
    "is_mem0_configured",
    # --- embeddings (7 — 5 fns + 2 constants) ---
    "index_text",
    "index_batch",
    "search_similar",
    "delete_chunk",
    "prune_embeddings",
    "EMBED_DIM",
    "EMBED_MODEL",
]
