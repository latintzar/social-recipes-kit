import { useState } from 'react';

export type RecipeVideoSource = 'TikTok' | 'Instagram' | 'YouTube' | 'Facebook' | 'Web';

export interface RecipeVideoPlayerProps {
  /** Which platform the clip came from — drives the iframe fallback + aspect ratio. */
  source: RecipeVideoSource;
  /** The original public URL (used for the iframe fallback and the "open original" link). */
  sourceUrl: string;
  /** Platform id / shortcode (TikTok video id, YouTube id, etc). */
  sourceId: string;
  /**
   * Absolute URL to YOUR self-hosted MP4 (the one `recipe-extractor` persisted),
   * e.g. `https://your-host/videos/tiktok-123.mp4`. When present, we render a
   * native <video> — this is the whole point: it autoplays everywhere and never
   * serves the wrong clip. Pass null/undefined to force the iframe fallback.
   */
  videoUrl?: string | null;
  /** Optional poster image shown before the video paints. */
  posterUrl?: string | null;
  /** Extra class on the outer wrapper, for your own styling. */
  className?: string;
}

function instagramShortcode(sourceUrl: string, fallback: string): string {
  const match = sourceUrl.match(/instagram\.com\/(?:p|reel|reels|tv)\/([^/?#]+)/i);
  return match?.[1] ?? fallback;
}

/**
 * Recipe video player. Two paths, in priority order:
 *
 *   1. **Self-hosted MP4** (`videoUrl`) — a plain
 *      `<video autoplay muted playsinline loop>`. This sidesteps every iframe
 *      failure mode:
 *        - iOS Safari forbids cross-origin iframe autoplay regardless of
 *          `allow="autoplay"` and `muted=1`;
 *        - TikTok's `/embed/v2/{id}` silently swaps in a random "For You" clip
 *          when the original can't autoplay;
 *        - Instagram's `/p/{shortcode}/embed/` disables autoplay entirely and
 *          forces a tap-through to instagram.com.
 *      A native `<video>` autoplays on every modern browser as long as it's
 *      muted, has no platform overlays, and never serves the wrong clip.
 *
 *   2. **Platform iframe fallback** — when you don't (yet) have a local MP4.
 *      `key={src}` forces an unmount so audio stops when the element is removed.
 */
export function RecipeVideoPlayer({
  source,
  sourceUrl,
  sourceId,
  videoUrl,
  posterUrl,
  className,
}: RecipeVideoPlayerProps) {
  const isPortrait = source === 'TikTok' || source === 'Instagram';
  const wrapperClass = ['recipe-video-player', className].filter(Boolean).join(' ');

  // Track a failed self-hosted URL so we don't re-render the broken <video>
  // on every state change — if the MP4 404s, fall through to the iframe.
  const [localFailed, setLocalFailed] = useState(false);

  if (videoUrl && !localFailed) {
    return (
      <div className={wrapperClass}>
        <video
          key={videoUrl}
          className={`recipe-video-player__media ${isPortrait ? 'is-portrait' : 'is-landscape'}`}
          src={videoUrl}
          poster={posterUrl ?? undefined}
          autoPlay
          muted
          loop
          playsInline
          controls
          preload="metadata"
          onError={() => setLocalFailed(true)}
        />
      </div>
    );
  }

  let iframeSrc: string | null = null;
  if (source === 'TikTok') {
    iframeSrc = `https://www.tiktok.com/embed/v2/${encodeURIComponent(sourceId)}?autoplay=1&muted=1`;
  } else if (source === 'Instagram') {
    const shortcode = instagramShortcode(sourceUrl, sourceId);
    iframeSrc = `https://www.instagram.com/p/${encodeURIComponent(shortcode)}/embed/`;
  } else if (source === 'YouTube') {
    iframeSrc = `https://www.youtube.com/embed/${encodeURIComponent(sourceId)}?autoplay=1&mute=1&playsinline=1`;
  }

  return (
    <div className={wrapperClass}>
      {posterUrl ? (
        <div className="recipe-video-player__poster" aria-hidden="true">
          <img src={posterUrl} alt="" loading="eager" />
        </div>
      ) : null}
      {iframeSrc ? (
        <iframe
          key={iframeSrc}
          className={`recipe-video-player__media ${isPortrait ? 'is-portrait' : 'is-landscape'}`}
          src={iframeSrc}
          title={`${source} recipe video`}
          allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share; fullscreen"
          allowFullScreen
        />
      ) : (
        <a
          className="recipe-video-player__link"
          href={sourceUrl}
          target="_blank"
          rel="noreferrer"
        >
          Open original on {source}
        </a>
      )}
    </div>
  );
}
