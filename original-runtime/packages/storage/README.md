> Retained April–May 2026 implementation record. [Current public setup](../../../docs/agent-runtime.md).

# `storage`

Durable engines for any Leo app — **mem0 semantic memory** + **pgvector embeddings**. Scope-free: a new app can `import storage` and nothing scope-specific leaks in.

Renamed from `packages/memory` in Phase 5 Wave 1; scope-specific persistent records (Lead, Proposal, Contractor, Message plus their CRUD helpers) split out to `apps/scope/product/backend/records/` in Phase 5 Wave 3. As of Wave 3, `storage` exports exactly 18 symbols — none of them reference scope schemas or Supabase table names.

## Capabilities owned

- **Semantic memory** — hosted [mem0](https://mem0.ai) wrapper. `remember`, `recall`, `migrate_user`, `forget`, `get_user_context`, `format_memories_for_prompt`, `is_mem0_configured`. Extracts facts from conversation turns server-side; retrieve by semantic search.
- **Embeddings** — pgvector substrate for vector search over app-owned knowledge (1536-dim via OpenAI `text-embedding-3-small`). `index_text`, `index_batch`, `search_similar`, `delete_chunk`, `prune_embeddings`, `EMBED_DIM`, `EMBED_MODEL`.
- **Engine types** — `Memory`, `MigrationResult`, `Embedding`, `EmbeddingMatch`, and the `ChatTurn` protocol (so any caller's Message-like type can feed `remember()`).

`storage` depends only on `core` (db, http, resilience) and `llm` (embeddings, summarization). It does **not** depend on `messaging`, `fulltext`, any app, or scope-specific schemas — import direction is strictly outward.

## Pairing with app-specific records

Scope's Leo app pairs `storage` (generic engines) with `apps/scope/product/backend/records/` (scope schemas):

```python
from storage import remember, recall, index_text   # generic engines
from records import save_lead, add_message, Lead   # scope Supabase schemas
```

Any new app picks its own records shape and uses the same two engines — that's the split.

## Install

```toml
[project]
dependencies = [
    "core    @ file:../../packages/core",
    "storage @ file:../../packages/storage",
]
```

## Use

```python
from storage import (
    # Semantic
    remember, recall, migrate_user, forget,
    get_user_context, format_memories_for_prompt, is_mem0_configured,
    Memory, MigrationResult,
    # Embeddings
    index_text, index_batch, search_similar, delete_chunk, prune_embeddings,
    EMBED_DIM, EMBED_MODEL,
    Embedding, EmbeddingMatch,
)
```

## Examples

### Example 1 — Remember conversation turns, recall for context later

```python
from dataclasses import dataclass
from storage import remember, get_user_context


@dataclass
class Turn:
    role: str
    text: str


async def after_exchange(user_id: str, turns: list[Turn]) -> None:
    await remember(user_id, turns, metadata={"channel": "whatsapp"})


async def agent_context(user_id: str) -> str:
    facts = await get_user_context(user_id, limit=10)
    return "\n".join(f"- {f.text}" for f in facts)
```

What storage guarantees:

- `remember` accepts any object with `.role` and `.text` (the `ChatTurn`
  protocol) — scope's `records.Message` fits naturally, but so does any
  bare dataclass. The engine filters to `user` + `assistant` roles and
  drops empty text.
- Returns `False` (never raises) on mem0 not configured, empty user id,
  empty filtered batch, or SaaS call failure — log-and-continue.
- `get_user_context` short-circuits to `""` when `MEM0_API_KEY` is unset,
  so callers don't need env branching.

### Example 2 — Index knowledge into pgvector, semantic search over it

```python
from storage import index_text, search_similar, EmbeddingMatch


async def ingest_doc(doc_id: str, text: str, *, tags: dict) -> None:
    await index_text(
        chunk_id=f"doc:{doc_id}",
        text=text,
        metadata=tags,
    )


async def find_docs(query: str, *, trade: str) -> list[EmbeddingMatch]:
    return await search_similar(
        query,
        limit=10,
        filters={"trade": trade},
        min_score=0.55,
    )
```

What storage guarantees:

- `index_text` is idempotent. It hashes the text (`sha256(text)`) and compares to the existing row's `content_hash`; if identical, no `llm.embed` call is made and no DB write happens.
- `search_similar` embeds the query once, runs HNSW top-k on cosine similarity, and returns results ordered by descending score.
- Dim lock: the `knowledge_embeddings.embedding` column is `vector(1536)`. Attempting to insert any other length raises `core.errors.ValidationError`.

## Environment variables read

| var | used by | required |
|---|---|---|
| `SUPABASE_URL` | `storage.embeddings` (via `core.db`) | yes for pgvector paths |
| `SUPABASE_SERVICE_ROLE_KEY` | `storage.embeddings` (via `core.db`) | preferred |
| `SUPABASE_ANON_KEY` | `storage.embeddings` (via `core.db`) | fallback |
| `MEM0_API_KEY` | `storage.semantic.*` | no (degrades to no-op) |
| `LITELLM_BASE_URL` / `LITELLM_API_KEY` | `storage.embeddings.*` (via `llm`) | yes when embedding paths are used |

## Testing

```bash
cd /root/leo
PYTHONPATH="packages/storage/src:packages/core/src:packages/llm/src" \
    /root/leo/apps/scope/product/backend/venv/bin/python3 -m pytest packages/storage/tests/
```

## Internal structure

See `internal/refactor/packages/memory/layout.txt` (`../../../internal/refactor/packages/memory/layout.txt`, historical internal reference) (historical path; the layout doc still reflects the `memory` name and lists the scope schemas that have since split out to `apps/scope/product/backend/records/`) for the full module-by-module source map.

## Changelog

- **Phase 5 Wave 3**: scope-specific schemas (`leads`, `proposals`, `dossier`, `conversations`, plus their types) split out to `apps/scope/product/backend/records/`. `storage` is now engine-only (mem0 + pgvector) — 18 public symbols.
- **Phase 5 Wave 1**: renamed `packages/memory` → `packages/storage` (the package owns *engines*, not the mem0 "memory" concept specifically).
