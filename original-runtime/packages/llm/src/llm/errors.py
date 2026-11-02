"""
llm.errors — typed exception hierarchy
=========================================

Mirrors ``core.errors.AppError`` ergonomics: every raise carries the
context needed to triage (provider, model, underlying exception), and
subclasses signal caller intent (retryable vs caller-bug vs schema-bug).

Inheritance tree::

    LLMError                        (base)
    ├── ProviderError               (any provider-originated failure)
    │   ├── ProviderUnavailable     (5xx / connection refused — retryable)
    │   ├── RateLimitError          (429 — retryable)
    │   └── ContextWindowExceeded   (input too long — may fall through to larger model)
    ├── InvalidResponseShape        (parse failure — NOT retryable)
    ├── ModelNotFound               (caller bug — NOT retryable)
    └── PromptNotFound              (missing template on disk)
"""

from __future__ import annotations

from typing import Optional


class LLMError(Exception):
    """Base for every error raised inside :mod:`llm`.

    Carries ``provider``, ``model``, and optional ``raw`` (underlying
    httpx / botocore exception) for log triage.
    """

    provider: Optional[str] = None
    model: Optional[str] = None
    raw: Optional[Exception] = None

    def __init__(
        self,
        message: str,
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        raw: Optional[Exception] = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.raw = raw

    def __repr__(self) -> str:
        bits = [f"{self.__class__.__name__}({self.args[0]!r}"]
        if self.provider:
            bits.append(f"provider={self.provider!r}")
        if self.model:
            bits.append(f"model={self.model!r}")
        return ", ".join(bits) + ")"


class ProviderError(LLMError):
    """Any error originating from a provider (proxy, Bedrock, Gemini)."""


class ProviderUnavailable(ProviderError):
    """5xx / connection refused / post-connect read timeout. Retryable."""


class RateLimitError(ProviderError):
    """429 from provider (or proxy relaying a provider 429). Retryable."""


class ContextWindowExceeded(ProviderError):
    """Input tokens exceeded the model's context window.

    Retryable only on a larger-context fallback; else re-raised to caller.
    """


class InvalidResponseShape(LLMError):
    """Provider returned something we couldn't parse.

    Non-retryable by default — this signals provider / model misbehaviour,
    not transient failure. Fallback chain does NOT trigger.
    """


class ModelNotFound(LLMError):
    """Requested model isn't registered, is on the anti-list, or lacks a
    required capability (e.g., vision call with a non-vision model).

    Always a caller bug. Non-retryable.
    """


class PromptNotFound(LLMError):
    """``render_prompt(id, version, language)`` has no matching template."""


__all__ = [
    "LLMError",
    "ProviderError",
    "ProviderUnavailable",
    "RateLimitError",
    "ContextWindowExceeded",
    "InvalidResponseShape",
    "ModelNotFound",
    "PromptNotFound",
]
