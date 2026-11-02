"""
core.auth.webhooks — constant-time HMAC helper for webhook payloads
====================================================================

One helper, one purpose:

.. code-block:: python

    from core import verify_webhook_signature

    if not verify_webhook_signature(
        payload=request_body_bytes,
        signature=request.headers["X-Signature"],
        secret=os.environ["RESEND_WEBHOOK_SECRET"],
        prefix="sha256=",
    ):
        raise Forbidden("Invalid webhook signature")

Stripe has its own :func:`stripe.Webhook.construct_event` — use that for
Stripe. This helper covers the generic HMAC case (Resend, Meta, internal
webhook routes).
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Literal, Union

__all__ = ["verify_webhook_signature"]


def verify_webhook_signature(
    payload: Union[bytes, str],
    signature: str,
    secret: str,
    *,
    algo: Literal["sha256", "sha1", "sha512"] = "sha256",
    prefix: str = "",
) -> bool:
    """Constant-time HMAC verification.

    Arguments
    ---------
    payload:
        Raw request body; either ``bytes`` or ``str`` (UTF-8 encoded).
    signature:
        The signature header value (hex digest). Optionally prefixed —
        e.g. ``"sha256=..."`` on GitHub-style headers; strip via ``prefix``.
    secret:
        The shared secret. Either the raw secret string or something
        UTF-8 encodable.
    algo:
        Hash function name; ``"sha256"`` by default. Must be one of
        :mod:`hashlib`'s supported digests.
    prefix:
        Optional prefix to strip from ``signature`` before comparison.
        E.g. ``"sha256="`` for GitHub-style headers.

    Returns ``True`` on match, ``False`` on any mismatch / missing values.
    """
    if not signature or not secret:
        return False
    if prefix and signature.startswith(prefix):
        signature = signature[len(prefix):]
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    hash_fn = getattr(hashlib, algo)
    expected = hmac.new(secret.encode("utf-8"), data, hash_fn).hexdigest()
    return hmac.compare_digest(expected, signature)
