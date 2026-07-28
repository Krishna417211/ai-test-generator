"""
test_git_publisher.py — Unit tests for GitPublisher and push preparation.

Network is stubbed with httpx.MockTransport so the full
blobs → tree → commit → ref sequence is exercised without hitting GitHub.
"""

import asyncio
import json

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
        kept, warnings, _ = filter_for_push(files)
        assert kept == {"src/App.tsx": "code"}
        assert any("build/dependency" in w for w in warnings)

    def test_drops_binary_assets(self):
        files = {"logo.png": "\x89PNG...", "readme.md": "# hi"}
        kept, _, _ = filter_for_push(files)
        assert "logo.png" not in kept
        assert "readme.md" in kept

    def test_drops_oversized_files(self):
        files = {"big.txt": "x" * 2_000_000, "small.txt": "ok"}
        kept, warnings, _ = filter_for_push(files)
        assert "big.txt" not in kept
        assert "small.txt" in kept
        assert any("larger than" in w for w in warnings)

    def test_keeps_normal_source(self):
        files = {"a.ts": "1", "docs/guide.md": "2", "package.json": "{}"}
        kept, _, _ = filter_for_push(files)
        assert kept == files

    def test_credentials_never_reach_the_push(self):
        """The whole point: a working tree gets zipped, .env and all."""
        files = {
            "src/App.tsx": "code",
            ".env": "STRIPE_SECRET=sk_live_x",
            ".env.example": "STRIPE_SECRET=",
            "deploy/id_rsa": "-----BEGIN OPENSSH PRIVATE KEY-----",
            "certs/server.pem": "-----BEGIN CERTIFICATE-----",
        }
        kept, warnings, secrets = filter_for_push(files)

        assert set(kept) == {"src/App.tsx", ".env.example"}
        assert {s["path"] for s in secrets} == {".env", "deploy/id_rsa", "certs/server.pem"}
        # Named, not just counted — the user has to know which files to go and check.
        assert any(".env" in w for w in warnings)

    def test_hardcoded_key_in_source_is_withheld(self):
        files = {"src/config.ts": 'export const k = "AKIAIOSFODNN7EXAMPLE";'}
        kept, _, secrets = filter_for_push(files)
        assert kept == {}
        assert secrets[0]["kind"] == "content"

    def test_unsafe_paths_are_skipped_not_written(self):
        files = {"../../etc/passwd": "root:x", "src/ok.ts": "1"}
        kept, warnings, _ = filter_for_push(files)
        assert kept == {"src/ok.ts": "1"}
        assert any("can't be written" in w for w in warnings)


# ── GitPublisher ─────────────────────────────

def _install_mock_transport(monkeypatch, handler):
    """Patch git_publisher.httpx.AsyncClient to route through a MockTransport."""
    real_client = httpx.AsyncClient  # capture before patching to avoid recursion

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), timeout=5)
    monkeypatch.setattr(git_publisher.httpx, "AsyncClient", fake_client)


class FakeGitHub:
    """A GitHub git-data API that actually stores what you push at it.

    A handler that answers every write with a canned 201 cannot exercise the
    verification step at all — the read-back has nothing to read. This keeps a
    real path→sha map so a test can ask the interesting question: what happens
    when the tree that comes back is not the tree we sent?

    `drop` names paths to swallow on the *first* commit only, simulating the
    failure this verification exists to catch: every call returns 2xx and the
    repo is still short a file.
    """

    def __init__(self, *, drop: set[str] | None = None, truncated: bool = False,
                 tree_read_status: int = 200):
        self.drop = set(drop or ())
        self.truncated = truncated
        self.tree_read_status = tree_read_status
        self.calls: list[tuple[str, str]] = []
        self.blobs: dict[str, str] = {}       # sha → content
        self.trees: dict[str, dict] = {}      # tree sha → {path: blob sha}
        self.commits: dict[str, str] = {}     # commit sha → tree sha
        self._n = 0

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        p, m = request.url.path, request.method

        if p == "/user" and m == "GET":
            return httpx.Response(200, json={"login": "tester"})
        if p == "/user/repos" and m == "POST":
            return httpx.Response(201, json={"name": "myrepo", "default_branch": "main"})
        if p.endswith("/git/ref/heads/main") and m == "GET":
            return httpx.Response(200, json={"object": {"sha": "basecommit"}})

        if p.endswith("/git/blobs") and m == "POST":
            content = json.loads(request.content)["content"]
            sha = self._next("blob")
            self.blobs[sha] = content
            return httpx.Response(201, json={"sha": sha})

        if p.endswith("/git/trees") and m == "POST":
            body = json.loads(request.content)
            entries = dict(self.trees.get(body.get("base_tree", ""), {}))
            for item in body["tree"]:
                if item["path"] in self.drop:
                    continue            # accepted, silently not stored
                entries[item["path"]] = item["sha"]
            # Only the first commit loses files; the repair must be able to work.
            self.drop = set()
            sha = self._next("tree")
            self.trees[sha] = entries
            return httpx.Response(201, json={"sha": sha})

        if p.endswith("/git/commits") and m == "POST":
            body = json.loads(request.content)
            sha = self._next("commit")
            self.commits[sha] = body["tree"]
            return httpx.Response(201, json={"sha": sha})

        if "/git/commits/" in p and m == "GET":
            sha = p.rsplit("/", 1)[-1]
            if sha == "basecommit":
                return httpx.Response(200, json={"tree": {"sha": "basetree"}})
            return httpx.Response(200, json={"tree": {"sha": self.commits[sha]}})

        if p.endswith("/git/refs/heads/main") and m == "PATCH":
            return httpx.Response(200, json={})

        # The verification read: GET /git/trees/{commit_sha}?recursive=1
        if "/git/trees/" in p and m == "GET":
            if self.tree_read_status != 200:
                return httpx.Response(self.tree_read_status, json={"message": "nope"})
            sha = p.rsplit("/", 1)[-1]
            entries = self.trees.get(self.commits.get(sha, ""), {})
            return httpx.Response(200, json={
                "truncated": self.truncated,
                "tree": [
                    {"path": path, "type": "blob", "sha": blob}
                    for path, blob in entries.items()
                ],
            })

        return httpx.Response(500, json={"message": f"unexpected {m} {p}"})


class TestGitPublisher:
    def test_requires_token(self):
        with pytest.raises(GitPublishError):
            GitPublisher(token="")

    def test_publish_full_sequence(self, monkeypatch):
        gh = FakeGitHub()
        _install_mock_transport(monkeypatch, gh)

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
        assert result.files_pushed == 2

        methods = [c[0] for c in gh.calls]
        assert methods.count("POST") >= 4          # 2 blobs + tree + commit
        assert ("PATCH", "/repos/tester/myrepo/git/refs/heads/main") in gh.calls

    def test_empty_files_rejected(self, monkeypatch):
        _install_mock_transport(monkeypatch, FakeGitHub())
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


class TestPushVerification:
    """"18 files pushed" has to mean 18 files are in the repo.

    Every write in the sequence can return 2xx and still leave the branch short
    — that is the failure these cover, and it is invisible without reading the
    tree back. The user finds out days later, when CI runs a suite that is
    missing a page object.
    """

    FILES = {
        "src/App.tsx": "code",
        "e2e/tests/login.spec.ts": "test('x', () => {})",
        "e2e/package.json": "{}",
        "README.md": "# demo",
    }

    def _publish(self, monkeypatch, gh, files=None):
        _install_mock_transport(monkeypatch, gh)
        return asyncio.run(
            GitPublisher("ghp_test").publish("myrepo", files or self.FILES)
        )

    def test_every_file_is_confirmed_present_in_the_repo(self, monkeypatch):
        gh = FakeGitHub()
        result = self._publish(monkeypatch, gh)

        assert result.verified is True
        assert result.repaired_files == []
        # The claim is checked against the repo's own tree, not our bookkeeping.
        pushed = gh.trees[gh.commits[result.commit_sha]]
        assert set(pushed) >= set(self.FILES)
        assert result.files_pushed == len(self.FILES)

    def test_content_is_verified_not_just_the_path(self, monkeypatch):
        """A path present with the wrong blob is a mismatch, not a pass.

        Git shas are content hashes, so this is decidable — and the failure it
        catches (a truncated blob upload) leaves a file that exists and is
        wrong, which is worse than one that's missing.
        """
        gh = FakeGitHub()
        _install_mock_transport(monkeypatch, gh)
        publisher = GitPublisher("ghp_test")

        async def go():
            async with httpx.AsyncClient() as client:
                expected = [{"path": "a.ts", "sha": "blob-we-uploaded"}]
                gh.commits["c1"] = "t1"
                gh.trees["t1"] = {"a.ts": "some-other-sha"}
                return await publisher._verify_tree(client, "tester", "myrepo", "c1", expected)

        missing, note = asyncio.run(go())
        assert missing == {"a.ts"}
        assert note == ""

    def test_a_file_lost_on_the_first_commit_is_re_pushed(self, monkeypatch):
        gh = FakeGitHub(drop={"e2e/tests/login.spec.ts"})
        result = self._publish(monkeypatch, gh)

        assert result.repaired_files == ["e2e/tests/login.spec.ts"]
        assert result.verified is True
        # And it really is there now — checked against the repo, not the flag.
        assert set(gh.trees[gh.commits[result.commit_sha]]) >= set(self.FILES)
        assert any("pushed again" in w for w in result.warnings)

    def test_repair_keeps_the_files_that_did_land(self, monkeypatch):
        """The second commit must layer on the live tree, not the empty base —
        otherwise fixing one missing file deletes the other three."""
        gh = FakeGitHub(drop={"README.md"})
        result = self._publish(monkeypatch, gh)
        assert set(gh.trees[gh.commits[result.commit_sha]]) == set(self.FILES)

    def test_a_file_still_missing_after_the_retry_is_an_error(self, monkeypatch):
        class NeverStores(FakeGitHub):
            def __call__(self, request):
                self.drop = {"e2e/tests/login.spec.ts"}   # re-arm on every call
                return super().__call__(request)

        with pytest.raises(GitPublishError, match="not in the repository"):
            self._publish(monkeypatch, NeverStores())

    def test_unreadable_tree_is_reported_not_guessed(self, monkeypatch):
        """We could not check. That is not the same as "files are missing", and
        it is not the same as "verified" either — say which it is."""
        result = self._publish(monkeypatch, FakeGitHub(tree_read_status=502))
        assert result.verified is False
        assert "confirm every file arrived" in result.verification_note
        assert result.repaired_files == []

    def test_truncated_tree_is_reported_not_guessed(self, monkeypatch):
        result = self._publish(monkeypatch, FakeGitHub(truncated=True))
        assert result.verified is False
        assert "too large" in result.verification_note


class TestForbiddenReason:
    """A 403 used to always be reported as "the token needs the 'repo' scope",
    whatever the real cause — unactionable when it was something else, and
    misleading when the scopes were fine. GitHub tells us the actual reason;
    these pin that we report it."""

    def _resp(self, *, headers=None, message="", status=403):
        return httpx.Response(
            status, headers=headers or {}, json={"message": message},
            request=httpx.Request("POST", "https://api.github.com/user/repos"),
        )

    def test_rate_limit_is_named_as_such(self):
        r = self._resp(headers={"x-ratelimit-remaining": "0"},
                       message="API rate limit exceeded")
        msg = GitPublisher("t")._forbidden_reason(r)
        assert "rate-limited" in msg
        assert "repo' scope" not in msg          # must NOT blame scopes

    def test_saml_sso_is_named(self):
        r = self._resp(message="Resource protected by organization SAML enforcement")
        msg = GitPublisher("t")._forbidden_reason(r)
        assert "SAML SSO" in msg and "authorize" in msg.lower()

    def test_missing_repo_scope_states_what_it_has(self):
        r = self._resp(headers={"x-oauth-scopes": "gist, read:org"})
        msg = GitPublisher("t")._forbidden_reason(r)
        assert "missing the 'repo' scope" in msg
        assert "gist, read:org" in msg           # says what it actually has

    def test_scopes_present_does_not_blame_scopes(self):
        r = self._resp(headers={"x-oauth-scopes": "repo, gist"},
                       message="Repository creation disabled")
        msg = GitPublisher("t")._forbidden_reason(r)
        assert "required scope" in msg
        assert "Repository creation disabled" in msg   # surfaces GitHub's reason


class TestScopePreflight:
    """The scope problem is caught on the first call, not half-way through."""

    def _client(self, monkeypatch, user_headers):
        real_client = httpx.AsyncClient

        def handler(request):
            return httpx.Response(200, headers=user_headers,
                                  json={"login": "tester"})

        def fake(*a, **k):
            return real_client(transport=httpx.MockTransport(handler), timeout=5)
        monkeypatch.setattr(git_publisher.httpx, "AsyncClient", fake)
        return fake

    def test_token_without_repo_scope_is_refused_early(self, monkeypatch):
        fake = self._client(monkeypatch, {"x-oauth-scopes": "gist, read:org"})
        p = GitPublisher("t")

        async def go():
            async with fake() as c:
                return await p._get_owner(c)

        with pytest.raises(GitPublishError) as e:
            asyncio.run(go())
        assert "missing" in str(e.value) and "repo" in str(e.value)

    def test_token_with_repo_scope_passes(self, monkeypatch):
        fake = self._client(monkeypatch, {"x-oauth-scopes": "repo, gist"})
        p = GitPublisher("t")

        async def go():
            async with fake() as c:
                return await p._get_owner(c)

        assert asyncio.run(go()) == "tester"

    def test_absent_scope_header_is_not_treated_as_no_scopes(self, monkeypatch):
        """Fine-grained PATs and App tokens omit X-OAuth-Scopes. We can't tell
        what they can do, so we must NOT refuse them — let the request decide."""
        fake = self._client(monkeypatch, {})
        p = GitPublisher("t")

        async def go():
            async with fake() as c:
                return await p._get_owner(c)

        assert asyncio.run(go()) == "tester"
