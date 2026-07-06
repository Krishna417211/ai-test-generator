"""test_validator.py — Unit tests for generated-test static validation."""

from dataclasses import dataclass

from services.validator import validate_files, _validate_structural


@dataclass
class F:
    filename: str
    content: str


class TestValidateFiles:
    def test_valid_python(self):
        r = validate_files([F("t.py", "def test_a():\n    assert True\n")])
        assert r[0].ok

    def test_invalid_python_flagged(self):
        r = validate_files([F("t.py", "def test_a(:\n")])
        assert not r[0].ok and r[0].error

    def test_truncated_ts_detected(self):
        r = validate_files([F("t.ts", 'test("a", async () => { await page.click(')])
        assert not r[0].ok

    def test_valid_ts(self):
        r = validate_files([F("t.ts", 'import {test} from "x"; test("a", () => { expect(1).toBe(1); });')])
        assert r[0].ok

    def test_non_code_files_skipped(self):
        r = validate_files([F("ci.yml", "name: CI"), F("README.md", "# hi there")])
        assert all(x.ok for x in r)


class TestStructural:
    def test_balanced(self):
        ok, _ = _validate_structural("function x() { return [1, 2, 3]; }")
        assert ok

    def test_unbalanced(self):
        ok, _ = _validate_structural("function x() { return [1, 2, 3;")
        assert not ok

    def test_brackets_inside_strings_ignored(self):
        ok, _ = _validate_structural('const s = "a { b [ c"; const y = 1;')
        assert ok
