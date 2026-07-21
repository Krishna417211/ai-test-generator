"""
quota.py — Per-user generation quota.

Free accounts get `settings.free_generations_per_month` test generations per
calendar month; Pro is unmetered. Quota is reserved before the work starts and
refunded if that work fails, so a crashed run doesn't cost the user a credit.

Why this is per-user and not tied to the LLM router
---------------------------------------------------
`services/llm_router.py` rotating through *our* API keys and finding them all
hard-blocked (AllProvidersExhausted, reason="quota_exhausted") means capacity is
gone for EVERYONE — Pro subscribers included. Upselling at that moment would be
selling something we cannot deliver. That failure stays a plain 503; only the
per-user cap below may ever surface the upgrade prompt.
"""

import time
import calendar
import logging
from dataclasses import dataclass, asdict

from fastapi import Depends, HTTPException

from config import settings
from services.auth import require_user
from services.llm_router import Tier
from services.store import store

logger = logging.getLogger(__name__)

PRO = "pro"
FREE = "free"


def current_period(now: float | None = None) -> str:
    """The billing bucket a timestamp falls in — UTC calendar month."""
    return time.strftime("%Y-%m", time.gmtime(now if now is not None else time.time()))


def next_period_start(now: float | None = None) -> float:
    """Epoch seconds at which the current month's usage resets.

    timegm (not mktime) — the buckets are UTC, and mktime would shift the reset
    by the host's local offset.
    """
    t = time.gmtime(now if now is not None else time.time())
    year, month = (t.tm_year + 1, 1) if t.tm_mon == 12 else (t.tm_year, t.tm_mon + 1)
    return float(calendar.timegm((year, month, 1, 0, 0, 0, 0, 1, 0)))


def limit_for(plan: str) -> int | None:
    """Generations allowed per month, or None for unlimited."""
    return None if plan == PRO else settings.free_generations_per_month


def tier_for(plan: str) -> Tier:
    """Which model quality a plan buys.

    Deliberately next to limit_for: these are the only two things a plan
    changes, and they should be readable in one glance. This one is what makes
    "upgrade for better results" a fact rather than a marketing line — Pro
    routes to a genuinely stronger model (see llm_router.MODELS). If that ever
    stops being true, this function collapses to a constant and every upgrade
    prompt that mentions quality has to come out with it.
    """
    return Tier.PRO if plan == PRO else Tier.FREE


def tier_for_user(user_id: str) -> Tier:
    return tier_for(store.get_plan(user_id))


@dataclass
class QuotaState:
    plan: str
    used: int
    limit: int | None          # None = unlimited
    remaining: int | None      # None = unlimited
    period: str
    resets_at: float

    def as_dict(self) -> dict:
        return asdict(self)


def get_quota(user_id: str) -> QuotaState:
    """Current usage for a user. Read-only — does not consume anything."""
    plan = store.get_plan(user_id)
    period = current_period()
    used = store.get_usage(user_id, period)
    limit = limit_for(plan)
    return QuotaState(
        plan=plan,
        used=used,
        limit=limit,
        remaining=None if limit is None else max(0, limit - used),
        period=period,
        resets_at=next_period_start(),
    )


def quota_exceeded_detail(state: QuotaState) -> dict:
    """The structured 402 payload the client turns into the upgrade modal.

    A dict rather than prose so the client can render prices and reset dates
    without parsing text. Shared by the /api/generate 402 (below) and the
    /api/stream in-band error, so both paths speak exactly the same shape.
    """
    return {
        "reason": "quota_exceeded",
        "message": (
            f"You've used all {state.limit} free generations this month. "
            f"Upgrade to Pro for unlimited runs, or wait for your quota to reset."
        ),
        "plan": state.plan,
        "used": state.used,
        "limit": state.limit,
        "resets_at": state.resets_at,
        "pricing": {
            "monthly_usd": settings.pro_price_monthly_usd,
            "yearly_usd": settings.pro_price_yearly_usd,
        },
    }


def has_quota_remaining(user_id: str) -> tuple[bool, QuotaState]:
    """Read-only check: does this user have at least one generation left?

    Consumes nothing. Used to refuse the expensive LLM stream up front for an
    exhausted user, so the cap applies to the work — not just to the credit that
    /api/generate charges after the work has already run.
    """
    state = get_quota(user_id)
    ok = state.limit is None or state.remaining is None or state.remaining > 0
    return ok, state


def _payment_required(state: QuotaState) -> HTTPException:
    """The 402 that drives the upgrade modal."""
    return HTTPException(status_code=402, detail=quota_exceeded_detail(state))


class QuotaLease:
    """A reserved unit of quota, released back on failure.

    Usage:
        lease = consume_quota(user_id)
        try:
            ...expensive work...
        except Exception:
            lease.refund()
            raise
    """

    def __init__(self, user_id: str, period: str, state: QuotaState):
        self.user_id = user_id
        self.period = period
        self.state = state
        self._settled = False

    def refund(self) -> None:
        if self._settled:
            return
        self._settled = True
        store.refund(self.user_id, self.period)
        logger.info(f"Refunded 1 generation to {self.user_id} (run failed)")

    def commit(self) -> None:
        self._settled = True


def consume_quota(user_id: str) -> QuotaLease:
    """Reserve one generation for `user_id`, or raise 402 if they're capped."""
    plan = store.get_plan(user_id)
    period = current_period()
    limit = limit_for(plan)
    allowed, used = store.try_consume(user_id, period, limit)
    state = QuotaState(
        plan=plan,
        used=used,
        limit=limit,
        remaining=None if limit is None else max(0, limit - used),
        period=period,
        resets_at=next_period_start(),
    )
    if not allowed:
        logger.info(f"Quota exceeded for {user_id} ({used}/{limit} in {period})")
        raise _payment_required(state)
    return QuotaLease(user_id, period, state)


async def require_quota(ctx: dict = Depends(require_user)) -> dict:
    """FastAPI dependency: authenticate, then reserve a generation credit.

    Returns the auth context with a "quota_lease" added. Handlers must call
    `.refund()` on that lease if the generation fails.
    """
    lease = consume_quota(ctx["user_id"])
    return {**ctx, "quota_lease": lease}
