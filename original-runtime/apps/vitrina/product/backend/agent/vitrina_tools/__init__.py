"""Vitrina Hermes tool-registration package.

With the Hermes-on-stock-MCP migration (2026-04-29), all Vitrina tools
are served by the separate ``vitrina-mcp`` Railway service over the
canonical MCP JSON-RPC wire. Hermes discovers them automatically via
the ``mcp_servers.vitrina`` block in ``$HERMES_HOME/config.yaml``.

This package is kept as a namespace placeholder so existing imports
(``from . import vitrina_tools``) don't break during the transition.
The ``_generated.py`` trampoline and ``_session_ctx.py`` plumbing have
been deleted — they are no longer needed.
"""
