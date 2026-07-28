/**
 * auth.js — `testra login` / `logout` / `whoami`.
 *
 * Login mirrors the web app's three-way outcome exactly (see LoginResponse in
 * backend/models/schemas.py): a session, an OTP challenge, or an unverified
 * address. Handling only the first would leave anyone with OTP enabled — the
 * default — unable to use the CLI at all, with a confusing "no token" error.
 *
 * The password is read with echo suppressed and never stored; only the returned
 * session token is persisted, and that at 0600.
 */

import readline from "node:readline/promises";

import * as config from "../config.js";
import { postJson, getJson, ApiError } from "../api.js";
import { c, info, warn, fail } from "../ui.js";

/** Read a line with echo suppressed, so the password stays out of the scrollback.
 *
 *  Node has no built-in for this. Setting the tty to raw and consuming keys by
 *  hand is the standard approach; the important part is the `finally`, because
 *  leaving a terminal in raw mode on an error makes the user's shell unusable
 *  until they run `reset`.
 */
async function readPassword(prompt) {
  const { stdin, stdout } = process;
  if (!stdin.isTTY) {
    // Piped input (CI). Read a line plainly — there is no terminal to hide it
    // from, and pretending otherwise would just hang.
    const rl = readline.createInterface({ input: stdin });
    try {
      for await (const line of rl) return line.trim();
      return "";
    } finally {
      rl.close();
    }
  }

  stdout.write(prompt);
  const wasRaw = stdin.isRaw;
  stdin.setRawMode(true);
  stdin.resume();

  return new Promise((resolve, reject) => {
    let value = "";
    const onData = (chunk) => {
      const s = chunk.toString("utf8");
      for (const ch of s) {
        if (ch === "\r" || ch === "\n") {
          cleanup();
          stdout.write("\n");
          return resolve(value);
        }
        if (ch === "\u0003") {                  // Ctrl-C
          cleanup();
          stdout.write("\n");
          return reject(new Error("Cancelled."));
        }
        if (ch === "\u007f" || ch === "\b") {   // DEL / backspace
          value = value.slice(0, -1);
          continue;
        }
        if (ch < " ") continue;          // ignore other control characters
        value += ch;
      }
    };
    const cleanup = () => {
      stdin.removeListener("data", onData);
      stdin.setRawMode(wasRaw);
      stdin.pause();
    };
    stdin.on("data", onData);
  });
}

async function ask(question) {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  try {
    return (await rl.question(question)).trim();
  } finally {
    rl.close();
  }
}

export async function login(args, cfg) {
  const apiUrl = args.api || cfg.apiUrl;

  // A token pasted from elsewhere (or minted by an admin) skips the password
  // dance entirely — and is the only path that works in CI.
  if (args.token) {
    const file = config.save({ token: args.token, apiUrl, email: "" });
    info(`${c.green("✓")} Token saved to ${c.grey(file)}`);
    return 0;
  }

  const email = args.email || (await ask("Email: "));
  if (!email) {
    fail("An email address is required.");
    return 1;
  }
  const password = await readPassword("Password: ");
  if (!password) {
    fail("A password is required.");
    return 1;
  }

  let res;
  try {
    res = await postJson(apiUrl, "/api/auth/login", { email, password });
  } catch (err) {
    fail(err instanceof ApiError ? err.message : `Could not reach ${apiUrl}: ${err.message}`);
    return 1;
  }

  if (res.status === "verification_required") {
    warn(res.message || "Confirm your email address first — we've sent a fresh link.");
    return 1;
  }

  if (res.status === "otp_required") {
    info(res.message || `We sent a 6-digit code to ${res.email_hint}.`);
    const code = args.code || (await ask("Code: "));
    try {
      res = await postJson(apiUrl, "/api/auth/login/verify-otp", {
        challenge_id: res.challenge_id,
        code,
      });
    } catch (err) {
      fail(err instanceof ApiError ? err.message : err.message);
      return 1;
    }
  }

  if (!res?.token) {
    fail("Login did not return a session. Try the web app to check the account state.");
    return 1;
  }

  const file = config.save({ token: res.token, apiUrl, email: res.user?.email || email });
  info(`${c.green("✓")} Signed in as ${c.bold(res.user?.email || email)}`);
  info(`  ${c.grey(`token stored at ${file} (mode 0600)`)}`);
  return 0;
}

export async function logout() {
  // Only the local copy is dropped. Revoking the session server-side needs the
  // web app's "log out everywhere" — said plainly rather than implying more
  // than this does.
  const had = config.clear();
  info(had ? `${c.green("✓")} Local token removed.` : c.grey("No stored token."));
  info(c.grey("The session itself stays valid until it expires — use \"log out everywhere\" in the app to revoke it."));
  return 0;
}

export async function whoami(args, cfg) {
  if (!cfg.token) {
    info("Not signed in.");
    return 1;
  }
  try {
    const me = await getJson(cfg.apiUrl, "/api/auth/me", cfg.token);
    const user = me.user || me;
    info(`${c.bold(user.email || "(no email)")} ${c.grey(`· ${cfg.apiUrl}`)}`);
    if (user.plan) info(`  plan: ${user.plan}`);
    if (cfg.tokenSource) info(`  ${c.grey(`token from ${cfg.tokenSource}`)}`);
    return 0;
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      fail("Stored token is no longer valid. Run `testra login`.");
      return 1;
    }
    fail(err.message);
    return 1;
  }
}
