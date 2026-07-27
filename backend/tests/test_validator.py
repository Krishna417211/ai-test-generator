"""test_validator.py — Unit tests for generated-test static validation."""

from dataclasses import dataclass

from services.validator import (
    validate_files, _validate_structural, _validate_field_init_order,
)


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


class TestStructuralComments:
    """Comments are skipped before the bracket scan.

    Regression: an apostrophe in prose opened a string that never closed, which
    inverted the scanner — every real string literal after it read as code, and
    brackets inside them were counted. One contraction failed a whole valid file.
    Since .ts gets no other check and is the default output, that surfaced to
    users as "N/M files passed syntax validation" on correct code, and drove the
    self-heal loop to spend LLM calls "fixing" it.
    """

    PAD = "\n" + "// pad\n" * 5  # clears the short-output guard

    def test_apostrophe_in_line_comment_is_not_a_string(self):
        ok, err = _validate_structural("// we don't wait here\nfunction f() { return 1; }" + self.PAD)
        assert ok, err

    def test_generated_playwright_config_validates(self):
        # The exact file that regressed: its comments say "can't" and "you're".
        from agents import scaffold

        cfg = scaffold.playwright_config(
            scaffold.plan_target("Static HTML/JS (multi-page)", "http://localhost:3000")
        )
        ok, err = _validate_structural(cfg)
        assert ok, err

    def test_brackets_inside_comments_ignored(self):
        ok, err = _validate_structural("/* { unclosed in prose */\nfunction f() { return 1; }" + self.PAD)
        assert ok, err

    def test_double_slash_inside_a_string_is_not_a_comment(self):
        # Would otherwise comment out the rest of the line and lose its brackets.
        ok, err = _validate_structural('const u = "http://x.dev/a";\nfunction f() { return 1; }' + self.PAD)
        assert ok, err

    def test_escaped_quote_does_not_close_the_string(self):
        ok, err = _validate_structural(r"const s = 'it\'s'; function f() { return 1; }" + self.PAD)
        assert ok, err

    def test_literal_backslash_does_not_escape_the_closing_quote(self):
        ok, err = _validate_structural(r"const s = 'a\\'; function f() { return 1; }" + self.PAD)
        assert ok, err

    def test_division_is_not_a_comment(self):
        ok, err = _validate_structural("const r = a / b / c;\nfunction f() { return 1; }" + self.PAD)
        assert ok, err

    # ── still catches real breakage ──

    def test_truncation_still_caught(self):
        ok, _ = _validate_structural("test('a', async () => {\n  const x = 1;" + self.PAD)
        assert not ok

    def test_mismatched_bracket_still_caught(self):
        ok, _ = _validate_structural("function f() { return [1, 2); }" + self.PAD)
        assert not ok

    def test_unterminated_block_comment_caught(self):
        ok, err = _validate_structural("/* never closed\nfunction f() { return 1; }" + self.PAD)
        assert not ok
        assert "block comment" in err


class TestDanglingImports:
    """A spec importing a page object nobody generated cannot run.

    Every file parses, so the per-file checks all pass and the suite reports
    valid — then `npx playwright test` dies on "Cannot find module" before a
    single test executes. Reachable whenever a planned file fails to generate
    (see WriterResult.failed_files) while the spec importing it succeeds.
    """

    @dataclass
    class F:
        filename: str
        content: str

    SPEC = ("import { test } from '@playwright/test';\n"
            "import { LoginPage } from '../pages/LoginPage';\n"
            "test('signs in', async ({ page }) => { new LoginPage(page); });\n")

    def test_missing_page_object_fails_the_spec(self):
        results = validate_files([self.F("e2e/tests/specs/login.spec.ts", self.SPEC)])
        assert results[0].ok is False
        assert "../pages/LoginPage" in results[0].error

    def test_complete_suite_passes(self):
        results = validate_files([
            self.F("e2e/tests/specs/login.spec.ts", self.SPEC),
            self.F("e2e/tests/pages/LoginPage.ts", "export class LoginPage {}\n"),
        ])
        assert all(r.ok for r in results), [r.error for r in results]

    def test_extensionless_import_resolves_to_the_ts_file(self):
        """`'../pages/LoginPage'` must match `pages/LoginPage.ts` — resolving
        only exact strings would fail every real suite."""
        results = validate_files([
            self.F("e2e/tests/specs/a.spec.ts",
                   "import { P } from '../pages/LoginPage';\ntest('x', () => {});\n"),
            self.F("e2e/tests/pages/LoginPage.ts", "export class P {}\n"),
        ])
        assert results[0].ok is True, results[0].error

    def test_package_imports_are_never_flagged(self):
        """Bare specifiers are the package manager's job, not ours."""
        results = validate_files([
            self.F("e2e/tests/specs/a.spec.ts",
                   "import { test, expect } from '@playwright/test';\n"
                   "import fs from 'fs';\ntest('x', () => {});\n"),
        ])
        assert results[0].ok is True, results[0].error

    def test_require_style_is_caught_too(self):
        results = validate_files([
            self.F("e2e/tests/specs/a.spec.js",
                   "const { P } = require('../pages/Missing');\ntest('x', () => {});\n"),
        ])
        assert results[0].ok is False
        assert "../pages/Missing" in results[0].error


class TestFieldInitOrder:
    """Syntactically perfect code that throws on the first line of the first test.

    The Page Object Model this product emits is exactly the shape that trips it:
    a `page` assigned in the constructor, and locator fields initialized from it.
    Field initializers run BEFORE the constructor body, so every such field
    throws "Cannot read properties of undefined". Nothing above catches it — the
    file parses — so a suite shipped graded "A, files parsed cleanly" and died on
    construction. Observed for real against a live site before this existed.
    """

    BROKEN = """
import { Page } from '@playwright/test';
export class LoginPage {
  readonly page: Page;
  constructor(page: Page) { this.page = page; }
  readonly emailField = this.page.locator('#email');
}
"""

    def test_catches_field_initialized_from_ctor_assigned_field(self):
        ok, err = _validate_field_init_order(self.BROKEN)
        assert not ok
        assert "emailField" in err and "this.page" in err

    def test_getter_is_fine(self):
        ok, err = _validate_field_init_order("""
export class LoginPage {
  readonly page: Page;
  constructor(page: Page) { this.page = page; }
  get emailField(): Locator { return this.page.locator('#email'); }
}
""")
        assert ok, err

    def test_assignment_inside_constructor_is_fine(self):
        ok, err = _validate_field_init_order("""
export class LoginPage {
  constructor(page) { this.page = page; this.emailField = this.page.locator('#email'); }
}
""")
        assert ok, err

    def test_field_not_depending_on_ctor_state_is_fine(self):
        ok, err = _validate_field_init_order("""
export class Cfg {
  readonly base = 'https://x.test';
  constructor(page) { this.page = page; }
}
""")
        assert ok, err

    def test_non_class_file_is_untouched(self):
        ok, err = _validate_field_init_order("export const x = 1;")
        assert ok, err

    def test_surfaces_through_validate_files(self):
        @dataclass
        class F:
            filename: str
            content: str

        results = validate_files([F("tests/pages/LoginPage.ts", self.BROKEN)])
        assert results[0].checked is True
        assert results[0].ok is False
