"""
login_guard.py — Per-account throttling for password login.

The per-IP limiter in ratelimit.py stops one host hammering the API. It does not
stop the attack that actually matters against a specific account: a credential
-stuffing run spread across hundreds of addresses, each staying comfortably under
a per-IP cap while collectively trying thousands of passwords against one inbox.
Botnets and residential-proxy pools make that cheap, and it is the shape almost
every real account takeover takes.

So the counter is keyed on the **account**, not the caller. Whoever is guessing,
and from wherever, the account itself only affords so many wrong answers.

## Why lock the attempt, not the account

Disabling an account after N failures hands anyone a denial-of-service button:
learn someone's email, fail five logins, they are locked out. So this never
disables anything. It introduces a delay that grows with consecutive failures and
disappears the moment a correct password arrives:

    failures  1  2  3  4   5   6    7+
    locked    -  -  -  5s  15s 60s  300s

The first few typos cost nothing, which is the overwhelmingly common case. By the
time someone is guessing in earnest, five minutes per attempt makes an online
search worthless — and the legitimate owner gets in immediately by typing the
right password, or by resetting it, neither of which this blocks.

## Why the store rather than memory

`ratelimit.py` keeps its windows in a process dict, which resets on restart and
is per-worker. That is a reasonable trade for coarse abuse control. It is not
reasonable here: an attacker who can trigger a restart, or who is simply load
-balanced onto another worker, resets the counter. These live in the same SQLite
the sessions do, so the limit is the limit no matter which process answers.
"""

import time
import logging

from services.store import store

logger = logging.getLogger(__name__)

# Delay after N consecutive failures. Index 0 is unused (zero failures).
_BACKOFF_SECONDS = (0, 0, 0, 0, 5, 15, 60, 300)
_MAX_BACKOFF = _BACKOFF_SECONDS[-1]

# How long a failure counter survives without any further attempts. Long enough
# that a slow drip cannot quietly reset it, short enough that a genuine user who
# gave up yesterday starts clean today.
_COUNTER_TTL = 24 * 60 * 60

_PREFIX = "loginfail:"


def _key(email: str) -> str:
    return _PREFIX + (email or "").strip().lower()


def backoff_for(failures: int) -> int:
    """Seconds a caller must wait after `failures` consecutive wrong passwords."""
    if failures < len(_BACKOFF_SECONDS):
        return _BACKOFF_SECONDS[failures]
    return _MAX_BACKOFF


def check(email: str) -> tuple[bool, int]:
    """May this account be tried right now? Returns (allowed, seconds_to_wait).

    Called before the password is verified — the point is to refuse the attempt,
    not to make it and then complain.
    """
    record = store.get_session(_key(email))
    if not record:
        return True, 0
    retry_at = float(record.get("retry_at", 0))
    remaining = retry_at - time.time()
    if remaining > 0:
        return False, int(remaining) + 1
    return True, 0


def record_failure(email: str) -> int:
    """Count a wrong password. Returns the new consecutive-failure total."""
    key = _key(email)
    record = store.get_session(key) or {"failures": 0}
    failures = int(record.get("failures", 0)) + 1
    delay = backoff_for(failures)
    store.create_session(
        key,
        {"failures": failures, "retry_at": time.time() + delay},
        _COUNTER_TTL,
    )
    if delay:
        # Logged at the point it starts costing someone time. The address is
        # already in the auth logs either way; the failure count is the part
        # that makes a stuffing run visible in them.
        logger.warning(
            f"Login throttled after {failures} consecutive failures "
            f"(next attempt in {delay}s)"
        )
    return failures


def clear(email: str) -> None:
    """Forget the failures for an account. Called on any successful password.

    Deliberately on the password being right, not on the login *completing*: a
    correct password proves the guessing has stopped, and holding the counter
    over an OTP step would let a user who fumbles a code inherit a delay earned
    by somebody else's guessing.
    """
    store.delete_session(_key(email))
