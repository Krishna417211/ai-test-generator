/** Display helpers for values that come off the API in raw form. */

/** Pluralise a noun against a count: pluralize(1, "call") → "1 call". */
export function pluralize(count: number, singular: string, plural?: string): string {
  return `${count} ${count === 1 ? singular : plural ?? singular + "s"}`;
}

/** Byte count → human size. Small files must not collapse to "0.0 MB". */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** Token count → compact label. Under 1k stays exact rather than "~0k". */
/** The API returns the writer agent's internal prompt-template key
 *  ("playwright_js"), which is not a name to show a user. */
const FRAMEWORK_LABELS: Record<string, string> = {
  playwright_js: "Playwright",
  playwright_python: "Playwright · Python",
  cypress_js: "Cypress",
  selenium_java: "Selenium · Java",
  selenium_python: "Selenium · Python",
};

export function frameworkLabel(key: string): string {
  if (FRAMEWORK_LABELS[key]) return FRAMEWORK_LABELS[key];
  // Unknown key — degrade to something readable rather than leaking the raw id.
  const [name] = key.split("_");
  return name ? name.charAt(0).toUpperCase() + name.slice(1) : key;
}

/** Epoch seconds → a plain calendar date. */
export function formatDate(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleDateString(undefined, {
    year: "numeric", month: "short", day: "numeric",
  });
}

/** Epoch seconds → "3 hours ago", falling back to a date once that stops being
 *  a useful way to say when. */
export function formatWhen(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "—";
  const mins = Math.round((Date.now() - epochSeconds * 1000) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${pluralize(mins, "min")} ago`;
  if (mins < 60 * 24) return `${pluralize(Math.round(mins / 60), "hour")} ago`;
  if (mins < 60 * 24 * 7) return `${pluralize(Math.round(mins / 1440), "day")} ago`;
  return formatDate(epochSeconds);
}

/** A repo URL is long and mostly boilerplate — show the part that identifies it. */
export function shortSource(source: string): string {
  if (!source) return "ZIP upload";
  const m = source.match(/github\.com\/([^/]+\/[^/?#]+)/i);
  return m ? m[1] : source.replace(/^https?:\/\//, "");
}

/** Seconds of uptime → "3d 4h" / "12m". Coarse on purpose: nobody reads an
 *  uptime to the second, and a ticking value would redraw on every poll. */
export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

/** The API stores the language key the generator used ("typescript"), which is
 *  a lowercase identifier, not a name. Mirrors frameworkLabel. */
const LANGUAGE_LABELS: Record<string, string> = {
  typescript: "TypeScript",
  javascript: "JavaScript",
  python: "Python",
  java: "Java",
};

export function languageLabel(key: string): string {
  if (LANGUAGE_LABELS[key]) return LANGUAGE_LABELS[key];
  return key ? key.charAt(0).toUpperCase() + key.slice(1) : key;
}
