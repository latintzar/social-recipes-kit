# recipe-kit

Turn a TikTok / Instagram / YouTube cooking video into a **recipe you can use as
data** (ingredients, steps, quantities) — plus a copy of the **video that
actually plays in your own app**. It even works on videos that have no caption
and no subtitles, because it *watches* the video instead of reading the text.

This is real, battle-tested code pulled out of a live app. You don't have to use
all of it.

---

## Pick only the parts you want

recipe-kit is **three separate pieces**. Use one, two, or all three — they don't
depend on each other (except #3, which uses #1 under the hood). Mix and match
based on what you're building.

### 🥄 Part 1 — Get a recipe out of a video
**What it does:** you give it a video link (TikTok, Instagram reel, YouTube,
etc.), and it gives you back the recipe as structured data — title, ingredients
with quantities, step-by-step directions — *and* downloads the video file so you
can show it later.
**Use this if:** you want to save/parse recipes from social videos. This is the
core. Most people want at least this.
**You need:** one API key for an AI model (a few minutes to get — see *Start
here* below). The code is Python, but you can run it as a little web service and
call it from an app in **any** language.

### ▶️ Part 2 — Play the saved video in a React app
**What it does:** a ready-made React component that plays the video copy from
Part 1 smoothly. (Embedding TikTok/Instagram directly is unreliable — videos
won't autoplay, or the wrong clip shows up. This avoids all that.)
**Use this if:** your app is built with React (or React Native) and you want the
video to just work.
**You need:** a React app. Nothing else. You can even use this on its own if you
already have video URLs from somewhere.

### 📩 Part 3 — Let people send you a reel on Instagram
**What it does:** connects to Instagram so that when someone **shares a reel into
your Instagram DMs**, it automatically runs Part 1 and (optionally) replies to
them. No copy-pasting links.
**Use this if:** you want a social "DM us a recipe" feature.
**You need:** an Instagram/Meta developer account and a bit of one-time setup
(this part is the most involved — see [`INSTAGRAM_SETUP.md`](INSTAGRAM_SETUP.md)).
**This part is optional and off by default** — if you skip it, Parts 1 and 2
work completely on their own.

| | Part 1 (extract) | Part 2 (player) | Part 3 (Instagram) |
|---|---|---|---|
| **Language** | Python | React / TypeScript | Python |
| **Needs** | 1 AI API key | a React app | a Meta app (fiddly) |
| **Works alone?** | ✅ yes | ✅ yes | builds on Part 1 |

> **You use Claude Code?** Easiest path: open this repo in Claude Code and say
> *"set up Part 1 of recipe-kit for me."* Everything here is written so your
> agent can follow it. The steps below are the same ones it'll do.

---

## Start here (Part 1, the 5-minute version)

You only do **two** manual things: install one program, and get one key.

**1. Install ffmpeg** (this is the only thing pip can't install for you — it's
the tool that reads the video). Pick your system:

- **Mac:** `brew install ffmpeg`
- **Ubuntu / Debian Linux:** `sudo apt install ffmpeg`
- **Windows:** download from <https://ffmpeg.org/download.html> (or `winget install ffmpeg`)

**2. Get an AI API key.** Go to <https://openrouter.ai/keys>, sign in, and create
a key. It starts with `sk-or-`. (OpenRouter lets you use lots of AI models with
one key. You can swap in OpenAI or others later — see the package README.)

**3. Install and run it:**

```bash
# from inside this repo
cd packages/recipe-extractor
pip install -e .

# tell it your key (paste your real key after the =)
export OPENROUTER_API_KEY=sk-or-...

# try it on any cooking video
recipe-extractor "https://www.tiktok.com/@user/video/123"
```

You'll see the recipe printed out, and the video saved into a `recipe_output/`
folder. That's it — Part 1 works.

**To call it from your own app instead**, run it as a tiny web service:

```bash
pip install -e ".[service]"
recipe-extractor-serve         # now running at http://127.0.0.1:8000
```

Then from anywhere: `POST http://127.0.0.1:8000/extract` with `{"url": "..."}`
gives you the recipe as JSON, and `GET /videos/<id>.mp4` is the playable video.

Full Part 1 options (changing the AI model, using your own storage, etc.) are in
the [recipe-extractor README](packages/recipe-extractor/README.md).

## Adding Part 2 (the React player)

```bash
npm install recipe-video-player
```

```tsx
import { RecipeVideoPlayer } from 'recipe-video-player';

<RecipeVideoPlayer
  source="TikTok"
  sourceUrl={recipe.source_url}
  sourceId={recipe.source_id}
  videoUrl={`${API}/videos/${recipe.id}.mp4`}  // the saved video from Part 1
  posterUrl={posterUrl}
/>
```

Details and styling: [recipe-video-player README](packages/recipe-video-player/README.md).

## Adding Part 3 (Instagram DM shares)

This one needs a Meta developer app and some dashboard clicking. It's all in a
dedicated, step-by-step guide: **[`INSTAGRAM_SETUP.md`](INSTAGRAM_SETUP.md)**.
Once set up, wiring it in is a few lines (see the
[package README](packages/recipe-extractor/README.md#the-instagram-messages-api-flow-share-a-reel--recipe)).

---

## How it actually works (for the curious)

You don't need this section to use the kit — but here's why it's good.

### It reads the video, not the caption
Most recipe scrapers read the text description. Half of cooking videos have a
useless caption ("link in bio 🔥") and no subtitles. So instead this *watches*
the video: `ffmpeg` grabs the frames at the visually important moments (the cut
where the tray comes out of the oven, the on-screen ingredient list, the final
plated dish), and an AI vision model reads the cooking from those frames. No
caption needed. Each ingredient comes back cleanly split into a plain name
("tomato"), a prep note ("diced"), and a gram estimate — handy whether or not
you do anything fancy with it.

### It saves the video so it actually plays
Embedding TikTok/Instagram directly is hostile: iPhones block autoplay, TikTok
sometimes shows a *random* clip instead of the one you wanted, Instagram forces a
tap-through. So the kit downloads the video once and serves it from your side,
and the React player just plays a normal video file — which works everywhere.

### The Instagram "share a reel" trick (the clever bit)
When someone shares a reel into your Instagram DMs, Instagram does **not** hand
you a normal link. Figuring out how to turn that DM into a downloadable video
took real work — two non-obvious findings make it possible, both baked into
[`instagram_cdn.py`](packages/recipe-extractor/src/recipe_extractor/instagram_cdn.py)
and [`instagram_webhook.py`](packages/recipe-extractor/src/recipe_extractor/instagram_webhook.py).
The whole connection (verification, security check, finding the reel, replying)
is turnkey via one function, `make_router`.

---

## What's in the box

```
recipe-kit/
├── packages/
│   ├── recipe-extractor/      Part 1 + Part 3 (Python)
│   └── recipe-video-player/   Part 2 (React/TypeScript)
├── examples/extract_one.py    a tiny runnable demo
├── INSTAGRAM_SETUP.md         step-by-step Meta setup for Part 3
└── README.md                  you are here
```

## License

MIT — use it however you like. See [LICENSE](LICENSE).
