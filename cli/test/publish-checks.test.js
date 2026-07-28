/**
 * publish-checks.test.js — The pre-upload warning the server cannot give.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { strippedRootDir } from "../src/publish-checks.js";

test("a shared non-standard top-level directory would be stripped", () => {
  // The monorepo case: run at the root of a repo whose files all live under
  // frontend/, and the published repo silently loses that level.
  assert.equal(
    strippedRootDir(["frontend/src/App.tsx", "frontend/package.json"]),
    "frontend"
  );
});

test("a normal project root is not affected", () => {
  assert.equal(
    strippedRootDir(["package.json", "src/App.tsx", "README.md"]),
    null
  );
});

test("the server's own keep-list is respected", () => {
  // extract_zip refuses to strip these, because a ZIP of just src/ is a real
  // project layout rather than a GitHub wrapper folder.
  assert.equal(strippedRootDir(["src/App.tsx", "src/main.tsx"]), null);
  assert.equal(strippedRootDir(["tests/a.spec.ts", "tests/b.spec.ts"]), null);
});

test("a partially shared prefix is not a wrapper", () => {
  assert.equal(
    strippedRootDir(["frontend/src/App.tsx", "backend/main.py"]),
    null
  );
});

test("edge cases don't throw", () => {
  assert.equal(strippedRootDir([]), null);
  assert.equal(strippedRootDir(["single-file.ts"]), null);
});
