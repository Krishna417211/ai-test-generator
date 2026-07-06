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
import asyncio
from dataclasses import dataclass, field
from typing import AsyncGenerator, Optional

from agents.filter_agent import FilterResult
from services.llm_router import router
from services.validator import validate_files

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
- Use page.waitForSelector() for dynamic content
- Use page.waitForLoadState('networkidle') after navigation
- Include beforeAll/beforeEach for setup
- Use test.describe() for grouping related tests
- Add await page.screenshot() on failures for debugging
- Sample structure:
  tests/
    pages/LoginPage.ts
    pages/DashboardPage.ts
    specs/login.spec.ts
    specs/dashboard.spec.ts
    playwright.config.ts
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
- Maven project structure with pom.xml
- Page Object Model using @FindBy annotations (PageFactory)
- WebDriverManager for automatic driver management
- TestNG: @BeforeClass, @AfterClass, @Test annotations
- ThreadLocal<WebDriver> for parallel execution readiness
- Data-driven tests using @DataProvider
""",
}

CI_TEMPLATES = {
    "playwright": """
name: Playwright Tests

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
      - name: Install dependencies
        run: npm ci
      - name: Install Playwright browsers
        run: npx playwright install --with-deps chromium
      - name: Run Playwright tests
        run: npx playwright test
      - name: Upload test results
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: playwright-report
          path: playwright-report/
""",
    "cypress": """
name: Cypress Tests

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

jobs:
  cypress-run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Cypress run
        uses: cypress-io/github-action@v6
        with:
          build: npm run build
          start: npm start
          wait-on: 'http://localhost:3000'
""",
    "selenium": """
name: Selenium Tests

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Install dependencies
        run: pip install -r requirements.txt
      - name: Run Selenium tests
        run: pytest tests/ -v --tb=short
        env:
          HEADLESS: 'true'
""",
}


# ─────────────────────────────────────────────
# Output types
# ─────────────────────────────────────────────

@dataclass
class GeneratedFile:
    filename: str
    content: str
    description: str


@dataclass
class WriterResult:
    files: list[GeneratedFile]
    framework: str
    test_count: int
    selector_warnings: list[str]
    summary: str
    validation: list[dict] = field(default_factory=list)   # per-file syntax validity


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
    ) -> WriterResult:

        framework_key = self._normalize_framework(framework, language)

        # If the SSE stream already generated the suite, parse that instead of
        # making a second (duplicate, costly) LLM call for the same job.
        if pregenerated_raw and pregenerated_raw.strip():
            generated_files = self._parse_response(pregenerated_raw, framework_key)
        else:
            generated_files = await self._generate_tests(
                filter_result=filter_result,
                framework_key=framework_key,
                framework_instructions=FRAMEWORK_INSTRUCTIONS.get(
                    framework_key, FRAMEWORK_INSTRUCTIONS["playwright_js"]
                ),
                test_flows=test_flows,
                base_url=base_url,
                language=language,
            )

        # Self-heal: if any generated file fails static validation, ask the LLM
        # to fix it. (Off by default — costs extra LLM calls.)
        if self_heal:
            for _ in range(max_heal_attempts):
                failing = [v for v in validate_files(generated_files, framework_key) if not v.ok]
                if not failing:
                    break
                logger.info(f"Self-heal: fixing {len(failing)} invalid file(s)")
                generated_files = await self._heal(generated_files, failing, framework_key)

        # Add CI/CD yaml
        if include_ci:
            ci_framework = framework.split("_")[0]  # playwright, cypress, selenium
            ci_yaml = CI_TEMPLATES.get(ci_framework, CI_TEMPLATES["playwright"])
            generated_files.append(GeneratedFile(
                filename=".github/workflows/e2e-tests.yml",
                content=ci_yaml.strip(),
                description="GitHub Actions CI/CD pipeline for automatic test runs",
            ))
            # Also add GitLab CI
            generated_files.append(GeneratedFile(
                filename=".gitlab-ci.yml",
                content=self._gitlab_ci(ci_framework),
                description="GitLab CI pipeline",
            ))

        # Post-processing: selector validation
        warnings = self._validate_selectors(generated_files, filter_result.files)

        # Count tests
        test_count = self._count_tests(generated_files, framework_key)

        # Build README
        readme = self._generate_readme(
            filter_result, framework_key, language, base_url, generated_files
        )
        generated_files.append(GeneratedFile(
            filename="README.md",
            content=readme,
            description="Setup and running instructions for the generated test suite",
        ))

        # Final static validation of the whole suite (for the "validated" signal).
        validations = validate_files(generated_files, framework_key)
        validation = [{"filename": v.filename, "ok": v.ok, "error": v.error} for v in validations]
        valid_count = sum(1 for v in validations if v.ok)

        return WriterResult(
            files=generated_files,
            framework=framework_key,
            test_count=test_count,
            selector_warnings=warnings,
            validation=validation,
            summary=(
                f"Generated {test_count} tests across {len(generated_files)} files "
                f"for {filter_result.framework} app using {framework_key}. "
                f"{valid_count}/{len(validation)} files passed syntax validation."
            ),
        )

    async def _heal(
        self,
        files: list[GeneratedFile],
        failing: list,
        framework_key: str,
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
            )
            healed = self._parse_response(raw, framework_key)
            return healed or files
        except Exception as e:
            logger.error(f"Self-heal failed: {e}")
            return files

    async def stream_run(
        self,
        filter_result: FilterResult,
        framework: str,
        language: str,
        test_flows: str,
        base_url: str = "http://localhost:3000",
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
        ):
            yield chunk

    # ─────────────────────────────────────────
    # Prompt construction
    # ─────────────────────────────────────────

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
6. Include a config file (playwright.config.ts / cypress.config.ts / etc.)

Generate comprehensive tests now:
""".strip()

    async def _generate_tests(
        self,
        filter_result: FilterResult,
        framework_key: str,
        framework_instructions: str,
        test_flows: str,
        base_url: str,
        language: str,
    ) -> list[GeneratedFile]:
        prompt = self._build_prompt(
            filter_result, framework_key, test_flows, base_url, language
        )

        try:
            raw = await router.complete(
                prompt=prompt,
                system_prompt=self._system_prompt(framework_key),
                temperature=0.15,
                context_hint="writer_agent",
                json_mode=True,
            )
        except Exception as e:
            logger.error(f"Writer agent error: {e}")
            raise

        return self._parse_response(raw, framework_key)

    def _parse_response(self, raw: str, framework_key: str) -> list[GeneratedFile]:
        """
        Parse the LLM's JSON response into GeneratedFile objects.
        Shared by both the direct (`_generate_tests`) and streamed paths so the
        downloaded suite matches exactly what the user watched being generated.
        Falls back to a single raw file if the response isn't valid JSON.
        """
        raw = (raw or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)

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
    ) -> list[str]:
        """
        Check that selectors used in the generated tests actually exist in the
        source. Validates ids, data-testid/data-cy, and bare class selectors
        (not just ids). Flags suspected hallucinations and annotates the file.
        """
        avail = self._extract_available_selectors(source_files)
        source_ids = set(avail["ids"])
        source_testids = set(avail["testids"]) | set(avail["data_cy"])
        source_classes = set(avail["classes"])
        warnings: list[str] = []

        for gf in generated_files:
            content = gf.content

            # IDs: "#someId"
            for id_ref in set(re.findall(r'["\']#([a-zA-Z][\w-]+)["\']', content)):
                if id_ref not in source_ids:
                    warnings.append(f"{gf.filename}: ID `#{id_ref}` not found in source — verify selector")
                    gf.content = gf.content.replace(
                        f'"#{id_ref}"', f'"#{id_ref}" /* ⚠️ selector not verified */'
                    )

            # data-testid / data-cy: getByTestId('x'), [data-testid="x"], cy.get('[data-cy=x]')
            testid_refs = set(re.findall(r'getByTestId\(["\']([^"\']+)["\']\)', content))
            testid_refs |= set(re.findall(r'data-(?:testid|cy)=["\']?([\w-]+)', content))
            for t in testid_refs:
                if t not in source_testids:
                    warnings.append(f"{gf.filename}: test-id `{t}` not found in source — verify selector")

            # Bare class selectors: ".some-class"
            for cls in set(re.findall(r'["\']\.([a-zA-Z][\w-]+)["\']', content)):
                if cls not in source_classes:
                    warnings.append(f"{gf.filename}: class `.{cls}` not found in source — verify selector")

        # De-duplicate and cap so a wall of warnings doesn't bury the signal.
        return sorted(set(warnings))[:40]

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

    def _gitlab_ci(self, framework: str) -> str:
        return f"""stages:
  - test

e2e-tests:
  stage: test
  image: mcr.microsoft.com/playwright:v1.44.0-jammy
  script:
    - npm ci
    - npx playwright test
  artifacts:
    when: always
    paths:
      - playwright-report/
    expire_in: 1 week
"""

    def _generate_readme(
        self,
        filter_result: FilterResult,
        framework_key: str,
        language: str,
        base_url: str,
        files: list[GeneratedFile],
    ) -> str:
        file_list = "\n".join(f"- `{f.filename}` — {f.description}" for f in files)
        return f"""# Generated E2E Test Suite

Auto-generated by [Testra](https://testra.xyz) for a **{filter_result.framework}** application.

## Framework
**{framework_key}** ({language})

## Project Summary
{filter_result.project_summary}

## Generated Files
{file_list}

## Setup

```bash
npm install
npx playwright install --with-deps   # if using Playwright
```

## Running Tests

```bash
# Run all tests
npx playwright test

# Run with UI mode
npx playwright test --ui

# Run specific test file
npx playwright test tests/specs/login.spec.ts
```

## Base URL
Tests are configured to run against: `{base_url}`

To change this, update the `baseURL` in `playwright.config.ts`.

## Selector Strategy
Tests use this selector priority:
1. `data-testid` attributes (most stable)
2. ARIA roles and labels
3. CSS class names from the source code
4. Element IDs

> ⚠️ Selectors marked with `/* ⚠️ Selector not verified */` should be manually confirmed.

## Potential Issues
{chr(10).join(f"- {c}" for c in filter_result.testing_challenges) or "No known issues."}

---
*Generated by Testra — paste your repo, get tests in seconds.*
"""
