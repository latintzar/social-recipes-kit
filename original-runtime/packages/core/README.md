> Retained April–May 2026 implementation record. [Current public setup](../../../docs/agent-runtime.md).

# `packages/core`

Leo runtime substrate. Every other package sits on top of this one. It
is the leaf: `core` **must not** import any other Leo package. Stdlib +
external runtimes (`httpx`, `fastapi`, `boto3`, `python-dotenv`) only.

Populated during Phase 4 Wave 3. Source of truth for the spec:
`internal/refactor/packages/core/{design.md,api.py,layout.txt}`.

## What lives here

```
packages/core/
├── pyproject.toml
├── README.md
├── src/core/
│   ├── __init__.py           # curated re-exports (mirrors api.py)
│   ├── py.typed              # PEP 561 marker
│   ├── bootstrap.py          # repo_root + load_env_files + init
│   ├── errors.py             # AppError hierarchy + api_error back-compat
│   ├── db.py                 # Supabase class + sb_url/sb_headers/sb_key
│   ├── http.py               # http_client / http_client_sync / sb_request
│   ├── auth/
│   │   ├── __init__.py       # re-exports CurrentUser + require_* + HMAC
│   │   ├── users.py          # CurrentUser dataclass
│   │   ├── jwt.py            # Supabase JWT verify + TTL cache (D6)
│   │   ├── api_keys.py       # leo_key_... verification + combined dep
│   │   ├── deps.py           # require_auth, optional_auth, require_*
│   │   ├── tokens.py         # require_ops_token / require_tracker_token (D8)
│   │   └── webhooks.py       # verify_webhook_signature (HMAC)
│   ├── aws.py                # cached creds + leo_boto3_client + bedrock
│   └── resilience.py         # retry / RateLimiter / timed_cache / run_sync
└── tests/
    └── (unit tests — see section below)
```

## Public API

The spec is `internal/refactor/packages/core/api.py` (`../../internal/refactor/packages/core/api.py`, historical internal reference).
`src/core/__init__.py` re-exports the 40 symbols listed there:

```python
from core import (
    # bootstrap
    repo_root, load_env_files,

    # errors
    AppError, NotFound, Unauthorized, Forbidden, ValidationError,
    Conflict, RateLimited, api_error,

    # db
    Supabase, get_supabase,
    sb_url, sb_auth_url, sb_key, sb_headers,
    close_supabase_pool, check_supabase_health,

    # http
    http_client, http_client_sync, sb_request,

    # auth
    CurrentUser,
    require_auth, optional_auth, require_contractor, require_homeowner,
    require_ops_token, require_tracker_token,
    require_api_key_or_jwt,
    verify_webhook_signature,

    # aws
    bedrock_client, leo_boto3_client,
    get_leo_aws_credentials, get_active_leo_aws_account,

    # resilience
    retry, async_retry, timed_cache, RateLimiter,
    shared_thread_pool, run_sync,
)
```

Submodules remain addressable for power users (e.g.
`from core.auth.jwt import verify_supabase_jwt`, `from core.resilience
import shutdown_thread_pool`). Anything not in `__all__` is considered
package-internal and may change without notice.

## Design decisions in force (from `design.md`)

| ID | Decision | Behavioural impact |
|---|---|---|
| D1 | `AppError` hierarchy + subclasses (`NotFound`, `Unauthorized`, …) | Response shape preserved: `{"ok": false, "error": ..., "code": ...}`. Routes migrate from `raise api_error(404, "…")` to `raise NotFound("…")` |
| D2 | No `config.settings` dependency | `core` reads env vars directly (`SUPABASE_URL`, `LEO_AWS_ACCOUNT`, `OPS_TOKEN`, `SALES_TRACKER_TOKEN`, …) |
| D3 | `repo_root()` via marker-ancestor walk | `Path(__file__).parent.parent.parent` is gone; looks up to the nearest ancestor containing `pyproject.toml` AND `.git` |
| D4 | `Supabase` class + `get_supabase()` singleton | 300s read / 30s connect timeouts preserved. `Prefer: return=representation` default header |
| D5 | `HTTPClient` Protocol deleted | 0 callers today; can be reintroduced when DI testing becomes a need |
| D6 | JWT cache in `core.auth.jwt` | TTL=60s, LRU=1000; env kill-switch `LEO_DISABLE_JWT_CACHE=true`. Role/`contractor_id` still fetched fresh |
| D7 | `circuit_breaker` deferred | 0 callers today; add on first real need |
| D8 | Ops/tracker tokens fail-closed | Dev fallback gated by `LEO_ENV=dev`. Hardcoded `"leo-sales-dev-token-2026"` dropped |

## Dataflow

```
.env.local  ─┐
             ├─► core.bootstrap.load_env_files ─► os.environ
.env        ─┤                                          │
             │                                          ▼
backend/.env ┘                      core.db    core.aws   core.auth.tokens
                                       │          │             │
                                       │          │             │
                                       └──► Supabase client ◄───┘
                                            Bedrock client
                                            JWT cache
```

All env-var reads are **deferred to call time** (never at module import)
so tests can stub via `monkeypatch.setenv`. The same pattern applies in
`core.aws` (credentials are looked up inside `get_leo_aws_credentials`,
not on import).

## Usage

Python / FastAPI:

```python
from fastapi import Depends, FastAPI
from core import (
    AppError, NotFound, Forbidden,
    CurrentUser, require_contractor,
    get_supabase, http_client,
    bedrock_client, retry,
)

app = FastAPI()

@app.exception_handler(AppError)
async def app_error_handler(request, exc: AppError):
    return exc.to_http()  # {"ok": false, "error": ..., "code": ...}

@app.get("/v1/quotes/{id}")
async def get_quote(id: str, user: CurrentUser = Depends(require_contractor)):
    sb = await get_supabase()
    rows = await sb.select(
        "quotes",
        filters={"id": f"eq.{id}", "contractor_id": f"eq.{user.contractor_id}"},
        limit=1,
    )
    if not rows:
        raise NotFound("Quote not found", code="QUOTE_NOT_FOUND")
    return rows[0]
```

Sync tooling:

```python
from core import http_client_sync, run_sync

with http_client_sync(timeout=30) as client:
    resp = client.get("https://example.com/status")

# Drive an async helper from a sync entry-point:
from core.db import check_supabase_health
healthy = run_sync(check_supabase_health())
```

## Testing

Unit tests live in `packages/core/tests/`. They exercise each module in
isolation; Supabase / AWS calls are stubbed via `respx` + `moto`.

To run from the repo root (after `pip install -e packages/core[dev]`):

```bash
pytest packages/core/tests/
```

The scope-backend venv is the blessed test runner (shared by both backends
post Phase 4 Wave E):

```bash
/root/leo/apps/scope/product/backend/venv/bin/python -m pip install -e packages/core[dev]
/root/leo/apps/scope/product/backend/venv/bin/python -m pytest packages/core/tests/
```

Coverage target per `design.md` §Testing: **>85% line**, **100%** on
`errors.py`, `auth/jwt.py`, and `aws.py` (safety-critical).

## Migration history

`packages/core` was a clean-room build. The old `shared/*` files at
`unmigrated/backend/shared/` (51 callers) were replaced wave-by-wave:

- `shared/bootstrap.py` → `core.bootstrap`
- `shared/errors.py` → `core.errors`
- `shared/db.py` → `core.db`
- `shared/http.py` → `core.http`
- `shared/auth.py` → `core.auth.*`
- `shared/aws.py` → `core.aws`
- `shared/resilience.py` → `core.resilience`

Phase 4 completed the sweep — the `shared/` directory is gone, every
caller imports from `core`. The CI gate `scripts/checks/packages_no_shared.sh`
enforces this going forward.
