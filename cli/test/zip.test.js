/**
 * zip.test.js — The ZIP writer, checked against a real reader.
 *
 * These tests prove the bytes are structurally right. The authoritative check
 * lives in backend/tests/test_cli_zip.py, which extracts an archive built here
 * with the server's own `extract_zip()` — the actual consumer. Both matter: this
 * one localises a fault to a field, that one proves the whole thing works.
 */

import test from "node:test";
import assert from "node:assert/strict";
import zlib from "node:zlib";

import { makeZip, crc32 } from "../src/zip.js";

const EOCD_SIG = 0x06054b50;

function readEocd(buf) {
  // Scan backwards for the signature, as any reader must — the EOCD is at a
  // fixed offset only when there is no archive comment.
  for (let i = buf.length - 22; i >= 0; i--) {
    if (buf.readUInt32LE(i) === EOCD_SIG) {
      return {
        entries: buf.readUInt16LE(i + 10),
        cdSize: buf.readUInt32LE(i + 12),
        cdOffset: buf.readUInt32LE(i + 16),
      };
    }
  }
  throw new Error("no EOCD found");
}

test("crc32 matches the known IEEE check value", () => {
  // The standard test vector: CRC-32 of "123456789" is 0xCBF43926.
  assert.equal(crc32(Buffer.from("123456789")), 0xcbf43926);
  assert.equal(crc32(Buffer.alloc(0)), 0);
});

test("central directory is located where the EOCD says it is", () => {
  const zip = makeZip([
    { path: "a.txt", content: "hello" },
    { path: "dir/b.txt", content: "world" },
  ]);
  const eocd = readEocd(zip);
  assert.equal(eocd.entries, 2);
  assert.equal(zip.readUInt32LE(eocd.cdOffset), 0x02014b50, "central sig at the stated offset");
  assert.equal(eocd.cdOffset + eocd.cdSize + 22, zip.length, "sizes account for the whole file");
});

test("stored and deflated entries both round-trip", () => {
  // "hi" compresses larger than it starts, so it must be STOREd; a long
  // repetitive string must be DEFLATEd. Both paths in one archive.
  const long = "abcabcabc".repeat(500);
  const zip = makeZip([
    { path: "small.txt", content: "hi" },
    { path: "big.txt", content: long },
  ]);

  const found = {};
  let off = 0;
  while (zip.readUInt32LE(off) === 0x04034b50) {
    const method = zip.readUInt16LE(off + 8);
    const csize = zip.readUInt32LE(off + 18);
    const usize = zip.readUInt32LE(off + 22);
    const nameLen = zip.readUInt16LE(off + 26);
    const name = zip.subarray(off + 30, off + 30 + nameLen).toString("utf8");
    const body = zip.subarray(off + 30 + nameLen, off + 30 + nameLen + csize);
    const plain = method === 8 ? zlib.inflateRawSync(body) : body;
    assert.equal(plain.length, usize, `${name}: uncompressed size field is right`);
    found[name] = { method, text: plain.toString("utf8") };
    off += 30 + nameLen + csize;
  }

  assert.equal(found["small.txt"].method, 0, "tiny input stays stored");
  assert.equal(found["small.txt"].text, "hi");
  assert.equal(found["big.txt"].method, 8, "compressible input is deflated");
  assert.equal(found["big.txt"].text, long);
});

test("non-ASCII paths are flagged as UTF-8", () => {
  // Without bit 11 a reader may decode the name as CP437 and mangle it.
  const zip = makeZip([{ path: "tests/café.spec.ts", content: "x" }]);
  assert.equal(zip.readUInt16LE(6) & 0x0800, 0x0800);
  const nameLen = zip.readUInt16LE(26);
  assert.equal(zip.subarray(30, 30 + nameLen).toString("utf8"), "tests/café.spec.ts");
});

test("output is reproducible when the timestamp is fixed", () => {
  // What makes a failed publish safe to retry identically.
  const when = new Date(Date.UTC(2020, 0, 1));
  const entries = [{ path: "a.txt", content: "hello" }];
  assert.deepEqual(makeZip(entries, when), makeZip(entries, when));
});

test("an empty archive is still a valid one", () => {
  const zip = makeZip([]);
  assert.equal(readEocd(zip).entries, 0);
  assert.equal(zip.length, 22);
});

test("pre-1980 timestamps are clamped rather than encoded negative", () => {
  // The DOS date field cannot represent them; a negative year is rejected by
  // some readers outright.
  const zip = makeZip([{ path: "a.txt", content: "x" }], new Date(Date.UTC(1970, 0, 1)));
  const dosDate = zip.readUInt16LE(12);
  assert.equal((dosDate >>> 9) + 1980, 1980, "1970 is clamped up to the DOS epoch");
});

test("too many entries fails loudly instead of wrapping the count", () => {
  // A wrapped count is not a corrupt-looking file — it is a plausible one
  // containing the wrong thing, which is far worse.
  const many = Array.from({ length: 0x10000 }, (_, i) => ({ path: `f${i}`, content: "x" }));
  assert.throws(() => makeZip(many), /Too many files/);
});
