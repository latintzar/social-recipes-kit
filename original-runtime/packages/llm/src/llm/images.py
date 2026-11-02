"""
llm.images — image generation via the LiteLLM proxy
=======================================================

Unified image-generation primitive. OpenAI-shape request
(``POST /v1/images/generations``), base64 PNG response.

The proxy owns retry, fallback, and audit policy. This module just
shapes the request, parses the response, and maps transport errors
into the :mod:`llm.errors` taxonomy — the same mapping as
:mod:`llm.providers.litellm_proxy`.

>>> png = generate_image("a black pencil sketch of a kitchen")
>>> png[:8]  # PNG magic bytes
b'\\x89PNG\\r\\n\\x1a\\n'

Callers (``packages/brand.illustrations``, future marketing tools,
etc.) import from the top-level :mod:`llm` namespace::

    from llm import generate_image, DEFAULT_IMAGE_GEN

    png_bytes = generate_image(prompt, model=DEFAULT_IMAGE_GEN)

Model routing is the proxy's job — whatever ``DEFAULT_IMAGE_GEN``
resolves to (``gemini-2.5-flash-image`` by default, overridable via
``LEO_LLM_DEFAULT_IMAGE_GEN``) funnels through the same endpoint.
"""

from __future__ import annotations

import base64
from typing import Literal, Optional

import httpx

from llm.config import proxy_api_key, proxy_base_url
from llm.errors import (
    InvalidResponseShape,
    ProviderError,
    ProviderUnavailable,
    RateLimitError,
)
from llm.models.tiers import DEFAULT_IMAGE_GEN

_DEFAULT_TIMEOUT = 120.0
_PROVIDER = "litellm_proxy"


def _endpoint() -> str:
    return f"{proxy_base_url()}/v1/images/generations"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {proxy_api_key()}",
        "Content-Type": "application/json",
    }


def _map_http_error(exc: httpx.HTTPError, *, model: str) -> Exception:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        body_preview = exc.response.text[:200]
        if status == 429:
            return RateLimitError(
                f"image-gen rate-limited (429): {body_preview}",
                provider=_PROVIDER,
                model=model,
                raw=exc,
            )
        if status >= 500:
            return ProviderUnavailable(
                f"image-gen 5xx: {status} {body_preview}",
                provider=_PROVIDER,
                model=model,
                raw=exc,
            )
        return ProviderError(
            f"image-gen 4xx: {status} {body_preview}",
            provider=_PROVIDER,
            model=model,
            raw=exc,
        )
    if isinstance(exc, (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException)):
        return ProviderUnavailable(
            f"image-gen transport error: {exc}",
            provider=_PROVIDER,
            model=model,
            raw=exc,
        )
    return ProviderError(
        f"image-gen unexpected error: {exc}",
        provider=_PROVIDER,
        model=model,
        raw=exc,
    )


def _parse_response(
    data: dict,
    *,
    model: str,
    response_format: Literal["bytes", "url"],
) -> bytes | str:
    try:
        item = data["data"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise InvalidResponseShape(
            f"image-gen response missing data[0]: {data!r}",
            provider=_PROVIDER,
            model=model,
            raw=exc,
        ) from exc

    if response_format == "bytes":
        b64 = item.get("b64_json")
        if not b64:
            raise InvalidResponseShape(
                f"image-gen response missing b64_json: {item!r}",
                provider=_PROVIDER,
                model=model,
            )
        return base64.b64decode(b64)

    url = item.get("url")
    if not url:
        raise InvalidResponseShape(
            f"image-gen response missing url: {item!r}",
            provider=_PROVIDER,
            model=model,
        )
    return url


def _build_body(
    *,
    prompt: str,
    model: str,
    size: str,
    response_format: Literal["bytes", "url"],
    extra: Optional[dict],
) -> dict:
    body: dict = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "n": 1,
        "response_format": "b64_json" if response_format == "bytes" else "url",
    }
    if extra:
        body.update(extra)
    return body


def generate_image(
    prompt: str,
    *,
    model: str = DEFAULT_IMAGE_GEN,
    size: str = "1024x1024",
    response_format: Literal["bytes", "url"] = "bytes",
    timeout: float = _DEFAULT_TIMEOUT,
    client: Optional[httpx.Client] = None,
    extra: Optional[dict] = None,
) -> bytes | str:
    """Generate an image via the LiteLLM proxy. Sync.

    Parameters
    ----------
    prompt
        Text description of the image to generate.
    model
        Canonical model id. Defaults to :data:`DEFAULT_IMAGE_GEN`
        (``gemini-2.5-flash-image`` unless overridden via
        ``LEO_LLM_DEFAULT_IMAGE_GEN``). The proxy routes to the
        correct backend — OpenAI DALL-E, Google Imagen, Stability, etc.
    size
        ``WxH`` string. Defaults ``1024x1024``. Some models restrict the
        set of valid sizes; pass-through.
    response_format
        ``"bytes"`` (decoded PNG) or ``"url"`` (ephemeral URL, depends
        on the upstream provider — not all providers support this).
    timeout
        HTTP timeout in seconds.
    client
        Optional pre-configured :class:`httpx.Client` (used by tests to
        inject a :class:`~httpx.MockTransport`). If not provided, a
        short-lived client is created per call.
    extra
        Extra body fields merged into the OpenAI payload (e.g.
        ``{"quality": "hd"}`` for DALL-E-3). Unknown fields are ignored
        by the proxy but preserved here so callers don't have to fork.

    Returns
    -------
    bytes | str
        Raw PNG bytes (``response_format="bytes"``) or URL string.

    Raises
    ------
    RateLimitError
        On HTTP 429.
    ProviderUnavailable
        On HTTP 5xx, connect/read errors, or timeout.
    ProviderError
        On HTTP 4xx (non-429) or other unexpected transport error.
    InvalidResponseShape
        On missing/malformed response payload.
    """
    body = _build_body(
        prompt=prompt,
        model=model,
        size=size,
        response_format=response_format,
        extra=extra,
    )
    url = _endpoint()
    headers = _headers()

    owned = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        resp = http.post(url, json=body, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return _parse_response(
            resp.json(), model=model, response_format=response_format
        )
    except httpx.HTTPError as exc:
        raise _map_http_error(exc, model=model) from exc
    finally:
        if owned:
            http.close()


async def generate_image_async(
    prompt: str,
    *,
    model: str = DEFAULT_IMAGE_GEN,
    size: str = "1024x1024",
    response_format: Literal["bytes", "url"] = "bytes",
    timeout: float = _DEFAULT_TIMEOUT,
    client: Optional[httpx.AsyncClient] = None,
    extra: Optional[dict] = None,
) -> bytes | str:
    """Async version of :func:`generate_image`. Same semantics and errors."""
    body = _build_body(
        prompt=prompt,
        model=model,
        size=size,
        response_format=response_format,
        extra=extra,
    )
    url = _endpoint()
    headers = _headers()

    owned = client is None
    http = client or httpx.AsyncClient(timeout=timeout)
    try:
        resp = await http.post(url, json=body, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return _parse_response(
            resp.json(), model=model, response_format=response_format
        )
    except httpx.HTTPError as exc:
        raise _map_http_error(exc, model=model) from exc
    finally:
        if owned:
            await http.aclose()


def _chat_endpoint() -> str:
    return f"{proxy_base_url()}/v1/chat/completions"


def _extract_image_from_chat(
    data: dict, *, model: str, response_format: Literal["bytes", "url"]
) -> bytes | str:
    """Parse an OpenRouter-style image response from chat-completions.

    Nano Banana Pro returns generated images in
    ``choices[0].message.images[0].image_url.url`` as a base64 data URL
    (``data:image/png;base64,...``). This helper normalises that into
    either raw PNG bytes or a URL string.
    """
    try:
        images = data["choices"][0]["message"]["images"]
        first = images[0]
        url = first["image_url"]["url"]
    except (KeyError, IndexError, TypeError) as exc:
        raise InvalidResponseShape(
            f"image-gen chat response missing images[0]: {data!r}",
            provider=_PROVIDER,
            model=model,
            raw=exc,
        ) from exc
    if response_format == "url":
        return url
    # Data URL shape: "data:image/png;base64,<payload>"
    if not url.startswith("data:"):
        raise InvalidResponseShape(
            f"image-gen chat response returned non-data URL: {url[:40]!r}",
            provider=_PROVIDER,
            model=model,
        )
    try:
        _, payload = url.split(",", 1)
    except ValueError as exc:
        raise InvalidResponseShape(
            f"image-gen chat response data URL malformed: {url[:40]!r}",
            provider=_PROVIDER,
            model=model,
            raw=exc,
        ) from exc
    return base64.b64decode(payload)


def generate_image_via_chat(
    prompt: str,
    *,
    model: str = DEFAULT_IMAGE_GEN,
    aspect_ratio: str = "1:1",
    image_size: str = "2K",
    response_format: Literal["bytes", "url"] = "bytes",
    timeout: float = _DEFAULT_TIMEOUT,
    client: Optional[httpx.Client] = None,
) -> bytes | str:
    """Text-to-image via the chat-completions endpoint.

    Use this instead of :func:`generate_image` whenever you need the
    ``image_config`` block (``image_size``, ``aspect_ratio``) to be
    honoured.  The legacy ``/v1/images/generations`` endpoint that
    :func:`generate_image` talks to ignores ``image_config`` on
    ``gemini-3-pro-image`` and silently falls back to 1024×1024 —
    documented behaviour, tracked on OpenRouter issue #269 and
    Google's Vertex AI docs.  This helper routes through
    ``/v1/chat/completions`` with ``modalities: ["image", "text"]``,
    which is the path ``image_size`` is actually wired to.

    Parameters mirror :func:`generate_image_with_references` except
    for the lack of ``references`` — for reference-conditioned image
    edits, keep using that function.
    """
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image", "text"],
        "image_config": {
            "aspect_ratio": aspect_ratio,
            "image_size": image_size,
        },
    }

    url = _chat_endpoint()
    headers = _headers()

    owned = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        resp = http.post(url, json=body, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return _extract_image_from_chat(
            resp.json(), model=model, response_format=response_format
        )
    except httpx.HTTPError as exc:
        raise _map_http_error(exc, model=model) from exc
    finally:
        if owned:
            http.close()


def generate_image_with_references(
    prompt: str,
    *,
    references: list[bytes],
    model: str = DEFAULT_IMAGE_GEN,
    aspect_ratio: str = "1:1",
    image_size: str = "2K",
    response_format: Literal["bytes", "url"] = "bytes",
    timeout: float = _DEFAULT_TIMEOUT,
    client: Optional[httpx.Client] = None,
) -> bytes | str:
    """Generate an image conditioned on one or more reference images.

    This is the multi-image / consistent-character entrypoint. It talks
    to the LiteLLM proxy's ``/v1/chat/completions`` endpoint with
    ``modalities: ["image", "text"]`` — the native shape for
    ``gemini-3-pro-image`` / Nano Banana Pro, which can preserve identity
    across up to 5 reference subjects.

    Parameters
    ----------
    prompt
        Text instruction. Describe what should change, what should stay
        the same, and how the references should be used ("keep the
        character's face identity, put them in a different pose", etc.).
    references
        PNG/JPEG byte-strings to condition on. Keep ≤5 for best
        identity preservation on Nano Banana Pro.
    model
        Must be a model that supports image output via chat-completions.
        Defaults to :data:`DEFAULT_IMAGE_GEN`.
    aspect_ratio
        ``"1:1"`` (default), ``"16:9"``, ``"9:16"``, ``"3:2"``, etc.
        Passed to OpenRouter via ``image_config.aspect_ratio``.
    image_size
        ``"1K"`` / ``"2K"`` / ``"4K"``. ``"4K"`` is Pro-only.

    Returns
    -------
    bytes | str
        Raw PNG bytes (``response_format="bytes"``) or data URL string.

    Notes
    -----
    For pure text-to-image with no references, use :func:`generate_image`
    — it's cheaper and simpler because it uses the ``/images/generations``
    endpoint. This helper only exists for the reference-conditioned path.
    """
    if not references:
        raise ValueError(
            "generate_image_with_references requires at least one reference; "
            "use generate_image for text-only prompts."
        )

    content_parts: list[dict] = []
    for i, ref in enumerate(references):
        if not isinstance(ref, bytes):
            raise TypeError(
                f"references[{i}] must be bytes, got {type(ref).__name__}"
            )
        b64 = base64.b64encode(ref).decode("ascii")
        content_parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            }
        )
    content_parts.append({"type": "text", "text": prompt})

    body = {
        "model": model,
        "messages": [{"role": "user", "content": content_parts}],
        "modalities": ["image", "text"],
        "image_config": {
            "aspect_ratio": aspect_ratio,
            "image_size": image_size,
        },
    }

    url = _chat_endpoint()
    headers = _headers()

    owned = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        resp = http.post(url, json=body, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return _extract_image_from_chat(
            resp.json(), model=model, response_format=response_format
        )
    except httpx.HTTPError as exc:
        raise _map_http_error(exc, model=model) from exc
    finally:
        if owned:
            http.close()


__all__ = [
    "generate_image",
    "generate_image_async",
    "generate_image_via_chat",
    "generate_image_with_references",
]
