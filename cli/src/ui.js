/**
 * ui.js — Terminal output.
 *
 * Colour is opt-out via NO_COLOR and auto-disabled when stdout isn't a TTY, so
 * piping into a file or a CI log yields plain text rather than escape codes.
 * That matters more than usual here: the publish output is the record of what
 * landed in someone's repository, and people paste it into issues.
 */

const useColor =
  process.stdout.isTTY && !process.env.NO_COLOR && process.env.TERM !== "dumb";

// The ESC byte is spelled as an escape, not embedded raw: an invisible control
// character in source is unreviewable and does not survive every editor.
const ESC = "\u001b";
const wrap = (code) => (s) => (useColor ? `${ESC}[${code}m${s}${ESC}[0m` : String(s));

export const c = {
  dim: wrap(2),
  bold: wrap(1),
  red: wrap(31),
  green: wrap(32),
  yellow: wrap(33),
  cyan: wrap(36),
  grey: wrap(90),
};

export function info(msg) {
  process.stdout.write(`${msg}\n`);
}

export function step(state, label, detail = "") {
  const mark =
    state === "done" ? c.green("✓") : state === "skipped" ? c.yellow("–") : c.cyan("•");
  const tail = detail ? ` ${c.grey(detail)}` : "";
  process.stdout.write(`  ${mark} ${label}${tail}\n`);
}

export function warn(msg) {
  process.stderr.write(`${c.yellow("!")} ${msg}\n`);
}

export function fail(msg) {
  process.stderr.write(`${c.red("✗")} ${msg}\n`);
}

/** A 0–100 score with its grade, coloured by band — amber never red, matching
 *  the web UI: a middling score means "read this", not "this is broken". */
export function score(value, grade) {
  const paint = value >= 90 ? c.green : value >= 70 ? c.bold : c.yellow;
  return `${paint(`${value}/100`)} ${c.grey(`(${grade})`)}`;
}

export function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}
