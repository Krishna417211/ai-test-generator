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
export function formatTokens(tokens: number): string {
  if (tokens < 1000) return `${tokens}`;
  if (tokens < 10_000) return `~${(tokens / 1000).toFixed(1)}k`;
  return `~${Math.round(tokens / 1000)}k`;
}

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
