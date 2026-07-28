/**
 * secrets.js — Client-side mirror of backend/services/secrets_guard.py.
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
 * ## On duplicating the rules
 *
 * These are the same rules in a second language, which is a real cost — they can
 * drift. It is accepted deliberately, because the drift is *safe in one
 * direction*: the server re-scans everything regardless, so a rule this file
 * lacks is still caught server-side. Drift can only ever mean "the CLI sent
 * something it could have stopped locally", never "a credential got through".
 * Defence in depth, with the weaker layer first.
 *
 * If you add a rule to one side, add it to the other and to both test suites.
 */

import path from "node:path";

const SECRET_EXTENSIONS = new Set([
  ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore",
  ".ppk", ".asc", ".gpg", ".pgp", ".kdbx",
]);

const SECRET_FILENAMES = new Set([
  ".npmrc", ".pypirc", ".netrc", "_netrc", ".htpasswd",
  ".git-credentials", ".dockercfg", ".s3cfg", ".pgpass",
  "credentials", "credentials.json", "client_secret.json",
  "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
  "secring.gpg", "terraform.tfstate", "terraform.tfstate.backup",
  ".flaskenv", ".secrets", "secrets.json", "secrets.yml", "secrets.yaml",
]);

const SECRET_DIRECTORIES = new Set([".aws", ".ssh", ".gnupg", ".gcloud", ".kube"]);

const DOTENV_RE = /^\.env(\..+)?$/i;
const DOTENV_SAFE_SUFFIXES = [
  ".example", ".sample", ".template", ".dist", ".defaults", ".test",
];
const GCP_KEY_RE = /(service[-_]?account|gcp|google).*(key|credential)s?\.json$/i;

const CONTENT_RULES = [
  ["an AWS access key id", /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/],
  ["a GitHub token", /\bgh[pousr]_[A-Za-z0-9]{36,}\b/],
  ["a GitHub fine-grained token", /\bgithub_pat_[A-Za-z0-9_]{60,}\b/],
  ["a Slack token", /\bxox[abprs]-[A-Za-z0-9-]{10,}\b/],
  ["a Stripe live secret key", /\bsk_live_[A-Za-z0-9]{20,}\b/],
  ["a Google API key", /\bAIza[0-9A-Za-z_\-]{35}\b/],
  ["an OpenAI API key", /\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}\b/],
  ["an Anthropic API key", /\bsk-ant-[A-Za-z0-9_\-]{20,}\b/],
  ["a SendGrid API key", /\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\b/],
  ["a Twilio account sid", /\bAC[0-9a-fA-F]{32}\b/],
  ["a private key block", /-----BEGIN [A-Z ]*PRIVATE KEY-----/],
  ["a PGP private key block", /-----BEGIN PGP PRIVATE KEY BLOCK-----/],
  [
    "a database URL with an inline password",
    /\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp):\/\/[^\s:@/]+:[^\s:@/]{6,}@[^\s/]+/,
  ],
];

const CONTENT_SCAN_BYTES = 100_000;

/** Strip a leading "./" as a *prefix*.
 *
 *  Not a regex character class and never String.replace(/^[./]+/): the Python
 *  original used lstrip("./"), which strips characters, so ".env" became "env"
 *  and the single most important file this module exists to catch walked
 *  straight through. Same mistake is available in JS; it is not repeated here.
 */
function normalise(p) {
  // Backslashes are converted unconditionally, not via path.sep. Using path.sep
  // makes the rule platform-dependent, so "backend\\.env" would be caught on
  // Windows and sail past on Linux — and the archive is built on one machine
  // and unpacked on another, which is exactly when that asymmetry bites.
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

  const ext = path.extname(low);
  if (SECRET_EXTENSIONS.has(ext)) return `${ext} key/certificate file`;

  if (GCP_KEY_RE.test(low)) return "looks like a cloud service-account key";

  // id_rsa.pub is a public key and harmless; id_rsa.bak is the private one
  // under another name.
  const privateStems = ["id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"];
  if (privateStems.some((s) => low.startsWith(s)) && !low.endsWith(".pub")) {
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
