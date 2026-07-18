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


def _is_local_url(url: str) -> tuple[bool, int]:
    """Whether a URL points at this machine, and the port it names."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False, 0
    host = (parsed.hostname or "").lower()
    if host not in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
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
    """
    url = (base_url or "").strip() or "http://localhost:3000"
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
        return (
            f"selenium=={_SELENIUM_PY_VERSION}\n"
            "pytest==8.3.3\n"
            "webdriver-manager==4.0.2\n"
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
'''

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
'''


def pytest_ini() -> str:
    return """[pytest]
testpaths = tests
addopts = -v --tb=short
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


def readme(
    *,
    framework_key: str,
    language: str,
    stack: str,
    target: Target,
    files: list,
    project_summary: str,
    testing_challenges: list[str],
) -> str:
    """Setup instructions for the framework that was actually generated.

    The version this replaces printed `npm install` and `npx playwright test`
    for every framework, so a Selenium/pytest user was told to run Playwright.
    """
    install, run, run_remote = _RUN_COMMANDS.get(framework_key, _RUN_COMMANDS["playwright_js"])
    file_list = "\n".join(f"- `{f.filename}` — {f.description}" for f in files)

    if target.is_local:
        serving = f"""The config boots the app for you before the tests run:

```
{target.serve_command}
```

If your app starts differently, edit the `webServer` command in the config file.
"""
    else:
        serving = (
            f"Tests run against `{target.base_url}`, which is expected to be "
            "already deployed and reachable.\n"
        )

    challenges = "\n".join(f"- {c}" for c in testing_challenges) or "None recorded."

    return f"""# Generated E2E Test Suite

Auto-generated by [Testra](https://testra.duckdns.org) for a **{stack}** application.

- **Framework:** {framework_key} ({language})
- **Lives in:** `{SUITE_DIR}/` — self-contained, with its own dependency manifest,
  so it never collides with your app's.

## Project summary
{project_summary}

## Quick start

```bash
cd {SUITE_DIR}
{install}
{run}
```

## Choosing an environment

`BASE_URL` overrides everything else, so the same suite runs anywhere:

```bash
BASE_URL=https://staging.example.com {run_remote}
```

Default target: `{target.base_url}`

{serving}
## CI

`.github/workflows/e2e-tests.yml` (and `.gitlab-ci.yml`) run this suite on every
push and pull request. Both install from the manifest in `{SUITE_DIR}/` — nothing
else to wire up.

To test a deployed environment instead of booting the app in CI, set a `BASE_URL`
repository variable (GitHub: *Settings → Secrets and variables → Actions →
Variables*). Leave it unset and CI starts the app itself.

## Generated files
{file_list}

## Selector strategy
Selectors are taken from your source, in this priority order:

1. `data-testid` / `data-cy` attributes (most stable)
2. Element IDs
3. ARIA roles and accessible names
4. CSS classes

> ⚠️ Anything marked `selector not verified` was not found in the source Testra
> read. Confirm those before relying on the test.

## Known gaps
{challenges}

---
*Generated by Testra. These tests passed a static check, not a real run — the
first `{run}` is yours.*
"""
