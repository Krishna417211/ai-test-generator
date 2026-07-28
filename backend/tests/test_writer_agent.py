"""test_writer_agent.py — Unit tests for the Writer Agent's pure logic."""

import asyncio
import json

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


class TestSuitePlacementIsSafe:
    """`_place_in_suite_dir` is the single gate every model-authored filename
    passes through, so it is where they have to be made safe.

    Prefixing with the suite directory is not a containment mechanism: git
    normalises `e2e/../../src/App.tsx` right back out of the suite and onto the
    user's application code.
    """

    def _place(self, names):
        w = WriterAgent()
        w._failed_files = []
        placed = w._place_in_suite_dir(
            [GeneratedFile(n, "content", "d") for n in names], "playwright_js")
        return [f.filename for f in placed], w._failed_files

    def test_ordinary_files_land_under_the_suite_directory(self):
        out, failed = self._place(["tests/login.spec.ts", "tests/pages/LoginPage.ts"])
        assert out == ["e2e/tests/login.spec.ts", "e2e/tests/pages/LoginPage.ts"]
        assert failed == []

    def test_traversal_never_escapes_the_suite(self):
        out, failed = self._place(["../../src/App.tsx", "tests/ok.spec.ts"])
        assert out == ["e2e/tests/ok.spec.ts"]
        # Recorded as failed, not merely dropped: the specs importing it are now
        # dangling, and failed_files is what withholds CI and warns the user.
        assert failed == ["../../src/App.tsx"]

    def test_absolute_path_is_pulled_back_into_the_suite(self):
        out, _ = self._place(["/tests/login.spec.ts"])
        assert out == ["e2e/tests/login.spec.ts"]

    def test_duplicate_names_keep_both_files(self):
        """Last-write-wins loses a spec and still reports the full count."""
        out, _ = self._place(["tests/a.spec.ts", "tests/a.spec.ts"])
        # ".spec." survives the rename — it's what the runner discovers on.
        assert out == ["e2e/tests/a.spec.ts", "e2e/tests/a-2.spec.ts"]

    def test_scaffold_owned_files_are_still_dropped(self):
        out, failed = self._place(["package.json", "tests/a.spec.ts"])
        assert out == ["e2e/tests/a.spec.ts"]
        # Ours winning is by design, not a generation failure.
        assert failed == []

    def test_files_already_under_the_suite_dir_are_not_nested_twice(self):
        out, _ = self._place(["e2e/tests/a.spec.ts"])
        assert out == ["e2e/tests/a.spec.ts"]


class TestHealNeverMakesThingsWorse:
    """Self-heal is an improvement pass, so its contract is "better, or
    unchanged".

    It used to be able to make a suite *worse*: `_parse_response` answers invalid
    JSON with a single synthetic "raw output" file, that value is truthy, and
    `return healed or files` therefore replaced a working suite with the model's
    unparsed reply. The run then reported success while shipping one unparseable
    file where the tests had been — which is exactly what happened generating a
    suite for a real site.
    """

    GOOD = [
        GeneratedFile("tests/login.spec.ts", "test('a', () => {});", "Spec"),
        GeneratedFile("tests/pages/LoginPage.ts", "export class LoginPage {}", "Page object"),
    ]

    def _heal_returning(self, monkeypatch, raw: str):
        import agents.writer_agent as wa

        async def fake_complete(**kwargs):
            return raw
        monkeypatch.setattr(wa.router, "complete", fake_complete)

        w = WriterAgent()
        failing = [type("V", (), {"filename": "tests/login.spec.ts", "error": "boom"})()]
        return asyncio.run(w._heal(list(self.GOOD), failing, "playwright_js"))

    def test_unparseable_heal_leaves_the_suite_alone(self, monkeypatch):
        out = self._heal_returning(monkeypatch, '{"files": [{"filename": "x.ts", "cont')
        assert [f.filename for f in out] == [f.filename for f in self.GOOD]
        assert not any("JSON parsing failed" in f.description for f in out)

    def test_a_valid_heal_is_applied(self, monkeypatch):
        raw = json.dumps({"files": [
            {"filename": "tests/login.spec.ts", "description": "Spec",
             "content": "test('fixed', () => {});"},
        ]})
        out = self._heal_returning(monkeypatch, raw)
        assert len(out) == 1
        assert "fixed" in out[0].content

    def test_an_empty_heal_leaves_the_suite_alone(self, monkeypatch):
        out = self._heal_returning(monkeypatch, '{"files": []}')
        assert [f.filename for f in out] == [f.filename for f in self.GOOD]


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
