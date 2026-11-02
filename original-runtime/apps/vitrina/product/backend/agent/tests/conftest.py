"""Shared pytest setup for product/backend/agent/ tests.

Puts the parent folder (``product/backend/agent/``) on ``sys.path`` so
the flat-module layout (``stream``, ``session``, ``config``, ``main``)
is importable without a package install. Mirrors
``product/backend/mcp/tests/conftest.py`` — same reasoning: our folder
deliberately isn't a package so sibling modules load via sys.path.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT_DIR = os.path.dirname(_HERE)  # apps/vitrina/product/backend/agent/
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

# ``platforms/instagram_dm.py`` imports ``core.http_client_sync`` from shared packages.
_LEO_ROOT = os.path.abspath(os.path.join(_AGENT_DIR, "..", "..", "..", "..", ".."))
_CORE_SRC = os.path.join(_LEO_ROOT, "packages", "core", "src")
if os.path.isdir(_CORE_SRC) and _CORE_SRC not in sys.path:
    sys.path.insert(0, _CORE_SRC)
