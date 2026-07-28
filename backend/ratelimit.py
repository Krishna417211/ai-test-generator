"""
ratelimit.py — Lightweight in-memory per-IP sliding-window rate limiting.

Applied as FastAPI dependencies on the expensive, LLM-spending endpoints so a
single client can't exhaust the free-tier quotas. In-memory (per worker); for
multi-worker/production, back this with Redis. Used via `Depends(rate_limit(...))`
which — unlike slowapi's decorator — works correctly on routes with path params.
"""

import time
from collections import defaultdict, deque

from fastapi import Request, HTTPException


class SlidingWindowLimiter:
    def __init__(self, max_calls: int, window_seconds: float):
        self.max_calls = max_calls
        self.window = window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)

    def hit(self, key: str) -> bool:
        """Record a call for `key`; return False if it exceeds the limit."""
        now = time.time()
        dq = self._hits[key]
        while dq and dq[0] <= now - self.window:
            dq.popleft()
        if len(dq) >= self.max_calls:
            return False
        dq.append(now)
        return True


def rate_limit(limiter: SlidingWindowLimiter):
    async def _dependency(request: Request):
        key = request.client.host if request.client else "unknown"
        if not limiter.hit(key):
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded — please wait a moment and try again.",
            )
    return _dependency


# Endpoint limiters (calls per 60s per IP).
analyze_limiter = SlidingWindowLimiter(max_calls=20, window_seconds=60)
generate_limiter = SlidingWindowLimiter(max_calls=10, window_seconds=60)

# Anything that sends an email, per hour per IP. Deliberately much tighter than
# the others: each call puts a message in someone else's inbox, so an unlimited
# one is a free mail-bombing service pointed at any address an attacker names,
# and it burns the relay's reputation and sending quota along the way.
email_limiter = SlidingWindowLimiter(max_calls=6, window_seconds=3600)

# Guessing an OTP is capped per-challenge in verification.py; this stops someone
# from parallelising that across many freshly-minted challenges from one host.
otp_limiter = SlidingWindowLimiter(max_calls=20, window_seconds=600)

# CLI device-code login (RFC 8628). Three distinct exposures, three limiters —
# and which endpoint gets the tight one matters, because the obvious guess is
# wrong.
#
# Starting a login is NOT the brute-force surface. Minting a code hands the caller
# a code they already know, so it buys an attacker nothing; and every extra live
# code only widens the target by a rounding error (even a thousand of them, against
# a 6.6e11 space and the guess budget below, is a ~1e-8 chance). What a tight cap
# here WOULD do is break a whole office behind one NAT address, where these
# per-IP buckets are shared — so it stays generous.
cli_start_limiter = SlidingWindowLimiter(max_calls=30, window_seconds=600)

# Submitting a user_code IS the brute-force surface: 8 characters from a
# 30-symbol alphabet is 6.6e11 possibilities, which is only out of reach while
# guessing stays slow. Codes also expire in 10 minutes, so this cap puts the
# search space many orders of magnitude beyond reach within a code's lifetime.
cli_code_limiter = SlidingWindowLimiter(max_calls=10, window_seconds=600)

# Polling is expected and frequent — the CLI asks every 5s for up to 10 minutes,
# so ~120 calls is a *normal* login and the cap only has to stop a client that
# ignores the interval it was given. The device_code is high-entropy, so this is
# not a guessing surface at all.
cli_poll_limiter = SlidingWindowLimiter(max_calls=240, window_seconds=600)
