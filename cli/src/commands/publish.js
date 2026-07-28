/**
 * publish.js — `testra publish` — this directory → a new GitHub repo.
 *
 * The ordering here is the whole point of the command, so it is worth stating:
 *
 *   1. collect   what belongs to the project (git decides, via .gitignore)
 *   2. scan      for credentials — LOCALLY, before a single byte is sent
 *   3. confirm   show the user what will be uploaded and what was held back
 *   4. upload    one multipart POST, progress streamed back as NDJSON
 *
 * Step 2 happening before step 4 is the reason this exists. In the web app the
 * server is the earliest place a `.env` can be caught, because by then it has
 * already been uploaded. Here it never leaves the machine.
 *
 * Step 3 is not decoration either: the file count is the only signal that the
 * collector got it wrong (a missing .gitignore, a directory nobody meant to
 * include), and finding that out *after* pushing 4,000 files to a new public
 * repository is not a recoverable kind of surprise.
 */

import path from "node:path";
import readline from "node:readline/promises";

import { collect } from "../collect.js";
import { scrub } from "../secrets.js";
import { strippedRootDir } from "../publish-checks.js";
import { makeZip } from "../zip.js";
import { postStream, ApiError } from "../api.js";
import { c, info, step, warn, fail, score, plural } from "../ui.js";

// The step ids the server emits for this flow (backend/services/progress.py
// PUBLISH_STEPS), mapped to something a human reads. An id we don't know still
// prints — using its raw id — rather than being dropped: a step that silently
// never appears is the confusing failure this avoids.
const STEP_LABELS = {
  read: "Archive read",
  filter: "Filtered",
  suite: "Test suite generated",
  push: "Pushed to GitHub",
  record: "Recorded",
};

const FRAMEWORKS = new Set(["playwright", "cypress", "selenium"]);
const LANGUAGES = new Set(["typescript", "javascript", "python", "java"]);

async function confirm(question) {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  try {
    const answer = (await rl.question(`${question} ${c.grey("[y/N]")} `)).trim().toLowerCase();
    return answer === "y" || answer === "yes";
  } finally {
    rl.close();
  }
}

export async function publish(args, cfg) {
  if (!cfg.token) {
    fail("Not signed in. Run `testra login` first, or set TESTRA_TOKEN.");
    return 1;
  }

  const root = path.resolve(args.dir || ".");
  const repoName = args.repo || path.basename(root);

  if (args.framework && !FRAMEWORKS.has(args.framework)) {
    fail(`Unknown framework '${args.framework}'. Choose one of: ${[...FRAMEWORKS].join(", ")}.`);
    return 1;
  }
  if (args.language && !LANGUAGES.has(args.language)) {
    fail(`Unknown language '${args.language}'. Choose one of: ${[...LANGUAGES].join(", ")}.`);
    return 1;
  }

  // ── 1. collect ──
  info(`${c.bold("Testra")} ${c.grey(`· ${root}`)}`);
  const { entries, source, skipped } = collect(root);

  if (source === "walk") {
    // Said out loud, because the fallback is materially worse: it guesses at
    // build directories from a fixed list instead of reading your ignores.
    warn(
      "Not a git repository — falling back to a built-in skip list instead of " +
        "your .gitignore. Check the file count below carefully."
    );
  }

  if (entries.length === 0) {
    fail("No files to publish. Check you're in the project directory.");
    return 1;
  }

  // ── 2. scan, locally ──
  const { safe, excluded } = scrub(entries);

  if (safe.length === 0) {
    fail("Every file was held back as a credential file. Nothing to publish.");
    return 1;
  }

  const skippedTotal = skipped.binary + skipped.large + skipped.unreadable + skipped.directories;
  info("");
  step("done", `${plural(safe.length, "file")} to upload`, `via ${source === "git" ? "git ls-files" : "directory walk"}`);
  if (skippedTotal) {
    const bits = [
      skipped.binary && `${skipped.binary} binary`,
      skipped.large && `${skipped.large} over 1 MB`,
      skipped.unreadable && `${skipped.unreadable} unreadable`,
    ].filter(Boolean);
    step("skipped", `${plural(skippedTotal, "file")} skipped`, bits.join(" · "));
  }

  // ── 3. confirm ──
  if (excluded.length) {
    info("");
    info(`${c.yellow("Held back — these never leave your machine:")}`);
    for (const f of excluded.slice(0, 10)) {
      info(`  ${c.grey("·")} ${f.path} ${c.grey(`— ${f.reason}`)}`);
    }
    if (excluded.length > 10) info(`  ${c.grey(`… and ${excluded.length - 10} more`)}`);
  }

  const stripped = strippedRootDir(safe.map((e) => e.path));
  if (stripped) {
    warn(
      `Every file sits under ${stripped}/, which the server strips when unwrapping ` +
        `an archive — the published repo would have ${stripped}/'s contents at its ` +
        `root. Run this inside ${stripped}/ if that isn't what you want.`
    );
  }

  info("");
  info(`Repository:  ${c.bold(repoName)} ${c.grey(args.private === false ? "(public)" : "(private)")}`);
  info(`Tests:       ${args.withCi ? `${args.framework || "playwright"} + CI/CD pipeline` : c.grey("none — code only")}`);

  if (!args.yes) {
    info("");
    if (!(await confirm("Create this repository and push?"))) {
      info(c.grey("Aborted. Nothing was uploaded."));
      return 130;
    }
  }

  // ── 4. upload ──
  // A fixed mtime keeps the archive byte-reproducible, which makes a failed
  // publish worth retrying identically instead of producing a different upload.
  const zip = makeZip(safe, new Date(Date.UTC(2020, 0, 1)));

  info("");
  const fields = [
    ["repo_name", repoName],
    ["add_cicd", String(Boolean(args.withCi))],
    ["private", String(args.private !== false)],
    ["framework", args.framework || "playwright"],
    ["language", args.language || "typescript"],
    ["test_flows", args.flows || ""],
    ["base_url", args.baseUrl || "http://localhost:3000"],
    ["repo_description", args.description || ""],
  ];

  let result;
  try {
    result = await postStream(cfg.apiUrl, "/api/publish-zip", {
      fields,
      file: { name: `${repoName}.zip`, data: zip },
      token: cfg.token,
      onStep: (msg) => {
        if (msg.state === "running") return; // only report outcomes, not starts
        step(msg.state, STEP_LABELS[msg.id] || msg.id, msg.detail || "");
      },
    });
  } catch (err) {
    info("");
    if (err instanceof ApiError && err.status === 401) {
      fail("Your session has expired. Run `testra login` again.");
      return 1;
    }
    if (err instanceof ApiError && err.status === 403) {
      fail(`${err.message}`);
      info(c.grey("Publishing needs a linked GitHub account — connect it in the web app."));
      return 1;
    }
    fail(err.message);
    return 1;
  }

  // ── report ──
  info("");
  info(`${c.green("✓")} ${c.bold(result.full_name)}`);
  info(`  ${result.repo_url}`);
  info(
    `  ${plural(result.files_pushed, "file")} on ${result.branch}` +
      (result.files_verified ? c.green(" · every file confirmed in the repo") : "")
  );

  if (result.verification_note) warn(result.verification_note);
  if (result.repaired_files?.length) {
    warn(`${plural(result.repaired_files.length, "file")} needed a second commit before landing.`);
  }

  if (result.cicd_added) {
    info(`  ${plural(result.test_count, "test")} + CI/CD pipeline`);
  }
  if (result.success_rate?.measured && result.success_rate.score !== null) {
    info("");
    info(`  Generation success rate: ${score(result.success_rate.score, result.success_rate.grade)}`);
    for (const comp of result.success_rate.components || []) {
      const ok = comp.passed === comp.total;
      info(
        `    ${ok ? c.green("✓") : c.yellow("!")} ${comp.label}: ${comp.passed}/${comp.total}` +
          (ok ? "" : ` ${c.grey(`— ${comp.detail}`)}`)
      );
    }
    info(`  ${c.grey(result.success_rate.not_measured)}`);
  }

  for (const w of result.warnings || []) warn(w);

  return 0;
}
