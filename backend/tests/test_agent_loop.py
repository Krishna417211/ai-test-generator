"""test_agent_loop.py — the agentic writer path: toolbox, loop, and fallback.

The loop is the part with teeth. Agent mode is an *upgrade* to how a suite is
produced, so every way it can go wrong has to land on the scripted pipeline or a
partial-but-honest result — never on a user with no tests and no explanation.
"""

import asyncio

import pytest

from services import agent_loop, grounding
from services.agent_tools import Toolbox, TOOL_SCHEMAS, MAX_WRITE_CHARS
from services.llm_router import LLMRouter, Tier, Provider, model_for, AllProvidersExhausted


SRC = {
    "src/pages/Login.tsx": (
        '<form id="login-form">\n'
        '  <input data-testid="email" name="email" />\n'
        '  <button class="btn-primary">Sign in</button>\n'
        '</form>\n'
    ),
    "src/pages/Dashboard.tsx": '<div id="dash" class="grid">Welcome</div>\n',
}


def _box(**kw) -> Toolbox:
    return Toolbox(
        source_files=kw.pop("source_files", SRC),
        framework_key=kw.pop("framework_key", "playwright_ts"),
        src_index=kw.pop("src_index", grounding.build_source_index(SRC)),
        **kw,
    )


# ─────────────────────────────────────────────
# Toolbox
# ─────────────────────────────────────────────

class TestSearchAndRead:
    def test_search_ranks_a_path_match_above_a_body_mention(self):
        """A query for 'login' wants Login.tsx first, not whatever merely
        mentions signing in — the model reads the top hit and stops."""
        src = {
            "src/pages/Login.tsx": '<form id="login-form"></form>',
            "src/util/notes.ts": "// remember to login before running this",
        }
        out = Toolbox(src, "playwright_ts").run("search_source", {"query": "login"})
        assert out.index("src/pages/Login.tsx") < out.index("src/util/notes.ts")

    def test_search_returns_the_matching_lines_not_just_filenames(self):
        out = _box().run("search_source", {"query": "email"})
        assert "data-testid" in out

    def test_search_with_no_match_points_at_list_files(self):
        out = _box().run("search_source", {"query": "kubernetes"})
        assert "No file matched" in out and "list_files" in out

    def test_read_file_returns_content(self):
        out = _box().run("read_file", {"path": "src/pages/Login.tsx"})
        assert "login-form" in out

    def test_a_guessed_path_gets_the_near_miss_named(self):
        """The model guessing 'Login.tsx' should not cost a whole wasted turn."""
        out = _box().run("read_file", {"path": "Login.tsx"})
        assert "Did you mean" in out and "src/pages/Login.tsx" in out

    def test_read_file_truncates_a_huge_file_and_says_so(self):
        big = {"big.tsx": "x" * 50_000}
        out = Toolbox(big, "playwright_ts").run("read_file", {"path": "big.tsx"})
        assert "truncated" in out and len(out) < 30_000

    def test_list_files_without_source_sends_the_model_to_the_dom(self):
        """Crawl-only runs have no repo source — say so instead of returning nothing."""
        out = Toolbox({}, "playwright_ts").run("list_files", {})
        assert "find_anchors" in out


class TestFindAnchors:
    def test_reports_real_anchors_with_provenance(self):
        out = _box().run("find_anchors", {"kind": "testid"})
        assert "'email'" in out
        assert "src/pages/Login.tsx:" in out       # cites file:line, not "trust me"

    def test_filters_by_substring(self):
        out = _box().run("find_anchors", {"contains": "login"})
        assert "login-form" in out
        assert "dash" not in out

    def test_a_selector_that_does_not_exist_is_reported_as_not_existing(self):
        """The whole point: the model must be told 'no', not given a near-match."""
        out = _box().run("find_anchors", {"contains": "checkout"})
        assert "No anchors found" in out

    def test_merges_source_and_dom_indexes(self):
        dom = grounding.build_dom_index('<button data-testid="only-live">go</button>')
        out = _box(dom_index=dom).run("find_anchors", {"kind": "testid"})
        assert "only-live" in out and "email" in out


class TestValidateAndWrite:
    def test_invalid_code_is_reported_with_the_real_error(self):
        box = _box(framework_key="playwright_python")
        out = box.run("validate_code", {"filename": "t.py", "content": "def broken(:"})
        assert "INVALID" in out

    def test_valid_code_passes(self):
        box = _box(framework_key="playwright_python")
        out = box.run("validate_code", {"filename": "t.py", "content": "x = 1\n"})
        assert "VALID" in out

    def test_validation_sees_the_files_already_written(self):
        """A spec importing a page object is only valid if that page object is
        part of *this* suite — which the validator can only know if the already
        written files are passed alongside the candidate."""
        box = _box()
        box.run("write_file", {
            "filename": "pages/LoginPage.ts",
            "description": "login page",
            "content": "export class LoginPage {}\n",
        })
        ok = box.run("validate_code", {
            "filename": "specs/login.spec.ts",
            "content": "import { LoginPage } from '../pages/LoginPage';\n",
        })
        assert "VALID" in ok

        missing = box.run("validate_code", {
            "filename": "specs/cart.spec.ts",
            "content": "import { CartPage } from '../pages/CartPage';\n",
        })
        assert "INVALID" in missing and "CartPage" in missing

    def test_write_file_refuses_empty_content(self):
        box = _box()
        assert "Refused" in box.run(
            "write_file", {"filename": "a.ts", "description": "d", "content": "  "})
        assert box.written == {}

    def test_write_file_refuses_a_runaway_file(self):
        box = _box()
        out = box.run("write_file", {
            "filename": "a.ts", "description": "d", "content": "x" * (MAX_WRITE_CHARS + 1)})
        assert "Refused" in out and box.written == {}

    def test_rewriting_a_filename_replaces_it(self):
        box = _box()
        for content in ("v1", "v2"):
            box.run("write_file",
                    {"filename": "a.ts", "description": "d", "content": content})
        assert box.written["a.ts"]["content"] == "v2"
        assert len(box.written) == 1

    def test_an_unknown_tool_answers_instead_of_raising(self):
        """A bad tool name must be recoverable information, not a dead run."""
        out = _box().run("rm_rf", {})
        assert "No such tool" in out

    def test_a_tool_that_throws_is_reported_back_to_the_model(self):
        box = _box()
        box._tool_boom = lambda args: (_ for _ in ()).throw(RuntimeError("kaboom"))
        out = box.run("boom", {})
        assert "kaboom" in out


class TestToolSchemas:
    def test_every_schema_has_a_handler(self):
        """A schema the model can call but the toolbox can't run is a silent
        dead end — the model would keep retrying a tool that never works."""
        box = _box()
        for schema in TOOL_SCHEMAS:
            assert hasattr(box, f"_tool_{schema['name']}"), schema["name"]

    def test_required_fields_are_declared(self):
        for schema in TOOL_SCHEMAS:
            assert schema["input_schema"]["type"] == "object"
            assert schema["description"].strip()


# ─────────────────────────────────────────────
# The loop
# ─────────────────────────────────────────────

def _text(s):
    return {"type": "text", "text": s}


def _use(name, inp, id="t1"):
    return {"type": "tool_use", "id": id, "name": name, "input": inp}


class FakeRouter:
    """Replays canned assistant turns and records what it was sent."""

    def __init__(self, replies, provider="claude"):
        self.replies = list(replies)
        self.provider = provider
        self.seen: list[list[dict]] = []
        self.pinned: list = []

    async def agent_turn(self, *, messages, system, tools, tier, context_hint,
                         pinned=None):
        self.pinned.append(pinned)
        self.seen.append([dict(m) for m in messages])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply["content"], reply["stop_reason"], self.provider


def _run(monkeypatch, replies, **kw):
    fake = FakeRouter(replies)
    monkeypatch.setattr(agent_loop, "router", fake)
    coro = agent_loop.run_agent(
        source_files=kw.pop("source_files", SRC),
        framework_key="playwright_ts",
        language="typescript",
        system_prompt="sys",
        project_summary="A React app",
        test_flows="log in",
        base_url="http://localhost:3000",
        src_index=grounding.build_source_index(SRC),
        tier=Tier.PRO,
        **kw,
    )
    return asyncio.run(coro), fake


WROTE_ONE = {
    "content": [_use("write_file", {
        "filename": "specs/login.spec.ts",
        "description": "login",
        "content": "test('login', async () => {});\n",
    })],
    "stop_reason": "tool_use",
}
DONE = {"content": [_text("Covered the login flow.")], "stop_reason": "end_turn"}


class TestLoop:
    def test_a_tool_call_then_a_summary_produces_the_suite(self, monkeypatch):
        result, _ = _run(monkeypatch, [WROTE_ONE, DONE])
        assert [f["filename"] for f in result.files] == ["specs/login.spec.ts"]
        assert result.summary == "Covered the login flow."
        assert result.stop_reason == "completed"
        assert result.turns == 2

    def test_tool_results_are_sent_back_paired_to_their_call(self, monkeypatch):
        """A tool_result is only meaningful next to the tool_use that asked for
        it — a mismatched id is a 400 from the API, mid-conversation."""
        _, fake = _run(monkeypatch, [WROTE_ONE, DONE])
        last = fake.seen[-1]
        results = [b for m in last for b in m["content"]
                   if b.get("type") == "tool_result"]
        assert len(results) == 1
        assert results[0]["id"] == "t1"
        # Gemini pairs by name, having issued no id — so both must travel.
        assert results[0]["name"] == "write_file"

    def test_the_assistant_turn_is_replayed_in_the_normalized_shape(self, monkeypatch):
        """The conversation never holds a provider's own shape — that is what
        lets a Gemini turn follow a Claude one."""
        _, fake = _run(monkeypatch, [WROTE_ONE, DONE])
        assistant = [m for m in fake.seen[-1] if m["role"] == "assistant"][0]
        assert assistant["content"][0]["type"] == "tool_use"
        assert assistant["content"][0]["name"] == "write_file"

    def test_a_model_that_forgets_to_write_is_nudged_once(self, monkeypatch):
        """Describing the plan instead of executing it is the common failure —
        worth one corrective turn, not a failed run."""
        result, fake = _run(monkeypatch, [
            {"content": [_text("Here is my plan…")], "stop_reason": "end_turn"},
            WROTE_ONE,
            DONE,
        ])
        assert len(result.files) == 1
        nudge = fake.seen[-1][2]
        assert "haven't written any files" in nudge["content"][0]["text"]

    def test_it_does_not_nudge_twice(self, monkeypatch):
        """If repeating the instruction didn't work, repeating it again won't."""
        empty = {"content": [_text("Still thinking")], "stop_reason": "end_turn"}
        with pytest.raises(agent_loop.AgentUnavailable):
            _run(monkeypatch, [empty, empty])

    def test_investigating_forever_without_writing_falls_back(self, monkeypatch):
        """Hitting the turn cap with nothing written is not a suite. The caller
        must get AgentUnavailable and run the scripted pipeline, not an empty
        archive that still scores and scaffolds like a real one."""
        look = {"content": [_use("list_files", {})], "stop_reason": "tool_use"}
        with pytest.raises(agent_loop.AgentUnavailable):
            _run(monkeypatch, [look, look], max_turns=2)

    def test_the_turn_cap_keeps_what_was_written(self, monkeypatch):
        """Not converging is a reason to stop, not a reason to bin real work."""
        result, _ = _run(monkeypatch, [WROTE_ONE, WROTE_ONE, WROTE_ONE], max_turns=3)
        assert result.stop_reason == "max_turns"
        assert len(result.files) == 1

    def test_running_out_of_capacity_mid_run_keeps_the_partial_suite(self, monkeypatch):
        result, _ = _run(monkeypatch, [
            WROTE_ONE,
            AllProvidersExhausted("dry", reason="quota_exhausted"),
        ])
        assert result.stop_reason == "exhausted"
        assert len(result.files) == 1

    def test_running_out_before_the_first_file_falls_back(self, monkeypatch):
        with pytest.raises(agent_loop.AgentUnavailable):
            _run(monkeypatch, [AllProvidersExhausted("dry", reason="quota_exhausted")])

    def test_the_conversation_stays_on_the_provider_that_started_it(self, monkeypatch):
        """Both providers sign their tool calls with opaque data that must be
        replayed and can't be translated — a mid-run switch would corrupt the
        history, not rescue it. Gemini enforces this with a 400."""
        _, fake = _run(monkeypatch, [WROTE_ONE, WROTE_ONE, DONE])
        assert fake.pinned == [None, "claude", "claude"]

    def test_the_trace_names_the_provider_that_did_the_work(self, monkeypatch):
        result, _ = _run(monkeypatch, [WROTE_ONE, DONE])
        assert result.as_dict()["providers"] == ["claude"]

    def test_the_trace_reports_what_the_agent_actually_did(self, monkeypatch):
        result, _ = _run(monkeypatch, [
            {"content": [_use("find_anchors", {"kind": "testid"}, id="a1")],
             "stop_reason": "tool_use"},
            WROTE_ONE,
            DONE,
        ])
        trace = result.as_dict()
        assert trace["files_written"] == 1
        assert [c["name"] for c in trace["tool_calls"]] == ["find_anchors", "write_file"]

    def test_crawl_only_runs_work_without_repo_source(self, monkeypatch):
        result, fake = _run(monkeypatch, [WROTE_ONE, DONE], source_files={})
        assert len(result.files) == 1
        assert "no repo source" in fake.seen[0][0]["content"][0]["text"]


# ─────────────────────────────────────────────
# Router: the tool-enabled request
# ─────────────────────────────────────────────

class TestClaudeToolRequest:
    def _r(self):
        return LLMRouter.__new__(LLMRouter)

    def test_tools_are_attached_when_given(self):
        body = self._r()._claude_request(
            [{"role": "user", "content": "hi"}], "sys", 0.2,
            model_for(Provider.CLAUDE, Tier.PRO), tools=TOOL_SCHEMAS)
        assert len(body["tools"]) == len(TOOL_SCHEMAS)

    def test_the_single_prompt_shape_is_unchanged(self):
        """Every non-agentic caller still goes through _claude_body, and adding
        tool support must not have altered what it sends."""
        body = self._r()._claude_body("p", "sys", 0.2, model_for(Provider.CLAUDE, Tier.FREE))
        assert body["messages"] == [{"role": "user", "content": "p"}]
        assert "tools" not in body
        assert body["temperature"] == 0.2

    def test_pro_flags_apply_to_the_tools_shape_too(self):
        body = self._r()._claude_request(
            [], "", 0.2, model_for(Provider.CLAUDE, Tier.PRO), tools=TOOL_SCHEMAS)
        assert "temperature" not in body            # Opus 4.7+ reject it
        assert body["thinking"] == {"type": "adaptive"}

    def test_no_agent_capable_key_is_a_clear_configuration_error(self):
        r = self._r()
        r._providers = {}
        with pytest.raises(AllProvidersExhausted) as e:
            asyncio.run(r.agent_turn(messages=[], system="", tools=[]))
        assert e.value.reason == "not_configured"
        assert "GEMINI_API_KEY_1" in str(e.value)

    def test_agent_turns_ask_claude_not_to_think(self):
        """A thinking block must be replayed verbatim with its signature and
        can't be rebuilt from the normalized form — keeping it would pin a run
        to one provider, which is the constraint agent_protocol removes."""
        body = self._r()._claude_request(
            [], "", 0.2, model_for(Provider.CLAUDE, Tier.PRO),
            tools=TOOL_SCHEMAS, thinking=False)
        assert "thinking" not in body


# ─────────────────────────────────────────────
# WriterAgent: agent mode never costs the user their suite
# ─────────────────────────────────────────────

class TestWriterAgentFallback:
    """Every way agent mode can fail is a reason to run the scripted pipeline —
    which `_run_agent_mode` signals by returning [], not by raising."""

    def _filter_result(self):
        from agents.filter_agent import FilterResult
        return FilterResult(
            project_summary="A React app",
            key_pages=[], key_components=[], routes=[],
            framework="react", testing_challenges=[],
            files=SRC, total_tokens=100, file_count=len(SRC),
        )

    def _agent(self):
        from agents.writer_agent import WriterAgent
        w = WriterAgent()
        w._agent_trace = {}
        w._crawl_index = None
        return w

    def _call(self, w):
        return asyncio.run(w._run_agent_mode(
            filter_result=self._filter_result(),
            framework_key="playwright_ts",
            test_flows="log in",
            base_url="http://localhost:3000",
            language="typescript",
            tier=Tier.PRO,
        ))

    def _patch(self, monkeypatch, run_agent):
        monkeypatch.setattr(agent_loop, "run_agent", run_agent)

    def test_an_unavailable_agent_falls_back_silently(self, monkeypatch):
        async def boom(**kw):
            raise agent_loop.AgentUnavailable("wrote nothing")
        self._patch(monkeypatch, boom)
        w = self._agent()
        assert self._call(w) == []
        assert w._agent_trace == {}      # no trace for work that didn't happen

    def test_no_anthropic_capacity_falls_back_to_the_other_providers(self, monkeypatch):
        """The scripted path still runs on Gemini or Groq. A Pro user who would
        otherwise get nothing gets a suite, exactly as the router already decides."""
        async def dry(**kw):
            raise AllProvidersExhausted("dry", reason="quota_exhausted")
        self._patch(monkeypatch, dry)
        assert self._call(self._agent()) == []

    def test_a_successful_run_returns_files_and_records_the_trace(self, monkeypatch):
        async def ok(**kw):
            return agent_loop.AgentRunResult(
                files=[{"filename": "a.spec.ts", "description": "d", "content": "c"}],
                summary="done", turns=3,
            )
        self._patch(monkeypatch, ok)
        w = self._agent()
        files = self._call(w)
        assert [f.filename for f in files] == ["a.spec.ts"]
        assert w._agent_trace["turns"] == 3

    def test_the_agent_is_given_the_grounding_index_not_just_the_source(self, monkeypatch):
        """Grounding is what the tools exist for — an agent handed only raw
        source is back to guessing selectors."""
        captured = {}

        async def capture(**kw):
            captured.update(kw)
            return agent_loop.AgentRunResult(
                files=[{"filename": "a.ts", "description": "d", "content": "c"}])
        self._patch(monkeypatch, capture)
        self._call(self._agent())
        assert captured["src_index"] is not None
        assert "email" in captured["anchor_hint"]
