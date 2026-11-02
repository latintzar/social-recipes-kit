"""Vitrina MCP server — FastMCP entrypoint.

Two transports out of one tool surface:

* **stdio** — for Claude Desktop / Cursor local config. Run
  ``uv run python server.py`` directly; the server reads JSON-RPC over
  stdin and writes to stdout. Claude/Cursor launches this as a subprocess.
* **streamable-http** — for the hosted deployment.
  Run ``uv run python server.py --http --port 8787`` and point any
  MCP client (Claude Desktop, ChatGPT custom GPT, Cursor, OpenClaw,
  Hermes) at ``https://<host>/mcp``.

  When ``VITRINA_MCP_BEARER_TOKEN`` is set in the environment every
  HTTP request must include ``Authorization: Bearer <token>``.  The
  stdio transport is always auth-free (local desktop).

All tool definitions and handlers live in :mod:`tools`; this file is the
thin transport shell. Keeping the two layers separate lets
:mod:`product.backend.api` reuse the same handlers over plain REST so the
future web/mobile surface speaks the identical contract.

Graduated from the Node prototype at
``apps/vitrina/product/scratch/experiments/mcp-server/`` on 2026-04-22.
The Node version stays deployed until this port is verified in staging.
"""
from __future__ import annotations

import argparse
import logging
from typing import Any

import os
import sys

# Backend/mcp/ is intentionally a FLAT MODULE DIRECTORY (no __init__.py),
# not a Python package. The name `mcp` is taken by the upstream pip
# package (the official MCP Python SDK that fastmcp depends on); if we
# exposed our folder as a package named `mcp`, fastmcp's internal
# `from mcp.types import ...` would resolve to our folder and crash.
# We keep the folder NAMED `mcp/` (matches the surface registry in
# AGENTS.md + is the convention for an MCP server) by loading our
# sibling modules via sys.path instead of via package imports.
# See README.md → "Why this folder has no __init__.py" for the full
# reasoning. DO NOT add an __init__.py back without reading that.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fastmcp import FastMCP  # type: ignore[import-not-found]

import tools  # noqa: E402 — sys.path set up above
import checkout_handlers  # noqa: E402

logger = logging.getLogger(__name__)

mcp = FastMCP(tools.SERVER_NAME, instructions=tools.SERVER_INSTRUCTIONS)


@mcp.tool
def list_demo_shops(trade: str | None = None) -> dict[str, Any]:
    """List the six Bulgarian demo shops (3 food + 3 hardware)."""
    return tools.handle_list_demo_shops(trade=trade)


@mcp.tool
def list_products_in_shop(shop: str, in_stock_only: bool = True) -> dict[str, Any]:
    """Return the catalogue of one demo shop with live-stock overlay."""
    return tools.handle_list_products_in_shop(shop=shop, in_stock_only=in_stock_only)


@mcp.tool
def search_across_shops(
    query: str,
    trade: str | None = None,
    max_results: int = 20,
    in_stock_only: bool = True,
) -> dict[str, Any]:
    """Search all demo shops for products matching the query (BG or EN)."""
    return tools.handle_search_across_shops(
        query=query,
        trade=trade,
        max_results=max_results,
        in_stock_only=in_stock_only,
    )


@mcp.tool
def report_pos_snapshot(
    shop_id: str,
    products: list[dict[str, Any]],
    source: str | None = None,
    host: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Ingest a read-only POS snapshot from a vendor-specific connector
    (e.g. `whp-reader` for Microinvest, `shopify-pull` for Shopify; see
    `product/integrations/`)."""
    return tools.handle_report_pos_snapshot(
        shop_id=shop_id,
        products=products,
        source=source,
        host=host,
        as_of=as_of,
    )


@mcp.tool
def get_live_pos_snapshot(shop_id: str | None = None) -> dict[str, Any]:
    """Read back the merged shop_stock / leo_sold_delta / effective_stock view."""
    return tools.handle_get_live_pos_snapshot(shop_id=shop_id)


@mcp.tool
def record_leo_sale(
    shop_id: str,
    product_id: str,
    quantity: int,
    order_ref: str | None = None,
) -> dict[str, Any]:
    """Record that Leo sold `quantity` units of `product_id` at `shop_id`."""
    return tools.handle_record_leo_sale(
        shop_id=shop_id,
        product_id=product_id,
        quantity=quantity,
        order_ref=order_ref,
    )


@mcp.tool
def shop_acknowledge_sales(
    shop_id: str,
    product_ids: list[str] | None = None,
    writeback_to_pos: bool = False,
) -> dict[str, Any]:
    """Shopkeeper confirms fulfilment; zeros ``leo_sold_delta``."""
    return tools.handle_shop_acknowledge_sales(
        shop_id=shop_id,
        product_ids=product_ids,
        writeback_to_pos=writeback_to_pos,
    )


@mcp.tool
def list_shop_notifications(
    shop_id: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """List recent notifications for a shop (the shopkeeper-app inbox)."""
    return tools.handle_list_shop_notifications(shop_id=shop_id, limit=limit)


@mcp.tool
def get_mcp_audit_trail(limit: int = 100) -> dict[str, Any]:
    """Last N audit events (snapshots, sales, acknowledgements, notifications)."""
    return tools.handle_get_mcp_audit_trail(limit=limit)


# ── ACP checkout surface (spec 2026-04-17) ────────────────────────────
# Each tool requires meta.api_version == "2026-04-17"; bad version raises
# ACPError(-32602). Stock races on complete raise -32000. FastMCP maps
# raised exceptions onto the JSON-RPC error envelope automatically, so
# the codes surface cleanly to ACP-aware agents. See checkout_handlers.py.


@mcp.tool
def create_checkout_session(
    meta: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Create an ACP checkout session. Click-and-collect only."""
    return checkout_handlers.handle_create_checkout_session(meta=meta, payload=payload)


@mcp.tool
def get_checkout_session(meta: dict[str, Any], id: str) -> dict[str, Any]:
    """Retrieve an ACP checkout session by its signed id."""
    return checkout_handlers.handle_get_checkout_session(meta=meta, id=id)


@mcp.tool
def update_checkout_session(
    meta: dict[str, Any],
    id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Update an in-progress ACP session. Reissues the session id."""
    return checkout_handlers.handle_update_checkout_session(meta=meta, id=id, payload=payload)


@mcp.tool
def complete_checkout_session(
    meta: dict[str, Any],
    id: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Finalize the order: decrement stock, mint order_id + collection_code, email receipt."""
    return checkout_handlers.handle_complete_checkout_session(meta=meta, id=id, payload=payload)


@mcp.tool
def cancel_checkout_session(meta: dict[str, Any], id: str) -> dict[str, Any]:
    """Cancel an in-progress ACP session."""
    return checkout_handlers.handle_cancel_checkout_session(meta=meta, id=id)


@mcp.tool
def reset_demo_stock() -> dict[str, Any]:
    """DEV — zero all leo_sold_delta ledgers so a demo can be rerun."""
    return checkout_handlers.handle_reset_demo_stock()


def _make_typed_wrapper(public_name: str, handler: Any) -> Any:
    """Build a fully-typed wrapper function FastMCP can introspect.

    FastMCP 2.x rejects ``**kwargs`` tool signatures — it needs explicit
    parameter names and type annotations to generate the JSON Schema that
    the MCP client sees. We copy the handler's signature onto a fresh
    function using ``functools.wraps`` + ``inspect.Signature``, so FastMCP
    gets the same parameter list the handler has without a hand-rolled stub
    for each of the ~25 coordinator-registered tools.
    """
    import functools
    import inspect

    sig = inspect.signature(handler)
    fn_name = public_name.replace(".", "_")

    # Build the exec source with the real parameter list.
    params = sig.parameters
    param_str = ", ".join(
        str(p) for p in params.values()
        if p.kind not in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        )
    )
    call_str = ", ".join(
        f"{n}={n}" if p.kind != inspect.Parameter.POSITIONAL_ONLY else n
        for n, p in params.items()
        if p.kind not in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        )
    )
    # Build a function with the exact same signature so FastMCP can parse it.
    src = (
        f"def {fn_name}({param_str}):\n"
        f"    return _handler({call_str})\n"
    )
    # Use the handler's own globals so annotations like ``Any``,
    # ``datetime``, ``dict``, etc. resolve in the exec'd namespace.
    ns: dict[str, Any] = {**handler.__globals__, "_handler": handler}
    try:
        exec(src, ns)  # noqa: S102 — controlled input, no user data
    except (SyntaxError, NameError):
        # Fallback: register with no parameters (at least it won't crash).
        def _fallback() -> dict[str, Any]:  # type: ignore[return-value]
            return handler()
        _fallback.__name__ = fn_name
        _fallback.__doc__ = f"Vitrina tool: {public_name}."
        return _fallback

    wrapper = ns[fn_name]
    wrapper.__doc__ = (
        inspect.getdoc(handler)
        or f"Coordinator-registered Vitrina tool: {public_name}."
    )
    # Copy annotations so FastMCP picks up type info.
    wrapper.__annotations__ = {
        k: v.annotation
        for k, v in params.items()
        if v.annotation is not inspect.Parameter.empty
        and v.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    }
    if "return" in (handler.__annotations__ or {}):
        wrapper.__annotations__["return"] = handler.__annotations__["return"]
    return wrapper


def _register_coordinator_tools() -> None:
    """Expose coordinator-registered tools from ``tools.HANDLERS``.

    FastMCP registers the legacy tools above from explicit named functions.
    The larger HANDLERS table has ~25 additional dotted-name tools (UCP /
    chat / replan / basket / etc.) that would require hand-written stubs.
    We generate properly-typed wrapper functions at import time instead.
    """
    legacy_names = {
        "list_demo_shops",
        "list_products_in_shop",
        "search_across_shops",
        "report_pos_snapshot",
        "get_live_pos_snapshot",
        "record_leo_sale",
        "shop_acknowledge_sales",
        "list_shop_notifications",
        "get_mcp_audit_trail",
        "create_checkout_session",
        "get_checkout_session",
        "update_checkout_session",
        "complete_checkout_session",
        "cancel_checkout_session",
        "reset_demo_stock",
    }

    for public_name, handler in tools.HANDLERS.items():
        if public_name in legacy_names:
            continue

        wrapper = _make_typed_wrapper(public_name, handler)
        try:
            mcp.tool(name=public_name)(wrapper)
        except Exception as exc:  # noqa: BLE001
            logger.warning("_register_coordinator_tools: skipped %s: %s", public_name, exc)


_register_coordinator_tools()


def _make_http_app(host: str, port: int):  # type: ignore[return]
    """Build a Starlette app wrapping the FastMCP streamable-HTTP transport.

    Adds two things on top of stock FastMCP:

    1. ``GET /health`` — returns ``{"ok": true}`` so the load-balancer / Caddy
       probe has something to check without speaking JSON-RPC.
    2. Bearer-token middleware — when ``VITRINA_MCP_BEARER_TOKEN`` is set,
       every request to ``/mcp`` must include ``Authorization: Bearer <token>``.
       stdio transport is always auth-free (used by local Claude Desktop /
       Cursor installs).  External callers (ChatGPT custom GPTs, OpenClaw,
       the hosted Hermes agent) use the env-configured token.
    """
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    bearer_token = (os.getenv("VITRINA_MCP_BEARER_TOKEN") or "").strip()

    # FastMCP 2.x exposes the streamable-HTTP ASGI app via http_app(); the
    # nested app's lifespan MUST be propagated to the outer Starlette app
    # or the FastMCP session manager never initialises.
    #
    # Path ownership: FastMCP itself routes `/mcp` and `/mcp/` correctly
    # (its internal router treats them as the same endpoint and does NOT
    # 307-redirect). We therefore mount FastMCP at the *root* and let it
    # own `/mcp`, instead of wrapping it in a Starlette Mount("/mcp/...")
    # which strips the prefix and confuses FastMCP's internal matcher
    # (POST /mcp/ → 404). This is the pattern documented in FastMCP 2.x.
    mcp_asgi = mcp.http_app(path="/mcp")

    class BearerAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):  # type: ignore[override]
            if not bearer_token:
                return await call_next(request)
            # Health probe is exempt — it carries no user data.
            if request.url.path == "/health":
                return await call_next(request)
            auth = request.headers.get("authorization", "")
            if not auth.lower().startswith("bearer "):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if auth.split(" ", 1)[1].strip() != bearer_token:
                return JSONResponse({"error": "Forbidden"}, status_code=403)
            return await call_next(request)

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "service": "vitrina-mcp"})

    # Mount FastMCP at the ROOT — it already owns `/mcp` internally (see
    # `mcp_asgi = mcp.http_app(path="/mcp")` above). Earlier we tried
    # Mount("/mcp/", ...) + Mount("/mcp", ...) but Starlette's Mount strips
    # the prefix, leaving FastMCP's inner router with a path of "/" which
    # it does not match → POST /mcp/ → 404 ("Session terminated" upstream).
    app = Starlette(
        routes=[
            Route("/health", endpoint=health, methods=["GET"]),
            Mount("/", app=mcp_asgi),
        ],
        middleware=[Middleware(BearerAuthMiddleware)],
        lifespan=mcp_asgi.lifespan,
    )
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Vitrina MCP server")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve over HTTP (streamable-http transport) instead of stdio.",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.http:
        import uvicorn
        logger.info(
            "Starting Vitrina MCP on http://%s:%d  (bearer auth: %s)",
            args.host,
            args.port,
            "enabled" if os.getenv("VITRINA_MCP_BEARER_TOKEN") else "disabled",
        )
        uvicorn.run(
            _make_http_app(args.host, args.port),
            host=args.host,
            port=args.port,
        )
    else:
        logger.info("Starting Vitrina MCP on stdio")
        mcp.run()


if __name__ == "__main__":
    main()
