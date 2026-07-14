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
import base64
import hashlib
import secrets
import logging

import bcrypt
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Request, HTTPException

from config import settings
from services.store import store

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ── secret encryption at rest (session GitHub tokens) ──

_ENC_PREFIX = "enc:v1:"
_fernet_cache: dict = {}


def _fernet():
    """Fernet built from settings.session_secret, or None if no secret is set."""
    secret = settings.session_secret
    if not secret:
        return None
    if _fernet_cache.get("secret") != secret:
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        _fernet_cache.update(secret=secret, fernet=Fernet(key))
    return _fernet_cache["fernet"]


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret for storage. No-op (returns plaintext) if no key is set."""
    if not plaintext:
        return plaintext
    f = _fernet()
    if f is None:
        return plaintext
    return _ENC_PREFIX + f.encrypt(plaintext.encode()).decode()


def decrypt_secret(value: str) -> str:
    """Decrypt a stored secret. Legacy plaintext (no prefix) is returned as-is."""
    if not value or not value.startswith(_ENC_PREFIX):
        return value
    f = _fernet()
    if f is None:
        return value
    try:
        return f.decrypt(value[len(_ENC_PREFIX):].encode()).decode()
    except InvalidToken:
        logger.warning("Could not decrypt a stored token (session_secret changed?)")
        return ""


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
        data["github_token"] = encrypt_secret(github_token)
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
    # Decrypt the linked GitHub token (if any) so downstream consumers get plaintext.
    if session.get("github_token"):
        session["github_token"] = decrypt_secret(session["github_token"])
    user = store.get_user_by_id(session["user_id"])
    if not user:
        raise HTTPException(401, "Account not found. Please log in again.")
    return {
        "user": public_user(user),
        "session": session,
        "session_id": token,
        "user_id": user["id"],
    }
