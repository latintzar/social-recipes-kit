"""
core.aws — AWS credential resolution + cached boto3 clients
============================================================

Every boto3 client for billable AWS APIs (Bedrock, S3, Transcribe) goes
through :func:`leo_boto3_client` (or the specialised
:func:`bedrock_client`) so we never fall back to ambient credentials or
the wrong IAM user. Billing correctness depends on this.

Configuration (``.env.local`` at repo root)::

    LEO_AWS_ACCOUNT=ALTERNATE2
    AWS_ACCESS_KEY_ID_ALTERNATE2=AKIA…
    AWS_SECRET_ACCESS_KEY_ALTERNATE2=…

Switch accounts by changing only ``LEO_AWS_ACCOUNT`` to another suffix
(e.g. ``PRIMARY``, ``ALTERNATE``, ``ALTERNATE2``). The generic
``AWS_ACCESS_KEY_ID`` is deliberately NOT read by Leo — only suffixed
keys. See ``internal/refactor/packages/core/audit.md`` §aws invariant 1.

Usage
-----
.. code-block:: python

    from core import bedrock_client, leo_boto3_client

    # Cached, region-aware Bedrock client:
    bedrock = bedrock_client("us-east-1")

    # Custom timeouts for long Claude prompts (separate cache entry):
    bedrock_slow = bedrock_client(
        "us-east-1", read_timeout=120, max_attempts=1,
    )

    # Other AWS services:
    s3 = leo_boto3_client("s3", region_name="us-east-1")
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from typing import Any, Tuple

from core.bootstrap import load_env_files, repo_root

logger = logging.getLogger(__name__)

__all__ = [
    "normalize_leo_aws_account",
    "get_active_leo_aws_account",
    "get_leo_aws_credentials",
    "clear_leo_aws_credential_cache",
    "leo_boto3_client",
    "bedrock_client",
    "validate_leo_aws_for_bedrock",
    "log_active_leo_aws_account",
]


def _load_env_local() -> None:
    """Ensure ``.env.local`` is loaded before reading suffixed creds."""
    load_env_files([repo_root() / ".env.local"])


def normalize_leo_aws_account(raw: str) -> str:
    """Upper-case + underscore-normalise the account name.

    Raises :class:`ValueError` on any non-``[A-Z0-9_]`` character after
    normalisation — suffixed env vars have strict shape requirements.
    """
    s = raw.strip().upper().replace("-", "_")
    if not re.match(r"^[A-Z][A-Z0-9_]*$", s):
        raise ValueError(
            f"Invalid LEO_AWS_ACCOUNT {raw!r}: use letters, digits, "
            "underscore (e.g. PRIMARY, ALTERNATE, ALTERNATE2)."
        )
    return s


def get_active_leo_aws_account() -> str:
    """Return the normalised active account name.

    Raises :class:`RuntimeError` if ``LEO_AWS_ACCOUNT`` is unset in env.
    """
    _load_env_local()
    raw = (os.environ.get("LEO_AWS_ACCOUNT") or "").strip()
    if not raw:
        raise RuntimeError(
            "LEO_AWS_ACCOUNT is not set. Set it in .env.local to the "
            "account you intend to bill (e.g. LEO_AWS_ACCOUNT=ALTERNATE2), "
            "and define AWS_ACCESS_KEY_ID_<ACCOUNT> and "
            "AWS_SECRET_ACCESS_KEY_<ACCOUNT>."
        )
    return normalize_leo_aws_account(raw)


@lru_cache(maxsize=8)
def _cached_pair(account: str) -> Tuple[str, str]:
    _load_env_local()
    ak_var = f"AWS_ACCESS_KEY_ID_{account}"
    sk_var = f"AWS_SECRET_ACCESS_KEY_{account}"
    access_key = (os.environ.get(ak_var) or "").strip().strip('"').strip("'")
    secret_key = (os.environ.get(sk_var) or "").strip().strip('"').strip("'")
    if not access_key or not secret_key:
        raise RuntimeError(
            f"Missing credentials for LEO_AWS_ACCOUNT={account}. "
            f"Set {ak_var} and {sk_var} in .env.local. "
            "Leo does not fall back to unsuffixed AWS_ACCESS_KEY_ID."
        )
    return access_key, secret_key


def get_leo_aws_credentials() -> Tuple[str, str]:
    """Return ``(access_key_id, secret_access_key)`` for the active account.

    Cached via ``lru_cache(8)`` — up to 8 distinct accounts can be kept
    hot without re-reading env. In practice there's one.
    """
    return _cached_pair(get_active_leo_aws_account())


def clear_leo_aws_credential_cache() -> None:
    """Reset the credential cache. Used by tests after env manipulation."""
    _cached_pair.cache_clear()
    _bedrock_client_for.cache_clear()


def leo_boto3_client(
    service_name: str,
    *,
    region_name: str,
    config: Any = None,
    **kwargs: Any,
) -> Any:
    """boto3.client with explicit Leo account credentials.

    Never falls back to the default credential chain — billing
    correctness requires explicit creds. See audit §aws invariant 1.
    """
    import boto3

    ak, sk = get_leo_aws_credentials()
    if config is not None:
        return boto3.client(
            service_name,
            region_name=region_name,
            aws_access_key_id=ak,
            aws_secret_access_key=sk,
            config=config,
            **kwargs,
        )
    return boto3.client(
        service_name,
        region_name=region_name,
        aws_access_key_id=ak,
        aws_secret_access_key=sk,
        **kwargs,
    )


@lru_cache(maxsize=8)
def _bedrock_client_for(
    region: str, read_timeout: int, connect_timeout: int, max_attempts: int
) -> Any:
    from botocore.config import Config

    config = Config(
        read_timeout=read_timeout,
        connect_timeout=connect_timeout,
        retries={"max_attempts": max_attempts},
    )
    return leo_boto3_client("bedrock-runtime", region_name=region, config=config)


def bedrock_client(
    region: str = "us-east-1",
    *,
    read_timeout: int = 60,
    connect_timeout: int = 10,
    max_attempts: int = 3,
) -> Any:
    """Return a cached Bedrock client for the given region + timeouts.

    Cache key is ``(region, read_timeout, connect_timeout, max_attempts)``
    — different timeouts yield different cached clients. Pass
    ``read_timeout=120, max_attempts=1`` for long-running prompts where
    the app wants to handle retry itself.
    """
    return _bedrock_client_for(region, read_timeout, connect_timeout, max_attempts)


def validate_leo_aws_for_bedrock() -> None:
    """Resolve credentials once; raise :class:`RuntimeError` if misconfigured."""
    get_leo_aws_credentials()


def log_active_leo_aws_account() -> None:
    """Emit a safe startup log: account name + short key prefix, no secrets."""
    try:
        acct = get_active_leo_aws_account()
        ak, _ = get_leo_aws_credentials()
        prefix = ak[:8] if len(ak) >= 8 else ak
        logger.info(
            "Leo AWS billing account: %s (access key id prefix %s…)",
            acct,
            prefix,
        )
    except Exception as e:
        logger.warning("Leo AWS account not configured: %s", e)
