"""test_writer_agent.py — Unit tests for the Writer Agent's pure logic."""

import pytest

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
    # One real + one invented selector of each kind (id, test-id, class).
    _SRC = {"a.html": '<div id="real" class="ok" data-testid="t1"></div>'}
    _GEN = [GeneratedFile(
        "x.ts",
        'locator("#real"); locator("#fake"); getByTestId("t1"); '
        'getByTestId("t2"); locator(".ok"); locator(".nope")',
        "",
    )]

    def test_flags_only_fakes(self):
        w = WriterAgent()
        warns, _, _ = w._validate_selectors(list(self._GEN), self._SRC)
        warns = "\n".join(warns)
        assert "fake" in warns and "t2" in warns and "nope" in warns
        assert "#real" not in warns and "`t1`" not in warns and "`.ok`" not in warns

    def test_counts_every_selector_checked_not_just_the_failures(self):
        """The counts are the denominator the UI quotes as grounding.

        Deriving them from the warnings would only ever see the failures, so
        they're taken where each selector is actually compared to the source.
        """
        w = WriterAgent()
        _, total, verified = w._validate_selectors(list(self._GEN), self._SRC)
        assert (total, verified) == (6, 3)   # 3 real, 3 invented

    def test_no_selectors_reports_nothing_rather_than_a_perfect_score(self):
        """A suite that referenced nothing has not earned 100%."""
        w = WriterAgent()
        gf = [GeneratedFile("x.ts", "test('noop', () => {});", "")]
        _, total, verified = w._validate_selectors(gf, self._SRC)
        assert (total, verified) == (0, 0)

        from agents.writer_agent import Grounding
        assert Grounding(selectors_total=0, selectors_verified=0).selector_rate is None

    # ── crawl-only: the live DOM is the ground truth ──
    #
    # A crawl-first run has no source files at all, so validating against source
    # marked every selector unverified: the results screen reported "0/N verified"
    # beside a panel showing N/N from the same run, and stamped
    # "⚠️ selector not verified" into code whose selectors were read straight off
    # the rendered page.

    def _dom(self):
        from services.grounding import (
            GroundIndex, Anchor, KIND_ID, KIND_TESTID, KIND_CLASS,
        )
        idx = GroundIndex(source="dom")
        idx.anchors = {Anchor(KIND_ID, "real"), Anchor(KIND_TESTID, "t1"),
                       Anchor(KIND_CLASS, "ok")}
        return idx

    def test_live_anchors_verify_when_there_is_no_source(self):
        w = WriterAgent()
        warns, total, verified = w._validate_selectors(
            list(self._GEN), {}, dom_index=self._dom()
        )
        assert (total, verified) == (6, 3)      # the 3 real ones verified off the DOM
        assert "not found on the live site" in "\n".join(warns)   # names what was checked

    def test_dom_and_source_are_merged_not_replaced(self):
        """A repo *and* a live URL: either ground truth may prove a selector."""
        from services.grounding import GroundIndex, Anchor, KIND_ID
        dom = GroundIndex(source="dom")
        dom.anchors = {Anchor(KIND_ID, "fake")}   # rendered, but absent from source
        w = WriterAgent()
        _, total, verified = w._validate_selectors(
            list(self._GEN), self._SRC, dom_index=dom
        )
        assert (total, verified) == (6, 4)        # 3 from source + #fake from the DOM

    def test_unverified_selector_is_still_flagged_in_crawl_mode(self):
        w = WriterAgent()
        gen = [GeneratedFile("x.ts", 'locator("#invented")', "")]
        warns, total, verified = w._validate_selectors(gen, {}, dom_index=self._dom())
        assert (total, verified) == (1, 0)
        assert "invented" in "\n".join(warns)


class TestPlanRetry:
    """The plan call gates the whole run — it must retry like the files do.

    Observed live: every per-file call had eight rate-limit retries while the
    plan call that produces the file list had none, so a momentary provider
    cooldown at exactly that step threw the generation away — after the crawl
    had already been paid for.
    """

    def _agent(self, monkeypatch):
        monkeypatch.setattr("agents.writer_agent.RATE_RETRY_WAIT_SECONDS", 0)
        return WriterAgent()

    def test_retries_until_a_provider_comes_back(self, monkeypatch):
        import asyncio
        from services.llm_router import AllProvidersExhausted
        w = self._agent(monkeypatch)
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise AllProvidersExhausted("cooling", reason="rate_limited")
            return [{"filename": "tests/specs/a.spec.ts"}]

        got = asyncio.run(w._retry_rate_limited("file plan", flaky))
        assert got == [{"filename": "tests/specs/a.spec.ts"}]
        assert calls["n"] == 3

    def test_gives_up_after_the_cap_rather_than_hanging(self, monkeypatch):
        import asyncio
        from services.llm_router import AllProvidersExhausted
        monkeypatch.setattr("agents.writer_agent.RATE_RETRY_MAX_ATTEMPTS", 2)
        w = self._agent(monkeypatch)

        async def always_dry():
            raise AllProvidersExhausted("out of credit", reason="quota_exhausted")

        with pytest.raises(AllProvidersExhausted):
            asyncio.run(w._retry_rate_limited("file plan", always_dry))

    def test_does_not_retry_a_non_rate_limit_error(self, monkeypatch):
        """Waiting doesn't fix bad JSON or an oversized prompt — fail fast."""
        import asyncio
        w = self._agent(monkeypatch)
        calls = {"n": 0}

        async def bad_request():
            calls["n"] += 1
            raise ValueError("malformed response")

        with pytest.raises(ValueError):
            asyncio.run(w._retry_rate_limited("file plan", bad_request))
        assert calls["n"] == 1


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


class TestCrawlOnlyPrompt:
    """When a crawl index is set, the prompt is built from live anchors and the
    source-code blob is dropped — the token-saving, higher-quality path."""

    def _fr(self):
        from agents.filter_agent import FilterResult
        # Non-empty files: proves the switch is the crawl index, not empty files.
        return FilterResult("app", [], [], ["/login"], "react", [],
                            {"src/Login.tsx": "<form id=src-only>"}, 0, 1)

    def _index(self):
        from services.grounding import GroundIndex, Anchor, KIND_ROLE, KIND_TEXT
        idx = GroundIndex(source="dom")
        idx.anchors = {Anchor(KIND_ROLE, "button"), Anchor(KIND_TEXT, "Log in")}
        return idx

    def test_crawl_only_omits_source_and_uses_live_anchors(self):
        w = WriterAgent()
        w._crawl_index = self._index()
        p = w._build_prompt(self._fr(), "playwright_ts", "login", "https://app.example", "typescript")
        assert "SOURCE CODE FILES" not in p          # no source blob
        assert "src-only" not in p                    # source markup not leaked
        assert "rendered from the LIVE site" in p     # crawl heading
        assert "Log in" in p and "button" in p        # live anchors present

    def test_default_path_still_embeds_source(self):
        w = WriterAgent()                             # no crawl index
        p = w._build_prompt(self._fr(), "playwright_ts", "login", "https://app.example", "typescript")
        assert "SOURCE CODE FILES" in p
        assert "src-only" in p
