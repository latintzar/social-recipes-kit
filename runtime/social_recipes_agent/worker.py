"""HTTP transport around the recovered, original Instagram turn adapter."""

from __future__ import annotations

import argparse
import os

from fastapi import FastAPI

from .config import ConfigurationError, RuntimeConfig


def create_app() -> FastAPI:
    config = RuntimeConfig.from_environment()
    if os.getenv("SOCIAL_RECIPES_RUNTIME") != "1" or os.getenv("HERMES_HOME") != str(
        config.hermes
    ):
        raise ConfigurationError(
            "Use social-recipes-agent serve to initialize isolated project configuration"
        )
    from platforms import instagram_dm

    app = FastAPI(
        title="Social Recipes", docs_url=None, redoc_url=None, openapi_url=None
    )
    app.add_api_route(
        "/v1/instagram/webhook", instagram_dm.handle_verify, methods=["GET"]
    )
    app.add_api_route(
        "/v1/instagram/webhook", instagram_dm.handle_webhook, methods=["POST"]
    )

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ready", "profile": "recipe"}

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    from agent_factory import (
        _load_ai_agent,
        _prepare_recipe_tools,
        ensure_hermes_config,
    )

    ensure_hermes_config()
    _load_ai_agent()  # Import/API dependency failure is a startup failure, not a fake reply.
    _prepare_recipe_tools()
    import uvicorn

    try:
        uvicorn.run(create_app(), host=args.host, port=args.port, access_log=False)
    finally:
        from tools.mcp_tool_lifecycle import shutdown_mcp_servers

        shutdown_mcp_servers(names={"social_recipes"})


if __name__ == "__main__":
    main()
