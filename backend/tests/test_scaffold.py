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

    def test_cypress_base_url_example_does_not_start_a_second_server(self):
        # `npm test` boots a server; against a deployed BASE_URL that would bind
        # a port for nothing and could fail the run.
        readme = self._readme("cypress_js")
        assert "BASE_URL=https://staging.example.com npm run cy:run" in readme

    def test_selenium_readme_does_not_mention_playwright(self):
        assert "playwright" not in self._readme("selenium_java").lower()

    def test_readme_documents_the_base_url_override(self):
        assert "BASE_URL" in self._readme("playwright_js")


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
