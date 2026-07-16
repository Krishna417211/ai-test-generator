"""
google_oauth.py — Google OAuth 2.0 (OpenID Connect) web-flow helpers.

The Google counterpart to github_oauth.py: "Continue with Google" on the login
page. Unlike GitHub, Google is only ever an *identity* here — it grants no
repo access — so the session it produces carries no provider token, and the
account it creates is email/password-less until the user sets one.

Flow (driven by main.py), identical in shape to the GitHub one:
  1. authorize_url()  → send the browser to Google's consent screen
  2. exchange_code()  → swap the returned ?code for an access token
  3. get_user()       → read the OpenID userinfo (sub, email, name, picture)

Requires GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET (see config.py). Google will
only redirect back to an https:// callback on a real hostname — a bare IP or
plain http is rejected when you register the URI — so this needs the site to be
reachable over HTTPS on a domain (see docs/DEPLOY.md).
"""

import logging
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"

# openid+email+profile is the minimum that returns a stable id (`sub`), the
# address, and a display name/avatar. No other scope is requested: this login
# never acts on the user's Google account, it only identifies them.
DEFAULT_SCOPE = "openid email profile"


class OAuthError(Exception):
    """Raised when the OAuth handshake fails."""


def authorize_url(client_id: str, redirect_uri: str, state: str, scope: str = DEFAULT_SCOPE) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        # Google, unlike GitHub, requires response_type explicitly.
        "response_type": "code",
        "scope": scope,
        "state": state,
        # We don't want a refresh token (no offline access), so access_type stays
        # online. prompt=select_account lets someone with several Google logins
        # pick, instead of being silently taken in as whoever's already signed in.
        "access_type": "online",
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
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            TOKEN_ENDPOINT, data=payload, headers={"Accept": "application/json"}
        )
    if r.status_code != 200:
        # Google returns the reason in an "error"/"error_description" body.
        try:
            err = r.json().get("error_description") or r.json().get("error")
        except Exception:
            err = None
        raise OAuthError(err or f"Google token exchange failed (HTTP {r.status_code}).")

    token = r.json().get("access_token")
    if not token:
        raise OAuthError("Google did not return an access token.")
    return token


async def get_user(token: str) -> dict:
    """Return the authenticated user's OpenID profile.

    email_verified is passed through rather than assumed: main.py decides what an
    unverified address means, exactly as it would for our own signup.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {token}"}
        )
    if r.status_code != 200:
        raise OAuthError("Could not read Google profile with the issued token.")
    data = r.json()
    return {
        # `sub` is Google's stable, immutable user id — safe to key on even if the
        # user later changes their email or display name.
        "id": data.get("sub"),
        "email": data.get("email"),
        "email_verified": bool(data.get("email_verified")),
        "name": data.get("name") or (data.get("email") or "").split("@")[0],
        "avatar_url": data.get("picture", ""),
    }
