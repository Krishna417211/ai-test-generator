"""test_test_runner.py — executing a generated suite for real.

The distinction this suite exists to protect: "we ran your tests and 3 failed"
and "we could not run your tests" are opposite messages. Collapsing them would
let a missing Node install read as a broken app.
"""

import asyncio
import json

import pytest

from config import settings
from services import test_runner
from services.test_runner import RunnerUnavailable, parse_report
from services.agent_tools import Toolbox


@pytest.fixture
def execution_enabled(monkeypatch):
    monkeypatch.setattr(settings, "enable_test_execution", True)


# ─────────────────────────────────────────────
# Availability — the gate, and the honesty about it
# ─────────────────────────────────────────────

class TestAvailability:
    def test_disabled_by_default(self, monkeypatch):
        """Executing model-written code is opt-in, everywhere, always."""
        monkeypatch.setattr(settings, "enable_test_execution", False)
        ok, why = test_runner.availability("playwright_ts")
        assert not ok and "disabled" in why

    def test_an_unsupported_framework_says_so_rather_than_pretending(self, execution_enabled):
        ok, why = test_runner.availability("cypress_js")
        assert not ok and "cypress_js" in why

    def test_missing_node_is_reported_as_a_server_limitation(
            self, execution_enabled, monkeypatch):
        monkeypatch.setattr(test_runner.shutil, "which", lambda _: None)
        ok, why = test_runner.availability("playwright_ts")
        assert not ok and "Node.js" in why

    def test_available_when_everything_is_in_place(self, execution_enabled, monkeypatch):
        monkeypatch.setattr(test_runner.shutil, "which", lambda _: "/usr/bin/npx")
        ok, why = test_runner.availability("playwright_ts")
        assert ok and why == ""

    def test_run_suite_refuses_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(settings, "enable_test_execution", False)
        with pytest.raises(RunnerUnavailable):
            asyncio.run(test_runner.run_suite({"a.ts": "x"}, "http://x"))


# ─────────────────────────────────────────────
# Report parsing
# ─────────────────────────────────────────────

def _report(*specs, duration=1234):
    return json.dumps({
        "stats": {"duration": duration},
        "suites": [{"specs": list(specs), "suites": []}],
    })


def _spec(title, status, message="", file="login.spec.ts"):
    return {
        "title": title,
        "file": file,
        "tests": [{"results": [{"status": status, "error": {"message": message}}]}],
    }


class TestParseReport:
    def test_counts_passes_and_failures(self):
        r = parse_report(_report(
            _spec("logs in", "passed"),
            _spec("shows an error", "failed", "expected visible"),
        ))
        assert (r.total, r.passed, r.failed) == (2, 1, 1)
        assert r.ok is False

    def test_a_clean_run_is_ok(self):
        r = parse_report(_report(_spec("logs in", "passed")))
        assert r.ok is True
        assert r.duration_ms == 1234

    def test_an_empty_run_is_not_ok(self):
        """Zero tests collected is a broken suite, not a passing one — `ok` must
        not be True just because nothing failed."""
        assert parse_report(_report()).ok is False

    def test_skips_are_counted_separately_not_as_passes(self):
        r = parse_report(_report(_spec("todo", "skipped")))
        assert (r.total, r.skipped, r.passed) == (0, 1, 0)

    def test_failure_carries_the_real_error_message(self):
        r = parse_report(_report(
            _spec("logs in", "failed", "locator('#email') resolved to 0 elements")))
        assert "#email" in r.failures[0].message
        assert r.failures[0].test == "logs in"

    def test_ansi_colour_is_stripped_from_messages(self):
        """The message goes into a prompt and into the UI — escape codes are
        noise in both."""
        r = parse_report(_report(_spec("x", "failed", "\x1b[31mred error\x1b[0m")))
        assert r.failures[0].message == "red error"

    def test_nested_suites_are_walked(self):
        raw = json.dumps({
            "stats": {"duration": 1},
            "suites": [{"specs": [], "suites": [{"specs": [_spec("deep", "failed")],
                                                 "suites": []}]}],
        })
        assert parse_report(raw).failed == 1

    def test_leading_noise_before_the_json_is_tolerated(self):
        """npm and playwright both write to stdout before the report does."""
        r = parse_report("npm warn config\n\n" + _report(_spec("a", "passed")))
        assert r.passed == 1

    def test_no_report_is_a_runner_error_not_a_test_failure(self):
        with pytest.raises(RunnerUnavailable) as e:
            parse_report("Error: Cannot find module '@playwright/test'")
        assert e.value.reason == "no_report"

    def test_malformed_json_is_a_runner_error(self):
        with pytest.raises(RunnerUnavailable) as e:
            parse_report("{not json")
        assert e.value.reason == "bad_report"


# ─────────────────────────────────────────────
# The run_test tool
# ─────────────────────────────────────────────

class TestRunTestTool:
    def _box(self, base_url="http://localhost:3000"):
        box = Toolbox({}, "playwright_ts", base_url=base_url)
        box.written["login.spec.ts"] = {"description": "d", "content": "test(...)"}
        return box

    def _run(self, box):
        return asyncio.run(box.run_async("run_test", {}))

    def test_nothing_written_yet_is_not_an_error(self):
        box = Toolbox({}, "playwright_ts", base_url="http://x")
        assert "Nothing to run" in self._run(box)

    def test_unavailable_tells_the_model_to_carry_on(self, monkeypatch):
        """An unrunnable server must not read as a failing suite — the agent
        would start 'fixing' tests that never ran."""
        async def unavailable(*a, **kw):
            raise RunnerUnavailable("Node.js isn't installed on this server.")
        monkeypatch.setattr(test_runner, "run_suite", unavailable)
        out = self._run(self._box())
        assert "Could not run" in out
        assert "says nothing about whether your suite is correct" in out

    def test_failures_are_handed_back_with_their_messages(self, monkeypatch):
        async def failing(*a, **kw):
            return parse_report(_report(
                _spec("logs in", "failed", "locator('#email') resolved to 0 elements")))
        monkeypatch.setattr(test_runner, "run_suite", failing)
        box = self._box()
        out = self._run(box)
        assert "0 elements" in out and "1 failed" in out
        assert box.last_run["failed"] == 1

    def test_a_pass_is_reported_as_a_pass(self, monkeypatch):
        async def passing(*a, **kw):
            return parse_report(_report(_spec("logs in", "passed")))
        monkeypatch.setattr(test_runner, "run_suite", passing)
        box = self._box()
        assert "Everything passed" in self._run(box)
        assert box.last_run["passed"] == 1

    def test_a_timeout_is_explained_not_just_reported(self, monkeypatch):
        async def hang(*a, **kw):
            return test_runner.RunResult(timed_out=True)
        monkeypatch.setattr(test_runner, "run_suite", hang)
        out = self._run(self._box())
        assert "time limit" in out and "never appears" in out

    def test_the_run_is_recorded_for_the_caller_not_just_the_model(self, monkeypatch):
        """last_run is the only evidence anywhere that a suite actually passes."""
        async def passing(*a, **kw):
            return parse_report(_report(_spec("a", "passed")))
        monkeypatch.setattr(test_runner, "run_suite", passing)
        box = self._box()
        assert box.last_run is None
        self._run(box)
        assert box.last_run["total"] == 1


class TestRunSuiteWiring:
    def test_a_suite_with_no_code_files_is_refused_early(self, execution_enabled, monkeypatch):
        """Running a README proves nothing and costs a browser launch."""
        monkeypatch.setattr(test_runner.shutil, "which", lambda _: "/usr/bin/npx")
        with pytest.raises(RunnerUnavailable) as e:
            asyncio.run(test_runner.run_suite(
                {"README.md": "# hi", "ci.yml": "on: push"}, "http://x"))
        assert e.value.reason == "nothing_to_run"

    def test_the_browser_path_is_inherited_never_invented(self, execution_enabled, monkeypatch):
        """The Dockerfile sets PLAYWRIGHT_BROWSERS_PATH=/ms-playwright. Defaulting
        to that path off the image sent Playwright looking somewhere that doesn't
        exist, and every local run failed with "browser not installed" on a
        machine that had it. Absent means "use your default"."""
        monkeypatch.setattr(test_runner.shutil, "which", lambda _: "/usr/bin/npx")
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        seen = {}

        async def fake_exec(argv, cwd, timeout, env=None):
            seen["env"] = env
            return 0, json.dumps({"stats": {"duration": 1}, "suites": []}), ""

        monkeypatch.setattr(test_runner, "_exec", fake_exec)
        monkeypatch.setattr(test_runner, "ensure_workspace",
                            lambda: asyncio.sleep(0, result="/tmp"))
        asyncio.run(test_runner.run_suite({"a.spec.js": "test"}, "http://x", "playwright_js"))
        assert not (seen["env"] or {}).get("PLAYWRIGHT_BROWSERS_PATH")

    def test_the_config_points_at_the_users_url_and_disables_retries(self):
        """A retry that passes second time hides exactly the flakiness the agent
        needs to be told about."""
        cfg = test_runner._config("/tmp/run", "https://example.com")
        assert '"https://example.com"' in cfg
        assert "retries: 0" in cfg
        assert "webServer" not in cfg      # the app is already deployed
