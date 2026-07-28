#!/usr/bin/env node
/**
 * testra — publish a project to a fresh GitHub repo with a generated E2E suite.
 *
 * Argument parsing is hand-rolled and stays that way: a flag parser is fifty
 * lines and a dependency is a supply-chain surface. For a tool whose selling
 * point is "your credentials never leave this machine", every dependency is a
 * thing a reviewer has to take on trust.
 */

import { parseArgs } from "node:util";

import * as config from "../src/config.js";
import { publish } from "../src/commands/publish.js";
import { login, logout, whoami } from "../src/commands/auth.js";
import { c, info, fail } from "../src/ui.js";

const USAGE = `${c.bold("testra")} — grounded E2E tests, from the directory your project lives in

${c.bold("Usage")}
  testra publish [dir] [options]     Create a GitHub repo and push this project
  testra login                       Sign in by approving a code in a browser
  testra logout                      Remove the locally stored token
  testra whoami                      Show the signed-in account

${c.bold("Login options")}
  ${c.grey("(default)")}              Show a short code, approve it in a browser — your
                         password never passes through this tool. Works over SSH:
                         approve from any device, a phone included.
  --token <t>            Use a token directly. This is what CI wants.
  --email <e>            Password login instead, for a machine with no browser
                         reachable at all.

${c.bold("Publish options")}
  --repo <name>          Repository name            ${c.grey("(default: directory name)")}
  --public               Create it public           ${c.grey("(default: private)")}
  --with-ci              Also generate an E2E suite + CI/CD pipeline
  --framework <f>        playwright | cypress | selenium    ${c.grey("(default: playwright)")}
  --language <l>         typescript | javascript | python | java
  --flows <text>         What to test, in prose
  --base-url <url>       Where the app under test listens
  --description <text>   Repository description
  -y, --yes              Skip the confirmation prompt

${c.bold("Global options")}
  --api <url>            API base URL   ${c.grey(`(default: ${config.DEFAULT_API}, or TESTRA_API_URL)`)}
  -h, --help             Show this
  -v, --version          Show the version

${c.bold("Environment")}
  TESTRA_TOKEN           Session token — overrides the stored one. Use this in CI.
  TESTRA_API_URL         API base URL.
  NO_COLOR               Disable colour.

${c.bold("Notes")}
  Files are chosen by ${c.bold("git ls-files")}, so your .gitignore decides what is
  uploaded. Credential files (.env, SSH keys, .pem, hard-coded API tokens) are
  detected and held back ${c.bold("before")} anything is sent.
`;

const OPTIONS = {
  repo: { type: "string" },
  public: { type: "boolean" },
  private: { type: "boolean" },
  "with-ci": { type: "boolean" },
  framework: { type: "string" },
  language: { type: "string" },
  flows: { type: "string" },
  "base-url": { type: "string" },
  description: { type: "string" },
  yes: { type: "boolean", short: "y" },
  email: { type: "string" },
  token: { type: "string" },
  code: { type: "string" },
  api: { type: "string" },
  help: { type: "boolean", short: "h" },
  version: { type: "boolean", short: "v" },
};

async function version() {
  const { readFileSync } = await import("node:fs");
  const url = new URL("../package.json", import.meta.url);
  return JSON.parse(readFileSync(url, "utf8")).version;
}

async function main(argv) {
  let parsed;
  try {
    parsed = parseArgs({ args: argv, options: OPTIONS, allowPositionals: true });
  } catch (err) {
    // parseArgs throws on an unknown flag. Better than silently ignoring it:
    // a typo'd --publik would otherwise create a private repo without comment.
    fail(err.message);
    info(c.grey("Run `testra --help` for the available options."));
    return 1;
  }

  const { values, positionals } = parsed;
  const command = positionals[0];

  if (values.version) {
    info(await version());
    return 0;
  }
  if (values.help || !command) {
    info(USAGE);
    return command ? 0 : 1;
  }

  const cfg = config.load();
  if (values.api) cfg.apiUrl = values.api;

  const args = {
    dir: positionals[1],
    repo: values.repo,
    // --public and --private are both accepted, and the explicit one wins.
    // Defaulting to private is the safe direction: a repo made public by
    // accident cannot be made un-public after someone has cloned it.
    private: values.public ? false : values.private !== false,
    withCi: values["with-ci"],
    framework: values.framework,
    language: values.language,
    flows: values.flows,
    baseUrl: values["base-url"],
    description: values.description,
    yes: values.yes,
    email: values.email,
    token: values.token,
    code: values.code,
    api: values.api,
  };

  switch (command) {
    case "publish":
      return publish(args, cfg);
    case "login":
      return login(args, cfg);
    case "logout":
      return logout(args, cfg);
    case "whoami":
      return whoami(args, cfg);
    default:
      fail(`Unknown command '${command}'.`);
      info(c.grey("Run `testra --help` for usage."));
      return 1;
  }
}

main(process.argv.slice(2))
  .then((code) => process.exit(code ?? 0))
  .catch((err) => {
    // Cancelling at a prompt is a normal way to leave, not a crash.
    if (err?.message === "Cancelled.") {
      process.exit(130);
    }
    fail(err?.message || String(err));
    process.exit(1);
  });
