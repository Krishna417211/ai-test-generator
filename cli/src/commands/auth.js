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

/** Best-effort browser open. Never fails the login — the URL is always printed
 *  too, because on a remote box there is no browser to open and the user is going
 *  to approve from their phone anyway. */
async function openBrowser(url) {
  const { spawn } = await import("node:child_process");
  const cmd =
    process.platform === "darwin" ? "open"
    : process.platform === "win32" ? "cmd"
    : "xdg-open";
  const argv = process.platform === "win32" ? ["/c", "start", "", url] : [url];
  try {
    const child = spawn(cmd, argv, { stdio: "ignore", detached: true });
    child.on("error", () => {});   // no opener installed; the printed URL covers it
    child.unref();
    return true;
  } catch {
    return false;
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * Device-code login (RFC 8628) — the default.
 *
 * The CLI cannot use a browser redirect: it may be on a box with no browser and
 * no way to receive a loopback callback. So it asks for a short code, the user
 * approves it in a browser anywhere (a phone works), and this polls until done.
 *
 * The point is that **the password never passes through the CLI** — and that this
 * works over SSH, where the alternative was copying a live session token by hand.
 */
async function deviceLogin(apiUrl) {
  let start;
  try {
    start = await postJson(apiUrl, "/api/auth/cli/start", {});
  } catch (err) {
    fail(err instanceof ApiError ? err.message : `Could not reach ${apiUrl}: ${err.message}`);
    return 1;
  }

  info("");
  info(`  Your code:  ${c.bold(start.user_code)}`);
  info(`  Approve at: ${c.cyan(start.verification_uri_complete)}`);
  info("");
  await openBrowser(start.verification_uri_complete);
  info(c.grey("  Waiting for approval… (Ctrl-C to cancel)"));

  const deadline = Date.now() + start.expires_in * 1000;
  // Honour the server's interval. It also rate-limits polling, so a client that
  // ignores this gets 429s rather than a faster login.
  const intervalMs = Math.max(1, start.interval) * 1000;

  while (Date.now() < deadline) {
    await sleep(intervalMs);
    let res;
    try {
      res = await postJson(apiUrl, "/api/auth/cli/token", { device_code: start.device_code });
    } catch (err) {
      // A blip mid-poll is not a failed login — the approval window is still
      // open, so keep trying until it genuinely closes.
      if (err instanceof ApiError && err.status === 429) continue;
      if (err instanceof ApiError && err.status >= 500) continue;
      fail(err.message);
      return 1;
    }

    if (res.status === "approved") {
      const file = config.save({ token: res.token, apiUrl, email: res.user?.email || "" });
      info("");
      info(`${c.green("✓")} Signed in as ${c.bold(res.user?.email || res.user?.id || "your account")}`);
      info(`  ${c.grey(`token stored at ${file} (mode 0600)`)}`);
      return 0;
    }
    if (res.status === "denied") {
      fail("That request was rejected in the browser.");
      return 1;
    }
    if (res.status === "expired") {
      fail("The code expired before it was approved. Run `testra login` again.");
      return 1;
    }
    // "pending" — keep waiting.
  }

  fail("Timed out waiting for approval. Run `testra login` again.");
  return 1;
}

export async function login(args, cfg) {
  const apiUrl = args.api || cfg.apiUrl;

  // A token from a secret store. The only path that needs no human at all, so
  // it is what CI uses.
  if (args.token) {
    const file = config.save({ token: args.token, apiUrl, email: "" });
    info(`${c.green("✓")} Token saved to ${c.grey(file)}`);
    return 0;
  }

  // Default: device code. No password touches this process.
  if (!args.email) {
    return deviceLogin(apiUrl);
  }

  // Password login, only when explicitly asked for with --email. Kept as the
  // fallback for a machine with no browser reachable at all — not even a phone
  // — where there is nowhere to approve a code.
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
