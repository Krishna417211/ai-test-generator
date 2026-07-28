/**
 * device-login.test.js — `testra login` via the device-code grant.
 *
 * Driven end to end: a real local HTTP server for the endpoints, and the real
 * `bin/testra.js` spawned as a child process. Two reasons it is done this way
 * rather than by importing the function and stubbing fetch.
 *
 * First, the flow is a *conversation* — start, poll, poll, approved — and the
 * bugs live in the conversation: polling too fast, abandoning the login on a
 * transient error, treating a decision as pending. A stub that returns whatever
 * the test wants next cannot fail any of those.
 *
 * Second, an earlier version of this file imported `login()` and captured output
 * by monkey-patching `process.stdout.write`. That also swallowed node:test's own
 * TAP output, so five of eight tests silently vanished from the report while
 * appearing to pass. A subprocess has its own stdout; there is nothing to patch.
 *
 * The server hands back `interval: 1` so the suite doesn't wait the real five
 * seconds per poll.
 */

import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const BIN = fileURLToPath(new URL("../bin/testra.js", import.meta.url));

/** A stand-in for the device-code endpoints.
 *
 *  `script` is the sequence /token answers, one entry per poll, so a test can say
 *  "pending, pending, approved" and assert the CLI waited it out. A number in the
 *  script is returned as an HTTP status instead of a body. */
function startServer(script, { interval = 1 } = {}) {
  const calls = { start: 0, token: 0 };
  const seen = { deviceCodes: [] };
  let i = 0;

  const srv = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : {};
    const send = (status, obj) => {
      res.writeHead(status, { "Content-Type": "application/json" });
      res.end(JSON.stringify(obj));
    };

    if (req.url === "/api/auth/cli/start") {
      calls.start++;
      return send(200, {
        device_code: "dev-secret-code",
        user_code: "ABCD-EFGH",
        verification_uri: "http://example.test/cli",
        verification_uri_complete: "http://example.test/cli?code=ABCD-EFGH",
        expires_in: 60,
        interval,
      });
    }
    if (req.url === "/api/auth/cli/token") {
      calls.token++;
      seen.deviceCodes.push(body.device_code);
      const step = script[Math.min(i++, script.length - 1)];
      if (typeof step === "number") return send(step, { detail: "transient" });
      if (step === "approved") {
        return send(200, {
          status: "approved",
          token: "sess_tok_123",
          user: { id: "u1", email: "dev@example.com" },
        });
      }
      return send(200, { status: step, token: null, user: null });
    }
    send(404, {});
  });

  return new Promise((resolve) => {
    srv.listen(0, "127.0.0.1", () =>
      resolve({ srv, calls, seen, url: `http://127.0.0.1:${srv.address().port}` })
    );
  });
}

/** Run the real CLI with a throwaway config directory.
 *
 *  XDG_CONFIG_HOME is honoured by src/config.js, which keeps this off the
 *  developer's real ~/.config — a test that overwrites your actual credentials
 *  is a test nobody runs twice. */
function runCli(argv, { apiUrl, env = {} } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "testra-cfg-"));
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [BIN, ...argv], {
      env: {
        ...process.env,
        XDG_CONFIG_HOME: dir,
        TESTRA_API_URL: apiUrl ?? "http://127.0.0.1:1",
        NO_COLOR: "1",
        // Must not inherit a real token from the developer's shell.
        TESTRA_TOKEN: "",
        ...env,
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let out = "";
    child.stdout.on("data", (d) => (out += d));
    child.stderr.on("data", (d) => (out += d));
    child.on("close", (code) => {
      const file = path.join(dir, "testra", "config.json");
      const saved = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : null;
      const mode = fs.existsSync(file) ? fs.statSync(file).mode & 0o777 : null;
      resolve({ code, output: out, saved, file, mode });
    });
  });
}

test("approval on the first poll stores the session token", async () => {
  const { srv, url, calls, seen } = await startServer(["approved"]);
  try {
    const r = await runCli(["login"], { apiUrl: url });
    assert.equal(r.code, 0, r.output);
    assert.equal(r.saved.token, "sess_tok_123");
    assert.equal(r.saved.email, "dev@example.com");
    assert.equal(calls.start, 1);
    assert.deepEqual(seen.deviceCodes, ["dev-secret-code"]);

    // The short code must be on screen — it is the whole interaction.
    assert.match(r.output, /ABCD-EFGH/);
    // The device_code must not be: it is the secret, and terminal scrollback
    // gets pasted into issues and screenshots.
    assert.doesNotMatch(r.output, /dev-secret-code/);
  } finally {
    srv.close();
  }
});

test("the token file is written 0600", async () => {
  const { srv, url } = await startServer(["approved"]);
  try {
    const r = await runCli(["login"], { apiUrl: url });
    assert.equal(r.mode, 0o600, `expected 0600, got ${r.mode?.toString(8)}`);
  } finally {
    srv.close();
  }
});

test("pending is waited out, not treated as failure", async () => {
  const { srv, url, calls } = await startServer(["pending", "pending", "approved"]);
  try {
    const r = await runCli(["login"], { apiUrl: url });
    assert.equal(r.code, 0, r.output);
    assert.equal(r.saved.token, "sess_tok_123");
    assert.equal(calls.token, 3, "should have polled until approved");
  } finally {
    srv.close();
  }
});

test("a transient error mid-poll does not abandon the login", async () => {
  // The approval window is still open, so giving up here would fail a login the
  // user is a click away from completing — because of a blip.
  const { srv, url, calls } = await startServer([500, 429, "approved"]);
  try {
    const r = await runCli(["login"], { apiUrl: url });
    assert.equal(r.code, 0, r.output);
    assert.equal(r.saved.token, "sess_tok_123");
    assert.equal(calls.token, 3);
  } finally {
    srv.close();
  }
});

test("denied stops immediately and stores nothing", async () => {
  const { srv, url, calls } = await startServer(["denied"]);
  try {
    const r = await runCli(["login"], { apiUrl: url });
    assert.equal(r.code, 1);
    assert.equal(r.saved, null, "a rejected login must not leave a token behind");
    assert.equal(calls.token, 1, "must not keep polling after a decision");
    assert.match(r.output, /rejected/i);
  } finally {
    srv.close();
  }
});

test("expired says to start again", async () => {
  const { srv, url } = await startServer(["expired"]);
  try {
    const r = await runCli(["login"], { apiUrl: url });
    assert.equal(r.code, 1);
    assert.equal(r.saved, null);
    assert.match(r.output, /expired/i);
  } finally {
    srv.close();
  }
});

test("--token skips the flow entirely", async () => {
  // What CI uses: no server call at all, so it works with no browser and no
  // interactive terminal.
  const { srv, url, calls } = await startServer(["approved"]);
  try {
    const r = await runCli(["login", "--token", "ci_token"], { apiUrl: url });
    assert.equal(r.code, 0, r.output);
    assert.equal(r.saved.token, "ci_token");
    assert.equal(calls.start, 0, "--token must not start a device flow");
  } finally {
    srv.close();
  }
});

test("an unreachable server fails with a message naming the URL", async () => {
  // Port 1 is reserved; nothing listens there.
  const r = await runCli(["login"], { apiUrl: "http://127.0.0.1:1" });
  assert.equal(r.code, 1);
  assert.match(r.output, /127\.0\.0\.1:1/);
});

test("publish without a token tells you to log in", async () => {
  const r = await runCli(["publish", "--yes"], { apiUrl: "http://127.0.0.1:1" });
  assert.equal(r.code, 1);
  assert.match(r.output, /testra login/);
});
