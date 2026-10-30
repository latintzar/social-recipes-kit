# Social Recipes

Extract recipes and video from social Reels

Social Recipes is a toolkit for turning cooking videos into structured recipes and playable video files. Give it a TikTok, Instagram, YouTube or Facebook video URL; the extractor downloads the video, samples frames and asks a vision model for ingredients and directions. The saved video can be played in your own app.

[Website](https://atelierneyra.com/social-recipes) · [Documentation](https://docs.atelierneyra.com/social-recipes/overview/) · [MIT license](LICENSE)

![Social Recipes](https://atelierneyra.com/assets/product-shots-20261030/social-recipes.jpg)

## Three modules

- **Recipe extractor:** Python CLI and optional HTTP service for downloading a video, extracting a recipe and saving its MP4. [Extractor documentation](packages/recipe-extractor/README.md)
- **Video player:** a React component that plays a saved MP4 and can fall back to a platform embed. [Player documentation](packages/recipe-video-player/README.md)
- **Instagram sharing:** webhook helpers and an optional FastAPI router for receiving a Reel shared by DM, running extraction and optionally replying. [Instagram setup](INSTAGRAM_SETUP.md)

The extractor and player can be used independently; Instagram sharing calls the extractor. Vitrina Local builds on this toolkit for its grocery workflow.

## How it works

Public social video URLs go through `yt-dlp`. `ffmpeg` samples frames using scene changes, and an OpenAI-compatible vision endpoint turns those frames into structured recipe data. The extractor stores the MP4 and can store a thumbnail, giving an app its own playback source. Results include the recipe, media paths, extraction route and processing status.

For Instagram DM shares, the original implementation investigated the message and media responses and handles several forms. It looks for a Reel media ID and tries the Graph media node for a permalink; it can expand a message ID into `shares`, requesting `link` as well as `url`; and it accepts an `ig_messaging_cdn` lookaside attachment as a direct MP4 source. The code streams that MP4 or uses a resolved public permalink. Signed `cdninstagram.com` and `fbcdn.net` URLs are treated separately because they can be short-lived and session-bound. See [`instagram_cdn.py`](packages/recipe-extractor/src/recipe_extractor/instagram_cdn.py), [`instagram_webhook.py`](packages/recipe-extractor/src/recipe_extractor/instagram_webhook.py) and [`extract.py`](packages/recipe-extractor/src/recipe_extractor/extract.py).

Platform response formats, permissions, signed URLs and download behavior change. These routes depend on the source platform, account access and current `yt-dlp` support; validate them with your own links and Meta app. The repository contains implementation and setup instructions, rather than a current cross-platform test suite. Extracted ingredients and quantities should be reviewed before use.

## Status and next steps

This is a reusable source kit with a CLI, a service, a React player and Instagram webhook integration. A vision API key and `ffmpeg` are needed for recipe extraction; Instagram DM sharing also needs a configured Meta app and account. Next, run the extractor with a video you can access, review its output, and connect the player or webhook to your application as needed.

## Install and extract

Install Python 3.10+, `ffmpeg` and an OpenAI-compatible vision API key. The default endpoint is OpenRouter.

```bash
cd packages/recipe-extractor
pip install -e .
export OPENROUTER_API_KEY=your_key
recipe-extractor "https://www.tiktok.com/@user/video/123"
```

The CLI prints JSON and saves artifacts under `recipe_output/`. For the HTTP service, install `pip install -e ".[service]"` and run `recipe-extractor-serve`; it exposes `POST /extract` and `GET /videos/<id>.mp4`. See the [extractor guide](packages/recipe-extractor/README.md) for configuration, model selection and storage options.

The [React player guide](packages/recipe-video-player/README.md) covers integration and styling. The [Instagram guide](INSTAGRAM_SETUP.md) covers Meta setup, webhook verification and credentials. Set `INSTAGRAM_APP_SECRET` when deploying the webhook so it verifies incoming signatures.

## Source

```text
packages/recipe-extractor/      Python extraction, service and Instagram integration
packages/recipe-video-player/    React/TypeScript player
examples/extract_one.py         CLI example
INSTAGRAM_SETUP.md              Meta app and webhook setup
```

[MIT](LICENSE)
