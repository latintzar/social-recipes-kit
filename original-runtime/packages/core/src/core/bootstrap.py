"""
core.bootstrap — repo-root discovery + dotenv loading
=======================================================

Replaces the old `shared.bootstrap` with a location-independent API.

Two layers, both idempotent:

1. :func:`repo_root` — marker-ancestor walk (looks for ``pyproject.toml`` AND
   ``.git``). Cached with ``lru_cache(1)``. Any file inside the repo can call
   this without needing caller-relative path math.

2. :func:`load_env_files` — layered dotenv loading. Default trio is
   ``repo_root/.env.local``, ``repo_root/.env``, and (during the migration
   window) the legacy ``backend/.env`` — each loaded in order with
   ``override=False``. Missing files are silently skipped.

Compatibility
-------------
The function :func:`init` is preserved for migration compatibility — 9
tools/scripts today do ``from shared.bootstrap import init; init()`` at
script-top. Post-migration these will flip to
``from core.bootstrap import init``. Eventually the whole ``init`` dance
becomes obsolete (once packages are installed via ``pip install -e``
everywhere), at which point :func:`init` can be marked deprecated and then
removed.

See ``internal/refactor/packages/core/design.md`` decision D3 and
``internal/refactor/packages/core/audit.md`` §bootstrap for rationale.
"""

from __future__ import annotations

import functools
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_INITIALIZED = False

__all__ = [
    "repo_root",
    "load_env_files",
    "init",
    "scope_backend_dir",
    "backend_dir",
]


@functools.lru_cache(maxsize=1)
def repo_root() -> Path:
    """Return the repo root by walking parents for ``pyproject.toml`` + ``.git``.

    Starts from this file's directory and walks up. The nearest ancestor
    that contains BOTH ``pyproject.toml`` and ``.git`` (file or dir) wins.

    Raises :class:`RuntimeError` if no such ancestor exists. Cached with
    ``lru_cache(1)`` — first call walks, subsequent calls are free.

    Location-independent: works whether this file lives at
    ``packages/core/src/core/bootstrap.py`` or moves elsewhere in the
    monorepo. Replaces the old ``Path(__file__).parent.parent.parent`` climb.
    """
    start = Path(__file__).resolve()
    for cand in [start, *start.parents]:
        if (cand / "pyproject.toml").is_file() and (cand / ".git").exists():
            return cand
    raise RuntimeError(
        "core.bootstrap.repo_root: no ancestor of "
        f"{start} contains both pyproject.toml and .git"
    )


def scope_backend_dir() -> Path:
    """Return the path to the scope-backend tree.

    During the migration window the backend lives at
    ``unmigrated/backend``. Post-migration it will move to
    ``apps/scope-backend``. This helper checks both and returns whichever
    exists (preferring the new location).
    """
    root = repo_root()
    new_path = root / "apps" / "scope-backend"
    if new_path.is_dir():
        return new_path
    return root / "unmigrated" / "backend"


def backend_dir() -> Path:
    """Deprecated alias for :func:`scope_backend_dir`.

    Kept for migration compatibility so the 9 script-top
    ``from shared.bootstrap import backend_dir`` sites keep working while
    their imports get flipped. Will emit a ``DeprecationWarning`` once the
    migration window closes.
    """
    return scope_backend_dir()


def init(*, load_dotenv_local: bool = True) -> None:
    """Idempotent: add the backend dir to ``sys.path`` + load ``.env.local``.

    Used at the top of stand-alone scripts that run outside FastAPI's
    process (e.g., ``tools/*.py``). FastAPI itself bootstraps via
    ``config.settings`` which calls :func:`load_env_files` directly — so
    FastAPI-hosted code does NOT need to call ``init()``.

    Guards against double-init with a module-level flag.
    """
    if os.getenv("SOCIAL_RECIPES_RUNTIME") == "1":
        return

    global _INITIALIZED
    if _INITIALIZED:
        return

    bdir = str(scope_backend_dir())
    if bdir not in sys.path:
        sys.path.insert(0, bdir)

    if load_dotenv_local:
        try:
            from dotenv import load_dotenv
        except ImportError:
            # dotenv is optional for tools that don't read env vars.
            pass
        else:
            env_path = repo_root() / ".env.local"
            if env_path.is_file():
                load_dotenv(env_path, override=False)

    _INITIALIZED = True


def load_env_files(
    paths: Optional[List[Path]] = None,
    *,
    override: bool = False,
) -> List[Path]:
    """Load one or more dotenv files; return the list of paths that loaded.

    Default trio (in order of precedence, highest first):
      1. ``repo_root/.env.local`` — developer overrides, gitignored
      2. ``repo_root/.env`` — shared committed defaults (if ever)
      3. ``scope_backend_dir()/.env`` — legacy backend-local env

    With ``override=False`` (default), values already present in
    ``os.environ`` are not clobbered — so the first file loaded wins for
    any given key. Pass ``override=True`` to flip that to "last wins".

    Missing files are silently skipped. If :mod:`python-dotenv` is not
    installed, returns ``[]`` (the legacy behaviour).
    """
    if os.getenv("SOCIAL_RECIPES_RUNTIME") == "1":
        return []

    try:
        from dotenv import load_dotenv
    except ImportError:
        return []
    if paths is None:
        paths = [
            repo_root() / ".env.local",
            repo_root() / ".env",
            scope_backend_dir() / ".env",
        ]
    loaded: List[Path] = []
    for p in paths:
        if p.is_file():
            load_dotenv(p, override=override)
            loaded.append(p)
    return loaded
