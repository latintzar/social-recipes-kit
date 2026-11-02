"""storage.embeddings — pgvector-backed knowledge index.

Net-new module per design.md D6/D7/D9. No code migrates in; this is the
first time Leo has a durable vector substrate.

Storage: Supabase Postgres + ``pgvector`` extension. One table
``knowledge_embeddings`` with an HNSW index on a fixed-dim ``vector(1536)``
column. The extension + table + indices are created by the migrations
landing in the same PR (``supabase/migrations/NN_enable_pgvector.sql``
and ``NN_knowledge_embeddings.sql``).

Embedding provider: OpenAI ``text-embedding-3-small`` via the LiteLLM
proxy (``llm.embed``). **Locked to 1536 dimensions in v1** — the column
type is ``vector(1536)`` and :func:`index_text` enforces the length
invariant before writing. See design.md D6 for the v2 multi-provider
path (deferred until a specialty embedding beats OpenAI on a real task).

Public functions
----------------

- :func:`index_text` / :func:`index_batch` — upsert one / many chunks.
  Idempotent by SHA-256 content hash: same text under the same
  ``chunk_id`` skips the embedding call entirely.
- :func:`search_similar` — cosine top-k, optional metadata filter, optional
  min-score threshold. Embeds the query via the same provider.
- :func:`delete_chunk` — hard delete by ``chunk_id``.
- :func:`prune_embeddings` — time-based prune with optional metadata filter.
  Explicit cleanup only — there is no background job (design.md D11).

Why pgvector rather than Pinecone/Weaviate/Qdrant
-------------------------------------------------
- Leo's scale is thousands of vectors, not millions. pgvector scales to
  10⁶ comfortably on a single Supabase instance.
- No new infra commitment. Supabase already hosts the system of record.
- One SQL statement does vector search + metadata filter + joins to
  ``cs_leads`` / ``contractors``. External vector DBs require a second
  round-trip.
- If scale forces a move later, the module API stays identical; the
  :func:`search_similar` implementation swaps underneath. The cost is a
  one-time re-embed pass, not an API break.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core import ValidationError, http_client, sb_headers, sb_url

from .types import Embedding, EmbeddingMatch

__all__ = [
    "EMBED_DIM",
    "EMBED_MODEL",
    "index_text",
    "index_batch",
    "search_similar",
    "delete_chunk",
    "prune_embeddings",
]

logger = logging.getLogger(__name__)

EMBED_DIM: int = 1536
"""OpenAI text-embedding-3-small. Single dim per design.md D6."""

EMBED_MODEL: str = "openai/text-embedding-3-small"
"""Provider model id routed through the LiteLLM proxy. Locked by D6."""


_TABLE = "knowledge_embeddings"
_WRITE_TIMEOUT = 15.0
_READ_TIMEOUT = 10.0


# --------------------------------- helpers ------------------------------------


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_vector(vec: List[float]) -> None:
    if len(vec) != EMBED_DIM:
        raise ValidationError(
            f"embedding dim mismatch: got {len(vec)}, expected {EMBED_DIM} "
            f"(model={EMBED_MODEL})",
            code="EMBED_DIM_MISMATCH",
        )


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _row_to_embedding(row: Dict[str, Any]) -> Embedding:
    return Embedding(
        id=str(row.get("id", "")),
        chunk_id=str(row.get("chunk_id", "")),
        content=str(row.get("content", "")),
        content_hash=str(row.get("content_hash", "")),
        metadata=dict(row.get("metadata") or {}),
        created_at=_parse_ts(row.get("created_at")),
        updated_at=_parse_ts(row.get("updated_at")),
    )


async def _embed_one(text: str) -> List[float]:
    """Embed a single string via the LiteLLM proxy.

    Import of :mod:`llm.embed` is local so module import doesn't require
    the LiteLLM proxy env to be populated (tests, static analysis, etc.).
    """

    from llm import embed as llm_embed

    vecs = await llm_embed([text], model=EMBED_MODEL)
    if not vecs or not vecs[0]:
        raise ValidationError(
            f"embedding provider returned no vector (model={EMBED_MODEL})",
            code="EMBED_EMPTY_RESPONSE",
        )
    return vecs[0]


async def _embed_many(texts: List[str]) -> List[List[float]]:
    from llm import embed as llm_embed

    return await llm_embed(texts, model=EMBED_MODEL)


async def _fetch_existing(chunk_id: str) -> Optional[Dict[str, Any]]:
    """Fetch the existing row for a chunk_id. Returns the raw JSON row or None."""

    params = {
        "chunk_id": f"eq.{chunk_id}",
        "select": "id,chunk_id,content,content_hash,metadata,created_at,updated_at",
        "limit": "1",
    }
    async with http_client(timeout=_READ_TIMEOUT) as http:
        resp = await http.get(
            f"{sb_url()}/{_TABLE}",
            params=params,
            headers=sb_headers(),
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        rows = resp.json()
        return rows[0] if rows else None


async def _upsert_row(
    *,
    chunk_id: str,
    content: str,
    content_hash: str,
    embedding: List[float],
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """Upsert one row via ``Prefer: resolution=merge-duplicates`` on ``chunk_id``."""

    payload = {
        "chunk_id": chunk_id,
        "content": content,
        "content_hash": content_hash,
        "embedding": embedding,
        "metadata": metadata,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    headers = {
        **sb_headers(),
        "Prefer": "resolution=merge-duplicates,return=representation",
    }
    params = {"on_conflict": "chunk_id"}
    async with http_client(timeout=_WRITE_TIMEOUT) as http:
        resp = await http.post(
            f"{sb_url()}/{_TABLE}",
            json=[payload],
            params=params,
            headers=headers,
        )
        resp.raise_for_status()
        rows = resp.json()
        return rows[0] if rows else payload


# --------------------------------- index --------------------------------------


async def index_text(
    chunk_id: str,
    text: str,
    *,
    metadata: Optional[Dict[str, Any]] = None,
) -> Embedding:
    """Upsert a chunk into ``knowledge_embeddings``.

    Idempotent: if a row already exists with the same ``chunk_id`` and
    the SHA-256 of its stored ``content`` equals the hash of ``text``,
    **the embed call is skipped** and the existing row is returned
    unchanged. This makes re-indexing a corpus free when most chunks
    haven't changed.

    Raises :class:`core.errors.ValidationError` if the provider returns
    a vector whose length is not :data:`EMBED_DIM`. This is the last
    line of defense against a misconfigured proxy routing to a
    different-dimension model.
    """

    if not chunk_id:
        raise ValidationError("chunk_id must not be empty", code="EMBED_EMPTY_CHUNK_ID")
    if not text:
        raise ValidationError("text must not be empty", code="EMBED_EMPTY_TEXT")

    metadata = metadata or {}
    new_hash = _content_hash(text)

    existing = await _fetch_existing(chunk_id)
    if existing and existing.get("content_hash") == new_hash:
        logger.debug("embeddings: hash-skip re-index for chunk_id=%s", chunk_id)
        return _row_to_embedding(existing)

    vec = await _embed_one(text)
    _validate_vector(vec)

    row = await _upsert_row(
        chunk_id=chunk_id,
        content=text,
        content_hash=new_hash,
        embedding=vec,
        metadata=metadata,
    )
    logger.info("embeddings: indexed chunk_id=%s (%d chars)", chunk_id, len(text))
    return _row_to_embedding(row)


async def index_batch(
    chunks: List[Tuple[str, str, Optional[Dict[str, Any]]]],
) -> List[Embedding]:
    """Bulk :func:`index_text`. One provider call per 96 chunks.

    ``chunks`` is a list of ``(chunk_id, text, metadata)`` tuples.
    Same hash-skip semantics as :func:`index_text`: entries whose stored
    hash already matches their current text are returned from the
    existing row without triggering an embed call.

    Failures on individual chunks don't abort the batch — exceptions
    are caught and logged, and the affected chunk is omitted from the
    returned list. Callers who need per-chunk success signalling
    should use :func:`index_text` in a loop.
    """

    if not chunks:
        return []

    needs_embed_idx: List[int] = []
    needs_embed_text: List[str] = []
    results: List[Optional[Embedding]] = [None] * len(chunks)

    for i, (chunk_id, text, _) in enumerate(chunks):
        if not chunk_id or not text:
            logger.warning("embeddings: skipping empty chunk at index %d", i)
            continue
        try:
            existing = await _fetch_existing(chunk_id)
        except Exception as exc:
            logger.warning("embeddings: fetch-existing failed for %s: %s", chunk_id, exc)
            existing = None
        if existing and existing.get("content_hash") == _content_hash(text):
            results[i] = _row_to_embedding(existing)
        else:
            needs_embed_idx.append(i)
            needs_embed_text.append(text)

    if needs_embed_text:
        try:
            vectors = await _embed_many(needs_embed_text)
        except Exception as exc:
            logger.warning("embeddings: batch embed failed: %s", exc)
            return [r for r in results if r is not None]

        for vec_idx, src_idx in enumerate(needs_embed_idx):
            vec = vectors[vec_idx] if vec_idx < len(vectors) else None
            if not vec:
                continue
            try:
                _validate_vector(vec)
                chunk_id, text, metadata = chunks[src_idx]
                row = await _upsert_row(
                    chunk_id=chunk_id,
                    content=text,
                    content_hash=_content_hash(text),
                    embedding=vec,
                    metadata=metadata or {},
                )
                results[src_idx] = _row_to_embedding(row)
            except Exception as exc:
                logger.warning("embeddings: upsert failed for chunk %d: %s", src_idx, exc)

    return [r for r in results if r is not None]


# --------------------------------- search -------------------------------------


async def search_similar(
    query: str,
    *,
    limit: int = 10,
    filters: Optional[Dict[str, str]] = None,
    min_score: Optional[float] = None,
) -> List[EmbeddingMatch]:
    """Cosine-similarity top-k search.

    Embeds ``query`` via the same provider used for writes, then calls
    a Supabase RPC ``match_knowledge_embeddings`` which runs pgvector's
    ``<=>`` operator against the HNSW index and returns
    ``(row, cosine_similarity)`` pairs. The RPC lives in
    ``supabase/migrations/NN_match_knowledge_embeddings.sql``.

    ``filters`` — simple ``metadata->>key = value`` predicates. More
    complex filters (array containment, numeric ranges) require
    dropping to raw SQL; the simple-case ergonomics here cover 90%
    of callers.

    ``min_score`` — drops results whose cosine similarity is below the
    threshold. ``None`` means no threshold. The RPC applies this
    server-side so there's no wasted row transfer.
    """

    if not query:
        return []

    vec = await _embed_one(query)
    _validate_vector(vec)

    payload: Dict[str, Any] = {
        "query_embedding": vec,
        "match_limit": limit,
    }
    if filters:
        payload["filter_metadata"] = filters
    if min_score is not None:
        payload["min_score"] = min_score

    async with http_client(timeout=_READ_TIMEOUT) as http:
        resp = await http.post(
            f"{sb_url()}/rpc/match_knowledge_embeddings",
            json=payload,
            headers=sb_headers(),
        )
        if resp.status_code == 404:
            logger.warning("embeddings: match_knowledge_embeddings RPC not deployed")
            return []
        resp.raise_for_status()
        rows = resp.json()

    matches: List[EmbeddingMatch] = []
    for row in rows or []:
        score = row.get("score")
        if score is None or not isinstance(score, (int, float)):
            continue
        matches.append(EmbeddingMatch(embedding=_row_to_embedding(row), score=float(score)))
    return matches


# --------------------------------- delete -------------------------------------


async def delete_chunk(chunk_id: str) -> bool:
    """Delete one row by ``chunk_id``. Returns ``True`` on 200/204, else ``False``.

    Never raises — delete is usually called during cleanup routines where
    a missing row is not an error and the caller just wants to fire and
    forget.
    """

    if not chunk_id:
        return False

    params = {"chunk_id": f"eq.{chunk_id}"}
    async with http_client(timeout=_WRITE_TIMEOUT) as http:
        try:
            resp = await http.delete(
                f"{sb_url()}/{_TABLE}",
                params=params,
                headers=sb_headers(),
            )
        except Exception as exc:
            logger.warning("embeddings: delete_chunk %s failed: %s", chunk_id, exc)
            return False

    return resp.status_code in (200, 204)


async def prune_embeddings(
    older_than: datetime,
    *,
    filters: Optional[Dict[str, str]] = None,
) -> int:
    """Delete rows with ``updated_at < older_than``, optional metadata filter.

    Returns the number of rows deleted. Uses ``Prefer: count=exact`` so
    the caller can log / alert on prune volumes. Callers invoke
    explicitly — there is no background prune job (design.md D11).
    """

    params: Dict[str, str] = {"updated_at": f"lt.{older_than.isoformat()}"}
    if filters:
        for k, v in filters.items():
            params[f"metadata->>{k}"] = f"eq.{v}"

    headers = {**sb_headers(), "Prefer": "count=exact,return=minimal"}

    async with http_client(timeout=_WRITE_TIMEOUT) as http:
        resp = await http.delete(
            f"{sb_url()}/{_TABLE}",
            params=params,
            headers=headers,
        )
        resp.raise_for_status()

    content_range = resp.headers.get("content-range", "")
    if "/" in content_range:
        try:
            deleted = int(content_range.split("/")[-1])
        except ValueError:
            deleted = 0
    else:
        deleted = 0

    logger.info("embeddings: pruned %d rows older than %s", deleted, older_than.isoformat())
    return deleted
