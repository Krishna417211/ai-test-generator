"""
git_publisher.py — Create a fresh GitHub repo and push a project into it.

Uses the GitHub Git Data API end-to-end (blobs → tree → commit → ref update),
so it needs no local `git` binary and pushes every file in a single commit.

Flow:
  1. GET  /user                       → resolve the authenticated owner
  2. POST /user/repos (auto_init)      → create an empty-ish repo with a base commit
  3. GET  /git/ref/heads/{branch}      → base commit sha
  4. GET  /git/commits/{sha}           → base tree sha
  5. POST /git/blobs  (per file)       → blob shas (uploaded concurrently)
  6. POST /git/trees                   → new tree layered on the base tree
  7. POST /git/commits                 → new commit
  8. PATCH /git/refs/heads/{branch}    → fast-forward the branch to it
"""

import base64
import asyncio
import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

BASE_API = "https://api.github.com"


class GitPublishError(Exception):
    """Raised when the repo cannot be created or pushed to."""


@dataclass
class PublishResult:
    repo_url: str
    full_name: str
    branch: str
    commit_sha: str
    files_pushed: int
    warnings: list[str] = field(default_factory=list)


class GitPublisher:
    """Creates a new GitHub repository and pushes files to it via the REST API."""

    def __init__(self, token: str, timeout: float = 30.0):
        if not token or not token.strip():
            raise GitPublishError("A GitHub token is required to publish a repo.")
        self.token = token.strip()
        self.timeout = timeout
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {self.token}",
        }

    async def publish(
        self,
        repo_name: str,
        files: dict[str, str],
        *,
        private: bool = True,
        description: str = "",
        commit_message: str = "Initial commit from Testra",
        blob_concurrency: int = 8,
    ) -> PublishResult:
        if not files:
            raise GitPublishError("No files to push.")

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            owner = await self._get_owner(client)
            repo, branch = await self._create_repo(client, repo_name, private, description)
            base_commit_sha, base_tree_sha = await self._get_base(client, owner, repo, branch)

            # Upload every file as a blob (concurrently), then assemble a tree.
            sem = asyncio.Semaphore(blob_concurrency)

            async def make_blob(path: str, content: str) -> dict:
                async with sem:
                    sha = await self._create_blob(client, owner, repo, content)
                return {"path": path, "mode": "100644", "type": "blob", "sha": sha}

            tree_items = await asyncio.gather(
                *(make_blob(p, c) for p, c in files.items())
            )

            tree_sha = await self._create_tree(client, owner, repo, base_tree_sha, tree_items)
            commit_sha = await self._create_commit(
                client, owner, repo, commit_message, tree_sha, base_commit_sha
            )
            await self._update_ref(client, owner, repo, branch, commit_sha)

        return PublishResult(
            repo_url=f"https://github.com/{owner}/{repo}",
            full_name=f"{owner}/{repo}",
            branch=branch,
            commit_sha=commit_sha,
            files_pushed=len(files),
        )

    # ── individual API steps ─────────────────────────────

    async def _get_owner(self, client: httpx.AsyncClient) -> str:
        r = await client.get(f"{BASE_API}/user", headers=self._headers)
        if r.status_code == 401:
            raise GitPublishError("GitHub token is invalid or expired.")
        if r.status_code != 200:
            raise GitPublishError(self._msg(r, "Could not authenticate with GitHub"))
        login = r.json().get("login")
        if not login:
            raise GitPublishError("Could not resolve GitHub username from token.")
        return login

    async def _create_repo(
        self, client: httpx.AsyncClient, repo_name: str, private: bool, description: str
    ) -> tuple[str, str]:
        payload = {
            "name": repo_name,
            "private": private,
            "auto_init": True,  # seed a base commit so we have a ref to build on
            "description": description or "Generated and published by Testra",
        }
        r = await client.post(f"{BASE_API}/user/repos", headers=self._headers, json=payload)
        if r.status_code == 422:
            # Almost always "name already exists on this account".
            raise GitPublishError(
                f"A repo named '{repo_name}' already exists on this account "
                "(or the name is invalid). Pick a different name."
            )
        if r.status_code == 403:
            raise GitPublishError(
                "GitHub rejected the request (403). The token needs the 'repo' scope "
                "to create and push repositories."
            )
        if r.status_code not in (200, 201):
            raise GitPublishError(self._msg(r, "Failed to create repository"))
        data = r.json()
        return data["name"], data.get("default_branch", "main")

    async def _get_base(
        self, client: httpx.AsyncClient, owner: str, repo: str, branch: str
    ) -> tuple[str, str]:
        # auto_init is usually instant, but the ref can lag a beat — retry briefly.
        ref_url = f"{BASE_API}/repos/{owner}/{repo}/git/ref/heads/{branch}"
        commit_sha = ""
        for attempt in range(5):
            r = await client.get(ref_url, headers=self._headers)
            if r.status_code == 200:
                commit_sha = r.json()["object"]["sha"]
                break
            if r.status_code == 404 and attempt < 4:
                await asyncio.sleep(0.8)
                continue
            raise GitPublishError(self._msg(r, "Could not read the repo's base branch"))
        if not commit_sha:
            raise GitPublishError("Repo was created but its initial commit never appeared.")

        r = await client.get(
            f"{BASE_API}/repos/{owner}/{repo}/git/commits/{commit_sha}", headers=self._headers
        )
        if r.status_code != 200:
            raise GitPublishError(self._msg(r, "Could not read the base commit"))
        return commit_sha, r.json()["tree"]["sha"]

    async def _create_blob(
        self, client: httpx.AsyncClient, owner: str, repo: str, content: str
    ) -> str:
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        r = await client.post(
            f"{BASE_API}/repos/{owner}/{repo}/git/blobs",
            headers=self._headers,
            json={"content": encoded, "encoding": "base64"},
        )
        if r.status_code not in (200, 201):
            raise GitPublishError(self._msg(r, "Failed to upload a file blob"))
        return r.json()["sha"]

    async def _create_tree(
        self, client: httpx.AsyncClient, owner: str, repo: str,
        base_tree_sha: str, tree_items: list[dict],
    ) -> str:
        r = await client.post(
            f"{BASE_API}/repos/{owner}/{repo}/git/trees",
            headers=self._headers,
            json={"base_tree": base_tree_sha, "tree": tree_items},
        )
        if r.status_code not in (200, 201):
            raise GitPublishError(self._msg(r, "Failed to build the git tree"))
        return r.json()["sha"]

    async def _create_commit(
        self, client: httpx.AsyncClient, owner: str, repo: str,
        message: str, tree_sha: str, parent_sha: str,
    ) -> str:
        r = await client.post(
            f"{BASE_API}/repos/{owner}/{repo}/git/commits",
            headers=self._headers,
            json={"message": message, "tree": tree_sha, "parents": [parent_sha]},
        )
        if r.status_code not in (200, 201):
            raise GitPublishError(self._msg(r, "Failed to create the commit"))
        return r.json()["sha"]

    async def _update_ref(
        self, client: httpx.AsyncClient, owner: str, repo: str, branch: str, commit_sha: str
    ) -> None:
        r = await client.patch(
            f"{BASE_API}/repos/{owner}/{repo}/git/refs/heads/{branch}",
            headers=self._headers,
            json={"sha": commit_sha, "force": False},
        )
        if r.status_code != 200:
            raise GitPublishError(self._msg(r, "Failed to push the commit to the branch"))

    @staticmethod
    def _msg(resp: httpx.Response, prefix: str) -> str:
        try:
            detail = resp.json().get("message", "")
        except Exception:
            detail = resp.text[:200]
        return f"{prefix} (HTTP {resp.status_code}): {detail}".strip()
