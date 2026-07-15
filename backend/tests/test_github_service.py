"""test_github_service.py — Unit tests for GitHub URL parsing."""

import inspect

import pytest

from services import github_service
from services.github_service import parse_github_url


class TestParseGithubUrl:
    def test_basic(self):
        info = parse_github_url("https://github.com/owner/repo")
        assert info.owner == "owner"
        assert info.repo == "repo"
        assert info.branch == "main"

    def test_with_branch(self):
        info = parse_github_url("https://github.com/owner/repo/tree/develop")
        assert info.branch == "develop"

    def test_strips_git_suffix(self):
        assert parse_github_url("https://github.com/owner/repo.git").repo == "repo"

    def test_trailing_slash(self):
        assert parse_github_url("https://github.com/owner/repo/").repo == "repo"

    def test_non_github_raises(self):
        with pytest.raises(ValueError):
            parse_github_url("https://gitlab.com/owner/repo")

    # ".git" must only be stripped as a trailing clone suffix. A replace() over
    # the whole name turned every GitHub Pages repo into a 404:
    # "krishna.github.io" -> "krishnahub.io".
    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://github.com/krishna/krishna.github.io", "krishna.github.io"),
            ("https://github.com/o/awesome.gitignore-demo", "awesome.gitignore-demo"),
            ("https://github.com/o/digital.gitbook", "digital.gitbook"),
            ("https://github.com/o/repo.git", "repo"),
            ("https://github.com/o/my.github.io.git", "my.github.io"),
        ],
    )
    def test_git_suffix_stripped_only_at_end(self, url, expected):
        assert parse_github_url(url).repo == expected


class TestRedirectHandling:
    """GitHub 301s renamed/transferred repos to /repositories/{id}, and
    raw.githubusercontent.com redirects as well. httpx defaults to NOT following
    redirects, which surfaced to users as a bare "Failed to fetch"."""

    def test_every_client_follows_redirects(self):
        source = inspect.getsource(github_service)
        constructions = [
            line.strip()
            for line in source.splitlines()
            if "httpx.AsyncClient(" in line
        ]
        assert constructions, "expected httpx.AsyncClient usage in github_service"
        for line in constructions:
            assert "follow_redirects=" in line, (
                f"httpx.AsyncClient without follow_redirects — a 301 from GitHub "
                f"will raise instead of resolving: {line}"
            )

    def test_follow_redirects_enabled(self):
        assert github_service.FOLLOW_REDIRECTS is True
