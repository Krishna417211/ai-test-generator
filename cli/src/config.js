/**
 * config.js — Where the API token lives on disk.
 *
 * The token is a session credential: it can create repositories on the user's
 * GitHub account through the server. So it gets the same handling anyone gives
 * an SSH key — a 0600 file in a 0700 directory, never an environment variable
 * baked into a shell profile, and never printed.
 *
 * Resolution order, most explicit first:
 *   1. TESTRA_TOKEN in the environment  (CI: set it from a secret, not a file)
 *   2. ~/.config/testra/config.json     (written by `testra login`)
 *
 * XDG_CONFIG_HOME is honoured because people who set it mean it.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const DEFAULT_API = "http://localhost:8000";

function configDir() {
  const xdg = process.env.XDG_CONFIG_HOME;
  const base = xdg && xdg.trim() ? xdg : path.join(os.homedir(), ".config");
  return path.join(base, "testra");
}

function configPath() {
  return path.join(configDir(), "config.json");
}

function readFileConfig() {
  try {
    return JSON.parse(fs.readFileSync(configPath(), "utf8"));
  } catch {
    return {};
  }
}

/** The stored config, plus whatever the environment overrides. */
export function load() {
  const stored = readFileConfig();
  const envToken = (process.env.TESTRA_TOKEN || "").trim();
  const envApi = (process.env.TESTRA_API_URL || "").trim();
  return {
    token: envToken || stored.token || "",
    apiUrl: envApi || stored.apiUrl || DEFAULT_API,
    email: stored.email || "",
    // Which source won, so `testra whoami` can say where the token came from
    // instead of leaving someone puzzling over a stale file.
    tokenSource: envToken ? "TESTRA_TOKEN" : stored.token ? configPath() : "",
  };
}

export function save({ token, apiUrl, email }) {
  const dir = configDir();
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const file = configPath();
  const body = JSON.stringify({ token, apiUrl, email }, null, 2) + "\n";
  // Create with 0600 from the outset. Writing then chmod-ing leaves a window in
  // which the token is world-readable, which on a shared machine is the whole
  // problem this function exists to avoid.
  fs.writeFileSync(file, body, { mode: 0o600 });
  fs.chmodSync(file, 0o600); // in case the file already existed with wider bits
  return file;
}

export function clear() {
  try {
    fs.rmSync(configPath());
    return true;
  } catch {
    return false;
  }
}

export const paths = { configDir, configPath };
