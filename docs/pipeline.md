# Recipe extraction and Instagram resolution



Public social video URLs go through `yt-dlp`. `ffmpeg` samples frames using scene changes, and an OpenAI-compatible vision endpoint turns those frames into structured recipe data. The extractor stores the MP4 and can store a thumbnail, giving an app its own playback source. Results include the recipe, media paths, extraction route and processing status.

For Instagram DM shares, the original implementation investigated the message and media responses and handles several forms. It looks for a Reel media ID and tries the Graph media node for a permalink; it can expand a message ID into `shares`, requesting `link` as well as `url`; and it accepts an `ig_messaging_cdn` lookaside attachment as a direct MP4 source. The code streams that MP4 or uses a resolved public permalink. Signed `cdninstagram.com` and `fbcdn.net` URLs are treated separately because they can be short-lived and session-bound. See [`instagram_cdn.py`](../packages/recipe-extractor/src/recipe_extractor/instagram_cdn.py), [`instagram_webhook.py`](../packages/recipe-extractor/src/recipe_extractor/instagram_webhook.py) and [`extract.py`](../packages/recipe-extractor/src/recipe_extractor/extract.py).

Platform response formats, permissions, signed URLs and download behavior change. These routes depend on the source platform, account access and current `yt-dlp` support; validate them with your own links and Meta app. The repository contains implementation and setup instructions, rather than a current cross-platform test suite. Extracted ingredients and quantities should be reviewed before use.


[Product tour](product-tour.md) · [Private configuration and first extraction](AGENT-SETUP.md) · [Extractor reference](../packages/recipe-extractor/README.md) · [Instagram setup](../INSTAGRAM_SETUP.md)
