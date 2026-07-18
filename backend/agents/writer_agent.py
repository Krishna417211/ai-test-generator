"""
writer_agent.py — Agent 2: Test Script Generator

Takes the filtered files + user config and produces:
  1. Page Object Model (POM) classes
  2. Test spec files with multiple test cases
  3. CI/CD YAML (GitHub Actions + GitLab CI)
  4. A README for the generated test suite

Framework support: Playwright (JS/TS/Python), Cypress (JS/TS), Selenium (Python/Java/JS)
"""

import re
import json
import logging
from dataclasses import dataclass, field
from typing import AsyncGenerator, Optional

from agents import scaffold
from agents.filter_agent import FilterResult
from services.llm_router import router, Tier
from services.validator import validate_files
from services import grounding as grounding_svc
from services import fragility as fragility_svc

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Framework-specific prompt instructions
# ─────────────────────────────────────────────

FRAMEWORK_INSTRUCTIONS = {
    "playwright_js": """
Generate tests using Playwright with JavaScript/TypeScript.
- Use @playwright/test with test() and expect() from '@playwright/test'
- Page Object Model: create a class per page in a /pages/ directory
- Selectors priority: data-testid > aria-label > role > CSS class > XPath (last resort)
- Prefer web-first assertions (await expect(locator).toBeVisible()) — they retry
  automatically. Do NOT use waitForLoadState('networkidle'); Playwright's own
  docs discourage it, and it hangs on apps that poll or hold a socket open.
- Never call page.screenshot() for debugging — screenshot/trace on failure is
  configured centrally in playwright.config.ts
- Include beforeEach for setup; group with test.describe()
- Do NOT write playwright.config.ts, package.json or a README — they are
  generated for you and yours would be discarded.
- Sample structure:
  tests/
    pages/LoginPage.ts
    pages/DashboardPage.ts
    specs/login.spec.ts
    specs/dashboard.spec.ts
""",
    "playwright_python": """
Generate tests using Playwright with Python (pytest-playwright).
- Import: from playwright.sync_api import Page, expect
- Use fixtures for browser and page setup
- Class-based Page Objects in tests/pages/
- Use page.locator() with CSS or ARIA selectors
- Use expect(locator).to_be_visible() for assertions
- Include conftest.py with shared fixtures
- Group tests with pytest classes and test_ prefix methods
""",
    "cypress_js": """
Generate tests using Cypress with JavaScript/TypeScript.
- Use cy.get(), cy.contains(), cy.visit(), cy.request()
- Custom commands in cypress/support/commands.ts for reusable actions
- Page Object pattern using classes exported from cypress/pages/
- cy.intercept() for API mocking where needed
- cy.session() for authentication state persistence
- Use data-cy attributes when present; fall back to data-testid, then CSS
- Include cypress.config.ts with baseUrl and viewport settings
""",
    "selenium_python": """
Generate tests using Selenium WebDriver with Python.
- Use unittest.TestCase or pytest
- WebDriverWait with expected_conditions for all dynamic interactions
- Page Object Model: one class per page in tests/pages/
- Use By.CSS_SELECTOR, By.ID, By.XPATH (in that priority order)
- Include setup_class / teardown_class
- Driver factory in tests/conftest.py or tests/driver_factory.py
- Support headless Chrome by default
""",
    "selenium_java": """
Generate tests using Selenium WebDriver with Java (TestNG or JUnit 5).
- Page Object Model using @FindBy annotations (PageFactory)
- WebDriverManager for automatic driver management
- TestNG: @BeforeClass, @AfterClass, @Test annotations
- ThreadLocal<WebDriver> for parallel execution readiness
- Data-driven tests using @DataProvider
- Read the base URL from the environment, never hardcode it:
  `String baseUrl = System.getenv().getOrDefault("BASE_URL", "<the base url given below>");`
  and resolve pages against it. CI sets BASE_URL to pick an environment, so a
  hardcoded host silently ignores it.
- Do NOT write pom.xml — it is generated for you and yours would be discarded.
- Run headless when the CI env var is set.
""",
}

# CI pipelines, dependency manifests, framework configs and the README are
# deterministic boilerplate and live in agents/scaffold.py. They used to be
# templates here, and the Playwright one ran `npm ci` against a suite that
# shipped no package.json and no lockfile — a pipeline that could not pass on
# any repo. Writing the manifest and the install step in one module is what
# stops that from drifting apart again.


# ─────────────────────────────────────────────
# Output types
# ─────────────────────────────────────────────

@dataclass
class GeneratedFile:
    filename: str
    content: str
    description: str


@dataclass
class Grounding:
    """What we actually verified about a generated suite — and nothing more.

    Deliberately NOT called "accuracy". Accuracy would mean "these tests pass
    against your app", and we never find that out: validator.py does a syntax
    check precisely because running the suite needs Docker and a live target
    (see its module docstring). What we can prove is narrower and worth saying
    plainly:

      • selectors_verified / selectors_total — every id, data-testid and class
        the model wrote, checked back against the user's real source.
      • files_valid / files_checked        — every code file we could parse.

    Reporting these two under their own names is the point. Rolling them into
    one number labelled "94% accurate" would invite the user to trust the suite,
    run it, watch it fail, and never come back — the exact disappointment this
    is meant to prevent. A low number here is also not a reason to sell an
    upgrade: it usually means the repo has few stable selectors, which a better
    model cannot invent.
    """
    selectors_total: int = 0
    selectors_verified: int = 0
    files_checked: int = 0      # code files a real parser looked at
    files_valid: int = 0
    heal_attempts: int = 0      # times we sent invalid files back to the model

    @property
    def selector_rate(self) -> Optional[float]:
        """None, not 1.0, when there were no selectors to check.

        A suite that referenced nothing has not earned a perfect score, and
        showing "100% verified" for it would be the most confident lie here.
        """
        if not self.selectors_total:
            return None
        return round(self.selectors_verified / self.selectors_total, 3)

    @property
    def file_rate(self) -> Optional[float]:
        if not self.files_checked:
            return None
        return round(self.files_valid / self.files_checked, 3)

    def as_dict(self) -> dict:
        return {
            "selectors_total": self.selectors_total,
            "selectors_verified": self.selectors_verified,
            "selector_rate": self.selector_rate,
            "files_checked": self.files_checked,
            "files_valid": self.files_valid,
            "file_rate": self.file_rate,
            "heal_attempts": self.heal_attempts,
        }


@dataclass
class WriterResult:
    files: list[GeneratedFile]
    framework: str
    test_count: int
    selector_warnings: list[str]
    summary: str
    validation: list[dict] = field(default_factory=list)   # per-file syntax validity
    # Planned files the LLM never produced (quota ran out mid-run, etc). A suite
    # missing pieces can't pass CI, so callers must not treat this as success.
    failed_files: list[str] = field(default_factory=list)
    grounding: Grounding = field(default_factory=Grounding)
    # Richer, provenance-carrying grounding (services/grounding.py) and the
    # break-risk report (services/fragility.py). Dicts, not dataclasses, because
    # these travel straight to the API response and the TrustPanel.
    selector_grounding: dict = field(default_factory=dict)
    fragility: dict = field(default_factory=dict)


# ─────────────────────────────────────────────
# Writer Agent
# ─────────────────────────────────────────────

class WriterAgent:
    """
    Agent 2: Generates complete, production-ready test scripts.

    Chunking strategy for large repos:
    - If all files fit in one prompt → single LLM call
    - If too large → generate POM classes first, then test specs separately
    """

    async def run(
        self,
        filter_result: FilterResult,
        framework: str,              # e.g. "playwright_js", "cypress_js"
        language: str,               # "javascript", "typescript", "python", "java"
        test_flows: str,             # user's free-text description of what to test
        base_url: str = "http://localhost:3000",
        include_ci: bool = True,
        pregenerated_raw: Optional[str] = None,  # reuse output already produced by stream_run
        self_heal: bool = False,        # re-prompt the LLM to fix files that don't parse
        max_heal_attempts: int = 1,
        tier: Tier = Tier.FREE,         # model quality this caller's plan entitles them to
        live_url: Optional[str] = None,  # a deployed URL the user owns → DOM grounding
    ) -> WriterResult:

        framework_key = self._normalize_framework(framework, language)
        self._failed_files: list[str] = []

        # If the SSE stream already generated the suite, reuse it instead of
        # making a second (costly) LLM call — but only if it parsed cleanly.
        # A large repo can truncate the streamed JSON at the output-token cap,
        # which yields the single "JSON parsing failed" fallback; in that case
        # we regenerate robustly (file-by-file) so no files are silently lost.
        generated_files: list[GeneratedFile] = []
        if pregenerated_raw and pregenerated_raw.strip():
            generated_files = self._parse_response(pregenerated_raw, framework_key)
            if self._looks_truncated(generated_files):
                logger.info("Streamed output was truncated/invalid — regenerating file-by-file")
                generated_files = []

        if not generated_files:
            generated_files = await self._generate_tests(
                filter_result=filter_result,
                framework_key=framework_key,
                test_flows=test_flows,
                base_url=base_url,
                language=language,
                tier=tier,
            )

        # DOM grounding: if the user gave us a URL they own, fetch the live page
        # once and index its real anchors. Best-effort — an unreachable or
        # private URL degrades to source-only grounding rather than failing the
        # whole generation. Nothing is ever executed; we only read the HTML.
        dom_index = None
        if live_url:
            try:
                html = await grounding_svc.fetch_dom(live_url)
                dom_index = grounding_svc.build_dom_index(html)
                logger.info(f"DOM grounding: indexed {len(dom_index.anchors)} anchors from {live_url}")
            except Exception as e:
                logger.warning(f"DOM grounding unavailable for {live_url}: {e} — using source only")

        # Self-heal: if any generated file fails static validation, ask the LLM
        # to fix it. (Off by default — costs extra LLM calls.)
        heal_attempts = 0
        if self_heal:
            for _ in range(max_heal_attempts):
                failing = [v for v in validate_files(generated_files, framework_key) if not v.ok]
                if not failing:
                    break
                logger.info(f"Self-heal: fixing {len(failing)} invalid file(s)")
                generated_files = await self._heal(generated_files, failing, framework_key, tier)
                heal_attempts += 1

            # Grounding heal: a file can parse cleanly and still target a
            # selector that does not exist. Feed the model the selectors that
            # missed and the real anchors that DO exist, so it repairs them
            # instead of the user discovering it on the first run.
            for _ in range(max_heal_attempts):
                report = grounding_svc.ground_suite(
                    generated_files, filter_result.files, dom_index=dom_index
                )
                unverified = report.unverified()
                if not unverified:
                    break
                logger.info(f"Grounding heal: repairing {len(unverified)} unverified selector(s)")
                generated_files = await self._heal_selectors(
                    generated_files, unverified, filter_result.files,
                    dom_index, framework_key, tier,
                )
                heal_attempts += 1

        # Everything the model wrote moves under e2e/, and anything it wrote
        # that scaffold owns (a config, a manifest) is dropped in favour of the
        # generated one. Relative imports between specs and page objects survive
        # the move because every file shifts by the same prefix.
        generated_files = self._place_in_suite_dir(generated_files)

        # Post-processing: selector validation. Runs before the scaffold is added
        # so a config file can't produce "selector not found" noise.
        warnings, sel_total, sel_verified = self._validate_selectors(
            generated_files, filter_result.files
        )

        # Count tests
        test_count = self._count_tests(generated_files, framework_key)

        # How this suite reaches the app: derived from the detected stack and the
        # user's base URL, and shared by the config, the CI and the README so the
        # three cannot disagree about what starts the app or where it listens.
        target = scaffold.plan_target(filter_result.framework, base_url, framework_key)
        generated_files.extend(self._scaffold_files(framework_key, target))

        # CI last, and only for a complete suite: the workflow runs the whole
        # suite, so shipping it next to missing files guarantees a red pipeline
        # on the first push.
        if include_ci and self._failed_files:
            logger.warning(
                f"Skipping CI workflow — {len(self._failed_files)} planned file(s) "
                f"were never generated: {', '.join(self._failed_files[:5])}"
            )
        if include_ci and not self._failed_files:
            generated_files.append(GeneratedFile(
                filename=".github/workflows/e2e-tests.yml",
                content=scaffold.github_workflow(framework_key, target),
                description="GitHub Actions pipeline — installs and runs the suite on every push/PR",
            ))
            generated_files.append(GeneratedFile(
                filename=".gitlab-ci.yml",
                content=scaffold.gitlab_ci(framework_key, target),
                description="GitLab CI pipeline",
            ))

        # README last — it lists every file above it.
        generated_files.append(GeneratedFile(
            filename=f"{scaffold.SUITE_DIR}/README.md",
            content=scaffold.readme(
                framework_key=framework_key,
                language=language,
                stack=filter_result.framework,
                target=target,
                files=generated_files,
                project_summary=filter_result.project_summary,
                testing_challenges=filter_result.testing_challenges,
            ),
            description="Setup and running instructions for the generated test suite",
        ))

        # Final static validation of the whole suite (for the "validated" signal).
        validations = validate_files(generated_files, framework_key)
        validation = [
            {"filename": v.filename, "ok": v.ok, "error": v.error, "checked": v.checked}
            for v in validations
        ]
        # Only files a parser actually looked at. The README and the CI yaml
        # pass by default, and counting them would inflate the rate we print.
        checked = [v for v in validations if v.checked]
        valid_count = sum(1 for v in checked if v.ok)

        # Richer grounding (provenance + optional live-DOM) and break-risk. These
        # are computed on the final suite and drive the TrustPanel. The legacy
        # Grounding counts stay sourced from _validate_selectors above so the
        # existing "N/M verified" signal is unchanged.
        ground_report = grounding_svc.ground_suite(
            generated_files, filter_result.files, dom_index=dom_index
        )
        fragility_report = fragility_svc.analyze(generated_files)

        grounding = Grounding(
            selectors_total=sel_total,
            selectors_verified=sel_verified,
            files_checked=len(checked),
            files_valid=valid_count,
            heal_attempts=heal_attempts,
        )

        return WriterResult(
            files=generated_files,
            framework=framework_key,
            test_count=test_count,
            selector_warnings=warnings,
            validation=validation,
            failed_files=self._failed_files,
            grounding=grounding,
            selector_grounding=ground_report.as_dict(),
            fragility=fragility_report.as_dict(),
            summary=(
                f"Generated {test_count} tests across {len(generated_files)} files "
                f"for {filter_result.framework} app using {framework_key}. "
                f"{valid_count}/{len(checked)} code files parsed cleanly"
                + (
                    f"; {sel_verified}/{sel_total} selectors verified against your source."
                    if sel_total
                    else "; no selectors to verify against the source."
                )
            ),
        )

    # Files scaffold.py owns. The model is told not to write these, but it is a
    # language model and sometimes writes one anyway; ours wins, because ours is
    # the one the generated CI was built against.
    _SCAFFOLD_OWNED = (
        "package.json", "package-lock.json", "requirements.txt", "pom.xml",
        "playwright.config.ts", "playwright.config.js", "playwright.config.mjs",
        "cypress.config.ts", "cypress.config.js", "conftest.py", "pytest.ini",
        "tsconfig.json", "serve.json", "readme.md",
    )

    def _place_in_suite_dir(self, files: list[GeneratedFile]) -> list[GeneratedFile]:
        """Move the model's files under SUITE_DIR and drop the ones we own.

        The suite gets its own directory because the repo root is already taken:
        a React app has a package.json there, and writing ours over it would
        destroy the manifest of the app under test.
        """
        placed: list[GeneratedFile] = []
        for f in files:
            name = f.filename.lstrip("./")
            # Anchored to the suite root, not matched on basename: only a file
            # that would land ON one of ours conflicts. A nested
            # tests/README.md or a fixture named package.json is the model's to
            # keep, and dropping those silently lost real work.
            if name.lower() in self._SCAFFOLD_OWNED:
                logger.info(f"Dropping model-written {name} — scaffold owns this file")
                continue
            if not name.startswith(f"{scaffold.SUITE_DIR}/"):
                name = f"{scaffold.SUITE_DIR}/{name}"
            placed.append(GeneratedFile(name, f.content, f.description))
        return placed

    def _scaffold_files(
        self, framework_key: str, target: scaffold.Target
    ) -> list[GeneratedFile]:
        """The manifest + config that make the model's tests runnable."""
        d = scaffold.SUITE_DIR
        out: list[GeneratedFile] = []

        if scaffold.is_js(framework_key):
            out.append(GeneratedFile(
                f"{d}/package.json",
                scaffold.package_json(framework_key, target),
                "Dependency manifest — `npm install` here installs the test runner",
            ))
        elif scaffold.is_python(framework_key):
            out.append(GeneratedFile(
                f"{d}/requirements.txt",
                scaffold.requirements_txt(framework_key),
                "Python dependencies for the suite",
            ))
        elif framework_key == "selenium_java":
            out.append(GeneratedFile(
                f"{d}/pom.xml",
                scaffold.pom_xml(),
                "Maven project descriptor — dependencies and test runner",
            ))

        for name, content in target.serve_files.items():
            out.append(GeneratedFile(
                f"{d}/{name}",
                content,
                "Static server settings — serves pages at their real paths",
            ))

        if framework_key == "playwright_js":
            out.append(GeneratedFile(
                f"{d}/playwright.config.ts",
                scaffold.playwright_config(target),
                "Playwright config — baseURL, reporters, and the app's dev server",
            ))
        elif framework_key == "cypress_js":
            out.append(GeneratedFile(
                f"{d}/cypress.config.ts",
                scaffold.cypress_config(target),
                "Cypress config — baseUrl and spec discovery",
            ))
            out.append(GeneratedFile(
                f"{d}/tests/support/e2e.ts",
                scaffold.cypress_support_file(),
                "Cypress support file — loaded before every spec",
            ))
            out.append(GeneratedFile(
                f"{d}/tsconfig.json",
                scaffold.cypress_tsconfig(),
                "TypeScript settings — Cypress won't compile .ts specs without it",
            ))
        elif scaffold.is_python(framework_key):
            out.append(GeneratedFile(
                f"{d}/conftest.py",
                scaffold.pytest_conftest(target),
                "Shared pytest fixtures, including the base URL",
            ))
            out.append(GeneratedFile(
                f"{d}/pytest.ini",
                scaffold.pytest_ini(),
                "pytest configuration",
            ))

        return out

    async def _heal(
        self,
        files: list[GeneratedFile],
        failing: list,
        framework_key: str,
        tier: Tier = Tier.FREE,
    ) -> list[GeneratedFile]:
        """Ask the LLM to fix files that failed static validation."""
        errors = "\n".join(f"- {v.filename}: {v.error}" for v in failing)
        current = json.dumps(
            {"files": [
                {"filename": f.filename, "description": f.description, "content": f.content}
                for f in files
            ]},
            indent=2,
        )
        prompt = f"""Some generated {framework_key} test files have syntax/validity errors:
{errors}

Here is the current file set as JSON:
{current}

Return the COMPLETE corrected file set in the SAME JSON structure
({{"files":[{{"filename","description","content"}}]}}). Fix the broken files so
they are valid, runnable code. Keep the valid files unchanged."""
        try:
            raw = await router.complete(
                prompt=prompt,
                system_prompt=self._system_prompt(framework_key),
                temperature=0.1,
                context_hint="writer_agent_heal",
                json_mode=True,
                tier=tier,
            )
            healed = self._parse_response(raw, framework_key)
            return healed or files
        except Exception as e:
            logger.error(f"Self-heal failed: {e}")
            return files

    async def _heal_selectors(
        self,
        files: list[GeneratedFile],
        unverified: list,
        source_files: dict[str, str],
        dom_index,
        framework_key: str,
        tier: Tier = Tier.FREE,
    ) -> list[GeneratedFile]:
        """Re-prompt the model to replace selectors that don't exist in the app.

        The prompt is grounded, not open-ended: it names the exact selectors that
        missed and hands over the real anchors that DO exist (from source and,
        when we have it, the live DOM), so the fix is a substitution the model
        can make correctly rather than another guess.
        """
        src_index = grounding_svc.build_source_index(source_files)
        real = self._available_anchor_hint(src_index, dom_index)
        misses = "\n".join(
            f"- {g.file}: `{g.selector}` — not found"
            + (f" (missing {', '.join(f'{a.kind}={a.value}' for a in g.missing)})" if g.missing else "")
            for g in unverified[:40]
        )
        current = json.dumps(
            {"files": [
                {"filename": f.filename, "description": f.description, "content": f.content}
                for f in files
            ]},
            indent=2,
        )
        prompt = f"""Some selectors in this {framework_key} suite target elements that DO NOT
exist in the application. Each must be replaced with one that does.

Selectors that were not found:
{misses}

These are the selectors that ACTUALLY EXIST in the app — use only these:
{real}

Here is the current file set as JSON:
{current}

Return the COMPLETE file set in the SAME JSON structure
({{"files":[{{"filename","description","content"}}]}}). Replace only the
not-found selectors with real ones from the list above; prefer data-testid and
role locators. Leave everything else unchanged."""
        try:
            raw = await router.complete(
                prompt=prompt,
                system_prompt=self._system_prompt(framework_key),
                temperature=0.1,
                context_hint="writer_agent_selector_heal",
                json_mode=True,
                tier=tier,
            )
            healed = self._parse_response(raw, framework_key)
            return healed or files
        except Exception as e:
            logger.error(f"Selector self-heal failed: {e}")
            return files

    def _available_anchor_hint(self, src_index, dom_index, cap: int = 60) -> str:
        """A compact, kind-grouped list of anchors the app really has."""
        from collections import defaultdict
        by_kind: dict[str, set] = defaultdict(set)
        for idx in (src_index, dom_index):
            if idx is None:
                continue
            for a in idx.anchors:
                by_kind[a.kind].add(a.value)
        lines = []
        for kind in ("testid", "id", "role", "label", "name", "class", "text"):
            vals = sorted(by_kind.get(kind, ()))[:cap]
            if vals:
                lines.append(f"  {kind}: {', '.join(vals)}")
        return "\n".join(lines) or "  (no stable anchors found in the source)"

    async def stream_run(
        self,
        filter_result: FilterResult,
        framework: str,
        language: str,
        test_flows: str,
        base_url: str = "http://localhost:3000",
        tier: Tier = Tier.FREE,
    ) -> AsyncGenerator[str, None]:
        """Streaming version — yields content chunks for real-time display."""
        framework_key = self._normalize_framework(framework, language)
        prompt = self._build_prompt(filter_result, framework_key, test_flows, base_url, language)

        async for chunk in router.stream_complete(
            prompt=prompt,
            system_prompt=self._system_prompt(framework_key),
            temperature=0.15,
            context_hint="writer_agent",
            json_mode=True,
            tier=tier,
        ):
            yield chunk

    # ─────────────────────────────────────────
    # Prompt construction
    # ─────────────────────────────────────────

    def _navigation_hint(self, stack: str) -> str:
        """How to navigate *this* stack, and where the base URL comes from.

        The relative-path rule is what makes the generated config's baseURL (and
        the BASE_URL override behind it) mean anything: a test that hardcodes
        http://localhost:3000/login ignores both and can only ever run on the
        machine it was written for.
        """
        common = (
            "- The framework config sets baseURL. Navigate with RELATIVE paths only.\n"
            "  NEVER hardcode a host or port in a test — it overrides the configured\n"
            "  environment and pins the suite to one machine.\n"
            "- Write the path WITHOUT a leading slash: page.goto('login.html'),\n"
            "  cy.visit('cart.html'). A leading slash resolves against the host root\n"
            "  and silently discards any sub-path in baseURL — which is how a site\n"
            "  deployed to a GitHub Pages project URL (example.github.io/my-repo/)\n"
            "  ends up 404ing every page.\n"
        )
        if "static html" in (stack or "").lower():
            return common + (
                "- This is a MULTI-PAGE STATIC SITE, not a SPA. Every page is a real\n"
                "  file served at its own path: navigate to 'login.html', not 'login'.\n"
                "  Read the actual filenames in the source below and use those exactly.\n"
                "- There is no client-side router and no build step. A click that\n"
                "  changes page triggers a full document load, so assert against the\n"
                "  new page's elements rather than waiting for a route transition.\n"
                "- State (auth, cart) lives in localStorage. To set up or reset a\n"
                "  precondition, drive the UI or seed localStorage directly.\n"
            )
        return common

    def _system_prompt(self, framework_key: str) -> str:
        return f"""You are a senior QA engineer specializing in end-to-end test automation.
You write production-quality, maintainable test scripts that real QA teams can use immediately.

Your tests:
- Use REAL selectors found in the actual source code (never make up IDs or classes)
- Include thorough assertions that catch real bugs
- Follow Page Object Model rigorously
- Have clear, descriptive test names
- Handle async operations correctly (proper waits, not setTimeout hacks)
- Include meaningful comments explaining WHY each step exists
- Are ready to run without modification

Framework: {framework_key}
{FRAMEWORK_INSTRUCTIONS.get(framework_key, '')}"""

    def _build_prompt(
        self,
        filter_result: FilterResult,
        framework_key: str,
        test_flows: str,
        base_url: str,
        language: str,
    ) -> str:
        # Serialize the filtered files
        files_json = json.dumps(filter_result.files, indent=2)

        # Ground the model on selectors that ACTUALLY exist in the source.
        sel = self._extract_available_selectors(filter_result.files)

        def _fmt(items: list[str], n: int) -> str:
            return ", ".join(items[:n]) if items else "(none found)"

        available_selectors = (
            f"data-testid: {_fmt(sel['testids'], 60)}\n"
            f"data-cy:     {_fmt(sel['data_cy'], 60)}\n"
            f"id:          {_fmt(sel['ids'], 60)}\n"
            f"name (form fields): {_fmt(sel['names'], 60)}\n"
            f"class (sample):     {_fmt(sel['classes'], 80)}"
        )

        return f"""
Generate a complete E2E test suite for this {filter_result.framework} web application.

## PROJECT ANALYSIS
{filter_result.project_summary}

## DETECTED ROUTES
{json.dumps(filter_result.routes, indent=2)}

## KEY PAGES TO TEST
{json.dumps(filter_result.key_pages, indent=2)}

## TESTING CHALLENGES TO HANDLE
{json.dumps(filter_result.testing_challenges, indent=2)}

## USER'S TEST FLOWS
{test_flows or "Test all main user flows including navigation, forms, and key interactions."}

## BASE URL
{base_url}

## NAVIGATION RULES
{self._navigation_hint(filter_result.framework)}
## AVAILABLE SELECTORS (extracted from the real source — prefer these; anything else is a guess)
{available_selectors}

## SOURCE CODE FILES
{files_json}

## YOUR TASK
Generate a complete, production-ready test suite using {framework_key}.

Return a JSON object with this exact structure:
{{
  "files": [
    {{
      "filename": "tests/pages/LoginPage.ts",
      "description": "Page Object for login page",
      "content": "// Full file content here..."
    }},
    {{
      "filename": "tests/specs/login.spec.ts",
      "description": "Login flow test cases",
      "content": "// Full file content here..."
    }}
  ]
}}

CRITICAL RULES:
1. Only use selectors (IDs, class names, data attributes) that ACTUALLY EXIST in the source code above
2. If you must guess a selector, add a comment: // ⚠️ WARNING: Selector may need verification
3. Generate at least 3 test cases per major page/flow
4. Include positive AND negative test cases (happy path + error states)
5. All imports must be correct for {framework_key}
6. Do NOT write a config file, package.json, requirements.txt or a README. Those
   are generated separately and anything you write with those names is discarded.
7. Paths are relative to the suite root: use "tests/...", not "e2e/tests/...".

Generate comprehensive tests now:
""".strip()

    async def _generate_tests(
        self,
        filter_result: FilterResult,
        framework_key: str,
        test_flows: str,
        base_url: str,
        language: str,
        tier: Tier = Tier.FREE,
    ) -> list[GeneratedFile]:
        """
        Generate the suite file-by-file: first plan the file list (a small,
        non-truncatable response), then generate each file's content in its own
        call. This keeps every response well under the output-token cap, so a
        large suite can't be silently truncated the way the old single-JSON-blob
        approach was. Falls back to single-shot if planning yields nothing.
        """
        plan = await self._plan_files(filter_result, framework_key, test_flows, base_url, tier)
        if not plan:
            logger.info("File planning produced nothing — falling back to single-shot generation")
            return await self._generate_single_shot(filter_result, framework_key, test_flows, base_url, language, tier)

        manifest = [p["filename"] for p in plan]
        files: list[GeneratedFile] = []
        failed: list[str] = []
        last_error: Optional[Exception] = None

        for spec in plan:
            try:
                content = await self._generate_one_file(
                    spec, manifest, filter_result, framework_key, test_flows, base_url, language, tier
                )
            except Exception as e:
                failed.append(spec["filename"])
                last_error = e
                continue
            if content and content.strip():
                files.append(GeneratedFile(
                    filename=spec["filename"],
                    content=content,
                    description=spec.get("description", ""),
                ))
            else:
                failed.append(spec["filename"])

        # Nothing usable — fall back to single-shot, which raises if it also fails
        # (the caller degrades to pushing the project without tests).
        if not files:
            if last_error is not None:
                raise last_error
            return await self._generate_single_shot(
                filter_result, framework_key, test_flows, base_url, language, tier
            )

        self._failed_files = failed
        return files

    async def _plan_files(
        self, filter_result: FilterResult, framework_key: str, test_flows: str, base_url: str,
        tier: Tier = Tier.FREE,
    ) -> list[dict]:
        """Ask the LLM for just the file manifest (names + roles) — a tiny response that can't truncate."""
        prompt = f"""Plan an E2E test suite for this {filter_result.framework} app using {framework_key}.

## PROJECT
{filter_result.project_summary}

## ROUTES
{json.dumps(filter_result.routes, indent=2)}

## KEY PAGES
{json.dumps(filter_result.key_pages, indent=2)}

## USER'S TEST FLOWS
{test_flows or "Cover all main user flows: navigation, forms, auth, key interactions."}

List the files the suite needs — a Page Object class per key page, and spec files
grouped by page/flow. Aim for 3-8 focused files (max 12).

Do NOT list a config file, package.json, requirements.txt, pom.xml or a README.
Those are generated separately and yours would be discarded. Paths are relative
to the suite root, so use "tests/..." — not "e2e/tests/...".

Return ONLY JSON (no markdown, no prose):
{{"files":[
  {{"filename":"tests/pages/LoginPage.ts","description":"Page Object for the login page","kind":"page_object"}},
  {{"filename":"tests/specs/login.spec.ts","description":"Login happy-path + error cases","kind":"spec"}}
]}}"""
        try:
            raw = await router.complete(
                prompt=prompt,
                system_prompt=self._system_prompt(framework_key),
                temperature=0.1,
                context_hint="writer_plan",
                json_mode=True,
                tier=tier,
            )
            data = json.loads(self._strip_fence(raw))
            plan = [f for f in data.get("files", []) if isinstance(f, dict) and f.get("filename")]
            return plan[:12]
        except Exception as e:
            logger.warning(f"File planning failed: {e}")
            return []

    async def _generate_one_file(
        self, spec: dict, manifest: list[str], filter_result: FilterResult,
        framework_key: str, test_flows: str, base_url: str, language: str,
        tier: Tier = Tier.FREE,
    ) -> str:
        """Generate the raw contents of a single planned file (small output → no truncation)."""
        sel = self._extract_available_selectors(filter_result.files)

        def _fmt(items: list[str], n: int) -> str:
            return ", ".join(items[:n]) if items else "(none found)"

        available = (
            f"data-testid: {_fmt(sel['testids'], 60)}\n"
            f"data-cy:     {_fmt(sel['data_cy'], 60)}\n"
            f"id:          {_fmt(sel['ids'], 60)}\n"
            f"name:        {_fmt(sel['names'], 60)}\n"
            f"class:       {_fmt(sel['classes'], 80)}"
        )
        prompt = f"""Generate ONE file of an E2E test suite ({framework_key}) for this {filter_result.framework} app.

## FILE TO WRITE
{spec['filename']} — {spec.get('description', '')}

## ALL FILES IN THE SUITE (so imports/paths line up)
{json.dumps(manifest, indent=2)}

## PROJECT
{filter_result.project_summary}

## ROUTES
{json.dumps(filter_result.routes, indent=2)}

## USER'S TEST FLOWS
{test_flows or "Cover the main user flows for this file's page/area."}

## BASE URL
{base_url}

## NAVIGATION RULES
{self._navigation_hint(filter_result.framework)}
## AVAILABLE SELECTORS (from the real source — prefer these; anything else is a guess)
{available}

## SOURCE CODE
{json.dumps(filter_result.files, indent=2)}

Output ONLY the raw contents of {spec['filename']} — no JSON, no markdown fences, no
commentary. Use only selectors that exist above; if you must guess, add a comment
`// ⚠️ WARNING: Selector may need verification`. For spec files, include at least 3
test cases covering positive and negative paths."""
        try:
            raw = await router.complete(
                prompt=prompt,
                system_prompt=self._system_prompt(framework_key),
                temperature=0.15,
                context_hint="writer_file",
                json_mode=False,
                tier=tier,
            )
            return self._strip_fence(raw)
        except Exception as e:
            # Swallowing this used to turn "the LLM died mid-suite" into a silent
            # gap: the file vanished, the run still looked successful, and CI got
            # pushed against a suite that couldn't run. Let the caller record it.
            logger.error(f"Generating {spec['filename']} failed: {e}")
            raise

    async def _generate_single_shot(
        self, filter_result: FilterResult, framework_key: str,
        test_flows: str, base_url: str, language: str, tier: Tier = Tier.FREE,
    ) -> list[GeneratedFile]:
        """Legacy single-call generation (whole suite as one JSON blob). Fallback only."""
        prompt = self._build_prompt(filter_result, framework_key, test_flows, base_url, language)
        try:
            raw = await router.complete(
                prompt=prompt,
                system_prompt=self._system_prompt(framework_key),
                temperature=0.15,
                context_hint="writer_agent",
                json_mode=True,
                tier=tier,
            )
        except Exception as e:
            logger.error(f"Writer agent error: {e}")
            raise
        return self._parse_response(raw, framework_key)

    def _looks_truncated(self, files: list[GeneratedFile]) -> bool:
        """True if a parsed suite looks empty or is the JSON-decode fallback (truncated stream)."""
        if not files:
            return True
        return len(files) == 1 and "JSON parsing failed" in files[0].description

    def _strip_fence(self, raw: str) -> str:
        """Strip a leading/trailing markdown code fence from an LLM response."""
        raw = (raw or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        return raw.strip()

    def _parse_response(self, raw: str, framework_key: str) -> list[GeneratedFile]:
        """
        Parse the LLM's JSON response into GeneratedFile objects.
        Used by the streamed path and the single-shot fallback.
        Falls back to a single raw file if the response isn't valid JSON.
        """
        raw = self._strip_fence(raw)

        try:
            data = json.loads(raw)
            files = []
            for f in data.get("files", []):
                files.append(GeneratedFile(
                    filename=f["filename"],
                    content=f["content"],
                    description=f.get("description", ""),
                ))
            return files
        except json.JSONDecodeError as e:
            logger.error(f"Writer agent returned invalid JSON: {e}")
            # Fallback: treat raw output as a single file
            return [GeneratedFile(
                filename=f"tests/generated.{self._ext(framework_key)}",
                content=raw,
                description="Generated test file (raw output — JSON parsing failed)",
            )]

    # ─────────────────────────────────────────
    # Post-processing
    # ─────────────────────────────────────────

    def _extract_available_selectors(self, source_files: dict[str, str]) -> dict:
        """
        Pull selectors that actually exist in the source markup so we can both
        ground the writer prompt and validate its output. Covers ids,
        data-testid / data-cy, form field names, and class tokens.
        """
        blob = "\n".join(source_files.values())
        raw_classes = re.findall(r'class(?:Name)?=["\']([^"\']+)["\']', blob)
        return {
            "ids": sorted(set(re.findall(r'\bid=["\']([^"\']+)["\']', blob))),
            "testids": sorted(set(re.findall(r'data-testid=["\']([^"\']+)["\']', blob))),
            "data_cy": sorted(set(re.findall(r'data-cy=["\']([^"\']+)["\']', blob))),
            "names": sorted(set(re.findall(r'\bname=["\']([^"\']+)["\']', blob))),
            "classes": sorted({c for group in raw_classes for c in group.split()}),
        }

    def _validate_selectors(
        self,
        generated_files: list[GeneratedFile],
        source_files: dict[str, str],
    ) -> tuple[list[str], int, int]:
        """
        Check that selectors used in the generated tests actually exist in the
        source. Validates ids, data-testid/data-cy, and bare class selectors
        (not just ids). Flags suspected hallucinations and annotates the file.

        Returns (warnings, total_checked, verified). The counts are what the UI
        reports as grounding, so they are taken here — at the one place that
        actually compares each selector against the source — rather than
        recovered later by counting warnings, which would only ever know about
        the failures and could not produce a denominator.
        """
        avail = self._extract_available_selectors(source_files)
        source_ids = set(avail["ids"])
        source_testids = set(avail["testids"]) | set(avail["data_cy"])
        source_classes = set(avail["classes"])
        warnings: list[str] = []
        total = 0
        verified = 0

        for gf in generated_files:
            content = gf.content

            # IDs: "#someId"
            for id_ref in set(re.findall(r'["\']#([a-zA-Z][\w-]+)["\']', content)):
                total += 1
                if id_ref in source_ids:
                    verified += 1
                else:
                    warnings.append(f"{gf.filename}: ID `#{id_ref}` not found in source — verify selector")
                    gf.content = gf.content.replace(
                        f'"#{id_ref}"', f'"#{id_ref}" /* ⚠️ selector not verified */'
                    )

            # data-testid / data-cy: getByTestId('x'), [data-testid="x"], cy.get('[data-cy=x]')
            testid_refs = set(re.findall(r'getByTestId\(["\']([^"\']+)["\']\)', content))
            testid_refs |= set(re.findall(r'data-(?:testid|cy)=["\']?([\w-]+)', content))
            for t in testid_refs:
                total += 1
                if t in source_testids:
                    verified += 1
                else:
                    warnings.append(f"{gf.filename}: test-id `{t}` not found in source — verify selector")

            # Bare class selectors: ".some-class"
            for cls in set(re.findall(r'["\']\.([a-zA-Z][\w-]+)["\']', content)):
                total += 1
                if cls in source_classes:
                    verified += 1
                else:
                    warnings.append(f"{gf.filename}: class `.{cls}` not found in source — verify selector")

        # De-duplicate and cap so a wall of warnings doesn't bury the signal.
        # The counts above are deliberately taken before this cap — they must
        # describe the whole suite, not the first 40 problems with it.
        return sorted(set(warnings))[:40], total, verified

    def _count_tests(self, files: list[GeneratedFile], framework_key: str) -> int:
        count = 0
        for f in files:
            if "playwright" in framework_key or "cypress" in framework_key:
                count += len(re.findall(r'\btest\(', f.content))
                count += len(re.findall(r'\bit\(', f.content))
            elif "selenium" in framework_key:
                count += len(re.findall(r'def test_', f.content))
                count += len(re.findall(r'@Test', f.content))
        return count

    def _normalize_framework(self, framework: str, language: str) -> str:
        fw = framework.lower()
        lang = language.lower()
        if "playwright" in fw:
            return "playwright_python" if "python" in lang else "playwright_js"
        if "cypress" in fw:
            return "cypress_js"
        if "selenium" in fw:
            return "selenium_java" if "java" in lang else "selenium_python"
        return "playwright_js"

    def _ext(self, framework_key: str) -> str:
        if "python" in framework_key:
            return "py"
        if "java" in framework_key:
            return "java"
        return "ts"
