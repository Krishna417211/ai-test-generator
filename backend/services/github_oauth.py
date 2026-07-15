"""
github_oauth.py — GitHub OAuth web flow helpers.

Lets a user click "Login with GitHub" and authorize the app to create/push
repos on their behalf, instead of pasting a Personal Access Token.

Flow (driven by main.py):
  1. authorize_url()  → send the browser to GitHub's consent screen
  2. exchange_code()  → swap the returned ?code for an access token
  3. get_user()       → resolve the logged-in username

Requires GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET (see config.py).
"""

import logging
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

AUTHORIZE_ENDPOINT = "https://github.com/login/oauth/authorize"
TOKEN_ENDPOINT = "https://github.com/login/oauth/access_token"
USER_ENDPOINT = "https://api.github.com/user"

# 'repo' is required to create private repos and push to them. If you only ever
# push public repos, 'public_repo' is narrower — but 'repo' covers both.
# 'delete_repo' backs the delete button. It authorises deleting ANY repo the
# user owns, so the API only ever deletes repos recorded in published_repos.
DEFAULT_SCOPE = "repo,delete_repo"


class OAuthError(Exception):
    """Raised when the OAuth handshake fails."""


def authorize_url(client_id: str, redirect_uri: str, state: str, scope: str = DEFAULT_SCOPE) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "allow_signup": "true",
    }
    return f"{AUTHORIZE_ENDPOINT}?{urlencode(params)}"


async def exchange_code(
    code: str, client_id: str, client_secret: str, redirect_uri: str
) -> str:
    """Exchange the OAuth `code` for a user access token."""
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            TOKEN_ENDPOINT, json=payload, headers={"Accept": "application/json"}
        )
    if r.status_code != 200:
        raise OAuthError(f"GitHub token exchange failed (HTTP {r.status_code}).")

    data = r.json()
    token = data.get("access_token")
    if not token:
        # GitHub returns 200 with an "error" field on bad/expired codes.
        raise OAuthError(data.get("error_description") or "GitHub did not return an access token.")
    return token


async def get_user(token: str) -> dict:
    """Return the authenticated user's profile (login, name, avatar_url)."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            USER_ENDPOINT,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    if r.status_code != 200:
        raise OAuthError("Could not read GitHub profile with the issued token.")
    data = r.json()
    return {
        "id": data.get("id"),
        "login": data.get("login", ""),
        "name": data.get("name") or data.get("login", ""),
        "email": data.get("email"),
        "avatar_url": data.get("avatar_url", ""),
    }
