/**
 * secrets.js — Credential detection, run before anything is uploaded.
 *
 * The server already refuses to store or push credential files, and will do so
 * again on whatever this uploads. So why scan here too?
 *
 * Because the server's scan happens *after* the bytes have crossed the network.
 * For the web app that is the earliest possible point. For a CLI it isn't: the
 * files are right here, and a `.env` we never send is categorically different
 * from one that was sent and then discarded. This is the single strongest reason
 * to run the CLI instead of the upload page, so it runs first, always, and
 * cannot be turned off.
 *
 * ## The rules are not in this file
 *
 * `secret-rules.json` holds them, and it is a verbatim copy of
 * `backend/services/secret_rules.json` — the canonical set that
 * `services/secrets_guard.py` also reads. The patterns used to be written out
 * twice, once per language, which meant a rule added to one side silently did
 * not exist on the other.
 *
 * The copy exists only because npm publishes `cli/` alone and cannot reach
 * outside it. Refresh it with `npm run sync-rules`; the backend test
 * `test_secret_rules_sync.py` fails CI if the two files differ, so the copy
 * cannot quietly go stale.
 *
 * The shared `self_test` corpus in that JSON is run by both implementations
 * (here in `test/secrets.test.js`, and in `tests/test_secrets_guard.py`), so the
 * two cannot disagree about a case even if the code paths differ.
 */

import fs from "node:fs";

const RULES = JSON.parse(
  fs.readFileSync(new URL("./secret-rules.json", import.meta.url), "utf8")
);

const P = RULES.path_rules;

const SECRET_EXTENSIONS = new Set(P.secret_extensions);
const SECRET_FILENAMES = new Set(P.secret_filenames);
const SECRET_DIRECTORIES = new Set(P.secret_directories);
const PRIVATE_KEY_STEMS = P.private_key_stems;
const DOTENV_RE = new RegExp(P.dotenv_pattern, "i");
const DOTENV_SAFE_SUFFIXES = P.dotenv_safe_suffixes;
const CLOUD_KEY_RE = new RegExp(P.cloud_key_pattern, "i");

const CONTENT_RULES = RULES.content_rules.map((r) => [
  r.label,
  new RegExp(r.pattern, r.flags?.includes("i") ? "i" : ""),
]);

const CONTENT_SCAN_BYTES = RULES.content_scan_bytes;

/** The file's extension, including the dot, or "" — the last dot in the
 *  basename, not counting a leading one (".npmrc" has no extension). */
function extname(basename) {
  const dot = basename.lastIndexOf(".");
  return dot > 0 ? basename.slice(dot) : "";
}

/** Strip a leading "./" as a *prefix*.
 *
 *  Not `lstrip`-style character stripping and not a `/^[./]+/` regex: the Python
 *  original used lstrip("./"), which strips characters, so ".env" became "env"
 *  and the single most important file this module exists to catch walked
 *  straight through.
 *
 *  Backslashes are converted unconditionally, never via path.sep. Using the
 *  platform separator makes the rule platform-dependent, so "backend\\.env"
 *  would be caught on Windows and sail past on Linux — and the archive is built
 *  on one machine and unpacked on another, which is exactly when that asymmetry
 *  bites. Both cases are in the shared self_test corpus.
 */
function normalise(p) {
  let out = p.replace(/\\/g, "/");
  while (out.startsWith("./")) out = out.slice(2);
  return out.replace(/^\/+/, "");
}

function pathReason(filePath) {
  const norm = normalise(filePath);
  const parts = norm.split("/");
  const name = parts[parts.length - 1];
  const low = name.toLowerCase();

  for (const part of parts.slice(0, -1)) {
    if (SECRET_DIRECTORIES.has(part.toLowerCase())) {
      return `lives in ${part}/ — a credential directory`;
    }
  }

  if (DOTENV_RE.test(low)) {
    if (DOTENV_SAFE_SUFFIXES.some((s) => low.endsWith(s))) return null;
    return "environment file — usually holds live secrets";
  }
  if (SECRET_FILENAMES.has(low)) return "known credential file";

  const ext = extname(low);
  if (SECRET_EXTENSIONS.has(ext)) return `${ext} key/certificate file`;

  if (CLOUD_KEY_RE.test(low)) return "looks like a cloud service-account key";

  // id_rsa.pub is a public key and harmless; id_rsa.bak is the private one
  // under another name.
  if (PRIVATE_KEY_STEMS.some((s) => low.startsWith(s)) && !low.endsWith(".pub")) {
    return "private SSH key";
  }
  return null;
}

function contentReason(content) {
  const head = content.slice(0, CONTENT_SCAN_BYTES);
  for (const [label, pattern] of CONTENT_RULES) {
    if (pattern.test(head)) return `contains what looks like ${label}`;
  }
  return null;
}

/** Classify one file. Returns null when it is safe to upload. */
export function inspect(filePath, content = "") {
  const byPath = pathReason(filePath);
  if (byPath) return { path: normalise(filePath), reason: byPath, kind: "path" };
  const byContent = contentReason(content);
  if (byContent) return { path: normalise(filePath), reason: byContent, kind: "content" };
  return null;
}

/**
 * Split entries into what may be uploaded and what must not be.
 * @param {Array<{path: string, content: string}>} entries
 */
export function scrub(entries) {
  const safe = [];
  const excluded = [];
  for (const entry of entries) {
    const finding = inspect(entry.path, entry.content || "");
    if (finding) excluded.push(finding);
    else safe.push(entry);
  }
  return { safe, excluded };
}

/** The shared specification corpus, so both languages' tests can run it. */
export const selfTest = RULES.self_test;
