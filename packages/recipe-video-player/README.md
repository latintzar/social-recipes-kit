# recipe-video-player

A tiny React component that plays your **self-hosted recipe MP4 natively**, with
an automatic fallback to the TikTok/Instagram/YouTube iframe when you don't have
a local file yet.

> Part of [recipe-kit](../../README.md). Pairs with
> [`recipe-extractor`](../recipe-extractor), which produces the MP4.

## Why not just embed the reel?

Platform embeds fight you:

- **iOS Safari** blocks cross-origin iframe autoplay regardless of `allow`.
- **TikTok's** embed swaps in a random "For You" clip when autoplay is blocked.
- **Instagram's** embed disables autoplay and forces a tap-through.

A plain `<video autoplay muted playsinline loop>` pointed at your own MP4
autoplays everywhere and always shows the right clip. This component does that,
and only falls back to the iframe if the local URL 404s.

## Install

```bash
npm install recipe-video-player
```

`react >= 17` is a peer dependency.

## Use

```tsx
import { RecipeVideoPlayer } from 'recipe-video-player';

<RecipeVideoPlayer
  source="TikTok"                 // 'TikTok' | 'Instagram' | 'YouTube' | 'Facebook' | 'Web'
  sourceUrl={recipe.source_url}   // original public URL (iframe fallback + "open original")
  sourceId={recipe.source_id}     // platform id / shortcode
  videoUrl={`${API}/videos/${recipe.id}.mp4`}  // your self-hosted MP4; null -> iframe
  posterUrl={posterUrl}           // optional
/>
```

When `videoUrl` is present it renders a native `<video>`. Pass `null`/omit it to
force the platform iframe (e.g. while extraction is still queued). If the MP4
fails to load it falls back to the iframe automatically.

## Styling

The component ships no CSS — it only sets class names so you own the look:

- `.recipe-video-player` — outer wrapper (also takes your `className`)
- `.recipe-video-player__media` — the `<video>` / `<iframe>` (gets `.is-portrait`
  or `.is-landscape`)
- `.recipe-video-player__poster` — poster image wrapper
- `.recipe-video-player__link` — the "Open original" fallback link

## Build

```bash
npm run build   # tsc -> dist/
```
