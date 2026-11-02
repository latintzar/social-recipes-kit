"""Recipe-only MCP binding to the existing published extraction pipeline.

Original IG transport, queue, session history and Hermes callbacks remain in
original-runtime/. Commerce tools remain a separately documented source profile.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse


def validate_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Use an HTTPS Instagram Reel or Meta Messaging CDN URL")
    public = host in {"instagram.com", "www.instagram.com"} and parsed.path.startswith(
        ("/reel/", "/reels/", "/p/")
    )
    cdn = (
        host == "lookaside.fbsbx.com" and parsed.path.rstrip("/") == "/ig_messaging_cdn"
    )
    if not (public or cdn):
        raise ValueError(
            "Use an Instagram Reel permalink or its Meta Messaging CDN share URL"
        )
    return url


def extract_from_url(url: str) -> dict:
    from recipe_extractor.extract import extract_recipe

    return extract_recipe(validate_url(url), cookies_from_browser="", persist=True)


def main() -> None:
    from fastmcp import FastMCP

    server = FastMCP("Social Recipes")

    @server.tool(name="recipes_extract_from_url")
    def recipes_extract_from_url(url: str) -> str:
        """Extract title, ingredients and steps from an Instagram Reel shared in this conversation."""
        return json.dumps(extract_from_url(url), ensure_ascii=False)

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
