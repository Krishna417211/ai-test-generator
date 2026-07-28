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
  9. GET  /git/trees/{sha}?recursive=1 → read the pushed tree back and prove
                                         every file is there, byte for byte

Step 9 is not ceremony. Steps 5–8 can each return 2xx and still leave a repo
short of files: a blob that uploaded under a truncated body, a tree assembled
from a stale base, a path the API normalised differently than we spelled it. The
user is told "18 files pushed" and finds 16, usually much later. Reading the
tree back is the only statement about the repo that comes from the repo, and git
makes it cheap — a tree entry's sha IS the hash of its content, so comparing the
shas we uploaded against the shas now in the branch verifies contents, not just
names. Anything still missing is re-pushed once; anything missing after that is
an error, never a success with a footnote.
"""

import time
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
    # True when the pushed tree was read back and every expected path was found
    # with the exact blob sha we uploaded. False only when GitHub could not give
    # us a complete tree to check against (see `verification_note`) — a genuinely
    # missing file raises instead of landing here.
    verified: bool = False
    verification_note: str = ""
    # Files that had to be pushed a second time before they appeared. Empty on a
    # normal run; non-empty means the first commit was incomplete and we fixed
    # it, which is worth telling the user.
    repaired_files: list[str] = field(default_factory=list)


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

        warnings: list[str] = []
        repaired: list[str] = []

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            owner = await self._get_owner(client)
            repo, branch = await self._create_repo(client, repo_name, private, description)
            base_commit_sha, base_tree_sha = await self._get_base(client, owner, repo, branch)

            tree_items = await self._upload_blobs(
                client, owner, repo, files, blob_concurrency
            )
            commit_sha = await self._commit_tree(
                client, owner, repo, branch, commit_message,
                base_tree_sha, base_commit_sha, tree_items,
            )

            # Read the branch back and prove the files are in it.
            missing, note = await self._verify_tree(client, owner, repo, commit_sha, tree_items)

            if missing:
                # One repair attempt, then it is an error. Re-uploading the blobs
                # rather than reusing the shas is deliberate: if the first upload
                # is why they are missing, reusing its output repeats the bug.
                logger.warning(
                    f"{len(missing)} file(s) missing from {owner}/{repo} after push — "
                    f"re-pushing: {', '.join(sorted(missing)[:5])}"
                )
                retry_items = await self._upload_blobs(
                    client, owner, repo,
                    {p: files[p] for p in missing}, blob_concurrency,
                )
                commit_sha = await self._commit_tree(
                    client, owner, repo, branch,
                    f"{commit_message} (completing {len(missing)} missing file(s))",
                    # Layer onto the tree that is now live, not the original base,
                    # or the repair commit would delete everything that did land.
                    await self._tree_sha_of(client, owner, repo, commit_sha),
                    commit_sha, retry_items,
                )
                # Verify against the shas the repair actually uploaded, not the
                # ones from the first attempt. Real GitHub blob shas are content
                # hashes and so come back identical, but relying on that would
                # make this check silently wrong the day a file is re-uploaded
                # with any normalisation applied.
                latest = {i["path"]: i for i in tree_items}
                latest.update({i["path"]: i for i in retry_items})
                still_missing, note = await self._verify_tree(
                    client, owner, repo, commit_sha, list(latest.values())
                )
                if still_missing:
                    raise GitPublishError(
                        f"Pushed to {owner}/{repo}, but "
                        f"{len(still_missing)} file(s) are not in the repository "
                        f"after a retry: {', '.join(sorted(still_missing)[:5])}"
                        f"{'...' if len(still_missing) > 5 else ''}. "
                        "The repo was created — delete it and publish again."
                    )
                repaired = sorted(missing)
                warnings.append(
                    f"{len(repaired)} file(s) did not land on the first commit and "
                    "were pushed again — the repo is complete, in two commits."
                )

        if note:
            warnings.append(note)

        return PublishResult(
            repo_url=f"https://github.com/{owner}/{repo}",
            full_name=f"{owner}/{repo}",
            branch=branch,
            commit_sha=commit_sha,
            files_pushed=len(files),
            warnings=warnings,
            verified=not note,
            verification_note=note,
            repaired_files=repaired,
        )

    async def _upload_blobs(
        self, client: httpx.AsyncClient, owner: str, repo: str,
        files: dict[str, str], concurrency: int,
    ) -> list[dict]:
        """Upload each file as a blob (concurrently) and return tree entries."""
        sem = asyncio.Semaphore(concurrency)

        async def make_blob(path: str, content: str) -> dict:
            async with sem:
                sha = await self._create_blob(client, owner, repo, content)
            return {"path": path, "mode": "100644", "type": "blob", "sha": sha}

        return list(await asyncio.gather(*(make_blob(p, c) for p, c in files.items())))

    async def _commit_tree(
        self, client: httpx.AsyncClient, owner: str, repo: str, branch: str,
        message: str, base_tree_sha: str, parent_sha: str, tree_items: list[dict],
    ) -> str:
        """tree → commit → ref, the three writes that make a push. Returns the sha."""
        tree_sha = await self._create_tree(client, owner, repo, base_tree_sha, tree_items)
        commit_sha = await self._create_commit(
            client, owner, repo, message, tree_sha, parent_sha
        )
        await self._update_ref(client, owner, repo, branch, commit_sha)
        return commit_sha

    async def _tree_sha_of(
        self, client: httpx.AsyncClient, owner: str, repo: str, commit_sha: str
    ) -> str:
        r = await client.get(
            f"{BASE_API}/repos/{owner}/{repo}/git/commits/{commit_sha}", headers=self._headers
        )
        if r.status_code != 200:
            raise GitPublishError(self._msg(r, "Could not read the commit we just made"))
        return r.json()["tree"]["sha"]

    async def _verify_tree(
        self, client: httpx.AsyncClient, owner: str, repo: str,
        commit_sha: str, expected: list[dict],
    ) -> tuple[set[str], str]:
        """Read the pushed tree back. Returns (missing_paths, note).

        A path counts as present only when its blob sha matches the one we
        uploaded. Git shas are content hashes, so an equal sha is proof the
        bytes match — a path present with different content is reported as
        missing, which is the honest reading: what we sent is not what is there.

        `note` is set only when the check itself could not be completed (GitHub
        truncates trees over ~100k entries, or the read failed). In that case
        nothing is claimed as verified, and nothing is claimed as missing
        either — an unread tree is not evidence of absence.
        """
        want = {item["path"]: item["sha"] for item in expected}
        r = await client.get(
            f"{BASE_API}/repos/{owner}/{repo}/git/trees/{commit_sha}",
            headers=self._headers,
            params={"recursive": "1"},
        )
        if r.status_code != 200:
            logger.warning(
                f"Could not verify the push to {owner}/{repo}: "
                f"git/trees returned {r.status_code}"
            )
            return set(), (
                "The push succeeded, but GitHub wouldn't let us read the repo back "
                f"to confirm every file arrived (HTTP {r.status_code}). "
                "Check the repo's file list."
            )

        data = r.json()
        if data.get("truncated"):
            return set(), (
                "This repo is too large for GitHub to return its full file list in "
                "one response, so we could not confirm every file individually."
            )

        actual = {
            e["path"]: e.get("sha")
            for e in data.get("tree", [])
            if e.get("type") == "blob"
        }
        missing = {p for p, sha in want.items() if actual.get(p) != sha}
        if missing:
            logger.warning(
                f"Verification found {len(missing)} missing/mismatched file(s) in "
                f"{owner}/{repo}"
            )
        return missing, ""

    # ── individual API steps ─────────────────────────────

    async def delete_repo(self, full_name: str) -> None:
        """Delete `owner/repo` on GitHub. Irreversible.

        Callers must confirm the repo was created by this app for the
        requesting user — this method does no ownership vetting of its own.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.delete(f"{BASE_API}/repos/{full_name}", headers=self._headers)

        if r.status_code == 204:
            logger.info(f"Deleted GitHub repo {full_name}")
            return
        if r.status_code == 404:
            raise GitPublishError(f"Repository {full_name} no longer exists on GitHub.")
        if r.status_code in (401, 403):
            raise GitPublishError(
                "GitHub refused the delete. The token needs the 'delete_repo' scope — "
                "disconnect and reconnect your GitHub account to grant it."
            )
        raise GitPublishError(f"Could not delete {full_name} (GitHub returned {r.status_code}).")

    async def _get_owner(self, client: httpx.AsyncClient) -> str:
        r = await client.get(f"{BASE_API}/user", headers=self._headers)
        if r.status_code == 401:
            raise GitPublishError(
                "GitHub token is invalid or expired. Disconnect and reconnect "
                "GitHub to get a fresh one."
            )
        if r.status_code == 403:
            raise GitPublishError(self._forbidden_reason(r))
        if r.status_code != 200:
            raise GitPublishError(self._msg(r, "Could not authenticate with GitHub"))

        # Check the granted scopes here, on the first call, rather than
        # discovering the problem half-way through a publish. GitHub reports what
        # the token can actually do in X-OAuth-Scopes; a token without 'repo'
        # cannot create or push a repository, and failing now says so precisely
        # instead of surfacing an opaque 403 after we've started.
        #
        # The header is only sent for OAuth/classic tokens. Fine-grained PATs and
        # GitHub App installation tokens omit it, so an ABSENT header must not be
        # treated as "no scopes" — we simply can't tell, and proceeding lets the
        # request itself be the judge.
        granted = r.headers.get("x-oauth-scopes")
        if granted is not None:
            have = {s.strip() for s in granted.split(",") if s.strip()}
            if "repo" not in have:
                raise GitPublishError(
                    "This GitHub token can't create repositories — it is missing "
                    f"the 'repo' scope (it has: {granted.strip() or 'none'}). "
                    "Disconnect and reconnect GitHub to re-grant access."
                )

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
            raise GitPublishError(self._forbidden_reason(r))
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
        # A freshly created repo can 404 here for a beat even though the ref read
        # and every blob upload just succeeded — GitHub serves those from a
        # different subsystem than the git-data writes. Retry: building a tree is
        # side-effect-free (it creates a dangling object, touching no refs), so a
        # repeat is harmless. A 404 that outlives the retries is reported as-is.
        # Malformed paths/shas come back as 422 and are surfaced immediately.
        for attempt in range(4):
            r = await client.post(
                f"{BASE_API}/repos/{owner}/{repo}/git/trees",
                headers=self._headers,
                json={"base_tree": base_tree_sha, "tree": tree_items},
            )
            if r.status_code in (200, 201):
                return r.json()["sha"]

            transient = r.status_code in (404, 500, 502, 503)
            if transient and attempt < 3:
                logger.warning(
                    f"git/trees returned {r.status_code} for {owner}/{repo} — "
                    f"retrying ({attempt + 1}/3)"
                )
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if transient:
                raise GitPublishError(
                    f"Failed to build the git tree — GitHub kept returning "
                    f"{r.status_code} for {owner}/{repo}, a repo it had just created. "
                    "This is usually temporary; try publishing again."
                )
            raise GitPublishError(self._msg(r, "Failed to build the git tree"))

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

    @staticmethod
    def _forbidden_reason(resp: httpx.Response) -> str:
        """Say why GitHub actually returned 403, instead of guessing.

        A 403 here used to be reported as "the token needs the 'repo' scope" no
        matter what caused it, which is unactionable when the real reason was
        something else — and actively misleading when the token's scopes were
        fine all along. GitHub tells us far more than that:

          • X-OAuth-Scopes lists what the token was actually granted, so a scope
            problem can be stated as fact ("has: public_repo") rather than
            guessed at.
          • X-RateLimit-Remaining: 0 means a rate limit, not a permission problem.
          • The JSON body's `message` covers everything else — SAML SSO
            authorization, blocked accounts, org policy, secondary rate limits.
        """
        try:
            detail = (resp.json() or {}).get("message", "") or ""
        except Exception:
            detail = ""
        low = detail.lower()

        if resp.headers.get("x-ratelimit-remaining") == "0" or "rate limit" in low:
            reset = resp.headers.get("x-ratelimit-reset", "")
            when = ""
            if reset.isdigit():
                mins = max(0, int((int(reset) - time.time()) // 60))
                when = f" It resets in about {mins} minute{'s' if mins != 1 else ''}."
            return f"GitHub rate-limited the request (403).{when} Wait and try again."

        if "saml" in low or "sso" in low:
            return (
                "GitHub refused the request (403) because the organisation requires "
                "SAML SSO authorization for this token. Open your GitHub settings → "
                "Applications, and authorize the token for that organisation."
            )

        granted = (resp.headers.get("x-oauth-scopes") or "").strip()
        if granted:
            have = {s.strip() for s in granted.split(",") if s.strip()}
            if "repo" not in have:
                return (
                    f"GitHub refused the request (403). This token is missing the "
                    f"'repo' scope — it currently has: {granted or 'none'}. "
                    "Disconnect and reconnect GitHub to re-grant access."
                )
            # Scopes are fine, so the cause is something else — say so plainly
            # rather than sending the user to re-check a scope that's already set.
            return (
                f"GitHub refused the request (403) even though the token has the "
                f"required scope (has: {granted}). GitHub said: "
                f"{detail or 'no reason given'}."
            )

        return (
            "GitHub refused the request (403). "
            + (f"GitHub said: {detail}" if detail else
               "No reason was given — the token may lack the 'repo' scope, or the "
               "account/organisation may be blocking it.")
        )
