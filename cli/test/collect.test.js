/**
 * collect.test.js — Which files count as "the project".
 *
 * The git path is the one that matters (it is what makes the CLI better than the
 * upload page), so it is tested against a real repository built in a temp dir
 * rather than a mock. Skipped, not silently passed, when git is unavailable —
 * a test that quietly stops checking is worse than one that says it didn't run.
 */

import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";

import { collect } from "../src/collect.js";

function hasGit() {
  try {
    execFileSync("git", ["--version"], { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

function tempProject(files) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "testra-cli-"));
  for (const [rel, content] of Object.entries(files)) {
    const abs = path.join(dir, rel);
    fs.mkdirSync(path.dirname(abs), { recursive: true });
    fs.writeFileSync(abs, content);
  }
  return dir;
}

function initRepo(dir) {
  const opts = { cwd: dir, stdio: "ignore" };
  execFileSync("git", ["init", "-q"], opts);
  // A commit isn't needed: `ls-files -co` reports untracked-but-not-ignored too,
  // which is what makes this work on a project that has never committed.
  execFileSync("git", ["config", "user.email", "t@example.com"], opts);
  execFileSync("git", ["config", "user.name", "t"], opts);
}

test("gitignore decides what is collected", { skip: !hasGit() && "git not available" }, () => {
  const dir = tempProject({
    ".gitignore": "node_modules/\ndist/\n.turbo/\nsecret-notes.txt\n",
    "src/App.tsx": "<div/>",
    "package.json": "{}",
    "node_modules/react/index.js": "junk",
    "dist/bundle.js": "built",
    // The point of using git: .turbo is in nobody's hard-coded skip list, but
    // it is in this project's .gitignore, so it must be excluded.
    ".turbo/cache.log": "cache",
    "secret-notes.txt": "private",
  });
  initRepo(dir);

  const { entries, source } = collect(dir);
  const paths = entries.map((e) => e.path).sort();

  assert.equal(source, "git");
  assert.deepEqual(paths, [".gitignore", "package.json", "src/App.tsx"]);
});

test("falls back to a walk outside a repo, and says so", () => {
  const dir = tempProject({
    "src/App.tsx": "<div/>",
    "node_modules/react/index.js": "junk",
  });

  const { entries, source } = collect(dir);
  assert.equal(source, "walk");
  // The built-in skip list still catches the obvious ones.
  assert.deepEqual(entries.map((e) => e.path), ["src/App.tsx"]);
});

test("binaries and oversized files are skipped with a count", () => {
  const dir = tempProject({
    "logo.png": "\x89PNG fake",
    "src/App.tsx": "<div/>",
    "big.txt": "x".repeat(1_000_001),
  });

  const { entries, skipped } = collect(dir);
  assert.deepEqual(entries.map((e) => e.path), ["src/App.tsx"]);
  assert.equal(skipped.binary, 1);
  assert.equal(skipped.large, 1);
});

test("file contents come through intact", () => {
  const dir = tempProject({ "a.ts": "const x = 1;\n// café ✓\n" });
  const { entries } = collect(dir);
  assert.equal(entries[0].content, "const x = 1;\n// café ✓\n");
});

test("symlinks are not followed", () => {
  // Following one can leave the project directory entirely — the same class of
  // mistake as a zip-slip, arrived at from the other side.
  const dir = tempProject({ "real.ts": "1" });
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), "testra-outside-"));
  fs.writeFileSync(path.join(outside, "secret.txt"), "should never be collected");
  try {
    fs.symlinkSync(path.join(outside, "secret.txt"), path.join(dir, "link.txt"));
  } catch {
    return; // no symlink permission (Windows without dev mode) — nothing to test
  }

  const { entries } = collect(dir);
  assert.deepEqual(entries.map((e) => e.path), ["real.ts"]);
});
