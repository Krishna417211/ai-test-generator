"""
scaffold.py — the runnable skeleton around the LLM's tests.

The writer agent's job is page objects and specs. Everything that turns those
files into a suite you can actually `npm install && npx playwright test` — the
dependency manifest, the framework config, the CI pipeline, the README — is
boilerplate with one correct answer per framework. It is built here, from
templates, rather than asked of the model: the LLM adds nothing to a
package.json and every token it spends on one is a token it can truncate.

Two rules this module exists to enforce
---------------------------------------
* **A suite ships its own dependencies.** Nothing here ever emitted a
  package.json or a requirements.txt, while the CI it emitted ran `npm ci` and
  `pip install -r requirements.txt`. Those pipelines could not pass on any repo:
  there was no manifest to install and no lockfile for `npm ci` to read. The
  install step and the manifest are written together, in this file, so they
  cannot drift apart again.

* **The suite lives in its own directory.** Everything lands under SUITE_DIR
  instead of the repo root. A React app already has a package.json at the root,
  and writing ours there would silently destroy the app's own manifest — the
  suite must never be able to damage the repo it is testing.

Reaching the app under test
---------------------------
`BASE_URL` in the environment always wins, so one variable points the same suite
at staging or prod. With it unset the config falls back to the URL the user
asked for, and — when that URL is local — a `webServer` block boots the app so a
fresh clone and CI both work with no arguments. A remote fallback URL gets no
webServer: there is nothing to start.
"""

import json
from dataclasses import dataclass, field
from urllib.parse import urlparse

# Where the generated suite lives inside the target repo. Not the repo root:
# see the module docstring.
SUITE_DIR = "e2e"

# Pinned, not floating. A suite that resolves a different Playwright on every CI
# run is a suite that can go red without anyone changing a line of it.
_PLAYWRIGHT_VERSION = "1.47.2"
_PLAYWRIGHT_PY_VERSION = "1.47.0"
_CYPRESS_VERSION = "13.15.0"
_SERVE_VERSION = "14.2.4"
_START_SERVER_AND_TEST_VERSION = "2.0.8"
_TYPESCRIPT_VERSION = "5.6.3"
_SELENIUM_PY_VERSION = "4.25.0"


@dataclass(frozen=True)
class Target:
    """How the generated suite reaches the application under test."""

    base_url: str
    # Command that serves the app, run from inside SUITE_DIR (so the repo root
    # is ".."). Empty when the target is remote and nothing needs starting.
    serve_command: str = ""
    port: int = 0
    # Dependencies the serve_command itself needs (e.g. `serve` for a static
    # site). Merged into the manifest so `npx serve` resolves locally instead of
    # trying to reach the network mid-run.
    serve_deps: dict[str, str] = field(default_factory=dict)
    # Extra files the serve_command needs, relative to SUITE_DIR (see
    # _SERVE_JSON). Emitted alongside the config.
    serve_files: dict[str, str] = field(default_factory=dict)
    # True when the app under test is a Node project with its own package.json,
    # which CI has to install before the webServer command can boot it.
    app_needs_npm_install: bool = False

    @property
    def is_local(self) -> bool:
        return bool(self.serve_command)


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def _is_local_url(url: str) -> tuple[bool, int]:
    """Whether a URL points at this machine, and the port it names."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False, 0
    host = (parsed.hostname or "").lower()
    if host not in _LOCAL_HOSTS:
        return False, 0
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return True, port


# `serve` rewrites /login.html to /login by default (cleanUrls), which would both
# break a toHaveURL('/login.html') assertion and make the local run disagree with
# the host the site actually ships on — GitHub Pages serves /login.html and 404s
# /login. Turning it off makes the dev server behave like production.
#
# It has to be a file: there is no --no-clean-urls flag. `serve -c` resolves the
# path against the directory being SERVED (the repo root), not the working
# directory, which is why the command below says "{suite}/serve.json".
_SERVE_JSON = """{
  "cleanUrls": false
}
"""

# Detected stack -> how to boot it, keyed by a substring of the framework string
# services/file_extractor.detect_framework returns. Order matters: the first
# match wins, so "Next.js (React)" must be tested before the bare "React".
_SERVE_COMMANDS: list[tuple[str, str, bool]] = [
    # (marker in the detected stack, command template, needs `npm install` in app
    # root). The Static HTML command is decided by _static_target, which needs the
    # test framework's runtime; the entry is here only to claim the marker.
    ("Static HTML", "", False),
    ("Next.js", "npm --prefix .. run dev -- --port {port}", True),
    ("Nuxt", "npm --prefix .. run dev -- --port {port}", True),
    ("Angular", "npm --prefix .. start -- --port {port}", True),
    ("Remix", "npm --prefix .. run dev", True),
    ("Gatsby", "npm --prefix .. run develop -- --port {port}", True),
    ("Svelte", "npm --prefix .. run dev -- --port {port}", True),
    ("Vue", "npm --prefix .. run dev -- --port {port}", True),
    ("React", "npm --prefix .. run dev -- --port {port}", True),
    ("Django", "python ../manage.py runserver {port}", False),
    ("Flask", "flask --app .. run --port {port}", False),
    ("FastAPI", "uvicorn main:app --app-dir .. --port {port}", False),
    ("Rails", "bundle exec rails server -p {port}", False),
    ("Laravel", "php ../artisan serve --port={port}", False),
]


def normalize_base_url(base_url: str) -> str:
    """Base URL in the one form that survives a sub-path deployment.

    The trailing slash is load-bearing, not cosmetic. Both Playwright and Cypress
    resolve a test's relative path with `new URL(path, baseURL)`, and that drops
    the last segment of a slashless base:

        new URL('login.html', 'https://x.github.io/shop')   -> /login.html      ✗
        new URL('login.html', 'https://x.github.io/shop/')  -> /shop/login.html ✓

    A GitHub Pages project site — where this repo's own demo lives — is served at
    exactly such a sub-path, so stripping the slash (as this function's caller
    used to) pointed every test at the wrong origin and 404'd the whole suite.
    See also _navigation_hint in writer_agent: the other half of the rule is that
    tests must not write a leading slash, which resets to the host root and
    discards the sub-path just as thoroughly.

    The scheme is supplied when the user omits it. `testra.duckdns.org` is what a
    person types, but it is not a URL: Selenium rejects it outright
    (InvalidArgumentException) and `new URL()` reads it as a relative path, so a
    scheme-less entry poisoned every generated config with a base that could
    never load. https is the right guess for a real host and http for localhost,
    where a dev server almost never has a certificate.
    """
    url = (base_url or "").strip() or "http://localhost:3000"
    if "://" not in url:
        authority = url.split("/", 1)[0]
        # [::1]:8080 keeps its colons inside the brackets; localhost:3000 does not.
        host = (
            authority[1:].split("]", 1)[0]
            if authority.startswith("[")
            else authority.split(":", 1)[0]
        )
        url = ("http://" if host.lower() in _LOCAL_HOSTS else "https://") + url
    return url if url.endswith("/") else url + "/"


def _static_target(base_url: str, port: int, framework_key: str) -> Target:
    """How to serve a static site — in the runtime the suite already has.

    A Python suite gets http.server, not `npx serve`: http.server is in the
    standard library, so the suite needs no Node, which matters because its CI
    image (python:3.11) has none. It also serves paths literally, which is what
    the site's real host does — so this needs no clean-URL opt-out at all.
    """
    if is_python(framework_key):
        return Target(
            base_url=base_url,
            serve_command=f"python -m http.server {port} --directory ..",
            port=port,
        )
    return Target(
        base_url=base_url,
        serve_command=f"npx serve -l {port} -c {SUITE_DIR}/serve.json ..",
        port=port,
        serve_deps={"serve": f"^{_SERVE_VERSION}"},
        serve_files={"serve.json": _SERVE_JSON},
    )


def plan_target(stack: str, base_url: str, framework_key: str = "") -> Target:
    """Work out how the suite reaches the app, from the detected stack + the
    user's base URL.

    A remote base_url means the app is already deployed and nothing should be
    started; only a local one gets a server.
    """
    base_url = normalize_base_url(base_url)
    local, port = _is_local_url(base_url)
    if not local:
        return Target(base_url=base_url)

    for marker, template, needs_install in _SERVE_COMMANDS:
        if marker.lower() in (stack or "").lower():
            if marker == "Static HTML":
                return _static_target(base_url, port, framework_key)
            return Target(
                base_url=base_url,
                serve_command=template.format(port=port),
                port=port,
                app_needs_npm_install=needs_install,
            )

    # Unknown stack pointed at localhost. Serving it statically is the guess most
    # likely to do something useful, and the README says to change it — better
    # than no server at all, which fails with a bare ECONNREFUSED and no hint
    # about why.
    return _static_target(base_url, port, framework_key)


def is_js(framework_key: str) -> bool:
    return framework_key in {"playwright_js", "cypress_js"}


def is_python(framework_key: str) -> bool:
    return framework_key in {"playwright_python", "selenium_python"}


# ── dependency manifests ─────────────────────

def package_json(framework_key: str, target: Target) -> str:
    """The manifest whose absence made every generated pipeline fail."""
    if framework_key == "cypress_js":
        # typescript is not optional here. Cypress does not bundle a transpiler
        # (Playwright does), so without it every .ts spec — and cypress.config.ts
        # itself — dies with "You are attempting to run a TypeScript file, but do
        # not have TypeScript installed."
        deps = {
            "cypress": f"^{_CYPRESS_VERSION}",
            "typescript": f"^{_TYPESCRIPT_VERSION}",
        }
        # Cypress has no `webServer` of its own — nothing in its config can boot
        # the app — so `npm test` wires the server and the run together with
        # start-server-and-test. `cy:run` is the same run against something that
        # is already up; the two exist separately because starting a second
        # server on a taken port fails the whole run.
        if target.is_local:
            deps["start-server-and-test"] = f"^{_START_SERVER_AND_TEST_VERSION}"
            scripts = {
                "serve:app": target.serve_command,
                "cy:run": "cypress run",
                "cy:open": "cypress open",
                "test": f"start-server-and-test serve:app {target.base_url} cy:run",
            }
        else:
            scripts = {
                "cy:run": "cypress run",
                "cy:open": "cypress open",
                "test": "cypress run",
            }
    else:
        deps = {
            "@playwright/test": f"^{_PLAYWRIGHT_VERSION}",
            "@types/node": "^20.16.5",
        }
        scripts = {
            "test": "playwright test",
            "test:headed": "playwright test --headed",
            "test:ui": "playwright test --ui",
            "report": "playwright show-report",
        }
    deps.update(target.serve_deps)

    return json.dumps(
        {
            "name": "e2e-tests",
            "version": "1.0.0",
            "private": True,
            "description": "End-to-end test suite generated by Testra",
            "scripts": scripts,
            "devDependencies": dict(sorted(deps.items())),
        },
        indent=2,
    ) + "\n"


def requirements_txt(framework_key: str) -> str:
    if framework_key == "selenium_python":
        # No webdriver-manager. Selenium 4.6+ ships Selenium Manager, which
        # resolves the driver itself, so the extra package is dead weight that
        # still runs at import time and still fetches a binary over the network —
        # a supply-chain surface the suite gets nothing back for.
        return (
            f"selenium=={_SELENIUM_PY_VERSION}\n"
            "pytest==8.3.3\n"
        )
    return (
        f"playwright=={_PLAYWRIGHT_PY_VERSION}\n"
        "pytest==8.3.3\n"
        "pytest-playwright==0.5.2\n"
    )


def pom_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>io.testra</groupId>
  <artifactId>e2e-tests</artifactId>
  <version>1.0.0</version>

  <properties>
    <maven.compiler.source>17</maven.compiler.source>
    <maven.compiler.target>17</maven.compiler.target>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>

  <dependencies>
    <dependency>
      <groupId>org.seleniumhq.selenium</groupId>
      <artifactId>selenium-java</artifactId>
      <version>4.25.0</version>
    </dependency>
    <dependency>
      <groupId>org.testng</groupId>
      <artifactId>testng</artifactId>
      <version>7.10.2</version>
      <scope>test</scope>
    </dependency>
  </dependencies>

  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-surefire-plugin</artifactId>
        <version>3.5.0</version>
      </plugin>
    </plugins>
  </build>
</project>
"""


# ── framework config ─────────────────────────

def _web_server_block(target: Target) -> str:
    """Playwright's webServer, or a comment explaining why there isn't one."""
    if not target.is_local:
        return (
            "  // No webServer: baseURL points at a deployed environment, so there\n"
            "  // is nothing to boot here.\n"
        )
    return f"""  // Boots the app under test so `npx playwright test` works from a fresh
  // clone with no other terminal open. Skipped when BASE_URL is set — that
  // means you're testing something already running. Adjust `command` if your
  // app starts differently.
  webServer: process.env.BASE_URL
    ? undefined
    : {{
        command: {target.serve_command!r},
        url: {target.base_url!r},
        timeout: 120 * 1000,
        reuseExistingServer: !process.env.CI,
      }},
"""


def playwright_config(target: Target) -> str:
    return f"""import {{ defineConfig, devices }} from '@playwright/test';

/**
 * Playwright configuration — generated by Testra.
 *
 * Point the suite at any environment without editing this file:
 *   BASE_URL=https://staging.example.com npx playwright test
 */
const baseURL = process.env.BASE_URL || {target.base_url!r};

export default defineConfig({{
  testDir: './tests',
  // A stray test.only must not silently shrink the CI run to one test.
  forbidOnly: !!process.env.CI,
  fullyParallel: true,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: [['html', {{ open: 'never' }}], ['list']],

  use: {{
    baseURL,
    // Artifacts for the run you can't reproduce locally. Kept to failures so a
    // green run stays cheap.
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  }},

  projects: [
    {{ name: 'chromium', use: {{ ...devices['Desktop Chrome'] }} }},
  ],

{_web_server_block(target)}}});
"""


def cypress_config(target: Target) -> str:
    return f"""import {{ defineConfig }} from 'cypress';

/**
 * Cypress configuration — generated by Testra.
 *
 * Point the suite at any environment without editing this file:
 *   BASE_URL=https://staging.example.com npx cypress run
 */
export default defineConfig({{
  e2e: {{
    baseUrl: process.env.BASE_URL || {target.base_url!r},
    // Both spellings: Cypress conventionally uses .cy.ts, but the suite is
    // planned with .spec.ts names. Matching only one of them is how a run exits
    // "no spec files were found" with a suite full of specs sitting right there.
    specPattern: 'tests/specs/**/*.{{cy,spec}}.{{js,ts}}',
    supportFile: 'tests/support/e2e.ts',
    video: false,
    screenshotOnRunFailure: true,
  }},
  viewportWidth: 1280,
  viewportHeight: 720,
}});
"""


def cypress_support_file() -> str:
    """Cypress fails to start if supportFile is configured but missing."""
    return """// Loaded before every spec. Put custom commands and global hooks here.
// Referenced by cypress.config.ts (supportFile) — Cypress errors out if this
// file is missing, so it ships even when empty.
export {};
"""


def cypress_tsconfig() -> str:
    """TypeScript settings for the Cypress suite.

    Required, not a nicety: Cypress compiles .ts specs through ts-loader, which
    refuses to start without a tsconfig it can read ("TS18002: The 'files' list
    in config file 'tsconfig.json' is empty"). `types` is what puts cy.* and
    describe/it in scope.
    """
    return json.dumps(
        {
            "compilerOptions": {
                "target": "ES2020",
                "lib": ["ES2020", "DOM"],
                "module": "commonjs",
                "moduleResolution": "node",
                "types": ["cypress", "node"],
                "esModuleInterop": True,
                "skipLibCheck": True,
                "strict": True,
                "noEmit": True,
            },
            "include": ["**/*.ts"],
            "exclude": ["node_modules"],
        },
        indent=2,
    ) + "\n"


# Appended to every Python conftest. A raw Selenium traceback names the
# exception class and nothing else, so the first thing anyone does with a red
# suite is paste it somewhere and ask what it means. This answers that in the
# terminal, at the moment of failure, with the one thing a traceback never
# carries: what to change.
#
# Keyed on the exception's class *name*, as a string, so this block imports
# neither Selenium nor Playwright and can ship in a suite using either.
_FAILURE_EXPLAINER = '''

# ── Failure diagnostics ──────────────────────────────────────────────────────

# Matched against the exception message first, because a bare WebDriverException
# covers everything from "no Chrome installed" to "the site is down" and the
# class name alone cannot tell those apart. First substring wins.
_MESSAGE_HINTS = (
    ("ERR_CONNECTION_REFUSED", "Nothing is listening at the URL under test.",
     "Start the app, or point the suite elsewhere with BASE_URL=..."),
    ("ERR_NAME_NOT_RESOLVED", "The hostname in the URL does not resolve.",
     "Check BASE_URL for a typo, and that the host is reachable from here."),
    ("ERR_CONNECTION_TIMED_OUT", "The host accepted nothing before the timeout.",
     "The app may be down or firewalled off from this machine."),
    ("ERR_CERT", "The site's TLS certificate was rejected.",
     "Expected on a self-signed staging cert. Use http:// locally, or install the CA."),
    ("cannot find Chrome binary", "Chrome is not installed where Selenium looks.",
     "Install Google Chrome or Chromium, then re-run."),
    ("session not created", "Chrome and its driver are different versions.",
     "Update Chrome. Selenium Manager fetches the matching driver on the next run."),
    ("DevToolsActivePort", "Chrome could not start in this environment.",
     "Usually a container with no /dev/shm and no display. Run with HEADLESS=1."),
)

# Fallback, keyed on the exception class.
_DIAGNOSIS = {
    "NoSuchElementException": (
        "The locator matched no element on the page.",
        "Inspect the real element and correct the locator in tests/pages/. "
        "Prefer a data-testid attribute — it survives restyling.",
    ),
    "TimeoutException": (
        "The element never reached the expected state before the wait expired.",
        "Either it renders later than the timeout allows, or it never renders. "
        "Load the page yourself and confirm the element is really there.",
    ),
    "ElementClickInterceptedException": (
        "Something is covering the element, so the click landed on the overlay.",
        "Usually a cookie banner, modal or sticky header. Dismiss it first.",
    ),
    "ElementNotInteractableException": (
        "The element exists but cannot be typed into or clicked.",
        "It is hidden, disabled, or zero-sized. Wait for the state that enables it.",
    ),
    "StaleElementReferenceException": (
        "The page re-rendered between finding the element and using it.",
        "Re-find the element immediately before acting on it, inside the wait.",
    ),
    "InvalidArgumentException": (
        "The browser rejected the URL it was asked to open.",
        "Almost always a missing scheme — BASE_URL needs https:// or http://.",
    ),
    "InvalidSelectorException": (
        "The browser rejected the selector as malformed.",
        "Check the CSS or XPath syntax in the page object.",
    ),
    "AssertionError": (
        "The page loaded, but it did not do what the test expected.",
        "This is the useful kind of failure: either the app has a real bug, or "
        "the expectation is out of date. Compare the assertion with the live page.",
    ),
    "AttributeError": (
        "The test called something that does not exist.",
        "A bug in the generated code, not in your app. Check the spelling against "
        "the library's API — e.g. expected_conditions.presence_of_element_located.",
    ),
    "ModuleNotFoundError": (
        "A dependency is missing from this environment.",
        "Run `pip install -r requirements.txt` from the suite directory.",
    ),
}


def _redacted(url: str) -> str:
    """The URL under test, with any embedded credentials removed.

    A base URL can carry basic-auth userinfo (https://user:pass@host). This line
    is printed to a terminal and, in CI, into a log that outlives the run — so
    the credentials come out before it is written anywhere.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    if parts.netloc and "@" in parts.netloc:
        parts = parts._replace(netloc=parts.netloc.rsplit("@", 1)[1])
    return urlunsplit(parts)


def _explain(exc: BaseException) -> tuple[str, str]:
    message = str(exc)
    for needle, cause, fix in _MESSAGE_HINTS:
        if needle in message:
            return cause, fix
    return _DIAGNOSIS.get(
        type(exc).__name__,
        ("The test raised an error before it could finish.",
         "Read the traceback above — the top frame inside tests/ is the line to look at."),
    )


def pytest_exception_interact(node, call, report):
    """Print a plain-language cause and fix next to every failure."""
    if call.excinfo is None:
        return
    exc = call.excinfo.value
    cause, fix = _explain(exc)

    # Selenium messages carry a full remote stacktrace; the first line is the
    # only part that identifies the failure.
    detail = str(exc).strip().splitlines()
    detail = detail[0][:200] if detail else ""

    rule = "─" * 78
    lines = [
        "", rule,
        f"✗ {node.nodeid}",
        f"  Error  {type(exc).__name__}" + (f": {detail}" if detail else ""),
        f"  Cause  {cause}",
        f"  Fix    {fix}",
        f"  URL    {_redacted(BASE_URL)}",
    ]
    # `-x` has no option of its own: it is a shortcut that sets maxfail to 1.
    if node.config.getoption("maxfail", 0):
        lines.append("  Stopping here. After fixing, `pytest --lf` re-runs just this test.")
    lines += [rule, ""]

    # Through the reporter, not print(): pytest's output capture is still active
    # at this point, and a print would be swallowed or shown far from the failure.
    reporter = node.config.pluginmanager.getplugin("terminalreporter")
    if reporter is not None:
        for line in lines:
            reporter.write_line(line)
    else:
        print("\\n".join(lines))
'''


def pytest_conftest(target: Target) -> str:
    """conftest.py carrying base_url — and, for a local target, the app itself.

    Playwright's `webServer` is a Node-only feature, so a Python suite has no
    config that can boot the app. Without the fixture below, `pytest` and CI both
    ran against nothing and every test died on connection-refused; the old
    conftest just mentioned the serve command in a comment and hoped.
    """
    if not target.is_local:
        return f'''"""Shared pytest fixtures — generated by Testra.

Point the suite at any environment without editing this file:
    BASE_URL=https://staging.example.com pytest
"""

import os

import pytest

BASE_URL = os.environ.get("BASE_URL") or {target.base_url!r}


@pytest.fixture(scope="session")
def base_url() -> str:
    """The root URL of the app under test."""
    return BASE_URL
''' + _FAILURE_EXPLAINER

    return f'''"""Shared pytest fixtures — generated by Testra.

Point the suite at any environment without editing this file:
    BASE_URL=https://staging.example.com pytest

With BASE_URL unset, the fixture below starts the app under test itself, so a
fresh clone and CI both work with no other terminal open.
"""

import os
import shlex
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest

BASE_URL = os.environ.get("BASE_URL") or {target.base_url!r}

# Serves the app under test. Run from this directory, so the repo root is "..".
# Adjust if your app starts differently.
SERVE_COMMAND = {target.serve_command!r}
SERVE_PORT = {target.port}
STARTUP_TIMEOUT_SECONDS = 120


def _port_is_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _stop(proc) -> None:
    """Stop the server and everything it spawned.

    The process group is the point. A dev server is usually a launcher that
    execs or forks the real thing (npx -> node), so signalling only the direct
    child can leave the actual server holding the port after the run. An orphan
    like that is worse than noise: the next run finds the port open, skips
    starting anything, and quietly tests a stale build.
    """
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def base_url() -> str:
    """The root URL of the app under test."""
    return BASE_URL


@pytest.fixture(scope="session", autouse=True)
def _app_under_test():
    """Start the app for the session, unless something is already serving it.

    Skipped when BASE_URL is set (you're testing a deployed environment) or when
    the port is already taken (you have a dev server running) — starting a second
    one would fail to bind and take the whole run down with it.
    """
    if os.environ.get("BASE_URL") or _port_is_open(SERVE_PORT):
        yield
        return

    # No shell=True: a shell would be the process we signal, and the server
    # underneath it would survive. start_new_session puts the server in its own
    # process group so _stop can take down the whole tree.
    proc = subprocess.Popen(
        shlex.split(SERVE_COMMAND),
        cwd=Path(__file__).parent,
        start_new_session=(os.name == "posix"),
    )
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while not _port_is_open(SERVE_PORT):
            if proc.poll() is not None:
                raise RuntimeError(
                    f"The app under test exited before it served port {{SERVE_PORT}}: "
                    f"{{SERVE_COMMAND}}"
                )
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"The app under test did not start within {{STARTUP_TIMEOUT_SECONDS}}s: "
                    f"{{SERVE_COMMAND}}"
                )
            time.sleep(0.25)
        yield
    finally:
        _stop(proc)
''' + _FAILURE_EXPLAINER


def selenium_driver_conftest() -> str:
    """The Chrome fixture for a Selenium suite — headed, paced, and watchable.

    This used to be the model's to write, and the model wrote what it was asked
    for: a hardcoded `--headless=new`. That is the right default for CI and the
    wrong one for a person who has just downloaded a suite and wants to watch it
    drive their site. Nothing in a driver factory needs a language model's
    judgement, so it moved here — deterministic, and one less file of output
    tokens per generation.
    """
    return r'''"""Chrome WebDriver fixture — generated by Testra.

Runs **headed by default**. A real browser window opens, and you watch the suite
work through your site: each element is outlined in red a moment before it is
clicked or typed into, and the same step is printed in the terminal as it
happens, so the window and the log tell you the same story.

    pytest                      # watch it run
    pytest -k login             # watch one flow
    HEADLESS=1 pytest           # no window (CI does this automatically)
    STEP_PAUSE_MS=0 pytest      # still narrated, but at full speed
"""

import os
import time

import pytest
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.remote.webelement import WebElement


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


# CI has no display and nobody watching, so it opts itself out of the window, the
# narration and the pacing alike — the suite must not get slower or noisier just
# because the default suits a human.
HEADLESS = _flag("HEADLESS", default=_flag("CI"))
WATCH = not HEADLESS

# Milliseconds to hold on each element before acting on it. Selenium clicks far
# faster than an eye can follow; without this, a headed run is a blur.
STEP_PAUSE_MS = int(os.environ.get("STEP_PAUSE_MS") or (0 if HEADLESS else 400))

# Outlines the element and describes it in one round trip. Two calls would be a
# second of latency across a suite this chatty, for the same two facts.
_ANNOTATE = r"""
var e = arguments[0];
e.style.outline = '3px solid #e11d48';
e.style.outlineOffset = '2px';
e.scrollIntoView({block: 'center', behavior: 'instant'});
var id = e.getAttribute('data-testid') || e.getAttribute('data-cy');
var name = e.tagName.toLowerCase()
    + (id ? '[data-testid="' + id + '"]' : (e.id ? '#' + e.id : ''));
var text = (e.innerText || e.value || e.getAttribute('aria-label') || '').trim();
return name + (text ? '  "' + text.replace(/\s+/g, ' ').slice(0, 40) + '"' : '');
"""

# Set once the session starts. Written through rather than print()ed because
# pytest captures stdout during a test, which would hold every line back until
# the test ended — the exact opposite of watching it happen.
_reporter = None


def _narrate(line: str) -> None:
    if _reporter is not None:
        _reporter.write_line(line)
    else:
        print(line, flush=True)


def _show(element: WebElement, action: str) -> None:
    """Point at the element about to be used, in the browser and the terminal."""
    try:
        label = element.parent.execute_script(_ANNOTATE, element)
    except Exception:
        label = "<element>"  # Narration only. Never fail a test over a label.
    _narrate(f"    → {action:<5} {label}")
    time.sleep(STEP_PAUSE_MS / 1000)


@pytest.fixture(scope="session", autouse=True)
def _watchable_run(pytestconfig):
    """Make every click and keystroke visible, once per session.

    Patching WebElement rather than wrapping the driver is what makes this work
    with page objects that were never written with it in mind: the pause lands on
    the element the test actually touched, however it was found — directly, or
    handed back by a WebDriverWait.
    """
    if not WATCH:
        yield
        return

    global _reporter
    _reporter = pytestconfig.pluginmanager.getplugin("terminalreporter")

    originals = {name: getattr(WebElement, name) for name in ("click", "send_keys")}

    def traced(original, action):
        def wrapper(self, *args, **kwargs):
            _show(self, action)
            return original(self, *args, **kwargs)
        return wrapper

    for name, action in (("click", "click"), ("send_keys", "type")):
        setattr(WebElement, name, traced(originals[name], action))
    try:
        yield
    finally:
        # Restored on the way out: the patch is global to the class, and leaving
        # it in place would follow anything else that imports Selenium in-process.
        for name, original in originals.items():
            setattr(WebElement, name, original)
        _reporter = None


@pytest.fixture
def driver():
    options = Options()
    if HEADLESS:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-dev-shm-usage")

    # --no-sandbox only when Chrome would otherwise refuse to start, which means
    # running as root — the usual state inside a CI container. It switches off the
    # process sandbox, the main thing standing between a hostile page and the
    # machine, so a normal desktop account keeps it on.
    if getattr(os, "geteuid", lambda: -1)() == 0:
        options.add_argument("--no-sandbox")

    # No implicit wait. Mixing one with WebDriverWait makes every poll inside the
    # wait block for the implicit timeout first, so a 10s explicit wait gets two
    # attempts instead of twenty and a missing element takes seconds to report.
    # The page objects wait explicitly; that is the only clock in the suite.
    chrome = webdriver.Chrome(options=options)
    try:
        yield chrome
    finally:
        chrome.quit()
'''


def pytest_ini() -> str:
    """pytest settings for the generated suite.

    `-x` is the deliberate one. A browser suite fails in cascades — one dead
    selector on a shared header takes out every test that navigates through it —
    so a full run buries the first real failure under twenty consequences of it.
    Stopping on the first keeps the terminal showing the thing to fix, which is
    also what makes the diagnostics block in conftest.py worth reading.
    """
    return """[pytest]
testpaths = tests
addopts = -v -x --tb=short
"""


# ── CI pipelines ─────────────────────────────

def _app_install_step(target: Target) -> str:
    """CI has to install the app's own deps before webServer can boot it."""
    if not (target.is_local and target.app_needs_npm_install):
        return ""
    return """      # The webServer command below starts the app under test, which needs
      # the app's own dependencies — not just the suite's.
      - name: Install app dependencies
        run: npm install

"""


def github_workflow(framework_key: str, target: Target) -> str:
    """GitHub Actions pipeline that can actually pass.

    `npm install`, never `npm ci`: a generated suite has no package-lock.json,
    and `npm ci` exits non-zero without one. The install and the manifest are
    written by the same module for exactly this reason.
    """
    base_url_env = """        env:
          # Set a BASE_URL repository variable to test a deployed environment
          # instead of booting the app locally.
          BASE_URL: ${{ vars.BASE_URL }}
"""

    if framework_key == "selenium_java":
        return f"""name: E2E Tests

on:
  push:
    branches: [main, master, develop]
  pull_request:
    branches: [main, master]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-java@v4
        with:
          java-version: '17'
          distribution: temurin
          cache: maven
      - name: Run Selenium tests
        run: mvn -B test
        working-directory: {SUITE_DIR}
{base_url_env}      - name: Upload test results
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: surefire-report
          path: {SUITE_DIR}/target/surefire-reports/
"""

    if is_python(framework_key):
        browsers = (
            "      - name: Install Playwright browsers\n"
            f"        run: python -m playwright install --with-deps chromium\n"
            f"        working-directory: {SUITE_DIR}\n"
            if framework_key == "playwright_python" else ""
        )
        return f"""name: E2E Tests

on:
  push:
    branches: [main, master, develop]
  pull_request:
    branches: [main, master]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: pip
      - name: Install dependencies
        run: pip install -r requirements.txt
        working-directory: {SUITE_DIR}
{browsers}      - name: Run tests
        run: pytest
        working-directory: {SUITE_DIR}
{base_url_env}      - name: Upload test results
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: test-results
          path: {SUITE_DIR}/test-results/
"""

    if framework_key == "cypress_js":
        # Cypress can't boot the app from its config, so which script to run
        # depends on whether there's already something to test: `npm test` wraps
        # the run in a server, `cy:run` doesn't. Playwright needs no such branch
        # — its webServer block makes the same decision itself.
        runner = 'if [ -n "$BASE_URL" ]; then npm run cy:run; else npm test; fi'
    else:
        runner = "npx playwright test"
    browsers = (
        "      - name: Install Playwright browsers\n"
        f"        run: npx playwright install --with-deps chromium\n"
        f"        working-directory: {SUITE_DIR}\n"
        if framework_key == "playwright_js" else ""
    )
    report_path = (
        f"{SUITE_DIR}/cypress/screenshots/" if framework_key == "cypress_js"
        else f"{SUITE_DIR}/playwright-report/"
    )
    return f"""name: E2E Tests

on:
  push:
    branches: [main, master, develop]
  pull_request:
    branches: [main, master]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'

{_app_install_step(target)}      # `npm install`, not `npm ci` — a generated suite ships no lockfile.
      - name: Install test dependencies
        run: npm install
        working-directory: {SUITE_DIR}
{browsers}      - name: Run tests
        run: {runner}
        working-directory: {SUITE_DIR}
{base_url_env}      - name: Upload report
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: e2e-report
          path: {report_path}
"""


def gitlab_ci(framework_key: str, target: Target) -> str:
    """GitLab pipeline.

    Framework-aware, unlike the version this replaces — that one took a
    `framework` argument, ignored it, and handed Cypress and Selenium users a
    Playwright image running `npx playwright test`.
    """
    if framework_key == "selenium_java":
        return f"""stages:
  - test

e2e-tests:
  stage: test
  image: maven:3.9-eclipse-temurin-17
  variables:
    # Set BASE_URL in CI/CD settings to test a deployed environment.
    BASE_URL: ""
  script:
    - cd {SUITE_DIR}
    - mvn -B test
  artifacts:
    when: always
    paths:
      - {SUITE_DIR}/target/surefire-reports/
    expire_in: 1 week
"""

    if is_python(framework_key):
        image = (
            f"mcr.microsoft.com/playwright/python:v{_PLAYWRIGHT_PY_VERSION}-jammy"
            if framework_key == "playwright_python" else "python:3.11"
        )
        return f"""stages:
  - test

e2e-tests:
  stage: test
  image: {image}
  variables:
    BASE_URL: ""
  script:
    - cd {SUITE_DIR}
    - pip install -r requirements.txt
    - pytest
  artifacts:
    when: always
    paths:
      - {SUITE_DIR}/test-results/
    expire_in: 1 week
"""

    if framework_key == "cypress_js":
        return f"""stages:
  - test

e2e-tests:
  stage: test
  image: cypress/included:{_CYPRESS_VERSION}
  variables:
    BASE_URL: ""
  script:
    - cd {SUITE_DIR}
    - npm install
    - npx cypress run
  artifacts:
    when: always
    paths:
      - {SUITE_DIR}/cypress/screenshots/
    expire_in: 1 week
"""

    return f"""stages:
  - test

e2e-tests:
  stage: test
  image: mcr.microsoft.com/playwright:v{_PLAYWRIGHT_VERSION}-jammy
  variables:
    BASE_URL: ""
  script:
    - cd {SUITE_DIR}
    - npm install
    - npx playwright test
  artifacts:
    when: always
    paths:
      - {SUITE_DIR}/playwright-report/
    expire_in: 1 week
"""


# ── README ───────────────────────────────────

# framework -> (install commands, run command, run command against a BASE_URL).
# The two run commands differ only for Cypress: everything else decides for
# itself whether to start the app (Playwright via webServer, pytest via the
# conftest fixture), while Cypress needs a different npm script.
_RUN_COMMANDS = {
    "playwright_js": ("npm install\nnpx playwright install --with-deps chromium", "npx playwright test", "npx playwright test"),
    "cypress_js": ("npm install", "npm test", "npm run cy:run"),
    "playwright_python": ("pip install -r requirements.txt\npython -m playwright install --with-deps chromium", "pytest", "pytest"),
    "selenium_python": ("pip install -r requirements.txt", "pytest", "pytest"),
    "selenium_java": ("mvn -B install -DskipTests", "mvn -B test", "mvn -B test"),
}


# Where the suite's own directory sits once the user has adopted it, and what a
# reader needs to see to believe it will not collide with their app.
_ADOPT_TREES = {
    "python": "    ├── conftest.py\n    ├── requirements.txt\n    └── tests/",
    "js": "    ├── package.json\n    └── tests/",
    "java": "    ├── pom.xml\n    └── src/test/java/",
}

_IGNORES = {
    "playwright_python": f"{SUITE_DIR}/.venv/\n{SUITE_DIR}/.pytest_cache/\n{SUITE_DIR}/__pycache__/\n{SUITE_DIR}/test-results/",
    "selenium_python": f"{SUITE_DIR}/.venv/\n{SUITE_DIR}/.pytest_cache/\n{SUITE_DIR}/__pycache__/",
    "playwright_js": f"{SUITE_DIR}/node_modules/\n{SUITE_DIR}/test-results/\n{SUITE_DIR}/playwright-report/",
    "cypress_js": f"{SUITE_DIR}/node_modules/\n{SUITE_DIR}/cypress/videos/\n{SUITE_DIR}/cypress/screenshots/",
    "selenium_java": f"{SUITE_DIR}/target/",
}


def _lang(framework_key: str) -> str:
    if is_python(framework_key):
        return "python"
    return "js" if is_js(framework_key) else "java"


def _headless_run(framework_key: str, run: str) -> str:
    """The same run command, with the window switched off.

    Only the Selenium suite has a window to switch off by hand; Playwright and
    Cypress are headless unless asked otherwise, and Java's fixture is the
    model's. Getting this wrong would put a no-op environment variable in front
    of a command and imply the default is wrong.
    """
    return f"HEADLESS=1 {run}" if framework_key == "selenium_python" else run


def _readme_watching(framework_key: str) -> str:
    """What the first run looks like — the section that sells the rest.

    Selenium only: it is the one framework here whose fixture this module writes,
    so it is the only one whose on-screen behaviour we can promise.
    """
    if framework_key != "selenium_python":
        return ""
    return """## What you'll see

`pytest` opens a real Chrome window and drives your site in front of you. Each
element is outlined in red just before it is used, and the same step is printed
in the terminal as it happens:

```
tests/test_navigation.py::test_navigate_from_home_to_login
    → click a[data-testid="login"]  "Log in"
    → type  input#email
tests/test_navigation.py::test_navigate_from_home_to_login PASSED
```

At the first failure it stops and tells you what to change:

```
✗ tests/test_navigation.py::test_navigate_from_home_to_login
  Error  NoSuchElementException: Unable to locate element: a[href="/login"]
  Cause  The locator matched no element on the page.
  Fix    Inspect the real element and correct the locator in tests/pages/.
         Prefer a data-testid attribute — it survives restyling.
  Stopping here. After fixing, `pytest --lf` re-runs just this test.
```

| Command | What it does |
|---|---|
| `pytest` | headed and narrated — the default |
| `pytest -k login` | one flow only |
| `pytest --lf` | just what failed last time |
| `STEP_PAUSE_MS=1000 pytest` | slower, for a demo |
| `HEADLESS=1 pytest` | no window, no narration |

"""


def _readme_adopt(framework_key: str, install: str, run: str, target: Target) -> str:
    """Step 1: the archive becomes part of the user's repository.

    The suite arrives as a zip, which makes "where does this go?" the first real
    question — and nothing about a zip answers it. Everything here is the part a
    hand-written suite would never need to say because its author already knew.
    """
    lang = _lang(framework_key)
    if lang == "python":
        isolate = f"""```bash
cd {SUITE_DIR}
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\\Scripts\\activate
{install}
```"""
        why = "The virtualenv keeps the suite's dependencies out of your app's."
    elif lang == "js":
        isolate = f"""```bash
cd {SUITE_DIR}
{install}
```"""
        why = (f"Installing inside `{SUITE_DIR}/` keeps a separate `node_modules`, so your "
               "app's lockfile is untouched.")
    else:
        isolate = f"""```bash
cd {SUITE_DIR}
{install}
```"""
        why = "The suite's `pom.xml` is separate from your app's build."

    pointing = (
        f"""It points at `{target.base_url}` by default. Override that per run:

```bash
BASE_URL=http://localhost:3000 {run}
```"""
        if not target.is_local else
        f"""With `BASE_URL` unset it starts your app itself:

```
{target.serve_command}
```

If your app starts differently, change `SERVE_COMMAND` in `{SUITE_DIR}/conftest.py`.
Set `BASE_URL` to test something already running instead."""
    )

    return f"""## 1 · Add it to your project

Copy `{SUITE_DIR}/` to the **root of your repository**:

```
your-project/
├── src/
├── package.json
└── {SUITE_DIR}/          ← this archive
{_ADOPT_TREES[lang]}
```

It never writes outside its own folder, and nothing in your app imports it.

**Install its dependencies**

{isolate}

{why}

**Point it at your app**

{pointing}

**Ignore the build output** — add to your `.gitignore`:

```gitignore
{_IGNORES[framework_key]}
```

Commit everything else. The tests and page objects are source: reviewing a
selector change in a pull request is the reason they live in the repo.

"""


def _readme_prepush(framework_key: str, run: str) -> str:
    """Step 2: the tests run before a push leaves the machine.

    This is the half of "catch it early" that CI cannot do. CI tells you the
    build is broken after the commit is already on the branch; a pre-push hook
    refuses the push. Both are worth having, and they fail at different moments.
    """
    lang = _lang(framework_key)
    headless = _headless_run(framework_key, run)
    runner = {
        "python": f"cd {SUITE_DIR} && .venv/bin/{headless.split()[-1]}",
        "js": f"npm --prefix {SUITE_DIR} test",
        "java": f"mvn -B -f {SUITE_DIR}/pom.xml test",
    }[lang]
    if lang == "python" and framework_key == "selenium_python":
        runner = f"cd {SUITE_DIR} && HEADLESS=1 .venv/bin/pytest"

    return f"""## 2 · Run it before every push

A `pre-push` hook fails the push when the suite is red, so a broken flow never
reaches the branch in the first place.

```bash
cat > .git/hooks/pre-push <<'EOF'
#!/bin/sh
{runner} || {{
  echo
  echo "E2E tests failed — push aborted."
  echo "Run them yourself to watch it happen:  cd {SUITE_DIR} && {run}"
  exit 1
}}
EOF
chmod +x .git/hooks/pre-push
```

Skip it for one push with `git push --no-verify`.

> `.git/hooks/` is not versioned, so each clone installs the hook once. That is
> why the pipeline below is still worth adding — it is the check nobody can
> forget to install.

"""


def _readme_ci(framework_key: str, target: Target, ci_as_files: bool) -> str:
    """Step 3: the same suite, running in a pipeline.

    Two deliveries, one YAML. When Testra pushes to a repository it created, the
    workflow is written in — that is the feature the user asked for, and telling
    them to paste a file they already have is nonsense. When they downloaded an
    archive, the workflow is quoted instead: they will unzip it into a project
    that usually already has a pipeline, and a second workflow duplicating their
    test job is a surprise, not a feature.

    Both branches read from github_workflow/gitlab_ci, so the instructions cannot
    describe a pipeline different from the one that shipped.
    """
    if ci_as_files:
        return """## 3 · The pipeline is already wired up

`.github/workflows/e2e-tests.yml` runs this suite on every push and pull
request, and `.gitlab-ci.yml` does the same on GitLab. Both install from the
manifest in the suite directory — nothing else to set up.

To test a deployed environment instead of the default, set a `BASE_URL`
variable in your repository settings (GitHub: *Settings → Secrets and variables
→ Actions → Variables*). Leave it unset and the pipeline uses the default.

"""

    return f"""## 3 · Add it to your pipeline

Optional, and only if you want the suite to run on every push and pull request
as well. Save this as `.github/workflows/e2e-tests.yml` in your repo:

```yaml
{github_workflow(framework_key, target).rstrip()}
```

<details>
<summary>GitLab CI — <code>.gitlab-ci.yml</code></summary>

```yaml
{gitlab_ci(framework_key, target).rstrip()}
```

</details>

Set a `BASE_URL` variable in your repository settings to test a deployed
environment; leave it unset and the pipeline uses the default above.

"""


def _readme_failures(framework_key: str, challenges: str) -> str:
    """What to do about a red suite — the section people arrive at in a hurry."""
    where = {
        "python": "`tests/pages/`, one class per page",
        "js": "the page objects, one per page",
        "java": "the page objects, as `@FindBy` fields",
    }[_lang(framework_key)]

    return f"""## When a test fails

A failing test usually means the UI moved, not that the suite is broken. The
terminal names the file and the locator to change.

- **Selectors live in {where}** — one UI change is one edit, however many tests
  depend on it.
- **Add a `data-testid` to anything you keep testing.** It is the one attribute
  a redesign will not silently take away, and it is the first thing the
  generator looks for — ahead of element IDs, ARIA roles, then CSS classes.
- **Anything marked `selector not verified`** was not found in the source that
  was read. Confirm those before trusting the test.

**Known gaps:** {challenges}

"""


def readme(
    *,
    framework_key: str,
    language: str,
    stack: str,
    target: Target,
    files: list,
    project_summary: str,
    testing_challenges: list[str],
    ci_included: bool = True,
    ci_as_files: bool = False,
    failed_files: list[str] | None = None,
    test_count: int | None = None,
) -> str:
    """Setup instructions for the framework that was actually generated.

    Three things this has to get right, each of which it once got wrong:

    * **The right framework.** It printed `npm install` and `npx playwright test`
      for every framework, so a Selenium/pytest user was told to run Playwright.

    * **An honest account of the archive.** It described a CI pipeline
      unconditionally, while the writer agent withholds output whenever a planned
      file fails to generate — so a run that lost its specs shipped a README
      promising files that weren't there, next to a suite collecting zero tests.
      The incomplete run was indistinguishable from a complete one. It now says
      what is missing before it says how to run anything.

    * **One place per fact.** Base URL, run command and install steps each used to
      appear in two or three sections that could drift apart. The structure below
      is a sequence — see it run, adopt it, guard the push, wire the pipeline —
      and each fact belongs to exactly one step of it.
    """
    install, run, _ = _RUN_COMMANDS.get(framework_key, _RUN_COMMANDS["playwright_js"])
    challenges = "; ".join(testing_challenges) or "none recorded."

    # ── incomplete-run banner ────────────────────────────────────────────────
    # Two separate failures, either of which makes the archive unrunnable:
    # planned files that never came back, and a suite that ends up with no test
    # cases at all (every spec failed, leaving only page objects — which collect
    # as zero tests and exit non-zero).
    missing = list(failed_files or [])
    banner = ""
    if missing or test_count == 0:
        lines = ["> ## ⚠️ This suite is incomplete — do not run it as-is", ">"]
        if missing:
            shown = ", ".join(f"`{f}`" for f in missing[:5])
            more = f" (and {len(missing) - 5} more)" if len(missing) > 5 else ""
            lines += [f"> **{len(missing)} planned file(s) were never generated:** {shown}{more}", ">"]
        if test_count == 0:
            lines += [
                "> **No test cases were produced.** What shipped are page objects and",
                f"> configuration only, so `{run}` will collect 0 tests and exit non-zero.",
                ">",
            ]
        lines += [
            "> This usually means the run hit a provider rate limit or quota partway",
            "> through — regenerating once capacity is back normally produces the full",
            "> suite.",
            "",
        ]
        banner = "\n".join(lines) + "\n"

    tree = "\n".join(
        f"- `{f.filename[len(SUITE_DIR) + 1:] if f.filename.startswith(SUITE_DIR + '/') else f.filename}`"
        f" — {f.description}"
        for f in files if f.description
    )

    sections = "".join([
        _readme_watching(framework_key),
        _readme_adopt(framework_key, install, run, target),
        _readme_prepush(framework_key, run),
        _readme_ci(framework_key, target, ci_as_files) if ci_included else "",
        _readme_failures(framework_key, challenges),
    ])

    return f"""{banner}# E2E Test Suite

Generated by [Testra](https://testra.duckdns.org) for a **{stack}** app, using
**{framework_key}** ({language}). {project_summary}

Everything lives in `{SUITE_DIR}/` and ships with its own dependency manifest, so
it never collides with your app's.

## Quick start

```bash
cd {SUITE_DIR}
{install}
{run}
```

{sections}## What's in here

{tree}

---
*These tests passed a static check, not a real run — the first `{run}` is yours.*
"""
