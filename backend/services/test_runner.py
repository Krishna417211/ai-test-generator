"""
test_runner.py — actually run a generated Playwright suite and report what failed.

Everything else in this codebase checks a suite *statically*: validator.py parses
it, grounding.py proves the selectors exist, fragility.py scores how brittle they
look. All of that is honest precisely because it never claims the tests pass —
see the `Grounding` docstring, and LIMITATIONS.md. This module is the one thing
that can make that claim, by running the suite against the user's deployed app
and reading the real result.

⚠️  It executes model-written code on this host. That is not a side effect, it is
the feature — but it means a repo whose contents steered the model can steer what
runs here. Three things contain it, and none of them should be removed casually:

  1. It is OFF unless `enable_test_execution` is set. The default is not "on for
     localhost" or "on in development": it is off, everywhere, until someone
     decides otherwise.
  2. Files are written through `safe_paths.sanitize`, so nothing lands outside
     the run directory no matter what filename the model asked for.
  3. Every run is killed at `test_execution_timeout_seconds`, and the process is
     started without a shell.

It is emphatically NOT a sandbox. Enabling it on a host with credentials or a
private network reachable from it is a decision to let generated code see them;
run it in a disposable container if that matters, which is why the setting exists
rather than a hardcoded `if app_env == "production"`.

Playwright JS/TS only for now. Cypress and Selenium need their own runner
invocation and result format, and reporting "couldn't run" is better than
pretending a framework is covered.
"""

import os
import re
import json
import shutil
import asyncio
import logging
import tempfile
from dataclasses import dataclass, field

from config import settings
from services import safe_paths

logger = logging.getLogger(__name__)

# Runners whose invocation and result format this module actually understands.
SUPPORTED_FRAMEWORKS = {"playwright_ts", "playwright_js"}

# The npm package the runner needs. Installed once into a cached workspace, not
# per run — a fresh `npm install` per generation would add ~30s and a network
# dependency to every single agent turn that wanted to check its work.
RUNNER_PACKAGE = "@playwright/test"

# Installing the runner is slower than running it, and happens at most once.
INSTALL_TIMEOUT_SECONDS = 300

_workspace_lock = asyncio.Lock()
_workspace_ready: str | None = None

# Playwright colourises error messages. They go into a prompt and into the UI,
# and escape codes are noise in both.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class RunnerUnavailable(Exception):
    """The suite could not be run at all — distinct from a suite that ran and failed.

    The difference matters more than it looks: "we ran your tests and 3 failed"
    and "we could not run your tests" are opposite messages, and collapsing them
    would let a missing runtime read as a broken app.
    """

    def __init__(self, message: str, reason: str = "unavailable"):
        self.reason = reason
        super().__init__(message)


@dataclass
class Failure:
    test: str
    file: str
    message: str

    def as_dict(self) -> dict:
        return {"test": self.test, "file": self.file, "message": self.message}


@dataclass
class RunResult:
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    failures: list[Failure] = field(default_factory=list)
    timed_out: bool = False
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.failed == 0 and not self.timed_out and self.total > 0

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "failures": [f.as_dict() for f in self.failures],
        }


def availability(framework_key: str = "playwright_ts") -> tuple[bool, str]:
    """(can we run, and if not, the reason in a sentence a user can act on).

    Checked before every run and exposed to the model as a tool result, so the
    agent is told plainly that it cannot verify its work rather than silently
    getting nothing back.
    """
    if not settings.enable_test_execution:
        return False, (
            "Running generated tests is disabled on this server "
            "(set ENABLE_TEST_EXECUTION=true to allow it)."
        )
    if framework_key not in SUPPORTED_FRAMEWORKS:
        return False, (
            f"Running {framework_key} suites isn't supported yet — only "
            f"{', '.join(sorted(SUPPORTED_FRAMEWORKS))}."
        )
    if not shutil.which("npx") or not shutil.which("npm"):
        return False, (
            "Node.js isn't installed on this server, so a Playwright suite "
            "can't be executed here."
        )
    return True, ""


def _workspace_dir() -> str:
    configured = (settings.test_runner_workspace or "").strip()
    if configured:
        return configured
    return os.path.join(tempfile.gettempdir(), "testra-runner")


async def _exec(argv: list[str], cwd: str, timeout: int, env: dict | None = None):
    """Run a process without a shell, and make sure it is dead when we return.

    `proc.kill()` on timeout is not enough on its own: npx spawns node, which
    spawns browsers, and killing only the parent leaves those running until the
    container does. Killing the whole process group is what actually reclaims
    them, which matters when a generated test opens a page that never settles.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **(env or {})},
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except (ProcessLookupError, PermissionError):       # pragma: no cover
            proc.kill()
        await proc.wait()
        raise


async def ensure_workspace() -> str:
    """Install the runner once into a cached directory, and return its path."""
    global _workspace_ready

    async with _workspace_lock:
        if _workspace_ready and os.path.isdir(
            os.path.join(_workspace_ready, "node_modules", *RUNNER_PACKAGE.split("/"))
        ):
            return _workspace_ready

        workspace = _workspace_dir()
        os.makedirs(workspace, exist_ok=True)
        installed = os.path.join(workspace, "node_modules", *RUNNER_PACKAGE.split("/"))

        if not os.path.isdir(installed):
            logger.info(f"Installing {RUNNER_PACKAGE} into {workspace} (one-off)")
            pkg = os.path.join(workspace, "package.json")
            if not os.path.exists(pkg):
                with open(pkg, "w") as fh:
                    json.dump({"name": "testra-runner", "private": True}, fh)
            try:
                code, _, err = await _exec(
                    ["npm", "install", "--no-audit", "--no-fund", RUNNER_PACKAGE],
                    cwd=workspace,
                    timeout=INSTALL_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                raise RunnerUnavailable(
                    "Timed out installing the Playwright runner.", reason="install_timeout")
            if code != 0:
                raise RunnerUnavailable(
                    f"Couldn't install the Playwright runner: {err[:200]}",
                    reason="install_failed")

        _workspace_ready = workspace
        return workspace


def _config(test_dir: str, base_url: str) -> str:
    """A config for this run only.

    No `webServer`: the app under test is the user's deployed URL, already
    running. Retries off and one worker, because the agent is reading these
    failures to decide what to fix — a retry that passes on the second attempt
    would hide exactly the flakiness worth telling it about.
    """
    return f"""const {{ defineConfig }} = require('@playwright/test');
module.exports = defineConfig({{
  testDir: {json.dumps(test_dir)},
  timeout: 30000,
  retries: 0,
  workers: 1,
  reporter: [['json']],
  use: {{
    baseURL: {json.dumps(base_url)},
    headless: true,
    screenshot: 'off',
    trace: 'off',
  }},
}});
"""


def _walk_suites(node: dict, failures: list[Failure], counts: dict) -> None:
    """Collect specs from Playwright's recursive JSON report."""
    for spec in node.get("specs", []) or []:
        title = spec.get("title", "(untitled)")
        file = spec.get("file", "")
        for test in spec.get("tests", []) or []:
            for result in test.get("results", []) or []:
                status = result.get("status")
                if status == "skipped":
                    counts["skipped"] += 1
                    continue
                counts["total"] += 1
                if status == "passed":
                    counts["passed"] += 1
                else:
                    counts["failed"] += 1
                    err = (result.get("error") or {}).get("message") or status or "failed"
                    # Strip ANSI — the message goes into a prompt and into the
                    # UI, and escape codes are noise in both.
                    err = _ANSI.sub("", err).strip()
                    failures.append(Failure(title, file, err[:1200]))
    for child in node.get("suites", []) or []:
        _walk_suites(child, failures, counts)


def parse_report(raw: str) -> RunResult:
    """Turn the JSON reporter's output into a RunResult.

    Tolerant of leading noise: npm and playwright both write to stdout before
    the report does, so the JSON rarely starts at byte zero.
    """
    start = raw.find("{")
    if start == -1:
        raise RunnerUnavailable(
            "The test runner produced no report.", reason="no_report")
    try:
        report = json.loads(raw[start:])
    except json.JSONDecodeError:
        raise RunnerUnavailable(
            "The test runner's report wasn't valid JSON.", reason="bad_report")

    failures: list[Failure] = []
    counts = {"total": 0, "passed": 0, "failed": 0, "skipped": 0}
    for suite in report.get("suites", []) or []:
        _walk_suites(suite, failures, counts)

    return RunResult(
        total=counts["total"],
        passed=counts["passed"],
        failed=counts["failed"],
        skipped=counts["skipped"],
        failures=failures,
        duration_ms=int((report.get("stats") or {}).get("duration") or 0),
    )


async def run_suite(
    files: dict[str, str],
    base_url: str,
    framework_key: str = "playwright_ts",
    timeout: int | None = None,
) -> RunResult:
    """Write the suite to a scratch dir, run it against `base_url`, report results."""
    ok, why = availability(framework_key)
    if not ok:
        raise RunnerUnavailable(why, reason="unavailable")

    code_files = {
        name: content for name, content in files.items()
        if name.lower().endswith((".ts", ".js", ".tsx", ".jsx"))
    }
    if not code_files:
        raise RunnerUnavailable(
            "There are no runnable test files in this suite.", reason="nothing_to_run")

    workspace = await ensure_workspace()
    run_dir = tempfile.mkdtemp(prefix="run-", dir=workspace)

    for name, content in code_files.items():
        # Whatever the model called it, it lands inside run_dir. sanitize()
        # rejects absolute paths and traversal outright.
        safe = safe_paths.sanitize(name)
        if not safe:
            logger.info(f"Skipping unrunnable filename from the model: {name!r}")
            continue
        dest = os.path.join(run_dir, safe)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w") as fh:
            fh.write(content)

    config_path = os.path.join(run_dir, "playwright.config.js")
    with open(config_path, "w") as fh:
        fh.write(_config(run_dir, base_url))

    limit = timeout or settings.test_execution_timeout_seconds
    # PLAYWRIGHT_BROWSERS_PATH is inherited, never invented. The Dockerfile sets
    # it to /ms-playwright so the runner finds the image's shared Chromium; off
    # the image that path doesn't exist, and hardcoding it as a default sent
    # Playwright looking there instead of its own ~/.cache/ms-playwright — every
    # local run then failed with "browser not installed" on a machine that had
    # it. Absent from the environment means "use your default", not "/ms-playwright".
    try:
        _, out, err = await _exec(
            ["npx", "playwright", "test", "--config", config_path],
            cwd=workspace,
            timeout=limit,
        )
    except asyncio.TimeoutError:
        logger.warning(f"Test run exceeded {limit}s — killed")
        return RunResult(timed_out=True)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)

    try:
        return parse_report(out)
    except RunnerUnavailable:
        # A non-zero exit with no parsable report is the runner itself failing
        # (a missing browser, a config error), not the suite failing. Say which.
        raise RunnerUnavailable(
            f"The test runner didn't complete: {(err or out)[-300:]}",
            reason="runner_error",
        )
