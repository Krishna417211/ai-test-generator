/**
 * secrets.test.js — The local credential scan.
 *
 * The load-bearing test here is the first one: it runs the `self_test` corpus
 * out of `secret-rules.json`, the same corpus `backend/tests/
 * test_secrets_guard.py` runs against the Python implementation. That is what
 * makes "these two agree" a checked fact rather than a hope — a case added to
 * the JSON is enforced in both languages at once, and neither can pass a case
 * the other fails.
 *
 * The tests after it cover behaviour that isn't expressible as a corpus entry:
 * the scan window, and how scrub() partitions its input.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { inspect, scrub, selfTest } from "../src/secrets.js";

test("the shared specification corpus passes", () => {
  assert.ok(selfTest.length >= 30, "corpus should be substantial");
  for (const c of selfTest) {
    const finding = inspect(c.path, c.content);
    const label = `${c.path} — ${c.note || (c.excluded ? "must be excluded" : "must be kept")}`;
    if (c.excluded) {
      assert.ok(finding, `${label}: expected excluded, was kept`);
      assert.ok(finding.reason, `${label}: exclusion needs a reason a user can act on`);
      assert.ok(["path", "content"].includes(finding.kind), `${label}: bad kind`);
    } else {
      assert.equal(finding, null, `${label}: expected kept, was excluded`);
    }
  }
});

test("dotenv files are caught", () => {
  // The one that matters most, and the one a naive lstrip("./") misses: it
  // strips characters, so ".env" becomes "env" and walks straight through. The
  // Python original had exactly that bug.
  for (const p of [".env", "./.env", "backend/.env", ".env.local", ".env.production"]) {
    assert.ok(inspect(p, "SECRET=1"), p);
  }
});

test("dotenv templates are kept", () => {
  // Placeholders by definition, and the file that documents configuration.
  for (const p of [".env.example", ".env.sample", ".env.template", ".env.dist"]) {
    assert.equal(inspect(p, "SECRET="), null, p);
  }
});

test("keys, certs and credential stores are caught", () => {
  for (const p of [
    "server.pem", "app.key", "keystore.jks", "cert.p12",
    "deploy/id_rsa", "id_ed25519", ".aws/credentials", ".ssh/config",
    "gcp-service-account-key.json", ".npmrc", "terraform.tfstate",
  ]) {
    assert.ok(inspect(p, "x"), p);
  }
});

test("a public key is not a secret", () => {
  assert.equal(inspect("id_rsa.pub", "ssh-rsa AAAA"), null);
});

test("ordinary source is untouched", () => {
  // The false-positive direction matters as much: a guard that eats real source
  // gets switched off, and then it protects nobody.
  for (const p of [
    "src/App.tsx", "package.json", "README.md",
    "src/hooks/useApiKey.ts", "tests/keyboard.spec.ts", "src/lib/keychain.ts",
  ]) {
    assert.equal(inspect(p, "export const x = 1;"), null, p);
  }
});

test("issuer-prefixed tokens in source are caught", () => {
  const cases = [
    'const k = "AKIAIOSFODNN7EXAMPLE"',
    `token = "ghp_${"a".repeat(36)}"`,
    `stripe("sk_live_${"a".repeat(24)}")`,
    `key = "AIza${"a".repeat(35)}"`,
    "-----BEGIN RSA PRIVATE KEY-----\nMIIE...",
    'DATABASE_URL = "postgres://admin:hunter2xyz@db.internal:5432/app"',
  ];
  for (const content of cases) {
    assert.ok(inspect("src/config.ts", content), content.slice(0, 40));
  }
});

test("placeholder assignments are not flagged", () => {
  // Deliberately unmatched. Generic `api_key = "..."` is overwhelmingly a
  // placeholder; matching it would drop half a codebase.
  for (const content of [
    'const API_KEY = "your-api-key-here";',
    'password = os.environ["DB_PASSWORD"]',
    "apiKey: process.env.VITE_API_KEY,",
    'const secret = "";',
  ]) {
    assert.equal(inspect("src/config.ts", content), null, content);
  }
});

test("only the head of a file is scanned", () => {
  const big = "x".repeat(200_000) + " AKIAIOSFODNN7EXAMPLE";
  assert.equal(inspect("vendor/bundle.js", big), null);
});

test("scrub splits entries and reports the reason", () => {
  const { safe, excluded } = scrub([
    { path: "src/App.tsx", content: "code" },
    { path: ".env", content: "K=v" },
    { path: "id_rsa", content: "key" },
  ]);
  assert.deepEqual(safe.map((e) => e.path), ["src/App.tsx"]);
  assert.deepEqual(excluded.map((e) => e.path).sort(), [".env", "id_rsa"]);
  assert.ok(excluded.every((e) => e.reason));
});

test("windows-style separators are normalised before matching", () => {
  // The collector emits forward slashes, but a path can reach inspect() from a
  // caller that didn't normalise — and ".aws\\credentials" must not slip past
  // the directory check just because the separator differs.
  assert.ok(inspect("backend\\.env", "K=v"));
});
