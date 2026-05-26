"""recipe-extractor — turn a social cooking video into a structured recipe.

    from recipe_extractor import extract_recipe
    result = extract_recipe("https://www.tiktok.com/@user/video/123")
    print(result["recipe"]["title"])
    print(result["media"]["video_path"])   # locally-pinned MP4 for native playback

See README.md for the full method (incl. the Instagram-DM CDN crack).
"""

from __future__ import annotations

from .extract import (
    extract_recipe,
    process_video,
    feed_row_from_url,
    download_video,
    extract_frames,
    call_llm,
    persist_video,
)
from .prompts import (
    RECIPE_JSON_SCHEMA_BLOCK,
    vision_extraction_prompt,
    synthesis_system_prompt,
    synthesis_user_prompt,
)
from .synthesize import synthesize_recipe_from_brief
from .instagram_cdn import (
    resolve_ig_reel_video_id_to_permalink,
    resolve_ig_share_url_from_message_mid,
    public_instagram_url_from_message_graph_response,
)

__all__ = [
    "extract_recipe",
    "process_video",
    "feed_row_from_url",
    "download_video",
    "extract_frames",
    "call_llm",
    "persist_video",
    "synthesize_recipe_from_brief",
    "RECIPE_JSON_SCHEMA_BLOCK",
    "vision_extraction_prompt",
    "synthesis_system_prompt",
    "synthesis_user_prompt",
    "resolve_ig_reel_video_id_to_permalink",
    "resolve_ig_share_url_from_message_mid",
    "public_instagram_url_from_message_graph_response",
]

__version__ = "0.1.0"
