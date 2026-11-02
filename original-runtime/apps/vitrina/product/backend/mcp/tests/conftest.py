"""Shared pytest setup for product/backend/mcp/ tests.

Puts the parent folder (``product/backend/mcp/``) on ``sys.path`` so the
flat-module layout (``tools``, ``data``, ``snapshots``) is importable.

See ``../README.md`` → "Why this folder has no __init__.py" for the
full reasoning — TL;DR: the name ``mcp`` is taken by the upstream MCP
Python SDK that fastmcp depends on, so our folder intentionally isn't
a package, and sibling modules load via sys.path instead. This same
pattern is baked into the ``backend-mcp`` bootstrap template at
``scripts/bootstrap/templates/backend-mcp/``.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_MCP_DIR = os.path.dirname(_HERE)  # apps/vitrina/product/backend/mcp/
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)
