"""
llm.config — the single LITELLM_* env reader
==============================================

Audit smell #11 (resolved D12): ``shared/constants.py`` defined
``LITELLM_BASE_URL`` without ``/v1``, while ``leo_agent.py`` set its
own default **with** ``/v1``. Silent drift if a developer exported the
env var globally. This module ends that split — one reader, one
canonical shape, one lint rule (archived at
``internal/refactor/archive/scripts-phase4-5/lint_no_env_direct``)
forbids ``os.getenv("LITELLM_`` outside this file.

Suffix decision (D12): ``LITELLM_BASE_URL`` is **without** ``/v1``.
Appending ``/v1/chat/completions`` is the responsibility of the
``litellm_proxy`` provider — that endpoint is a function of the
*target* (chat completions), not the *server*.

Callers prefer the ``proxy_base_url()`` / ``proxy_api_key()``
functions over the module constants so tests can monkey-patch.
"""

from __future__ import annotations

import os

LITELLM_PORT: int = int(os.getenv("LITELLM_PORT", "4000"))
"""LiteLLM proxy port. Defaults to 4000."""

LITELLM_BASE_URL: str = os.getenv(
    "LITELLM_BASE_URL",
    f"http://localhost:{LITELLM_PORT}",
).rstrip("/")
"""Canonical LiteLLM proxy URL. **No ``/v1`` suffix** — providers append it."""

LITELLM_API_KEY: str = os.getenv("LITELLM_API_KEY", "sk-litellm-local")
"""Bearer token for the LiteLLM proxy."""


def proxy_base_url() -> str:
    """Return the proxy base URL.

    Prefer this over importing the module constant directly — tests can
    monkey-patch this function without reloading the module.
    """
    return LITELLM_BASE_URL


def proxy_api_key() -> str:
    """Return the proxy API key. See :func:`proxy_base_url` for rationale."""
    return LITELLM_API_KEY


__all__ = [
    "LITELLM_BASE_URL",
    "LITELLM_API_KEY",
    "proxy_base_url",
    "proxy_api_key",
]
