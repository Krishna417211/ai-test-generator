"""test_github_service.py — Unit tests for GitHub URL parsing."""

import pytest

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
