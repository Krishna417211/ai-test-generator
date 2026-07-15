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
