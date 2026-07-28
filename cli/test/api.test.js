/**
 * api.test.js — Reading the server's two response shapes.
 *
 * The NDJSON reader is where a subtle bug would hide: chunk boundaries don't
 * respect line boundaries, so a naive implementation that parses each chunk
 * loses or duplicates events depending on how the network happened to split
 * them. That is untestable in production and trivial here.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { readNdjson, ApiError } from "../src/api.js";

/** A ReadableStream that emits exactly the chunks given, as bytes. */
function streamOf(chunks) {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

async function collectEvents(chunks) {
  const out = [];
  for await (const msg of readNdjson(streamOf(chunks))) out.push(msg);
  return out;
}

test("parses one object per line", async () => {
  const events = await collectEvents([
    '{"type":"step","id":"read"}\n',
    '{"type":"result","data":{"ok":true}}\n',
  ]);
  assert.equal(events.length, 2);
  assert.equal(events[1].data.ok, true);
});

test("an object split across chunks is not lost", async () => {
  // The failure mode this exists to prevent: the JSON arrives in three pieces,
  // none of which parses alone.
  const events = await collectEvents(['{"type":"st', 'ep","id":"pu', 'sh"}\n']);
  assert.deepEqual(events, [{ type: "step", id: "push" }]);
});

test("several objects in one chunk all arrive", async () => {
  const events = await collectEvents(['{"a":1}\n{"a":2}\n{"a":3}\n']);
  assert.deepEqual(events.map((e) => e.a), [1, 2, 3]);
});

test("a final line with no trailing newline is still parsed", async () => {
  // The server's last event is the one that carries the result, so dropping an
  // unterminated final line would turn every successful run into a failure.
  const events = await collectEvents(['{"type":"result","data":{"n":1}}']);
  assert.equal(events.length, 1);
  assert.equal(events[0].data.n, 1);
});

test("blank lines and garbage are skipped, not fatal", async () => {
  const events = await collectEvents(['\n{"a":1}\n\nnot json\n{"a":2}\n']);
  assert.deepEqual(events.map((e) => e.a), [1, 2]);
});

test("multi-byte characters split across a chunk boundary survive", async () => {
  // TextDecoder({stream:true}) holds the partial code point; without that flag
  // the character becomes U+FFFD and the JSON may still parse — silently wrong.
  const encoder = new TextEncoder();
  const bytes = encoder.encode('{"s":"café"}\n');
  const cut = bytes.indexOf(0xc3); // the first byte of "é"
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(bytes.slice(0, cut + 1));
      controller.enqueue(bytes.slice(cut + 1));
      controller.close();
    },
  });
  const out = [];
  for await (const msg of readNdjson(stream)) out.push(msg);
  assert.equal(out[0].s, "café");
});

test("ApiError renders our structured payloads as prose", () => {
  // The unsupported-project 422. Without this the user reads a wall of braces.
  const err = new ApiError(422, {
    code: "backend_only",
    reason: "This looks like a backend or API-only project.",
    supported: "Testra writes browser-based E2E tests.",
    suggestion: "Point it at the frontend directory.",
  }, "fallback");
  assert.match(err.message, /backend or API-only/);
  assert.match(err.message, /frontend directory/);
  assert.doesNotMatch(err.message, /[{}]/);
});

test("ApiError flattens Pydantic validation arrays", () => {
  const err = new ApiError(422, [{ loc: ["body", "x"], msg: "field required" }], "fallback");
  assert.equal(err.message, "field required");
});

test("ApiError falls back when there is no detail", () => {
  assert.equal(new ApiError(500, null, "Something broke").message, "Something broke");
});
