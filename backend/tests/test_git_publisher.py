"""
test_git_publisher.py — Unit tests for GitPublisher and push preparation.

Network is stubbed with httpx.MockTransport so the full
blobs → tree → commit → ref sequence is exercised without hitting GitHub.
"""

import asyncio

import httpx
import pytest

from services import git_publisher
from services.git_publisher import GitPublisher, GitPublishError
from services.file_extractor import filter_for_push


# ── push preparation ─────────────────────────

class TestFilterForPush:
    def test_drops_node_modules_and_build(self):
        files = {
            "src/App.tsx": "code",
            "node_modules/react/index.js": "junk",
            "dist/bundle.js": "built",
            "src/nested/__pycache__/x.pyc": "cache",
        }
        kept, warnings = filter_for_push(files)
        assert kept == {"src/App.tsx": "code"}
        assert any("build/dependency" in w for w in warnings)

    def test_drops_binary_assets(self):
        files = {"logo.png": "\x89PNG...", "readme.md": "# hi"}
        kept, _ = filter_for_push(files)
        assert "logo.png" not in kept
        assert "readme.md" in kept

    def test_drops_oversized_files(self):
        files = {"big.txt": "x" * 2_000_000, "small.txt": "ok"}
        kept, warnings = filter_for_push(files)
        assert "big.txt" not in kept
        assert "small.txt" in kept
        assert any("larger than" in w for w in warnings)

    def test_keeps_normal_source(self):
        files = {"a.ts": "1", "docs/guide.md": "2", "package.json": "{}"}
        kept, _ = filter_for_push(files)
        assert kept == files


# ── GitPublisher ─────────────────────────────

def _install_mock_transport(monkeypatch, handler):
    """Patch git_publisher.httpx.AsyncClient to route through a MockTransport."""
    real_client = httpx.AsyncClient  # capture before patching to avoid recursion

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), timeout=5)
    monkeypatch.setattr(git_publisher.httpx, "AsyncClient", fake_client)


def _happy_handler(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        p, m = request.url.path, request.method
        if p == "/user" and m == "GET":
            return httpx.Response(200, json={"login": "tester"})
        if p == "/user/repos" and m == "POST":
            return httpx.Response(201, json={"name": "myrepo", "default_branch": "main"})
        if p.endswith("/git/ref/heads/main") and m == "GET":
            return httpx.Response(200, json={"object": {"sha": "basecommit"}})
        if "/git/commits/basecommit" in p and m == "GET":
            return httpx.Response(200, json={"tree": {"sha": "basetree"}})
        if p.endswith("/git/blobs") and m == "POST":
            return httpx.Response(201, json={"sha": "blobsha"})
        if p.endswith("/git/trees") and m == "POST":
            return httpx.Response(201, json={"sha": "newtree"})
        if p.endswith("/git/commits") and m == "POST":
            return httpx.Response(201, json={"sha": "newcommit"})
        if p.endswith("/git/refs/heads/main") and m == "PATCH":
            return httpx.Response(200, json={})
        return httpx.Response(500, json={"message": f"unexpected {m} {p}"})
    return handler


class TestGitPublisher:
    def test_requires_token(self):
        with pytest.raises(GitPublishError):
            GitPublisher(token="")

    def test_publish_full_sequence(self, monkeypatch):
        calls: list[tuple[str, str]] = []
        _install_mock_transport(monkeypatch, _happy_handler(calls))

        result = asyncio.run(
            GitPublisher("ghp_test").publish(
                "myrepo",
                {"src/App.tsx": "code", "README.md": "# demo"},
                private=True,
            )
        )

        assert result.full_name == "tester/myrepo"
        assert result.repo_url == "https://github.com/tester/myrepo"
        assert result.branch == "main"
        assert result.commit_sha == "newcommit"
        assert result.files_pushed == 2

        methods = [c[0] for c in calls]
        assert methods.count("POST") >= 4          # 2 blobs + tree + commit
        assert ("PATCH", "/repos/tester/myrepo/git/refs/heads/main") in calls

    def test_empty_files_rejected(self, monkeypatch):
        _install_mock_transport(monkeypatch, _happy_handler([]))
        with pytest.raises(GitPublishError):
            asyncio.run(GitPublisher("ghp_test").publish("myrepo", {}))

    def test_duplicate_repo_name_raises(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/user" and request.method == "GET":
                return httpx.Response(200, json={"login": "tester"})
            if request.url.path == "/user/repos":
                return httpx.Response(422, json={"message": "name already exists"})
            return httpx.Response(500, json={})
        _install_mock_transport(monkeypatch, handler)

        with pytest.raises(GitPublishError, match="already exists"):
            asyncio.run(GitPublisher("ghp_test").publish("myrepo", {"a.txt": "hi"}))

    def test_bad_token_raises(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Bad credentials"})
        _install_mock_transport(monkeypatch, handler)

        with pytest.raises(GitPublishError, match="invalid or expired"):
            asyncio.run(GitPublisher("ghp_bad").publish("myrepo", {"a.txt": "hi"}))
