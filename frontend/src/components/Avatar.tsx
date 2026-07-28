import { useEffect, useState } from "react";

/**
 * A user's picture, or their initial. Never a blank square.
 *
 * The bug this exists to end: the old code was `avatar_url ? <img> : <initial>`,
 * duplicated in three places. That is wrong whenever the URL is present but the
 * image does not load — and on this deployment that is the *common* case, not an
 * edge one. The Caddy CSP allows `img-src 'self' data:`, while Google avatars
 * live on lh3.googleusercontent.com and GitHub's on avatars.githubusercontent.com,
 * so every OAuth user's avatar was blocked. `avatar_url` was truthy, the <img>
 * rendered, the browser refused to load it, and the fallback never ran: a blank
 * block with no way to reach the initial behind it.
 *
 * So the fallback is driven by whether the image actually *loaded*, not by
 * whether a URL exists. That covers the CSP case, a deleted GitHub avatar, an
 * offline user, and whatever the next cause turns out to be.
 */

interface Props {
  name?: string | null;
  email?: string | null;
  src?: string | null;
  /** Tailwind size classes — the caller owns the dimensions. */
  className?: string;
  /** Font size for the initial; should scale with `className`. */
  textClassName?: string;
  rounded?: string;
}

/** First letter of the name, else of the email, else "?". */
export function initialFor(name?: string | null, email?: string | null): string {
  const source = (name || email || "").trim();
  // `charAt` splits surrogate pairs, so an emoji or a non-BMP character would
  // render as half a glyph. Array spread iterates by code point.
  const first = [...source][0];
  return (first || "?").toUpperCase();
}

export default function Avatar({
  name, email, src,
  className = "w-10 h-10",
  textClassName = "text-sm",
  rounded = "rounded-xl",
}: Props) {
  const [failed, setFailed] = useState(false);

  // A new src deserves a fresh attempt — otherwise uploading a picture after a
  // failed one leaves the initial showing until a reload.
  useEffect(() => { setFailed(false); }, [src]);

  const initial = initialFor(name, email);

  if (!src || failed) {
    return (
      <span
        aria-hidden="true"
        className={`${className} ${rounded} bg-brand-gradient flex items-center justify-center font-bold text-ink-950 shrink-0 ${textClassName}`}
      >
        {initial}
      </span>
    );
  }

  return (
    <img
      src={src}
      alt=""
      onError={() => setFailed(true)}
      className={`${className} ${rounded} border border-grey-700 object-cover shrink-0`}
    />
  );
}
