"""
llm.embed — text embeddings via the LiteLLM proxy
====================================================

Currently: OpenAI ``text-embedding-3-small`` via the proxy's
``/v1/embeddings`` endpoint. Shape matches OpenAI's response format
(``{"data":[{"embedding":[...]}]}``).

No production caller uses this today; pre-shipped for future
RAG/search features. Providers with native embedding support are a
future addition — this module grows a small provider dispatch then.
"""

from __future__ import annotations

from typing import List, Optional

import httpx

from llm.config import proxy_api_key, proxy_base_url
from llm.errors import InvalidResponseShape, ProviderError

_DEFAULT_MODEL = "openai/text-embedding-3-small"
_TIMEOUT = 30.0


def _endpoint() -> str:
    return f"{proxy_base_url()}/v1/embeddings"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {proxy_api_key()}",
        "Content-Type": "application/json",
    }


def _parse(data: dict, *, model: str) -> List[List[float]]:
    try:
        return [row["embedding"] for row in data["data"]]
    except (KeyError, TypeError) as exc:
        raise InvalidResponseShape(
            f"embed response missing data[].embedding: {data!r}",
            provider="litellm_proxy",
            model=model,
            raw=exc,
        ) from exc


async def embed(
    texts: List[str],
    *,
    model: str = _DEFAULT_MODEL,
    client: Optional[httpx.AsyncClient] = None,
) -> List[List[float]]:
    """Async text embedding."""
    if not texts:
        return []
    body = {"model": model, "input": texts}

    owned = client is None
    http = client or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        resp = await http.post(_endpoint(), json=body, headers=_headers())
        resp.raise_for_status()
        return _parse(resp.json(), model=model)
    except httpx.HTTPError as exc:
        raise ProviderError(
            f"embed failed: {exc}",
            provider="litellm_proxy",
            model=model,
            raw=exc,
        ) from exc
    finally:
        if owned:
            await http.aclose()


def embed_sync(
    texts: List[str],
    *,
    model: str = _DEFAULT_MODEL,
    client: Optional[httpx.Client] = None,
) -> List[List[float]]:
    """Sync text embedding. One-shot ``httpx.Client`` per call."""
    if not texts:
        return []
    body = {"model": model, "input": texts}

    owned = client is None
    http = client or httpx.Client(timeout=_TIMEOUT)
    try:
        resp = http.post(_endpoint(), json=body, headers=_headers())
        resp.raise_for_status()
        return _parse(resp.json(), model=model)
    except httpx.HTTPError as exc:
        raise ProviderError(
            f"embed failed: {exc}",
            provider="litellm_proxy",
            model=model,
            raw=exc,
        ) from exc
    finally:
        if owned:
            http.close()


__all__ = ["embed", "embed_sync"]
