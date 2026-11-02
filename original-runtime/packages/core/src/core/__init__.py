"""
core — Leo runtime substrate
==============================

Everything every other Leo package sits on — and nothing more. ``core``
is the leaf package; it depends on stdlib + external runtimes
(``httpx``, ``fastapi``, ``boto3``, ``python-dotenv``) but **must not**
import from any other Leo package.

Top-level import surface
------------------------
Callers write::

    from core import (
        get_supabase, http_client, api_error, NotFound,
        require_contractor, CurrentUser, bedrock_client, retry,
    )

rather than reaching into submodules. The submodules remain addressable
for power users (``from core.auth.jwt import verify_supabase_jwt``,
``from core.db import Supabase``, etc.).

See ``internal/refactor/packages/core/design.md`` for the design and
``internal/refactor/packages/core/api.py`` for the authoritative public
API spec. This ``__init__.py`` is the runtime mirror of that spec.
"""

from __future__ import annotations

from .aws import (
    bedrock_client,
    get_active_leo_aws_account,
    get_leo_aws_credentials,
    leo_boto3_client,
    log_active_leo_aws_account,
    validate_leo_aws_for_bedrock,
)
from .bootstrap import init as bootstrap_init, load_env_files, repo_root
from .db import (
    Supabase,
    check_supabase_health,
    close_supabase_pool,
    get_supabase,
    sb_auth_url,
    sb_headers,
    sb_key,
    sb_url,
)
from .errors import (
    AppError,
    Conflict,
    Forbidden,
    NotFound,
    RateLimited,
    Unauthorized,
    ValidationError,
    api_error,
)
from .http import http_client, http_client_sync, sb_request
from .money import (
    Money,
    add_iva,
    add_iva_percent,
    format_eur,
    from_cents,
    iva_amount,
    iva_amount_percent,
    strip_iva,
    strip_iva_percent,
    to_cents,
)
from .phone import (
    dedup_by_phone,
    extract_from_html,
    is_valid_spanish,
    to_e164,
)
from .resilience import (
    RateLimiter,
    async_retry,
    retry,
    run_sync,
    shared_thread_pool,
    timed_cache,
)
from .text import normalize_spanish, slugify, strip_accents
from .auth.api_keys import require_api_key_or_jwt
from .auth.deps import (
    optional_auth,
    require_auth,
    require_contractor,
    require_homeowner,
)
from .auth.tokens import (
    require_ops_token,
    require_tracker_token,
    tracker_token_valid,
)
from .auth.users import CurrentUser
from .auth.webhooks import verify_webhook_signature

__all__ = [
    "repo_root",
    "load_env_files",
    "bootstrap_init",
    "AppError",
    "NotFound",
    "Unauthorized",
    "Forbidden",
    "ValidationError",
    "Conflict",
    "RateLimited",
    "api_error",
    "Supabase",
    "get_supabase",
    "sb_url",
    "sb_auth_url",
    "sb_key",
    "sb_headers",
    "close_supabase_pool",
    "check_supabase_health",
    "http_client",
    "http_client_sync",
    "sb_request",
    "CurrentUser",
    "require_auth",
    "optional_auth",
    "require_contractor",
    "require_homeowner",
    "require_ops_token",
    "require_tracker_token",
    "tracker_token_valid",
    "require_api_key_or_jwt",
    "verify_webhook_signature",
    "bedrock_client",
    "leo_boto3_client",
    "get_leo_aws_credentials",
    "get_active_leo_aws_account",
    "log_active_leo_aws_account",
    "validate_leo_aws_for_bedrock",
    "retry",
    "async_retry",
    "timed_cache",
    "RateLimiter",
    "shared_thread_pool",
    "run_sync",
    # --- Money (from core.money) ------------------------------------
    "Money",
    "to_cents",
    "from_cents",
    "add_iva",
    "strip_iva",
    "iva_amount",
    "add_iva_percent",
    "strip_iva_percent",
    "iva_amount_percent",
    "format_eur",
    # --- Phone (from core.phone) ------------------------------------
    "to_e164",
    "is_valid_spanish",
    "extract_from_html",
    "dedup_by_phone",
    # --- Text (from core.text) --------------------------------------
    "strip_accents",
    "normalize_spanish",
    "slugify",
]
