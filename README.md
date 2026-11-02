# Social Recipes

An Instagram agent runtime that turns Reels into recipes

Social Recipes is an Instagram agent runtime that resolves a shared cooking Reel into video, ingredients and instructions. The reusable source contains the inbox adapter, Instagram/CDN resolver, vision-model extractor and React video player. Use the extraction layer on its own or connect it to an Instagram conversation.

[Website](https://atelierneyra.com/social-recipes) · [Product tour](docs/product-tour.md) · [Documentation](https://docs.atelierneyra.com/social-recipes/overview/) · [First extraction](docs/AGENT-SETUP.md)

![Social Recipes](https://atelierneyra.com/assets/product-motion-v4/social-recipes.jpg)

The website illustrates the Instagram conversation. The published kit supplies its reusable components; you connect your Meta app, vision provider and recipe reply callback. The separate original agent runtime is being reconciled with these components.

## One Reel to a recipe

1. Supply a cooking-video URL you can access, through the CLI or a shared Instagram Reel.
2. Resolve and download its media. The pipeline uses public permalinks, Graph share responses or Instagram’s messaging CDN attachment where available.
3. Sample frames with FFmpeg and extract a structured recipe using your configured vision model.
4. Review the recipe and saved MP4. Display them in your app or pass them to an Instagram reply callback.

The [product tour](docs/product-tour.md) follows the result through its exact fields, inbox handoff and player. [Vitrina Local](https://github.com/latintzar/vitrina-local) builds the local-stock and basket workflow above this recipe layer.

## Three modules

| Module | Action and result | Guide |
| --- | --- | --- |
| Recipe extractor | Video URL → structured recipe, MP4, thumbnail and extraction status | [Extractor](packages/recipe-extractor/README.md) |
| Instagram inbox | Signed Reel event → resolved media → recipe callback and optional reply | [Instagram setup](INSTAGRAM_SETUP.md) |
| Video player | Saved MP4 → native React playback, with platform-embed fallback | [Player](packages/recipe-video-player/README.md) |

`on_reel(sender_id, result)` receives the full extraction result and can return your recipe reply. The built-in `auto_reply=True` sends a saved-title confirmation. [Webhook source](packages/recipe-extractor/src/recipe_extractor/instagram_webhook.py).

## Run and adapt

Start with the [agent setup guide](docs/AGENT-SETUP.md): Python 3.10+, FFmpeg and your own OpenAI-compatible vision account. It covers private configuration, the first CLI result and connecting a professional Instagram account.

| Source area | Contents |
| --- | --- |
| [extract.py](packages/recipe-extractor/src/recipe_extractor/extract.py) | Download, frame selection, model request and result/artifact assembly |
| [instagram_cdn.py](packages/recipe-extractor/src/recipe_extractor/instagram_cdn.py) | Graph media/message lookup and Instagram share/CDN resolution |
| [instagram_webhook.py](packages/recipe-extractor/src/recipe_extractor/instagram_webhook.py) | Challenge verification, signed events, callback and replies |
| [service.py](packages/recipe-extractor/src/recipe_extractor/service.py) | Optional extraction/synthesis HTTP service and saved-video routes |
| [recipe-video-player](packages/recipe-video-player) | Reusable React playback component |

Platform access, current response formats and vision accuracy are connection checks with your own video and accounts. Review extracted quantities before using them. The [pipeline guide](docs/pipeline.md) preserves the original CDN investigation and route details; the [extractor reference](packages/recipe-extractor/README.md) covers configuration, storage and deployment.

Original code is [MIT licensed](LICENSE). Dependencies, source videos and connected platforms retain their own terms.

[made by atelier neyra](https://atelierneyra.com)
