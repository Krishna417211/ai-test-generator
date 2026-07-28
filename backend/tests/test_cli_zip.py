"""
test_cli_zip.py — The CLI's hand-rolled ZIP writer, checked against its consumer.

`cli/src/zip.js` encodes archives with no dependencies, which is a deliberate
trade (see its module comment) but only defensible if something proves it right.
Its own tests assert the byte layout; this one asserts the thing that actually
matters — that the server can read what the CLI writes.

The check runs the real path end to end: node builds an archive, Python's
`zipfile` validates it, and `services.file_extractor.extract_zip()` — the exact
function `/api/publish-zip` calls — extracts it. If the writer is wrong in a way
both test suites in `cli/test/` miss, it fails here, against the consumer.

Skipped rather than silently passed when node or the CLI directory is absent (the
backend CI job installs Python only). A test that quietly stops checking is worse
than one that says it didn't run.
"""

import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from services.file_extractor import extract_zip, filter_for_push

CLI_DIR = Path(__file__).resolve().parents[2] / "cli"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not (CLI_DIR / "src" / "zip.js").exists(),
    reason="needs node and the cli/ package",
)


def build_zip(entries: dict[str, str]) -> bytes:
    """Ask the CLI's writer for an archive of `entries`, as bytes.

    The payload goes in on stdin and the archive comes out on stdout as base64:
    writing raw bytes through a pipe invites newline translation, and a corrupted
    archive here would look exactly like a bug in the writer.
    """
    script = """
      import { makeZip } from './src/zip.js';
      const chunks = [];
      for await (const c of process.stdin) chunks.push(c);
      const entries = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      const zip = makeZip(entries, new Date(Date.UTC(2020, 0, 1)));
      process.stdout.write(zip.toString('base64'));
    """
    payload = json.dumps([{"path": p, "content": c} for p, c in entries.items()])
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        input=payload.encode("utf-8"),
        capture_output=True,
        cwd=CLI_DIR,
        timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(f"CLI zip writer failed: {proc.stderr.decode()[:600]}")
    import base64
    return base64.b64decode(proc.stdout)


class TestRoundTrip:
    def test_the_server_extracts_exactly_what_the_cli_packed(self):
        files = {
            "src/App.tsx": "export default () => <div id='root'>hi</div>;\n",
            "package.json": '{"name":"demo","dependencies":{"react":"^18"}}',
            "README.md": "# demo\n",
            # Deep path, and one that compresses well enough to take the
            # deflate branch rather than the stored one.
            "src/pages/very/deeply/nested/Page.tsx": "const x = 1;\n" * 500,
        }
        archive = build_zip(files)

        # Python's zipfile is a strict, independent reader: testzip() verifies
        # every CRC, so a wrong checksum field fails here rather than silently
        # producing plausible content.
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            assert zf.testzip() is None, "a CRC in the archive is wrong"
            assert sorted(zf.namelist()) == sorted(files)

        assert extract_zip(archive) == files

    def test_unicode_paths_and_content_survive(self):
        files = {
            "tests/café.spec.ts": "test('héllo wörld ✓', () => {});\n",
            "docs/日本語.md": "# 見出し\n",
        }
        archive = build_zip(files)
        assert extract_zip(archive) == files

    def test_empty_and_tiny_files_survive(self):
        # An empty file exercises the stored branch with a zero-length body,
        # where an off-by-one in the size fields would corrupt the next entry's
        # local header and cascade through the whole archive.
        files = {"empty.txt": "", "one.txt": "x", "after.txt": "still here\n"}
        archive = build_zip(files)
        assert extract_zip(archive) == files

    def test_crlf_and_tabs_are_not_translated(self):
        files = {"win.txt": "line one\r\nline two\r\n", "tabs.tsv": "a\tb\tc\n"}
        archive = build_zip(files)
        assert extract_zip(archive) == files

    def test_a_realistic_publish_survives_the_whole_pipeline(self):
        """CLI archive → extract_zip → filter_for_push, as publishing does.

        Ties the two halves together: the CLI already withholds credentials
        locally, and the server re-scans regardless (defence in depth). This
        proves the server's scan still fires on a CLI-built archive rather than
        being bypassed by some encoding difference.
        """
        archive = build_zip({
            "src/App.tsx": "<div/>",
            "package.json": "{}",
            ".env": "STRIPE_SECRET=sk_live_abcdefghijklmnopqrstuvwx",
        })
        raw = extract_zip(archive)
        kept, warnings, secrets = filter_for_push(raw)

        assert set(kept) == {"src/App.tsx", "package.json"}
        assert [s["path"] for s in secrets] == [".env"]
        assert any(".env" in w for w in warnings)


class TestWriterLimits:
    def test_a_zip_of_many_small_files_stays_valid(self):
        # 600 entries is well inside the non-zip64 limits but past the point
        # where a central-directory offset bug would still line up by luck.
        files = {f"src/mod{i:03d}.ts": f"export const v{i} = {i};\n" for i in range(600)}
        archive = build_zip(files)
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            assert zf.testzip() is None
            assert len(zf.namelist()) == 600
        assert extract_zip(archive) == files
