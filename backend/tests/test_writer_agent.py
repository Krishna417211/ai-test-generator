"""test_writer_agent.py — Unit tests for the Writer Agent's pure logic."""

from agents.writer_agent import WriterAgent, GeneratedFile


class TestSelectorExtraction:
    def test_extracts_all_selector_kinds(self):
        w = WriterAgent()
        src = {"a.html": '<input id="email" name="user" data-testid="btn" class="c1 c2">'}
        sel = w._extract_available_selectors(src)
        assert sel["ids"] == ["email"]
        assert sel["testids"] == ["btn"]
        assert sel["names"] == ["user"]
        assert set(sel["classes"]) == {"c1", "c2"}


class TestValidateSelectors:
    def test_flags_only_fakes(self):
        w = WriterAgent()
        src = {"a.html": '<div id="real" class="ok" data-testid="t1"></div>'}
        gf = [GeneratedFile(
            "x.ts",
            'locator("#real"); locator("#fake"); getByTestId("t1"); '
            'getByTestId("t2"); locator(".ok"); locator(".nope")',
            "",
        )]
        warns = "\n".join(w._validate_selectors(gf, src))
        assert "fake" in warns and "t2" in warns and "nope" in warns
        assert "#real" not in warns and "`t1`" not in warns and "`.ok`" not in warns


class TestNormalizeFramework:
    def test_maps_framework_and_language(self):
        w = WriterAgent()
        assert w._normalize_framework("playwright", "typescript") == "playwright_js"
        assert w._normalize_framework("playwright", "python") == "playwright_python"
        assert w._normalize_framework("cypress", "javascript") == "cypress_js"
        assert w._normalize_framework("selenium", "python") == "selenium_python"
        assert w._normalize_framework("selenium", "java") == "selenium_java"
        assert w._normalize_framework("nonsense", "x") == "playwright_js"


class TestParseResponse:
    def test_valid_json(self):
        w = WriterAgent()
        raw = '{"files":[{"filename":"a.ts","content":"x","description":"d"}]}'
        files = w._parse_response(raw, "playwright_js")
        assert len(files) == 1 and files[0].filename == "a.ts"

    def test_markdown_fenced_json(self):
        w = WriterAgent()
        raw = '```json\n{"files":[{"filename":"a.ts","content":"x"}]}\n```'
        files = w._parse_response(raw, "playwright_js")
        assert files[0].filename == "a.ts"

    def test_fallback_on_bad_json(self):
        w = WriterAgent()
        files = w._parse_response("not json at all", "playwright_python")
        assert len(files) == 1 and files[0].filename.endswith(".py")


class TestCountTests:
    def test_counts_js_and_python(self):
        w = WriterAgent()
        gf = [GeneratedFile("s.ts", 'test("a",()=>{}); test("b",()=>{}); it("c",()=>{})', "")]
        assert w._count_tests(gf, "playwright_js") == 3
        gf2 = [GeneratedFile("s.py", "def test_a(): pass\ndef test_b(): pass", "")]
        assert w._count_tests(gf2, "selenium_python") == 2


class TestTruncationDetection:
    def test_empty_is_truncated(self):
        assert WriterAgent()._looks_truncated([]) is True

    def test_json_parse_fallback_is_truncated(self):
        # The single-file fallback _parse_response emits on a truncated JSON stream.
        gf = [GeneratedFile("tests/generated.ts", "half a fi", "Generated test file (raw output — JSON parsing failed)")]
        assert WriterAgent()._looks_truncated(gf) is True

    def test_real_suite_is_not_truncated(self):
        gf = [GeneratedFile("a.ts", "x", "Page object"), GeneratedFile("b.ts", "y", "Spec")]
        assert WriterAgent()._looks_truncated(gf) is False


class TestStripFence:
    def test_strips_json_and_lang_fences(self):
        w = WriterAgent()
        assert w._strip_fence("```json\n{\"a\":1}\n```") == '{"a":1}'
        assert w._strip_fence("```ts\nconst a = 1;\n```") == "const a = 1;"
        assert w._strip_fence("no fence here") == "no fence here"
