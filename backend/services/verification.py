"""
verification.py — One-time secrets for email verification, password reset, and
the login OTP, plus the emails that carry them.

Threat model / why the two kinds of secret are stored differently
----------------------------------------------------------------
Link tokens (verify, reset) are 256-bit random. Guessing one is not a thing that
happens, so a plain SHA-256 is enough to keep the database from being a list of
working links: it's preimage-resistant, and there's no dictionary to run against
a value with that much entropy.

The login OTP is six digits — a million possibilities, which a GPU walks through
in microseconds. It therefore gets bcrypt (slow by construction) rather than
SHA-256, so a stolen database still doesn't hand over live codes. Online guessing
is capped separately by `login_otp_max_attempts`, because bcrypt only makes each
guess expensive, not impossible.

Everything is single-use and short-lived, and lives in the sessions table (which
already has expiry + sweeping) under a namespace prefix — the same pattern the
OAuth state uses in main.py.
"""

import hashlib
import logging
import secrets
from dataclasses import dataclass

import bcrypt

from config import settings
from services.mailer import send_email
from services.store import store

logger = logging.getLogger(__name__)

# Namespaces inside the shared sessions table. A verify token must never be
# accepted as a reset token, so they can't share a keyspace.
_VERIFY_PREFIX = "emailverify:"
_RESET_PREFIX = "pwreset:"
_OTP_PREFIX = "loginotp:"


class InvalidCode(Exception):
    """A token/code that is wrong, used, or expired. Deliberately one exception
    for all three: telling an attacker *which* is a free oracle."""


@dataclass
class OtpChallenge:
    challenge_id: str
    expires_in: int


def _hash_token(token: str) -> str:
    """SHA-256 for high-entropy tokens (see module docstring)."""
    return hashlib.sha256(token.encode()).hexdigest()


def _hash_code(code: str) -> str:
    """bcrypt for the low-entropy OTP (see module docstring)."""
    return bcrypt.hashpw(code.encode(), bcrypt.gensalt()).decode()


def mask_email(email: str) -> str:
    """`ada@example.com` → `a**@example.com`.

    The client shows the user which address a code went to without this becoming
    a way to read back an address the caller didn't already know.
    """
    if not email or "@" not in email:
        return ""
    local, _, domain = email.partition("@")
    shown = local[0] if local else ""
    return f"{shown}{'*' * max(len(local) - 1, 1)}@{domain}"


# ── email verification ───────────────────────

async def send_verification_email(user_id: str, email: str, name: str = "") -> None:
    """Mint a fresh verification link and email it. Raises on delivery failure."""
    token = secrets.token_urlsafe(32)
    store.create_session(
        _VERIFY_PREFIX + _hash_token(token),
        {"user_id": user_id, "email": email},
        settings.email_verify_ttl_seconds,
    )
    link = f"{settings.frontend_url.rstrip('/')}/verify-email?token={token}"
    hours = settings.email_verify_ttl_seconds // 3600
    greeting = f"Hi {name}," if name else "Hi,"

    await send_email(
        to=email,
        subject="Verify your email for Testra",
        text=(
            f"{greeting}\n\n"
            "Confirm this address to activate your Testra account:\n\n"
            f"{link}\n\n"
            f"The link works once and expires in {hours} hours.\n\n"
            "If you didn't sign up for Testra, ignore this email — no account "
            "will be activated.\n"
        ),
        html=(
            f"<p>{greeting}</p>"
            "<p>Confirm this address to activate your Testra account:</p>"
            f'<p><a href="{link}">Verify my email</a></p>'
            f"<p>The link works once and expires in {hours} hours.</p>"
            "<p>If you didn't sign up for Testra, ignore this email — no account "
            "will be activated.</p>"
        ),
    )


def consume_verification_token(token: str) -> str:
    """Redeem a verification token → user_id. Raises InvalidCode if it isn't good."""
    key = _VERIFY_PREFIX + _hash_token(token or "")
    data = store.get_session(key)
    if not data:
        raise InvalidCode("This verification link is invalid or has expired.")
    store.delete_session(key)          # single use
    return data["user_id"]


# ── password reset ───────────────────────────

async def send_password_reset_email(user_id: str, email: str, name: str = "") -> None:
    token = secrets.token_urlsafe(32)
    store.create_session(
        _RESET_PREFIX + _hash_token(token),
        {"user_id": user_id, "email": email},
        settings.password_reset_ttl_seconds,
    )
    link = f"{settings.frontend_url.rstrip('/')}/reset-password?token={token}"
    minutes = settings.password_reset_ttl_seconds // 60
    greeting = f"Hi {name}," if name else "Hi,"

    await send_email(
        to=email,
        subject="Reset your Testra password",
        text=(
            f"{greeting}\n\n"
            "Someone asked to reset the password on your Testra account. If that "
            "was you, set a new one here:\n\n"
            f"{link}\n\n"
            f"The link works once and expires in {minutes} minutes.\n\n"
            "If it wasn't you, ignore this email — your password stays as it is, "
            "and nobody can use this link without access to your inbox.\n"
        ),
        html=(
            f"<p>{greeting}</p>"
            "<p>Someone asked to reset the password on your Testra account. "
            "If that was you, set a new one here:</p>"
            f'<p><a href="{link}">Choose a new password</a></p>'
            f"<p>The link works once and expires in {minutes} minutes.</p>"
            "<p>If it wasn't you, ignore this email — your password stays as it "
            "is, and nobody can use this link without access to your inbox.</p>"
        ),
    )


def consume_reset_token(token: str) -> str:
    key = _RESET_PREFIX + _hash_token(token or "")
    data = store.get_session(key)
    if not data:
        raise InvalidCode("This reset link is invalid or has expired.")
    store.delete_session(key)
    return data["user_id"]


# ── login OTP ────────────────────────────────

def _new_code() -> str:
    """A 6-digit code from a CSPRNG. `secrets`, never `random` — the latter's
    Mersenne Twister is reconstructible from past outputs."""
    return f"{secrets.randbelow(1_000_000):06d}"


async def start_login_challenge(user_id: str, email: str, name: str = "") -> OtpChallenge:
    """Issue an OTP for a login that has already passed the password check.

    The challenge id is itself a random token, not the user id: it's handed to a
    client that hasn't finished authenticating, so it must not leak who the login
    is for or be forgeable into someone else's challenge.
    """
    challenge_id = secrets.token_urlsafe(24)
    code = _new_code()
    store.create_session(
        _OTP_PREFIX + challenge_id,
        {"user_id": user_id, "code_hash": _hash_code(code), "attempts": 0},
        settings.login_otp_ttl_seconds,
    )
    minutes = max(1, settings.login_otp_ttl_seconds // 60)
    greeting = f"Hi {name}," if name else "Hi,"

    await send_email(
        to=email,
        subject=f"{code} is your Testra login code",
        text=(
            f"{greeting}\n\n"
            f"Your login code is: {code}\n\n"
            f"It expires in {minutes} minutes and can only be used once.\n\n"
            "If you didn't try to log in, someone may know your password — "
            "change it, and don't share this code with anyone.\n"
        ),
        html=(
            f"<p>{greeting}</p>"
            f"<p>Your login code is: <strong style=\"font-size:20px;letter-spacing:2px\">{code}</strong></p>"
            f"<p>It expires in {minutes} minutes and can only be used once.</p>"
            "<p>If you didn't try to log in, someone may know your password — "
            "change it, and don't share this code with anyone.</p>"
        ),
    )
    return OtpChallenge(challenge_id=challenge_id, expires_in=settings.login_otp_ttl_seconds)


def verify_login_challenge(challenge_id: str, code: str) -> str:
    """Check an OTP → user_id. Raises InvalidCode on anything but a clean hit.

    A wrong guess burns an attempt; running out burns the whole challenge, so an
    attacker gets `login_otp_max_attempts` shots at 1-in-a-million and then has
    to get past the password again to earn a new code.
    """
    key = _OTP_PREFIX + (challenge_id or "")
    data = store.get_session(key)
    if not data:
        raise InvalidCode("This login code has expired. Please log in again.")

    submitted = (code or "").strip()
    ok = False
    if len(submitted) == 6 and submitted.isdigit():
        # Skip bcrypt on malformed input — it can't match, and hashing whatever
        # arrives lets an unauthenticated caller spend our CPU at will.
        ok = bcrypt.checkpw(submitted.encode(), data["code_hash"].encode())

    if not ok:
        attempts = int(data.get("attempts", 0)) + 1
        if attempts >= settings.login_otp_max_attempts:
            store.delete_session(key)
            logger.warning(
                f"Login OTP for {data['user_id']} burned after "
                f"{attempts} failed attempts"
            )
            raise InvalidCode(
                "Too many incorrect codes. Please log in again to get a new one."
            )
        data["attempts"] = attempts
        store.update_session(key, data)
        left = settings.login_otp_max_attempts - attempts
        raise InvalidCode(
            f"That code isn't right. {left} attempt{'s' if left != 1 else ''} left."
        )

    store.delete_session(key)          # single use
    return data["user_id"]
