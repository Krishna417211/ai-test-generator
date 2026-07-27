"""Tests for agents/scaffold.py — the runnable skeleton around the LLM's tests.

Everything here is deterministic (no LLM), which is the point: the reasons a
generated suite couldn't be run or wired into CI all lived in this layer, not in
the model's output.
"""

import json

import pytest

from agents import scaffold
from agents.scaffold import SUITE_DIR, plan_target
from agents.writer_agent import GeneratedFile, WriterAgent

STATIC = "Static HTML/JS (multi-page)"
REACT = "React (Vite/CRA)"

JS_FRAMEWORKS = ["playwright_js", "cypress_js"]
PY_FRAMEWORKS = ["playwright_python", "selenium_python"]
ALL_FRAMEWORKS = JS_FRAMEWORKS + PY_FRAMEWORKS + ["selenium_java"]


# ─────────────────────────────────────────────
# Base URL normalization
# ─────────────────────────────────────────────

class TestNormalizeBaseUrl:
    """The trailing slash is what keeps a sub-path deployment addressable."""

    @pytest.mark.parametrize("raw,expected", [
        ("http://localhost:3000", "http://localhost:3000/"),
        ("http://localhost:3000/", "http://localhost:3000/"),
        ("https://x.github.io/my-repo", "https://x.github.io/my-repo/"),
        ("  https://x.github.io/my-repo/  ", "https://x.github.io/my-repo/"),
        ("", "http://localhost:3000/"),
    ])
    def test_always_ends_in_slash(self, raw, expected):
        assert scaffold.normalize_base_url(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("testra.duckdns.org", "https://testra.duckdns.org/"),
        ("testra.duckdns.org/", "https://testra.duckdns.org/"),
        ("example.com/app", "https://example.com/app/"),
        # A dev server almost never has a certificate, so localhost gets http.
        ("localhost:3000", "http://localhost:3000/"),
        ("127.0.0.1:8080", "http://127.0.0.1:8080/"),
        ("[::1]:8080", "http://[::1]:8080/"),
    ])
    def test_a_missing_scheme_is_supplied(self, raw, expected):
        """`testra.duckdns.org` is what a person types, but it is not a URL.

        Selenium rejects it with InvalidArgumentException and `new URL()` reads
        it as a relative path, so a scheme-less entry reached every generated
        config as a base that could never load.
        """
        assert scaffold.normalize_base_url(raw) == expected

    def test_a_scheme_less_localhost_is_still_detected_as_local(self):
        """The scheme is what _is_local_url parses to find the host and port, so
        supplying it is also what lets a local target get its server."""
        t = plan_target(REACT, "localhost:3000", "selenium_python")
        assert t.is_local and t.port == 3000

    def test_subpath_survives_url_resolution(self):
        """The regression this guards: a slashless base silently drops its last
        segment when a test's relative path is resolved against it, so every
        page of a GitHub Pages project site 404s."""
        from urllib.parse import urljoin

        base = scaffold.normalize_base_url("https://x.github.io/my-repo")
        assert urljoin(base, "login.html") == "https://x.github.io/my-repo/login.html"


# ─────────────────────────────────────────────
# plan_target
# ─────────────────────────────────────────────

class TestPlanTarget:
    def test_local_static_site_gets_a_static_server(self):
        t = plan_target(STATIC, "http://localhost:3000")
        assert t.is_local
        assert t.port == 3000
        assert "serve" in t.serve_command
        assert t.serve_deps.get("serve")

    def test_static_server_disables_clean_urls(self):
        """Without this, serve 301s /login.html -> /login: the URL assertions
        break and the local run stops matching the real host."""
        t = plan_target(STATIC, "http://localhost:3000")
        assert "serve.json" in t.serve_command
        assert json.loads(t.serve_files["serve.json"])["cleanUrls"] is False

    def test_remote_target_starts_nothing(self):
        t = plan_target(STATIC, "https://staging.example.com")
        assert not t.is_local
        assert t.serve_command == ""
        assert t.serve_files == {}

    def test_spa_boots_via_npm_and_needs_app_install(self):
        t = plan_target(REACT, "http://localhost:5173")
        assert t.is_local
        assert "npm" in t.serve_command
        assert t.app_needs_npm_install is True

    def test_static_site_needs_no_app_install(self):
        # There is no app package.json to install — that's what "static" means.
        assert plan_target(STATIC, "http://localhost:3000").app_needs_npm_install is False

    def test_port_is_taken_from_the_users_url(self):
        assert plan_target(STATIC, "http://localhost:4321").port == 4321

    def test_unknown_stack_still_serves_something(self):
        t = plan_target("Wat", "http://localhost:8080")
        assert t.is_local and t.port == 8080

    def test_next_wins_over_bare_react(self):
        # "Next.js (React)" contains "React"; the more specific marker must win.
        t = plan_target("Next.js (React)", "http://localhost:3000")
        assert "run dev" in t.serve_command


# ─────────────────────────────────────────────
# Dependency manifests
# ─────────────────────────────────────────────

class TestManifests:
    def test_playwright_manifest_declares_the_runner(self):
        pkg = json.loads(scaffold.package_json("playwright_js", plan_target(STATIC, "http://localhost:3000")))
        assert "@playwright/test" in pkg["devDependencies"]
        assert pkg["scripts"]["test"] == "playwright test"
        assert pkg["private"] is True

    def test_cypress_manifest_declares_cypress_not_playwright(self):
        pkg = json.loads(scaffold.package_json("cypress_js", plan_target(REACT, "https://x.dev")))
        assert "cypress" in pkg["devDependencies"]
        assert "@playwright/test" not in pkg["devDependencies"]

    def test_serve_dependency_ships_when_a_static_server_is_used(self):
        pkg = json.loads(scaffold.package_json("playwright_js", plan_target(STATIC, "http://localhost:3000")))
        assert "serve" in pkg["devDependencies"]

    def test_no_serve_dependency_for_a_remote_target(self):
        pkg = json.loads(scaffold.package_json("playwright_js", plan_target(STATIC, "https://x.dev")))
        assert "serve" not in pkg["devDependencies"]

    def test_manifest_is_valid_json(self):
        json.loads(scaffold.package_json("playwright_js", plan_target(REACT, "http://localhost:5173")))

    @pytest.mark.parametrize("fw,expected", [
        ("playwright_python", "pytest-playwright"),
        ("selenium_python", "selenium"),
    ])
    def test_python_requirements(self, fw, expected):
        assert expected in scaffold.requirements_txt(fw)

    def test_versions_are_pinned_not_floating(self):
        # A suite that resolves a different runner on every CI run can go red
        # without anyone touching it.
        assert "==" in scaffold.requirements_txt("playwright_python")


# ─────────────────────────────────────────────
# Framework config
# ─────────────────────────────────────────────

class TestConfig:
    def test_playwright_config_reads_base_url_from_env(self):
        cfg = scaffold.playwright_config(plan_target(STATIC, "http://localhost:3000"))
        assert "process.env.BASE_URL" in cfg
        assert "http://localhost:3000/" in cfg

    def test_playwright_config_boots_the_app_when_local(self):
        cfg = scaffold.playwright_config(plan_target(STATIC, "http://localhost:3000"))
        assert "webServer" in cfg
        assert "serve" in cfg

    def test_playwright_config_skips_web_server_when_base_url_is_set(self):
        # Testing a deployed environment must not also boot a local server.
        cfg = scaffold.playwright_config(plan_target(STATIC, "http://localhost:3000"))
        assert "process.env.BASE_URL\n    ? undefined" in cfg

    def test_playwright_config_has_no_web_server_for_a_remote_target(self):
        # There is nothing to boot; the key itself must be absent, though the
        # config still says in a comment why.
        cfg = scaffold.playwright_config(plan_target(STATIC, "https://staging.example.com"))
        assert "webServer: process.env" not in cfg
        assert "serve" not in cfg

    def test_cypress_config_reads_base_url_from_env(self):
        cfg = scaffold.cypress_config(plan_target(REACT, "http://localhost:5173"))
        assert "process.env.BASE_URL" in cfg

    def test_conftest_reads_base_url_from_env(self):
        cfg = scaffold.pytest_conftest(plan_target(STATIC, "http://localhost:3000"))
        assert 'os.environ.get("BASE_URL"' in cfg


class TestEveryFrameworkBootsTheApp:
    """A local target must start the app, whatever the framework.

    Only Playwright/JS has a `webServer` config; Cypress and pytest have no such
    thing, so each needs its own mechanism. Without one, `npm test` / `pytest`
    and their pipelines ran against nothing and every test died on
    connection-refused.
    """

    def test_playwright_js_boots_via_web_server(self):
        cfg = scaffold.playwright_config(plan_target(STATIC, "http://localhost:3000"))
        assert "webServer" in cfg and "serve" in cfg

    def test_cypress_boots_via_start_server_and_test(self):
        target = plan_target(STATIC, "http://localhost:3000", "cypress_js")
        pkg = json.loads(scaffold.package_json("cypress_js", target))
        assert "start-server-and-test" in pkg["devDependencies"]
        assert pkg["scripts"]["test"].startswith("start-server-and-test")
        assert "serve" in pkg["scripts"]["serve:app"]

    def test_cypress_does_not_boot_anything_for_a_remote_target(self):
        pkg = json.loads(scaffold.package_json("cypress_js", plan_target(STATIC, "https://x.dev")))
        assert "start-server-and-test" not in pkg["devDependencies"]
        assert pkg["scripts"]["test"] == "cypress run"

    @pytest.mark.parametrize("fw", PY_FRAMEWORKS)
    def test_python_boots_via_a_session_fixture(self, fw):
        cfg = scaffold.pytest_conftest(plan_target(STATIC, "http://localhost:3000", fw))
        assert "subprocess.Popen" in cfg
        assert "autouse=True" in cfg

    @pytest.mark.parametrize("fw", PY_FRAMEWORKS)
    def test_python_server_is_stopped_by_process_group(self, fw):
        """Regression: with shell=True, terminate() killed the shell and left the
        server holding the port. The next run then found the port open, skipped
        starting anything, and tested a stale build.

        Checked against the parsed call, not the source text — the conftest
        explains in a comment why it avoids shell=True, and a substring match
        would find the explanation.
        """
        import ast

        cfg = scaffold.pytest_conftest(plan_target(STATIC, "http://localhost:3000", fw))
        tree = ast.parse(cfg)

        popens = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Popen"
        ]
        assert len(popens) == 1
        kwargs = {kw.arg: kw.value for kw in popens[0].keywords}
        assert "shell" not in kwargs, "a shell would be the process we signal, not the server"
        assert "start_new_session" in kwargs

        assert "killpg" in cfg

    @pytest.mark.parametrize("fw", PY_FRAMEWORKS)
    def test_python_static_server_needs_no_node(self, fw):
        """The selenium_python CI image is python:3.11 and has no npx."""
        t = plan_target(STATIC, "http://localhost:3000", fw)
        assert "http.server" in t.serve_command
        assert "npx" not in t.serve_command
        assert t.serve_deps == {}

    def test_js_static_server_uses_serve(self):
        t = plan_target(STATIC, "http://localhost:3000", "playwright_js")
        assert "npx serve" in t.serve_command

    @pytest.mark.parametrize("fw", PY_FRAMEWORKS)
    def test_python_fixture_skips_when_base_url_is_set(self, fw):
        cfg = scaffold.pytest_conftest(plan_target(STATIC, "http://localhost:3000", fw))
        assert 'if os.environ.get("BASE_URL") or _port_is_open(SERVE_PORT):' in cfg

    @pytest.mark.parametrize("fw", PY_FRAMEWORKS)
    def test_python_conftest_for_a_remote_target_starts_nothing(self, fw):
        cfg = scaffold.pytest_conftest(plan_target(STATIC, "https://x.dev", fw))
        assert "subprocess" not in cfg

    @pytest.mark.parametrize("fw", PY_FRAMEWORKS + ["playwright_js", "cypress_js"])
    def test_generated_conftest_is_valid_python(self, fw):
        import ast

        for url in ("http://localhost:3000", "https://x.dev"):
            ast.parse(scaffold.pytest_conftest(plan_target(STATIC, url, fw)))


# ─────────────────────────────────────────────
# CI pipelines
# ─────────────────────────────────────────────

class TestCI:
    @pytest.mark.parametrize("fw", ALL_FRAMEWORKS)
    def test_workflow_is_valid_yaml(self, fw):
        yaml = pytest.importorskip("yaml")
        wf = yaml.safe_load(scaffold.github_workflow(fw, plan_target(STATIC, "http://localhost:3000")))
        assert wf["jobs"]["test"]["steps"]

    @pytest.mark.parametrize("fw", ALL_FRAMEWORKS)
    def test_gitlab_pipeline_is_valid_yaml(self, fw):
        yaml = pytest.importorskip("yaml")
        assert yaml.safe_load(scaffold.gitlab_ci(fw, plan_target(STATIC, "http://localhost:3000")))["e2e-tests"]

    @pytest.mark.parametrize("fw", JS_FRAMEWORKS)
    def test_never_npm_ci(self, fw):
        """`npm ci` exits non-zero without a lockfile, and a generated suite has
        none — this is why every emitted pipeline used to fail on step one.

        Asserted over the parsed commands rather than the raw text: the workflow
        explains in a comment why it is not `npm ci`, and a substring match would
        catch the explanation.
        """
        yaml = pytest.importorskip("yaml")
        target = plan_target(STATIC, "http://localhost:3000")

        wf = yaml.safe_load(scaffold.github_workflow(fw, target))
        commands = [s["run"] for s in wf["jobs"]["test"]["steps"] if "run" in s]
        assert commands
        assert not any("npm ci" in c for c in commands)

        gl = yaml.safe_load(scaffold.gitlab_ci(fw, target))
        assert not any("npm ci" in c for c in gl["e2e-tests"]["script"])

    @pytest.mark.parametrize("fw", ALL_FRAMEWORKS)
    def test_ci_installs_from_the_suite_directory(self, fw):
        wf = scaffold.github_workflow(fw, plan_target(STATIC, "http://localhost:3000"))
        assert SUITE_DIR in wf

    def test_cypress_pipeline_runs_cypress(self):
        target = plan_target(REACT, "http://localhost:5173")
        assert "cy:run" in scaffold.github_workflow("cypress_js", target)
        assert "playwright" not in scaffold.gitlab_ci("cypress_js", target).lower()

    def test_cypress_pipeline_picks_a_runner_based_on_base_url(self):
        """Cypress can't boot the app from its config, so CI has to choose:
        `npm test` wraps the run in a server, `cy:run` assumes one is up."""
        wf = scaffold.github_workflow("cypress_js", plan_target(STATIC, "http://localhost:3000"))
        assert 'if [ -n "$BASE_URL" ]' in wf
        assert "npm run cy:run" in wf and "npm test" in wf

    def test_python_pipeline_runs_pytest_not_playwright_cli(self):
        wf = scaffold.github_workflow("selenium_python", plan_target(STATIC, "http://localhost:3000"))
        assert "pip install -r requirements.txt" in wf
        assert "npx playwright" not in wf

    def test_java_pipeline_runs_maven(self):
        wf = scaffold.github_workflow("selenium_java", plan_target(STATIC, "http://localhost:3000"))
        assert "mvn -B test" in wf
        assert "pip install" not in wf

    def test_gitlab_pipeline_is_framework_aware(self):
        """Regression: _gitlab_ci took a framework argument and ignored it, so
        Cypress and Selenium users were handed a Playwright pipeline."""
        target = plan_target(REACT, "http://localhost:5173")
        pipelines = {fw: scaffold.gitlab_ci(fw, target) for fw in ALL_FRAMEWORKS}
        assert len(set(pipelines.values())) == len(ALL_FRAMEWORKS)

    def test_ci_passes_base_url_through(self):
        wf = scaffold.github_workflow("playwright_js", plan_target(STATIC, "http://localhost:3000"))
        assert "BASE_URL" in wf

    def test_spa_ci_installs_the_apps_own_dependencies(self):
        # webServer boots the app with `npm run dev`, which needs the app's deps.
        wf = scaffold.github_workflow("playwright_js", plan_target(REACT, "http://localhost:5173"))
        assert "Install app dependencies" in wf

    def test_static_ci_does_not_install_app_dependencies(self):
        wf = scaffold.github_workflow("playwright_js", plan_target(STATIC, "http://localhost:3000"))
        assert "Install app dependencies" not in wf


# ─────────────────────────────────────────────
# README
# ─────────────────────────────────────────────

class TestReadme:
    def _readme(self, fw):
        return scaffold.readme(
            framework_key=fw,
            language="typescript",
            stack=STATIC,
            target=plan_target(STATIC, "http://localhost:3000"),
            files=[GeneratedFile("e2e/tests/specs/a.spec.ts", "x", "spec")],
            project_summary="A shop.",
            testing_challenges=["localStorage state"],
        )

    @pytest.mark.parametrize("fw,expected", [
        ("cypress_js", "npm test"),
        ("selenium_python", "pytest"),
        ("selenium_java", "mvn -B test"),
        ("playwright_js", "npx playwright test"),
    ])
    def test_run_command_matches_the_framework(self, fw, expected):
        """Regression: the README hardcoded Playwright's commands, so a Selenium
        user was told to run `npx playwright test`."""
        assert expected in self._readme(fw)

    def test_selenium_readme_does_not_mention_playwright(self):
        assert "playwright" not in self._readme("selenium_java").lower()

    def test_readme_documents_the_base_url_override(self):
        assert "BASE_URL" in self._readme("playwright_js")

    @pytest.mark.parametrize("fw", ALL_FRAMEWORKS)
    def test_each_fact_appears_in_exactly_one_place(self, fw):
        """The structure is a sequence, and a fact belongs to one step of it.

        Base URL, install and run each used to appear in two or three sections
        that could drift apart — the reason a Selenium user was once told to run
        Playwright in one section and pytest in another.
        """
        readme = self._readme(fw)
        assert readme.count("## Quick start") == 1
        assert readme.count("Add it to your project") == 1
        assert "## Choosing an environment" not in readme

    @pytest.mark.parametrize("fw", ALL_FRAMEWORKS)
    def test_the_headings_read_as_a_sequence(self, fw):
        readme = self._readme(fw)
        for step in ("## 1 · Add it to your project",
                     "## 2 · Run it before every push",
                     "## 3 · Add it to your pipeline"):
            assert step in readme, f"{fw} is missing {step}"
        assert readme.index("## 1 ·") < readme.index("## 2 ·") < readme.index("## 3 ·")


class TestReadmeReportsAnIncompleteSuite:
    """Regression: a run that lost its spec files shipped a README promising a
    CI workflow that wasn't in the archive, next to a suite that collects zero
    tests. Nothing in the download said so, so a partial run was indis-
    tinguishable from a complete one."""

    def _readme(self, **kw):
        defaults = dict(
            framework_key="playwright_python",
            language="python",
            stack=STATIC,
            target=plan_target(STATIC, "http://localhost:3000"),
            files=[GeneratedFile("e2e/tests/pages/LoginPage.py", "x", "page object")],
            project_summary="A shop.",
            testing_challenges=[],
        )
        return scaffold.readme(**{**defaults, **kw})

    def test_complete_suite_still_describes_ci(self):
        readme = self._readme(ci_included=True, failed_files=[], test_count=4)
        assert ".github/workflows/e2e-tests.yml" in readme
        assert "incomplete" not in readme.lower()

    def test_the_pipeline_is_quoted_not_shipped(self):
        """The archive must not drop a workflow into a repo that already has one.

        The YAML comes from the same functions that used to write those files, so
        the instructions and the pipeline cannot drift apart."""
        readme = self._readme(ci_included=True, failed_files=[], test_count=4)
        assert "Save this as `.github/workflows/e2e-tests.yml`" in readme
        assert "name: E2E Tests" in readme
        assert ".gitlab-ci.yml" in readme

    def test_missing_planned_files_are_named(self):
        readme = self._readme(
            ci_included=False,
            failed_files=["e2e/tests/specs/test_cart.py", "e2e/tests/specs/test_login.py"],
            test_count=3,
        )
        assert "incomplete" in readme.lower()
        assert "e2e/tests/specs/test_cart.py" in readme
        assert "e2e/tests/specs/test_login.py" in readme

    def test_zero_tests_is_called_out_even_when_nothing_failed(self):
        """Every planned file can arrive and still leave no test cases if the
        plan itself was only page objects — the archive looks whole and
        collects nothing."""
        readme = self._readme(ci_included=False, failed_files=[], test_count=0)
        assert "No test cases were produced" in readme

    def test_withheld_ci_is_not_advertised(self):
        """Recommending a pipeline next to missing files is recommending a red
        pipeline. The section is absent, not present-and-apologising."""
        readme = self._readme(
            ci_included=False, failed_files=["e2e/tests/specs/test_cart.py"], test_count=0
        )
        assert "## 3 · Add it to your pipeline" not in readme
        assert "name: E2E Tests" not in readme

    def test_defaults_keep_the_old_behaviour_for_existing_callers(self):
        readme = self._readme()
        assert ".github/workflows/e2e-tests.yml" in readme
        assert "⚠️ This suite is incomplete" not in readme


# ─────────────────────────────────────────────
# Suite placement (writer_agent)
# ─────────────────────────────────────────────

class TestSuitePlacement:
    def test_model_files_move_under_the_suite_dir(self):
        out = WriterAgent()._place_in_suite_dir([
            GeneratedFile("tests/specs/login.spec.ts", "x", "spec"),
        ])
        assert out[0].filename == f"{SUITE_DIR}/tests/specs/login.spec.ts"

    def test_relative_layout_is_preserved(self):
        # Every file shifts by the same prefix, so ../pages/X imports still resolve.
        out = WriterAgent()._place_in_suite_dir([
            GeneratedFile("tests/pages/LoginPage.ts", "x", "po"),
            GeneratedFile("tests/specs/login.spec.ts", "y", "spec"),
        ])
        assert [f.filename for f in out] == [
            f"{SUITE_DIR}/tests/pages/LoginPage.ts",
            f"{SUITE_DIR}/tests/specs/login.spec.ts",
        ]

    @pytest.mark.parametrize("owned", [
        "package.json", "playwright.config.ts", "cypress.config.ts",
        "requirements.txt", "conftest.py", "README.md", "pom.xml",
    ])
    def test_scaffold_owned_files_from_the_model_are_dropped(self, owned):
        """Ours is the one the generated CI was built against."""
        out = WriterAgent()._place_in_suite_dir([
            GeneratedFile(owned, "model version", "x"),
            GeneratedFile("tests/specs/a.spec.ts", "keep", "spec"),
        ])
        assert [f.filename for f in out] == [f"{SUITE_DIR}/tests/specs/a.spec.ts"]

    @pytest.mark.parametrize("nested", [
        "tests/README.md",
        "tests/fixtures/package.json",
        "tests/support/conftest.py",
    ])
    def test_nested_files_sharing_a_scaffold_name_are_kept(self, nested):
        """Only a file that lands ON one of ours conflicts. Matching by basename
        threw away real work — a suite's own tests/README.md, say."""
        out = WriterAgent()._place_in_suite_dir([GeneratedFile(nested, "keep", "x")])
        assert [f.filename for f in out] == [f"{SUITE_DIR}/{nested}"]

    def test_already_prefixed_files_are_not_double_prefixed(self):
        out = WriterAgent()._place_in_suite_dir([
            GeneratedFile(f"{SUITE_DIR}/tests/a.spec.ts", "x", "spec"),
        ])
        assert out[0].filename == f"{SUITE_DIR}/tests/a.spec.ts"

    def test_leading_dot_slash_is_normalized(self):
        out = WriterAgent()._place_in_suite_dir([
            GeneratedFile("./tests/a.spec.ts", "x", "spec"),
        ])
        assert out[0].filename == f"{SUITE_DIR}/tests/a.spec.ts"


class TestScaffoldFileSet:
    def _names(self, fw):
        target = plan_target(STATIC, "http://localhost:3000")
        return [f.filename for f in WriterAgent()._scaffold_files(fw, target)]

    def test_playwright_js_ships_a_manifest_and_config(self):
        names = self._names("playwright_js")
        assert f"{SUITE_DIR}/package.json" in names
        assert f"{SUITE_DIR}/playwright.config.ts" in names

    def test_cypress_ships_its_support_file(self):
        # Cypress refuses to start if supportFile is configured but absent.
        assert f"{SUITE_DIR}/tests/support/e2e.ts" in self._names("cypress_js")

    def test_cypress_ships_a_tsconfig(self):
        """ts-loader refuses to start without one (TS18002), so a .ts spec never
        even compiles."""
        assert f"{SUITE_DIR}/tsconfig.json" in self._names("cypress_js")

    def test_cypress_tsconfig_is_valid_json_and_types_cy(self):
        cfg = json.loads(scaffold.cypress_tsconfig())
        assert "cypress" in cfg["compilerOptions"]["types"]

    def test_cypress_declares_typescript(self):
        """Cypress bundles no transpiler (Playwright does), so without this every
        .ts spec dies with "do not have TypeScript installed"."""
        pkg = json.loads(scaffold.package_json("cypress_js", plan_target(STATIC, "http://localhost:3000")))
        assert "typescript" in pkg["devDependencies"]

    def test_cypress_spec_pattern_matches_planned_filenames(self):
        """The planner's own example is tests/specs/login.spec.ts; a pattern that
        only matched *.cy.ts found nothing and exited "no spec files were found"."""
        cfg = scaffold.cypress_config(plan_target(STATIC, "http://localhost:3000"))
        assert "{cy,spec}" in cfg

    def test_python_ships_requirements_and_conftest(self):
        names = self._names("selenium_python")
        assert f"{SUITE_DIR}/requirements.txt" in names
        assert f"{SUITE_DIR}/conftest.py" in names

    def test_java_ships_a_pom(self):
        assert f"{SUITE_DIR}/pom.xml" in self._names("selenium_java")

    def test_static_site_ships_serve_config(self):
        assert f"{SUITE_DIR}/serve.json" in self._names("playwright_js")

    @pytest.mark.parametrize("fw", ALL_FRAMEWORKS)
    def test_every_framework_ships_a_dependency_manifest(self, fw):
        """The defect that made every generated pipeline fail: no manifest, but
        CI ran an install step against one."""
        manifests = {"package.json", "requirements.txt", "pom.xml"}
        assert {n.rsplit("/", 1)[-1] for n in self._names(fw)} & manifests


# ─────────────────────────────────────────────
# A Selenium suite you can watch, and a failure you can act on
# ─────────────────────────────────────────────

class TestTheSuiteIsWatchable:
    """The generated suite's first run is a person watching their own site.

    The driver fixture used to be the model's to write, and the model wrote what
    the prompt asked for: a hardcoded `--headless=new`. That is right for CI and
    wrong for someone who has just downloaded the archive — they get an invisible
    run and a bare traceback. Everything below is now scaffold's, so none of it
    can drift back.
    """

    def _driver_conftest(self):
        return scaffold.selenium_driver_conftest()

    def test_it_parses(self):
        import ast
        ast.parse(self._driver_conftest())

    def test_headed_by_default(self):
        cfg = self._driver_conftest()
        # The flag exists, but only behind the HEADLESS/CI switch — never
        # unconditionally, which is the state this replaces.
        assert '_flag("HEADLESS", default=_flag("CI"))' in cfg
        assert "if HEADLESS:" in cfg
        assert '\n    options.add_argument("--headless=new")' not in cfg

    def test_ci_opts_itself_back_out(self):
        """CI has no display and nobody watching; it must not pay for the pacing."""
        cfg = self._driver_conftest()
        assert '(0 if HEADLESS else 400)' in cfg

    def test_interactions_are_paced_and_highlighted(self):
        cfg = self._driver_conftest()
        assert "STEP_PAUSE_MS" in cfg
        assert "outline" in cfg
        assert 'for name in ("click", "send_keys")' in cfg

    def test_the_class_patch_is_undone(self):
        """It patches WebElement, which is global to the process. Left in place
        it would follow anything else importing Selenium in the same run."""
        cfg = self._driver_conftest()
        assert "finally:" in cfg
        assert "setattr(WebElement, name, original)" in cfg

    def test_the_sandbox_stays_on_unless_chrome_refuses_to_start(self):
        """--no-sandbox switches off the process sandbox — the main thing between
        a hostile page and the machine. Only root (i.e. a CI container) needs it."""
        import ast

        cfg = self._driver_conftest()
        # Checked against the tree, not the text: the conftest explains in a
        # comment why it guards the flag, and a substring match would find the
        # explanation whether or not the guard survived.
        tree = ast.parse(cfg)
        guarded = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and any(
                isinstance(c, ast.Constant) and c.value == "--no-sandbox"
                for c in ast.walk(node)
            )
        ]
        assert len(guarded) == 1, "--no-sandbox must be added under exactly one guard"
        assert "geteuid" in ast.unparse(guarded[0].test)

    def test_no_implicit_wait(self):
        """Mixing one with WebDriverWait makes each poll block for the implicit
        timeout, so a 10s explicit wait gets two attempts instead of twenty."""
        assert "implicitly_wait" not in self._driver_conftest()

    def test_selenium_manager_resolves_the_driver(self):
        """webdriver-manager fetches a binary over the network at import time and
        buys nothing Selenium 4.6+ does not already do."""
        assert "webdriver-manager" not in scaffold.requirements_txt("selenium_python")


class TestFailuresExplainThemselves:
    @pytest.mark.parametrize("fw", PY_FRAMEWORKS)
    def test_the_run_stops_at_the_first_failure(self, fw):
        """A browser suite fails in cascades: one dead selector on a shared header
        takes out every test that navigates through it, burying the real one."""
        assert "-x" in scaffold.pytest_ini().split("addopts")[1]

    @pytest.mark.parametrize("fw", PY_FRAMEWORKS)
    def test_both_python_frameworks_get_the_explainer(self, fw):
        for url in ("http://localhost:3000", "https://staging.example.com"):
            cfg = scaffold.pytest_conftest(plan_target(STATIC, url, fw))
            assert "def pytest_exception_interact" in cfg

    def test_the_explainer_imports_no_browser_library(self):
        """It is keyed on the exception's class name, as a string, so the same
        block ships in a Selenium suite and a Playwright one."""
        cfg = scaffold.pytest_conftest(plan_target(STATIC, "https://x.dev", "playwright_python"))
        explainer = cfg.split("Failure diagnostics")[1]
        assert "import selenium" not in explainer
        assert "from selenium" not in explainer

    @pytest.mark.parametrize("name", [
        "NoSuchElementException", "TimeoutException", "AssertionError",
        "ElementClickInterceptedException", "StaleElementReferenceException",
        "InvalidArgumentException", "AttributeError",
    ])
    def test_the_failures_a_generated_suite_actually_hits_are_covered(self, name):
        cfg = scaffold.pytest_conftest(plan_target(STATIC, "https://x.dev", "selenium_python"))
        assert name in cfg

    def test_credentials_never_reach_the_terminal(self):
        """A base URL can carry basic-auth userinfo, and this line is printed into
        CI logs that outlive the run."""
        cfg = scaffold.pytest_conftest(plan_target(STATIC, "https://x.dev", "selenium_python"))
        # Run the generated function itself rather than assert on its source —
        # the point is the behaviour, and the source is a template.
        ns: dict = {}
        source = "def _redacted" + cfg.split("def _redacted")[1].split("def _explain")[0]
        exec(source, ns)
        assert ns["_redacted"]("https://user:hunter2@x.dev/app") == "https://x.dev/app"
        assert ns["_redacted"]("https://x.dev/app") == "https://x.dev/app"


class TestScaffoldOwnsTheDriverFixture:
    def _names(self, fw):
        target = plan_target(REACT, "https://x.dev", fw)
        return {f.filename for f in WriterAgent()._scaffold_files(fw, target)}

    def test_selenium_ships_a_generated_driver_fixture(self):
        assert f"{SUITE_DIR}/tests/conftest.py" in self._names("selenium_python")

    def test_playwright_python_does_not(self):
        """pytest-playwright already supplies the browser, so that file stays the
        model's — it is where real per-suite fixtures belong."""
        assert f"{SUITE_DIR}/tests/conftest.py" not in self._names("playwright_python")

    def test_a_model_written_driver_fixture_is_dropped(self):
        """Ours is the one the rest of the suite was generated against."""
        agent = WriterAgent()
        mine = [GeneratedFile("tests/conftest.py", "webdriver.Chrome(headless)", "d")]
        assert agent._place_in_suite_dir(mine, "selenium_python") == []

    def test_but_kept_for_playwright_python(self):
        agent = WriterAgent()
        mine = [GeneratedFile("tests/conftest.py", "@pytest.fixture", "d")]
        kept = agent._place_in_suite_dir(mine, "playwright_python")
        assert [f.filename for f in kept] == [f"{SUITE_DIR}/tests/conftest.py"]


class TestTheRunIsNarratedLive:
    """The browser window is only half of "watch it happen".

    A headed run shows what the page does; the terminal has to say what the suite
    is doing to it, as it goes. Both stop for CI, which has neither a display nor
    anyone reading.
    """

    def test_each_interaction_is_printed_as_it_happens(self):
        cfg = scaffold.selenium_driver_conftest()
        assert '_narrate(f"    → {action:<5} {label}")' in cfg
        assert '("click", "click"), ("send_keys", "type")' in cfg

    def test_narration_bypasses_pytest_capture(self):
        """Regression risk: a plain print() is captured during the test and
        flushed only when it ends — so the log would arrive after the fact, which
        is the one thing this must not do."""
        cfg = scaffold.selenium_driver_conftest()
        assert 'getplugin("terminalreporter")' in cfg
        assert "_reporter.write_line(line)" in cfg

    def test_the_label_costs_one_round_trip(self):
        """Outline, scroll and describe in a single execute_script. Split across
        three calls it would be a second of latency over a suite this chatty."""
        cfg = scaffold.selenium_driver_conftest()
        assert cfg.count("execute_script") == 1
        for behaviour in ("style.outline", "scrollIntoView", "data-testid"):
            assert behaviour in cfg

    def test_headless_pays_for_none_of_it(self):
        cfg = scaffold.selenium_driver_conftest()
        assert "WATCH = not HEADLESS" in cfg
        assert "if not WATCH:" in cfg

    def test_full_speed_is_still_narrated(self):
        """STEP_PAUSE_MS=0 turns off the pacing, not the log — the two are
        separate switches because 'too slow' and 'too quiet' are different
        complaints."""
        cfg = scaffold.selenium_driver_conftest()
        assert "if STEP_PAUSE_MS <= 0" not in cfg

    def test_the_generated_file_compiles_without_warnings(self):
        """The annotate script carries a JS regex (\\s+), which is an invalid
        Python escape unless the literal is raw."""
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error", SyntaxWarning)
            compile(scaffold.selenium_driver_conftest(), "conftest.py", "exec")


class TestReadmeExplainsHowToAdoptTheSuite:
    """The suite arrives as a zip, which makes "where do I put this?" the first
    real question — and the one the README never answered."""

    def _guide(self, fw, url="https://x.dev"):
        target = plan_target(REACT, url, fw)
        text = scaffold.readme(
            framework_key=fw, language="l", stack=REACT, target=target, files=[],
            project_summary="s", testing_challenges=[],
        )
        return text[text.index("## 1 · Add it to your project"):text.index("## 2 · Run it before every push")]

    @pytest.mark.parametrize("fw", ALL_FRAMEWORKS)
    def test_every_framework_gets_the_whole_walkthrough(self, fw):
        guide = self._guide(fw)
        for step in ("root of your repository", "**Install its dependencies**",
                     "**Point it at your app**", "**Ignore the build output**"):
            assert step in guide, f"{fw} is missing {step}"

    @pytest.mark.parametrize("fw", ALL_FRAMEWORKS)
    def test_it_says_where_the_folder_goes(self, fw):
        assert "root of your repository" in self._guide(fw)
        assert "your-project/" in self._guide(fw)

    @pytest.mark.parametrize("fw", ALL_FRAMEWORKS)
    def test_the_ignore_list_matches_the_runtime(self, fw):
        """A .gitignore naming another stack's build directory is worse than
        none: it reads as though the suite produces files it never will."""
        ignore = self._guide(fw).split(".gitignore`:")[1].split("```")[1]
        expected = {
            "playwright_js": "node_modules", "cypress_js": "node_modules",
            "playwright_python": ".venv", "selenium_python": ".venv",
            "selenium_java": "target",
        }[fw]
        assert expected in ignore
        if fw != "playwright_js":
            assert "playwright-report" not in ignore

    def test_a_remote_target_is_pointed_with_base_url(self):
        assert "BASE_URL=http://localhost:3000" in self._guide("selenium_python")

    def test_a_local_target_explains_the_serve_command(self):
        guide = self._guide("selenium_python", "http://localhost:3000")
        assert "SERVE_COMMAND" in guide
        assert "starts your app itself" in guide

    def test_it_says_which_files_to_edit_when_the_ui_moves(self):
        """Not in the adoption steps — in "When a test fails", which is where
        someone with a red suite actually looks."""
        text = scaffold.readme(
            framework_key="selenium_python", language="l", stack=REACT,
            target=plan_target(REACT, "https://x.dev", "selenium_python"), files=[],
            project_summary="s", testing_challenges=[],
        )
        failing = text[text.index("## When a test fails"):]
        assert "tests/pages/" in failing
        assert "data-testid" in failing


class TestTheSuiteGuardsThePush:
    """CI tells you the build is broken after the commit is on the branch. A
    pre-push hook refuses the push. They fail at different moments and the
    README has to offer both."""

    def _readme(self, fw, ci_included=True):
        return scaffold.readme(
            framework_key=fw, language="l", stack=REACT,
            target=plan_target(REACT, "https://x.dev", fw), files=[],
            project_summary="s", testing_challenges=[], ci_included=ci_included,
        )

    @pytest.mark.parametrize("fw", ALL_FRAMEWORKS)
    def test_every_framework_gets_a_pre_push_hook(self, fw):
        readme = self._readme(fw)
        assert ".git/hooks/pre-push" in readme
        assert "chmod +x .git/hooks/pre-push" in readme
        assert "exit 1" in readme

    @pytest.mark.parametrize("fw", ALL_FRAMEWORKS)
    def test_the_hook_runs_the_frameworks_own_command(self, fw):
        """A hook that runs another framework's runner fails every push for a
        reason that has nothing to do with the user's change."""
        runner = {
            "playwright_js": "npm --prefix e2e test",
            "cypress_js": "npm --prefix e2e test",
            "playwright_python": "e2e && .venv/bin/pytest",
            "selenium_python": "e2e && HEADLESS=1 .venv/bin/pytest",
            "selenium_java": "mvn -B -f e2e/pom.xml test",
        }[fw]
        assert runner in self._readme(fw)

    def test_only_selenium_is_told_to_force_headless(self):
        """Playwright and Cypress are headless already; the variable would be a
        no-op in front of the command, implying the default is wrong."""
        assert "HEADLESS=1" not in self._readme("playwright_js")
        assert "HEADLESS=1" in self._readme("selenium_python")

    @pytest.mark.parametrize("fw", ALL_FRAMEWORKS)
    def test_the_escape_hatch_is_documented(self, fw):
        """A hook nobody can skip is a hook people delete."""
        assert "--no-verify" in self._readme(fw)

    @pytest.mark.parametrize("fw", ALL_FRAMEWORKS)
    def test_it_says_why_the_pipeline_is_still_worth_adding(self, fw):
        """`.git/hooks/` is not versioned, so the hook is per-clone. Without
        that caveat the two sections read as alternatives."""
        assert "not versioned" in self._readme(fw)

    def test_the_hook_survives_without_a_pipeline(self):
        """The pipeline is withheld for an incomplete suite; the hook is not,
        because running the tests locally is exactly what that user needs."""
        readme = self._readme("selenium_python", ci_included=False)
        assert ".git/hooks/pre-push" in readme
        assert "## 3 · Add it to your pipeline" not in readme


class TestThePipelineIsDeliveredTheRightWay:
    """One YAML, two deliveries — and a README that says which one happened.

    Regression this guards: when the archive stopped shipping workflow files, the
    push-to-GitHub flow silently stopped shipping them too. That flow *creates*
    the repository and the user ticked a CI/CD box to get there, so it still owes
    them a pipeline — and it went on reporting `cicd_added` either way.
    """

    def _run(self, ci_as_files):
        return scaffold.readme(
            framework_key="selenium_python", language="python", stack=REACT,
            target=plan_target(REACT, "https://x.dev", "selenium_python"), files=[],
            project_summary="s", testing_challenges=[],
            ci_included=True, ci_as_files=ci_as_files,
        )

    def test_a_download_is_told_to_paste_the_workflow(self):
        readme = self._run(ci_as_files=False)
        assert "## 3 · Add it to your pipeline" in readme
        assert "Save this as `.github/workflows/e2e-tests.yml`" in readme
        assert "name: E2E Tests" in readme

    def test_a_pushed_repo_is_told_it_already_has_one(self):
        """Telling someone to paste a file that is already in their repo is
        nonsense, and quoting 30 lines of YAML they own is noise."""
        readme = self._run(ci_as_files=True)
        assert "## 3 · The pipeline is already wired up" in readme
        assert "Save this as" not in readme
        assert "name: E2E Tests" not in readme
        assert "BASE_URL" in readme

    def test_both_deliveries_name_the_same_files(self):
        for ci_as_files in (True, False):
            readme = self._run(ci_as_files)
            assert ".github/workflows/e2e-tests.yml" in readme
            assert ".gitlab-ci.yml" in readme


class TestWriterShipsWorkflowFilesOnlyWhenPushing:
    def test_workflow_files_need_both_a_complete_suite_and_a_push(self):
        """Checked on the writer's own emission branch rather than a full run:
        the point is which files leave the agent, not how the LLM got there."""
        import inspect

        from agents import writer_agent as wa

        src = inspect.getsource(wa.WriterAgent.run)
        assert "if ci_included and ci_as_files:" in src, (
            "workflow files must be gated on BOTH a complete suite and a push"
        )
        assert ".github/workflows/e2e-tests.yml" in src

    def test_the_push_flow_asks_for_files(self):
        """main.py's GitHub push is the one caller that must set it."""
        import pathlib

        main_src = pathlib.Path(__file__).resolve().parents[1].joinpath("main.py").read_text()
        assert "ci_as_files=True" in main_src, (
            "the push-to-GitHub flow must still write the workflow into the repo"
        )
        assert main_src.count("ci_as_files=True") == 1

    def test_the_download_flow_does_not(self):
        import inspect

        from agents import writer_agent as wa

        sig = inspect.signature(wa.WriterAgent.run)
        assert sig.parameters["ci_as_files"].default is False
