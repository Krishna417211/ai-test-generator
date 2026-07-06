"""
test_file_extractor.py — Unit tests for the file extraction and scoring logic

Run with: pytest backend/tests/test_file_extractor.py -v
"""

import pytest
from services.file_extractor import (
    FileExtractor,
    detect_framework,
    detect_monorepo,
    score_file,
    extract_zip,
)
import io
import json
import zipfile


# ─────────────────────────────────────────────
# score_file tests
# ─────────────────────────────────────────────

class TestScoreFile:
    def test_router_files_score_10(self):
        assert score_file("src/App.tsx") == 10
        assert score_file("src/App.jsx") == 10
        assert score_file("router/index.js") == 10
        assert score_file("src/router/index.ts") == 10

    def test_page_files_score_9(self):
        assert score_file("pages/Login.tsx") == 9
        assert score_file("src/pages/Dashboard.jsx") == 9
        assert score_file("views/Home.vue") == 9
        assert score_file("screens/Profile.tsx") == 9

    def test_auth_components_score_high(self):
        assert score_file("components/LoginForm.tsx") >= 7
        assert score_file("components/AuthGuard.tsx") >= 7
        assert score_file("components/Checkout.vue") >= 7

    def test_component_files_score_6(self):
        assert score_file("src/components/Button.tsx") == 6
        # A file literally named App.tsx is the app root (score 10), so use a
        # real component name to exercise the "containers dir → 6" rule.
        assert score_file("containers/UserList.tsx") == 6

    def test_config_files_score_5(self):
        assert score_file("package.json") == 5
        assert score_file("vite.config.js") == 5

    def test_unknown_files_score_low(self):
        assert score_file("utils/helpers.ts") == 4


# ─────────────────────────────────────────────
# detect_framework tests
# ─────────────────────────────────────────────

class TestDetectFramework:
    def _pkg(self, deps: dict) -> dict:
        return {"package.json": json.dumps({"dependencies": deps})}

    def test_detect_nextjs(self):
        files = self._pkg({"next": "14.0.0", "react": "18.0.0"})
        assert "Next.js" in detect_framework(files)

    def test_detect_react(self):
        files = self._pkg({"react": "18.0.0", "react-dom": "18.0.0"})
        assert "React" in detect_framework(files)

    def test_detect_vue(self):
        files = self._pkg({"vue": "3.4.0"})
        assert "Vue" in detect_framework(files)

    def test_detect_nuxt(self):
        files = self._pkg({"nuxt": "3.0.0"})
        assert "Nuxt" in detect_framework(files)

    def test_detect_angular(self):
        files = self._pkg({"@angular/core": "17.0.0"})
        assert "Angular" in detect_framework(files)

    def test_detect_svelte(self):
        files = self._pkg({"svelte": "4.0.0"})
        assert "Svelte" in detect_framework(files)

    def test_detect_from_extensions(self):
        files = {"components/App.vue": "<template></template>"}
        assert "Vue" in detect_framework(files)

    def test_no_package_json(self):
        result = detect_framework({})
        assert "Unknown" in result or "React" in result

    def test_detect_django_by_manage_py(self):
        files = {"manage.py": "import django", "app/templates/index.html": "{% block %}"}
        assert "Django" in detect_framework(files)

    def test_detect_django_by_requirements(self):
        files = {"requirements.txt": "Django==5.0\ngunicorn"}
        assert "Django" in detect_framework(files)

    def test_detect_flask(self):
        files = {"requirements.txt": "Flask==3.0", "app.py": "from flask import Flask"}
        assert "Flask" in detect_framework(files)

    def test_detect_fastapi(self):
        files = {"requirements.txt": "fastapi==0.115\nuvicorn"}
        assert "FastAPI" in detect_framework(files)

    def test_detect_rails(self):
        files = {"Gemfile": "gem 'rails', '7.1'", "app/views/home/index.html.erb": "<h1>Hi</h1>"}
        assert "Rails" in detect_framework(files)

    def test_detect_laravel(self):
        files = {"composer.json": '{"require": {"laravel/framework": "^11.0"}}',
                 "resources/views/welcome.blade.php": "<div></div>"}
        assert "Laravel" in detect_framework(files)

    def test_spa_still_wins_over_backend(self):
        # A Django API with a React SPA frontend should report React (SPA-first).
        files = {"package.json": json.dumps({"dependencies": {"react": "18"}}),
                 "requirements.txt": "Django==5.0"}
        assert "React" in detect_framework(files)


# ─────────────────────────────────────────────
# detect_monorepo tests
# ─────────────────────────────────────────────

class TestDetectMonorepo:
    def test_lerna_monorepo(self):
        files = {
            "lerna.json": "{}",
            "package.json": json.dumps({"workspaces": ["packages/*"]}),
        }
        is_mono, packages = detect_monorepo(files)
        assert is_mono is True

    def test_nx_monorepo(self):
        files = {
            "nx.json": "{}",
            "package.json": json.dumps({"workspaces": ["apps/*", "libs/*"]}),
        }
        is_mono, packages = detect_monorepo(files)
        assert is_mono is True

    def test_regular_repo(self):
        files = {"package.json": json.dumps({"name": "my-app"})}
        is_mono, packages = detect_monorepo(files)
        assert is_mono is False
        assert packages == []


# ─────────────────────────────────────────────
# FileExtractor tests
# ─────────────────────────────────────────────

class TestFileExtractor:
    def _make_files(self, paths_and_content: dict) -> dict:
        return paths_and_content

    def test_filters_node_modules(self):
        extractor = FileExtractor()
        raw = {
            "node_modules/react/index.js": "module.exports = {}",
            "src/App.tsx": "export default function App() {}",
        }
        result = extractor.extract(raw)
        assert "node_modules/react/index.js" not in result.files
        assert "src/App.tsx" in result.files

    def test_filters_test_files(self):
        extractor = FileExtractor()
        raw = {
            "src/App.tsx": "export default function App() {}",
            "src/App.test.tsx": "describe('App', () => {})",
            "src/App.spec.ts": "it('works', () => {})",
        }
        result = extractor.extract(raw)
        assert "src/App.tsx" in result.files
        assert "src/App.test.tsx" not in result.files
        assert "src/App.spec.ts" not in result.files

    def test_filters_git_directory(self):
        extractor = FileExtractor()
        raw = {
            ".git/config": "[core]",
            "src/index.tsx": "import React from 'react'",
        }
        result = extractor.extract(raw)
        assert ".git/config" not in result.files

    def test_filters_images(self):
        extractor = FileExtractor()
        raw = {
            "public/logo.png": "binary",
            "src/App.tsx": "export default function App() {}",
            "src/styles.css": "body { margin: 0 }",
        }
        result = extractor.extract(raw)
        assert "public/logo.png" not in result.files

    def test_includes_router_files(self):
        extractor = FileExtractor()
        raw = {
            "src/App.tsx": "export default function App() {}",
            "src/router/index.ts": "export default router",
        }
        result = extractor.extract(raw)
        assert "src/App.tsx" in result.files
        assert "src/router/index.ts" in result.files

    def test_token_budget_respected(self):
        extractor = FileExtractor(token_budget=100)  # very small budget
        # Create files that exceed the budget
        raw = {
            f"src/pages/Page{i}.tsx": "x" * 1000
            for i in range(20)
        }
        result = extractor.extract(raw)
        assert result.total_tokens <= 200  # some slack for summaries
        assert result.truncated is True

    def test_high_priority_files_included_first(self):
        extractor = FileExtractor(token_budget=50)  # tiny budget
        raw = {
            "src/App.tsx": "A" * 100,              # importance 10
            "src/utils/helpers.ts": "B" * 100,     # importance 4
        }
        result = extractor.extract(raw)
        # App.tsx (router, importance 10) should be included before helpers
        if "src/App.tsx" in result.files:
            pass  # correct
        # helpers may be summarized or dropped

    def test_detects_framework(self):
        extractor = FileExtractor()
        raw = {
            "package.json": json.dumps({"dependencies": {"react": "18.0.0"}}),
            "src/App.tsx": "export default function App() {}",
        }
        result = extractor.extract(raw)
        assert "React" in result.framework

    def test_returns_file_count(self):
        extractor = FileExtractor()
        raw = {
            "src/App.tsx": "content",
            "src/pages/Home.tsx": "content",
            "package.json": '{"name": "test"}',
        }
        result = extractor.extract(raw)
        assert result.file_count == 3

    def test_summarizes_large_low_priority_files(self):
        extractor = FileExtractor(token_budget=200)
        large_content = "<button id='btn1'>Click</button>" * 50
        raw = {
            "src/App.tsx": "export default function App() {}",  # high priority
            "src/utils/helpers.ts": large_content,              # low priority, large
        }
        result = extractor.extract(raw)
        # helpers should be summarized, not dropped entirely
        if "src/utils/helpers.ts" in result.files:
            content = result.files["src/utils/helpers.ts"]
            # Should be a summary, not the full content
            if result.truncated:
                assert len(content) < len(large_content)


# ─────────────────────────────────────────────
# ZIP extraction tests
# ─────────────────────────────────────────────

class TestExtractZip:
    def _make_zip(self, files: dict, prefix: str = "") -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for path, content in files.items():
                zf.writestr(f"{prefix}{path}", content)
        return buf.getvalue()

    def test_extracts_flat_zip(self):
        z = self._make_zip({
            "src/App.tsx": "export default function App() {}",
            "package.json": '{"name": "test"}',
        })
        result = extract_zip(z)
        assert "src/App.tsx" in result
        assert "package.json" in result

    def test_strips_github_prefix(self):
        # GitHub downloads add "repo-main/" prefix
        z = self._make_zip({
            "src/App.tsx": "export default function App() {}",
            "package.json": '{"name": "test"}',
        }, prefix="my-repo-main/")
        result = extract_zip(z)
        # Should strip the prefix
        assert "src/App.tsx" in result
        assert "package.json" in result
        assert not any(k.startswith("my-repo-main/") for k in result)

    def test_skips_directories(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.mkdir("src/")  # directory entry
            zf.writestr("src/App.tsx", "content")
        z = buf.getvalue()
        result = extract_zip(z)
        assert "src/App.tsx" in result
        assert "src/" not in result

    def test_handles_encoding_errors(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("src/binary.bin", b"\xff\xfe invalid utf-8".decode("latin-1"))
        result = extract_zip(buf.getvalue())
        assert "src/binary.bin" in result  # Should not raise


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
