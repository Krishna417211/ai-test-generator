"""
auth.py — Authentication helpers: password hashing, validation, and the
FastAPI dependency that turns a session token into the current user.

Sessions are opaque tokens stored server-side (services/store.py) so they're
revocable. A token is accepted from either the Authorization: Bearer header
or a `token` query param (the latter is needed for SSE / EventSource, which
cannot set custom headers).
"""

import re
import uuid
import time
import secrets
import logging

import bcrypt
from fastapi import Request, HTTPException

from config import settings
from services.store import store

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ── password hashing ─────────────────────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ── validation ───────────────────────────────

def validate_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise HTTPException(400, "Please enter a valid email address.")
    return email


def validate_password(password: str) -> None:
    if len(password or "") < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")
    if len(password) > 200:
        raise HTTPException(400, "Password is too long.")


# ── users & sessions ─────────────────────────

def new_user_id() -> str:
    return "usr_" + uuid.uuid4().hex[:20]


def public_user(user: dict) -> dict:
    """Strip secrets before sending a user to the client."""
    return {
        "id": user["id"],
        "email": user.get("email"),
        "name": user.get("name") or (user.get("email") or "").split("@")[0],
        "github_login": user.get("github_login"),
        "avatar_url": user.get("avatar_url"),
        "has_github": bool(user.get("github_login")),
        "has_google": bool(user.get("google_id")),
    }


def create_login_session(user_id: str, *, github_token: str = "") -> str:
    """Create a session for a user and return its opaque token."""
    token = secrets.token_urlsafe(32)
    data = {"user_id": user_id}
    if github_token:
        data["github_token"] = github_token
    store.create_session(token, data, settings.oauth_session_ttl_seconds)
    return token


def _extract_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.query_params.get("token", "").strip()


async def require_user(request: Request) -> dict:
    """
    FastAPI dependency: resolve the current user from the session token, or 401.
    Returns a context dict: {"user": <public user>, "session": <session data>,
    "session_id": <token>, "user_id": ...}.
    """
    token = _extract_token(request)
    if not token:
        raise HTTPException(401, "Not authenticated. Please log in.")
    session = store.get_session(token)
    if not session or not session.get("user_id"):
        raise HTTPException(401, "Your session has expired. Please log in again.")
    user = store.get_user_by_id(session["user_id"])
    if not user:
        raise HTTPException(401, "Account not found. Please log in again.")
    return {
        "user": public_user(user),
        "session": session,
        "session_id": token,
        "user_id": user["id"],
    }
