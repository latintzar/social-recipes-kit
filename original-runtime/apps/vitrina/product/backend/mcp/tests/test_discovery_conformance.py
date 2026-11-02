"""PR 12 discovery + conformance smoke tests."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[6]
MCP_DIR = ROOT / "apps/vitrina/product/backend/mcp"
BOOTSTRAP_DIR = ROOT / "scripts/bootstrap"
DISCOVERY_ROUTE = ROOT / "apps/vitrina/product/backend/api/routes/discovery.py"
for p in (MCP_DIR, BOOTSTRAP_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import gen_wellknown  # noqa: E402
import tools  # noqa: E402

spec = importlib.util.spec_from_file_location("_vitrina_api_discovery", DISCOVERY_ROUTE)
assert spec is not None and spec.loader is not None
discovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discovery)


def test_all_tool_schemas_have_handlers():
    missing = [t["name"] for t in tools.TOOLS if t["name"] not in tools.HANDLERS]
    assert missing == []


def test_acp_manifest_advertises_checkout_and_feed():
    manifest = gen_wellknown.build_acp_manifest()
    assert manifest["protocol"]["name"] == "acp"
    assert manifest["protocol"]["version"] == "2026-04-17"
    assert manifest["capabilities"]["fulfillment_types"] == ["pickup"]
    assert manifest["endpoints"]["checkout_sessions"].endswith("/checkout_sessions")
    assert manifest["endpoints"]["product_feed"].endswith("/acp/feed-products.json")


def test_ucp_manifest_advertises_cart_tools():
    manifest = gen_wellknown.build_ucp_manifest()
    assert manifest["protocol"]["capability"] == "dev.ucp.shopping.cart"
    assert "dev.ucp.shopping.cart.create" in manifest["tools"]
    assert "dev.ucp.shopping.cart.substitute_in_recipe" in manifest["tools"]
    assert manifest["anonymous_auth"]["header"] == "X-Vitrina-Cart-Key"


def test_mcp_manifest_contains_pr9_tools():
    manifest = gen_wellknown.build_mcp_manifest()
    names = {tool["name"] for tool in manifest["tools"]}
    assert "complete_checkout_group" in names
    assert "replan.find_alternatives" in names
    assert "agent_errors.create" in names


def test_product_feed_falls_back_to_static_catalogue(monkeypatch):
    monkeypatch.setattr(discovery, "_product_feed_from_supabase", lambda: (_ for _ in ()).throw(RuntimeError("no sb")))
    feed = asyncio.run(discovery.product_feed())
    assert feed["schema"] == "dev.acp.product-feed"
    assert feed["source"] == "static-demo"
    assert feed["count"] == len(feed["products"])
    assert feed["products"]
