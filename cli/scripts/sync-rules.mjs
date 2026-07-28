#!/usr/bin/env node
/**
 * sync-rules.mjs — Refresh the CLI's copy of the canonical credential rules.
 *
 *   npm run sync-rules
 *
 * `backend/services/secret_rules.json` is the single source of truth. This copy
 * exists only because npm publishes `cli/` on its own and cannot reference a file
 * outside the package directory.
 *
 * The copy is verbatim and must stay that way: `backend/tests/
 * test_secret_rules_sync.py` compares the two byte for byte and fails CI on any
 * difference. So a rule is added in one place, this is run, and both languages
 * pick it up — including the shared self_test corpus both test suites execute.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const canonical = path.resolve(here, "../../backend/services/secret_rules.json");
const copy = path.resolve(here, "../src/secret-rules.json");

if (!fs.existsSync(canonical)) {
  console.error(
    `Canonical rules not found at ${canonical}.\n` +
      "This script only works inside the monorepo — a published package already " +
      "has its copy and needs no sync."
  );
  process.exit(1);
}

const source = fs.readFileSync(canonical);

// Parse before writing. A malformed canonical file would otherwise be copied
// happily and then blow up at require-time in every CLI install.
try {
  JSON.parse(source.toString("utf8"));
} catch (err) {
  console.error(`Canonical rules are not valid JSON: ${err.message}`);
  process.exit(1);
}

const before = fs.existsSync(copy) ? fs.readFileSync(copy) : null;
if (before && before.equals(source)) {
  console.log("Already in sync — nothing to do.");
  process.exit(0);
}

fs.writeFileSync(copy, source);
console.log(`Updated ${path.relative(process.cwd(), copy)} from the canonical rules.`);
