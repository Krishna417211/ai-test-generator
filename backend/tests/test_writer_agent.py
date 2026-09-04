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
    unchanged" — and the improvement is now *measured* rather than assumed: the
    repaired suite is re-validated and kept only if fewer files are invalid than
    before.

    Two ways it used to break that contract, both reachable on a real run:

    - It could make a suite worse. `_parse_response` answers invalid JSON with a
      single synthetic "raw output" file, that value is truthy, and
      `return healed or files` therefore replaced a working suite with the
      model's unparsed reply. The run then reported success while shipping one
      unparseable file where the tests had been.
    - It could lose files outright. Healing sent the whole suite and swapped in
      whatever came back, so a model that returned only the file it had been
      asked to fix silently deleted every other file.
    """

    BROKEN = (
        "import { test, expect } from '@playwright/test';\n"
        "test('logs in', async ({ page }) => {\n"
        "  await page.goto('login.html');\n"
        "  await expect(page.locator('#email')).toBeVisible();\n"
    )
    FIXED = BROKEN + "});\n"
    PAGE_OBJECT = "export class LoginPage {}"

    def _suite(self):
        return [
            GeneratedFile("tests/login.spec.ts", self.BROKEN, "Spec"),
            GeneratedFile("tests/pages/LoginPage.ts", self.PAGE_OBJECT, "Page object"),
        ]

    def _heal_returning(self, monkeypatch, raw: str):
        """Heal a two-file suite whose spec is genuinely invalid.

        Returns (healed_files, prompts_sent) — the prompts matter as much as the
        files, since one call per broken file carrying only that file is the
        whole point of the rewrite.
        """
        import agents.writer_agent as wa

        prompts: list[str] = []

        async def fake_complete(**kwargs):
            prompts.append(kwargs["prompt"])
            return raw
        monkeypatch.setattr(wa.router, "complete", fake_complete)

        w = WriterAgent()
        failing = [type("V", (), {
            "filename": "tests/login.spec.ts", "error": "unbalanced brackets",
        })()]
        out = asyncio.run(w._heal(self._suite(), failing, "playwright_js"))
        return out, prompts

    def test_a_valid_heal_is_applied(self, monkeypatch):
        # Compared stripped: `_strip_fence` trims surrounding whitespace off
        # every model reply, healed files included.
        out, _ = self._heal_returning(monkeypatch, self.FIXED)
        assert out[0].content == self.FIXED.strip()

    def test_healing_one_file_does_not_drop_the_others(self, monkeypatch):
        """The file nobody asked about must still be there afterwards."""
        out, _ = self._heal_returning(monkeypatch, self.FIXED)
        assert [f.filename for f in out] == [
            "tests/login.spec.ts", "tests/pages/LoginPage.ts",
        ]
        assert out[1].content == self.PAGE_OBJECT

    def test_only_the_broken_file_is_sent_to_the_model(self, monkeypatch):
        """One call, carrying one file — not the whole suite.

        Sending the suite was what made healing the most expensive thing in the
        module, and on any real suite it could not work at all: the reply is
        capped at the provider's output limit, so it came back truncated and was
        discarded.
        """
        _, prompts = self._heal_returning(monkeypatch, self.FIXED)
        assert len(prompts) == 1
        assert self.BROKEN in prompts[0]
        assert self.PAGE_OBJECT not in prompts[0]

    def test_unparseable_heal_leaves_the_suite_alone(self, monkeypatch):
        out, _ = self._heal_returning(monkeypatch, '{"files": [{"filename": "x.ts", "cont')
        assert [f.content for f in out] == [self.BROKEN, self.PAGE_OBJECT]
        assert not any("JSON parsing failed" in f.description for f in out)

    def test_a_heal_that_does_not_fix_anything_is_rejected(self, monkeypatch):
        """Still-invalid output is not an improvement, so it is not kept.

        This is also what stops the caller's loop spinning: `_heal` hands back
        the very list it was given, and run() reads that identity as "no
        progress" and stops instead of asking the same question again.
        """
        out, _ = self._heal_returning(monkeypatch, self.BROKEN + "  await page.click('#go');\n")
        assert [f.content for f in out] == [self.BROKEN, self.PAGE_OBJECT]

    def test_an_empty_heal_leaves_the_suite_alone(self, monkeypatch):
        out, _ = self._heal_returning(monkeypatch, "")
        assert [f.content for f in out] == [self.BROKEN, self.PAGE_OBJECT]


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


class TestTheSuiteIsGeneratedOnce:
    """The stream IS the generation, not a preview of one.

    It used to ask the model for the whole suite as a single streamed JSON blob
    so the browser had something to render, and `/api/generate` then generated
    the same suite again file-by-file — because that blob had almost always been
    cut off at the provider's output-token cap and could not be parsed. Every
    run therefore paid for a large call whose output went straight in the bin.

    Both paths now share `_generate_tests_events`, and the finalise step reuses
    what the stream produced. These tests pin that: the model is asked for the
    suite exactly once across the two requests.
    """

    SPEC = ("import { test, expect } from '@playwright/test';\n"
            "test('logs in', async ({ page }) => {\n"
            "  await page.goto('login.html');\n"
            "});\n")

    def _fr(self):
        from agents.filter_agent import FilterResult
        return FilterResult(
            "A login page.", [], [], ["/login"], "static html", [],
            {"login.html": "<form><input id='email'></form>"}, 0, 1,
        )

    def _patched(self, monkeypatch):
        """Mock the router and record the context of every call it receives."""
        import agents.writer_agent as wa
        calls: list[str] = []

        async def fake_complete(**kwargs):
            calls.append(kwargs["context_hint"])
            if kwargs["context_hint"] == "writer_plan":
                return json.dumps({"files": [
                    {"filename": "tests/login.spec.ts",
                     "description": "Login flow", "kind": "spec"},
                ]})
            return self.SPEC
        monkeypatch.setattr(wa.router, "complete", fake_complete)
        return calls

    def _stream(self, agent, events_out):
        async def drive():
            async for ev in agent.stream_run(
                filter_result=self._fr(), framework="playwright",
                language="typescript", test_flows="log in",
                base_url="https://app.example",
            ):
                events_out.append(ev)
        asyncio.run(drive())

    def test_streaming_returns_the_files_it_generated(self, monkeypatch):
        self._patched(monkeypatch)
        events: list[dict] = []
        self._stream(WriterAgent(), events)

        result = events[-1]
        assert result["type"] == "result"
        assert [f.filename for f in result["files"]] == ["tests/login.spec.ts"]
        assert result["failed"] == []

    def test_streaming_reports_progress_per_file(self, monkeypatch):
        """The status events are what stop a slow run looking like a hung one."""
        self._patched(monkeypatch)
        events: list[dict] = []
        self._stream(WriterAgent(), events)

        statuses = [e["message"] for e in events if e["type"] == "status"]
        assert any("Planning" in s for s in statuses)
        assert any("tests/login.spec.ts" in s for s in statuses)
        assert any(e["type"] == "chunk" for e in events)

    def test_finalising_reuses_the_streamed_suite_without_regenerating(self, monkeypatch):
        calls = self._patched(monkeypatch)
        events: list[dict] = []
        self._stream(WriterAgent(), events)

        generated = events[-1]["files"]
        after_stream = list(calls)
        assert "writer_plan" in after_stream and "writer_file" in after_stream

        # Exactly the envelope main.py hands to /api/generate.
        pregenerated = json.dumps({"files": [
            {"filename": f.filename, "description": f.description, "content": f.content}
            for f in generated
        ]})

        result = asyncio.run(WriterAgent().run(
            filter_result=self._fr(), framework="playwright", language="typescript",
            test_flows="log in", base_url="https://app.example",
            pregenerated_raw=pregenerated, pregenerated_failed=[],
        ))

        assert calls == after_stream, (
            "finalising re-asked the model for a suite the stream had already "
            f"written (extra calls: {calls[len(after_stream):]})"
        )
        assert any(f.filename.endswith("login.spec.ts") for f in result.files)

    def test_files_the_stream_could_not_write_still_withhold_ci(self, monkeypatch):
        """A partial suite must not be finalised as a whole one.

        `failed_files` is what withholds the CI workflow. It lives on the run
        that discovered it, so if the finalise step doesn't carry it across, a
        suite missing a spec ships a pipeline that can only go red.
        """
        self._patched(monkeypatch)
        result = asyncio.run(WriterAgent().run(
            filter_result=self._fr(), framework="playwright", language="typescript",
            test_flows="log in", base_url="https://app.example", include_ci=True,
            ci_as_files=True,
            pregenerated_raw=json.dumps({"files": [
                {"filename": "tests/login.spec.ts", "description": "Login",
                 "content": self.SPEC},
            ]}),
            pregenerated_failed=["tests/checkout.spec.ts"],
        ))

        assert result.failed_files == ["tests/checkout.spec.ts"]
        assert not any(".github/workflows" in f.filename for f in result.files)


class TestOneFilePromptSharesAStablePrefix:
    """Every per-file call in a run must start with the same bytes.

    Providers cache on a stable prefix, and the suite context — project summary,
    routes, navigation rules, selectors, source excerpt — is several thousand
    tokens of it, identical for every file. It used to sit *behind* the filename,
    so the first line differed on every call and none of it could be cached.
    """

    def _fr(self):
        from agents.filter_agent import FilterResult
        return FilterResult(
            "A login page.", [], [], ["/login"], "static html", [],
            {"login.html": "<form><input id='email' data-testid='email-field'></form>"},
            0, 1,
        )

    def _prompts(self, monkeypatch):
        import agents.writer_agent as wa
        seen: list[str] = []

        async def fake_complete(**kwargs):
            seen.append(kwargs["prompt"])
            return "x"
        monkeypatch.setattr(wa.router, "complete", fake_complete)

        w = WriterAgent()
        manifest = ["tests/login.spec.ts", "tests/pages/LoginPage.ts"]
        ctx = w._suite_context(self._fr(), "playwright_js", "log in",
                               "https://app.example", manifest)

        async def drive():
            for name in manifest:
                await w._generate_one_file(
                    {"filename": name, "description": "does a thing"},
                    ctx, "playwright_js",
                )
        asyncio.run(drive())
        return seen

    def test_the_shared_context_leads_every_prompt(self, monkeypatch):
        a, b = self._prompts(monkeypatch)
        shared = 0
        for x, y in zip(a, b):
            if x != y:
                break
            shared += 1
        # The differing tail is only the filename and its instructions; the
        # cacheable head is the overwhelming majority of the prompt.
        assert shared > 300, f"only {shared} shared leading chars"
        assert a[:shared] == b[:shared]
        assert shared > len(a) - shared, "the varying tail is larger than the cacheable head"

    def test_the_context_carries_the_grounding(self, monkeypatch):
        a, _ = self._prompts(monkeypatch)
        assert "AVAILABLE SELECTORS" in a
        assert "email-field" in a                  # the real testid, from source
        assert "ALL FILES IN THE SUITE" in a
        assert a.rstrip().endswith("test cases covering positive and negative paths.")
