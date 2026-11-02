# Social Recipes product tour

Social Recipes is the Instagram recipe-agent layer: shared Reel → resolved video → structured recipe → reply. The kit also exposes the extractor and player for applications with their own intake.

[Website](https://atelierneyra.com/social-recipes) · [First extraction and account setup](AGENT-SETUP.md)

## One video to a usable recipe

Choose a cooking video your account can access. After [private vision configuration](AGENT-SETUP.md#extract-one-recipe), run the extractor against that URL. It downloads the media, samples scene frames with FFmpeg and sends those frames to the selected vision model.

The returned JSON contains:

| Field | What you use it for |
| --- | --- |
| `status` | Decide whether extraction produced a recipe or needs review/retry |
| `recipe` | Read the extracted title, ingredients and directions |
| `source_url`, `source_id` | Keep the result connected to its original video |
| `media` | Locate the saved video, cover and duration for playback |
| `extraction` | Inspect the run directory, model, frame count and download route |

On a processed result, open the saved recipe JSON and review it against the video. Pass the resulting MP4 URL and original source URL to [RecipeVideoPlayer](../packages/recipe-video-player/README.md). The component renders native video when a saved file is available and keeps a platform embed as its fallback. This is the smallest complete extraction-to-playback journey.

The result contract is defined by [process_video and extract_recipe](../packages/recipe-extractor/src/recipe_extractor/extract.py); [extract_one.py](../examples/extract_one.py) is the supplied Python example.

## A Reel shared in Instagram

A connected deployment mounts `make_router` in its FastAPI app and registers its HTTPS callback with Meta. A person shares a Reel by DM:

1. The router verifies the subscription challenge and the signed incoming event.
2. The resolver looks for a Reel/media ID, expands a message share to a public permalink, or accepts a usable Instagram messaging CDN MP4 attachment.
3. The same extractor produces the recipe and media result.
4. Your `on_reel(sender_id, result)` callback saves or formats it and returns text to send to the person.

The built-in `auto_reply=True` is a saved-title confirmation. A full ingredient/instruction reply is the callback’s job. The separate original agent runtime remains under reconciliation; these reusable interfaces are the published handoff points.

[Instagram setup](../INSTAGRAM_SETUP.md) records account configuration. [instagram_webhook.py](../packages/recipe-extractor/src/recipe_extractor/instagram_webhook.py) owns verification and replies; [instagram_cdn.py](../packages/recipe-extractor/src/recipe_extractor/instagram_cdn.py) and [extract.py](../packages/recipe-extractor/src/recipe_extractor/extract.py) own resolution and processing.

## Modules to reuse

| You want to build | Start here |
| --- | --- |
| A recipe extractor in another service | [recipe-extractor](../packages/recipe-extractor/README.md) |
| A video-backed recipe interface | [recipe-video-player](../packages/recipe-video-player/README.md) |
| An Instagram inbox integration | [Webhook router](../packages/recipe-extractor/src/recipe_extractor/instagram_webhook.py), [configuration](AGENT-SETUP.md#connect-an-instagram-inbox) |
| A local-grocer shopping flow | [Vitrina Local](https://github.com/latintzar/vitrina-local), [Instagram Recipe Cart](https://github.com/latintzar/instagram-recipe-cart) |

## Resolution decisions and reached stage

The original work investigated how Instagram shares expose media: Graph media-node permalinks, expanded message `shares` fields and `ig_messaging_cdn` lookaside attachments. Short-lived signed CDN URLs have their own handling. [The pipeline record](pipeline.md) keeps those details available for the next person maintaining the adapter.

The code supplies CLI, service, inbox and player components. Running the connected journey uses your selected Meta and vision accounts and a source video those accounts can access. Validate current permissions and media routes with that account; review the extracted recipe before using it downstream. The website conversation is an illustration of the product’s interaction, while these source interfaces are its reusable release.
