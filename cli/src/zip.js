/**
 * zip.js — A minimal ZIP writer, built on Node's own zlib.
 *
 * Why hand-rolled rather than `archiver` or `jszip`: this package has zero
 * dependencies, and that is a feature rather than asceticism. `npx testra`
 * should start instantly, and a tool whose entire pitch is "we don't send your
 * secrets anywhere" is a poor place to introduce a transitive dependency tree
 * nobody has read. Writing a ZIP is a small, fully specified problem — we only
 * need the encoder, single-disk, no zip64 — so the honest trade is a hundred
 * lines here against an install nobody can audit.
 *
 * Correctness is not taken on trust: backend/tests/test_cli_zip.py builds an
 * archive with this and extracts it with the server's own `extract_zip()`,
 * asserting the round-trip is byte-identical. If this file is wrong, that test
 * fails against the real consumer.
 *
 * Deliberately not supported (and asserted against, not silently mis-encoded):
 *   • zip64 — archives are capped well below 4 GB by the server's upload limit
 *   • encryption, multi-disk, directory entries (the server ignores them)
 */

import zlib from "node:zlib";

// CRC-32 (IEEE 802.3), table-built on first use. `zlib.crc32` exists only from
// Node 20.15 / 22.2, and this package supports 18.
let CRC_TABLE = null;

function crcTable() {
  if (CRC_TABLE) return CRC_TABLE;
  CRC_TABLE = new Int32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    CRC_TABLE[n] = c;
  }
  return CRC_TABLE;
}

export function crc32(buf) {
  const table = crcTable();
  let c = 0 ^ -1;
  for (let i = 0; i < buf.length; i++) c = (c >>> 8) ^ table[(c ^ buf[i]) & 0xff];
  return (c ^ -1) >>> 0;
}

/** DOS date/time. Pre-1980 timestamps can't be represented, so clamp rather
 *  than emit a negative year that some readers reject outright. */
function dosDateTime(date) {
  const y = Math.max(1980, date.getFullYear());
  const time =
    (date.getHours() << 11) | (date.getMinutes() << 5) | (date.getSeconds() >> 1);
  const day = ((y - 1980) << 9) | ((date.getMonth() + 1) << 5) | date.getDate();
  return { time, day };
}

const LOCAL_SIG = 0x04034b50;
const CENTRAL_SIG = 0x02014b50;
const EOCD_SIG = 0x06054b50;

// Bit 11 declares the filename is UTF-8. Without it, a reader is entitled to
// decode names as CP437, which mangles any non-ASCII path.
const FLAG_UTF8 = 0x0800;

const METHOD_STORE = 0;
const METHOD_DEFLATE = 8;

/**
 * Build a ZIP archive.
 *
 * @param {Array<{path: string, content: Buffer|string}>} entries
 * @param {Date} [mtime] fixed timestamp for every entry — defaults to now.
 *        Passing one makes the output byte-reproducible, which is what lets the
 *        tests compare archives at all.
 * @returns {Buffer}
 */
export function makeZip(entries, mtime = new Date()) {
  const { time, day } = dosDateTime(mtime);
  const locals = [];
  const centrals = [];
  let offset = 0;

  for (const entry of entries) {
    const nameBuf = Buffer.from(entry.path, "utf8");
    const raw = Buffer.isBuffer(entry.content)
      ? entry.content
      : Buffer.from(entry.content, "utf8");

    // Deflate, unless it made things bigger — which it reliably does for tiny
    // or already-compressed files, and a ZIP entry larger than its input is
    // just waste on the wire.
    const deflated = zlib.deflateRawSync(raw, { level: 9 });
    const useDeflate = deflated.length < raw.length;
    const body = useDeflate ? deflated : raw;
    const method = useDeflate ? METHOD_DEFLATE : METHOD_STORE;
    const crc = crc32(raw);

    const local = Buffer.alloc(30);
    local.writeUInt32LE(LOCAL_SIG, 0);
    local.writeUInt16LE(20, 4); // version needed
    local.writeUInt16LE(FLAG_UTF8, 6);
    local.writeUInt16LE(method, 8);
    local.writeUInt16LE(time, 10);
    local.writeUInt16LE(day, 12);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(body.length, 18);
    local.writeUInt32LE(raw.length, 22);
    local.writeUInt16LE(nameBuf.length, 26);
    local.writeUInt16LE(0, 28); // extra field length
    locals.push(local, nameBuf, body);

    const central = Buffer.alloc(46);
    central.writeUInt32LE(CENTRAL_SIG, 0);
    central.writeUInt16LE(20, 4); // version made by
    central.writeUInt16LE(20, 6); // version needed
    central.writeUInt16LE(FLAG_UTF8, 8);
    central.writeUInt16LE(method, 10);
    central.writeUInt16LE(time, 12);
    central.writeUInt16LE(day, 14);
    central.writeUInt32LE(crc, 16);
    central.writeUInt32LE(body.length, 20);
    central.writeUInt32LE(raw.length, 24);
    central.writeUInt16LE(nameBuf.length, 28);
    central.writeUInt16LE(0, 30); // extra
    central.writeUInt16LE(0, 32); // comment
    central.writeUInt16LE(0, 34); // disk number start
    central.writeUInt16LE(0, 36); // internal attrs
    // Unix mode 0100644 in the high 16 bits. `<< 16` alone overflows: JS
    // bitwise operators work on SIGNED 32-bit ints, and 0o100644 << 16 comes
    // back as -2119958528, which writeUInt32LE rejects outright. `>>> 0`
    // reinterprets it as unsigned.
    central.writeUInt32LE((0o100644 << 16) >>> 0, 38);
    central.writeUInt32LE(offset, 42);
    centrals.push(central, nameBuf);

    offset += local.length + nameBuf.length + body.length;
  }

  const centralBuf = Buffer.concat(centrals);
  const count = entries.length;

  // The one limit worth failing loudly on: past 65535 entries or 4 GB the
  // 16/32-bit fields wrap, and a wrapped archive is not a corrupt-looking file
  // — it is a plausible one that silently contains the wrong thing.
  if (count > 0xffff) {
    throw new Error(
      `Too many files for a non-zip64 archive (${count}); narrow the directory or add ignores.`
    );
  }
  if (offset + centralBuf.length > 0xffffffff) {
    throw new Error("Archive exceeds 4 GB, which needs zip64 support this writer lacks.");
  }

  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(EOCD_SIG, 0);
  eocd.writeUInt16LE(0, 4); // this disk
  eocd.writeUInt16LE(0, 6); // disk with central directory
  eocd.writeUInt16LE(count, 8);
  eocd.writeUInt16LE(count, 10);
  eocd.writeUInt32LE(centralBuf.length, 12);
  eocd.writeUInt32LE(offset, 16);
  eocd.writeUInt16LE(0, 20); // comment length

  return Buffer.concat([...locals, centralBuf, eocd]);
}
