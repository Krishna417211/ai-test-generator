"""
google_oauth.py — Google OAuth web flow helpers.

Lets a user click "Continue with Google" to sign up/log in with their Google
account instead of an email/password. Mirrors github_oauth.py's shape:

  1. authorize_url()  → send the browser to Google's consent screen
  2. exchange_code()  → swap the returned ?code for an access token
  3. get_user()       → resolve the logged-in profile

Requires GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET (see config.py).
"""

import logging
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"

DEFAULT_SCOPE = "openid email profile"


class OAuthError(Exception):
    """Raised when the OAuth handshake fails."""


def authorize_url(client_id: str, redirect_uri: str, state: str, scope: str = DEFAULT_SCOPE) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "response_type": "code",
        "prompt": "select_account",
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
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            TOKEN_ENDPOINT, data=payload, headers={"Accept": "application/json"}
        )
    if r.status_code != 200:
        raise OAuthError(f"Google token exchange failed (HTTP {r.status_code}).")

    data = r.json()
    token = data.get("access_token")
    if not token:
        raise OAuthError(data.get("error_description") or "Google did not return an access token.")
    return token


async def get_user(token: str) -> dict:
    """Return the authenticated user's profile (id, name, email, avatar_url)."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            USERINFO_ENDPOINT,
            headers={"Authorization": f"Bearer {token}"},
        )
    if r.status_code != 200:
        raise OAuthError("Could not read Google profile with the issued token.")
    data = r.json()
    return {
        "id": data.get("sub"),
        "login": (data.get("email") or "").split("@")[0],
        "name": data.get("name") or (data.get("email") or "").split("@")[0],
        "email": data.get("email"),
        "avatar_url": data.get("picture", ""),
    }
