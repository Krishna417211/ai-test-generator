"""
github_service.py — Fetch files from GitHub repos (public + private)

Public repos: raw.githubusercontent.com (no auth needed)
Private repos: GitHub REST API with OAuth token or Personal Access Token
ZIP upload: handled by file_extractor.py
"""

import re
import base64
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

# Max individual file size to fetch (skip huge files like bundled JS)
MAX_FILE_SIZE_BYTES = 500_000  # 500 KB

# GitHub 301s a renamed/transferred repo to its canonical /repositories/{id} URL,
# and raw.githubusercontent.com redirects too. httpx does not follow redirects by
# default, so every one of those became an unhandled HTTPStatusError surfacing to
# the user as "Failed to fetch". httpx drops the Authorization header on a
# cross-origin redirect, so a private-repo token cannot leak to another host.
FOLLOW_REDIRECTS = True


@dataclass
class RepoFile:
    path: str
    content: str
    size: int
    sha: str = ""


@dataclass
class RepoInfo:
    owner: str
    repo: str
    branch: str
    is_private: bool = False
    default_branch: str = "main"


def parse_github_url(url: str) -> RepoInfo:
    """
    Parse a GitHub URL into its components.
    Handles:
      - https://github.com/owner/repo
      - https://github.com/owner/repo/tree/branch
      - https://github.com/owner/repo/tree/branch/subpath
    """
    url = url.strip().rstrip("/")
    pattern = r"github\.com/([^/]+)/([^/]+)(?:/tree/([^/]+))?"
    match = re.search(pattern, url)
    if not match:
        raise ValueError(f"Cannot parse GitHub URL: {url}")

    owner, repo, branch = match.group(1), match.group(2), match.group(3)
    # Strip only a trailing ".git" clone suffix. replace() matched anywhere in the
    # name, so "krishna.github.io" became "krishnahub.io" — every GitHub Pages
    # repo was unresolvable.
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    return RepoInfo(owner=owner, repo=repo, branch=branch or "main")


class GitHubService:
    """
    Fetch file trees and file contents from GitHub.
    Supports public repos without auth and private repos with PAT/OAuth.
    """

    BASE_API = "https://api.github.com"

    def __init__(self, token: Optional[str] = None):
        self.token = token
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    async def get_file_tree(self, repo_info: RepoInfo) -> list[dict]:
        """
        Return flat list of all files in the repo using the Git Trees API
        (recursive=1 means we get everything in one request).
        """
        # First, resolve the default branch if user didn't specify
        if repo_info.branch == "main":
            actual_branch = await self._resolve_default_branch(repo_info)
            repo_info.branch = actual_branch

        url = (
            f"{self.BASE_API}/repos/{repo_info.owner}/{repo_info.repo}"
            f"/git/trees/{repo_info.branch}?recursive=1"
        )

        async with httpx.AsyncClient(timeout=30, follow_redirects=FOLLOW_REDIRECTS) as client:
            r = await client.get(url, headers=self._headers)

        if r.status_code == 404:
            raise RepoNotFoundError(
                f"Repo {repo_info.owner}/{repo_info.repo} not found or is private. "
                "Add a GitHub token to access private repos."
            )
        if r.status_code == 403:
            raise RepoAccessError("GitHub API rate limit or permission denied.")
        r.raise_for_status()

        data = r.json()
        if data.get("truncated"):
            logger.warning(
                "Git tree response was truncated (repo too large). "
                "Only partial file list available."
            )

        # Return only files (blobs), not directories
        return [
            item for item in data.get("tree", [])
            if item["type"] == "blob"
        ]

    async def fetch_files(
        self,
        repo_info: RepoInfo,
        paths: list[str],
        concurrency: int = 10,
    ) -> list[RepoFile]:
        """
        Fetch the contents of specific files concurrently.
        Uses raw.githubusercontent.com for public repos (faster, no rate limits).
        Falls back to API for private repos.
        """
        sem = asyncio.Semaphore(concurrency)
        tasks = [
            self._fetch_one(repo_info, path, sem)
            for path in paths
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        files = []
        for path, result in zip(paths, results):
            if isinstance(result, Exception):
                logger.warning(f"Failed to fetch {path}: {result}")
            elif result:
                files.append(result)
        return files

    async def _fetch_one(
        self,
        repo_info: RepoInfo,
        path: str,
        sem: asyncio.Semaphore,
    ) -> Optional[RepoFile]:
        async with sem:
            if not self.token:
                return await self._fetch_raw(repo_info, path)
            else:
                return await self._fetch_api(repo_info, path)

    async def _fetch_raw(self, repo_info: RepoInfo, path: str) -> Optional[RepoFile]:
        """Public repos: use raw.githubusercontent.com (no rate limits)."""
        url = (
            f"https://raw.githubusercontent.com/"
            f"{repo_info.owner}/{repo_info.repo}/{repo_info.branch}/{path}"
        )
        async with httpx.AsyncClient(timeout=20, follow_redirects=FOLLOW_REDIRECTS) as client:
            r = await client.get(url)

        if r.status_code == 404:
            return None
        r.raise_for_status()

        content = r.text
        if len(r.content) > MAX_FILE_SIZE_BYTES:
            logger.info(f"Skipping {path} — too large ({len(r.content)} bytes)")
            return None

        return RepoFile(path=path, content=content, size=len(r.content))

    async def _fetch_api(self, repo_info: RepoInfo, path: str) -> Optional[RepoFile]:
        """Private repos: GitHub Contents API (has rate limits)."""
        url = (
            f"{self.BASE_API}/repos/{repo_info.owner}/{repo_info.repo}"
            f"/contents/{path}?ref={repo_info.branch}"
        )
        async with httpx.AsyncClient(timeout=20, follow_redirects=FOLLOW_REDIRECTS) as client:
            r = await client.get(url, headers=self._headers)

        if r.status_code == 404:
            return None
        r.raise_for_status()

        data = r.json()
        if data.get("size", 0) > MAX_FILE_SIZE_BYTES:
            logger.info(f"Skipping {path} — API reports {data['size']} bytes")
            return None

        content_b64 = data.get("content", "")
        content = base64.b64decode(content_b64).decode("utf-8", errors="replace")

        return RepoFile(
            path=path,
            content=content,
            size=data.get("size", 0),
            sha=data.get("sha", ""),
        )

    async def _resolve_default_branch(self, repo_info: RepoInfo) -> str:
        url = f"{self.BASE_API}/repos/{repo_info.owner}/{repo_info.repo}"
        async with httpx.AsyncClient(timeout=10, follow_redirects=FOLLOW_REDIRECTS) as client:
            r = await client.get(url, headers=self._headers)
        if r.status_code == 200:
            return r.json().get("default_branch", "main")
        return "main"


# ─────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────

class RepoNotFoundError(Exception):
    pass

class RepoAccessError(Exception):
    pass
