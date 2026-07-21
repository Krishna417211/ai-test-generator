"""test_quota.py — Per-user generation quota, plans, and the 402 upsell path."""

import time
import sqlite3
import tempfile

import pytest
from fastapi import HTTPException

from services import quota
from services.store import JobStore


def _store() -> JobStore:
    return JobStore(tempfile.mktemp(suffix=".db"))


def _user(s: JobStore, uid="usr_1", **kw) -> str:
    s.create_user({"id": uid, "email": f"{uid}@x.com", "created_at": time.time(), **kw})
    return uid


@pytest.fixture
def store(monkeypatch):
    """Point the quota module's store singleton at a throwaway DB."""
    s = _store()
    monkeypatch.setattr(quota, "store", s)
    return s


@pytest.fixture
def free_limit(monkeypatch):
    monkeypatch.setattr(quota.settings, "free_generations_per_month", 3)
    return 3


class TestPeriods:
    def test_period_is_utc_calendar_month(self):
        # 2026-07-14T12:00:00Z
        assert quota.current_period(1784030400) == "2026-07"

    def test_reset_rolls_to_first_of_next_month(self):
        # Mid-July → 1 Aug 00:00:00 UTC
        assert quota.next_period_start(1784030400) == 1785542400.0

    def test_reset_rolls_over_year_end(self):
        # 2026-12-13T00:00:00Z → 2027-01-01T00:00:00Z
        assert quota.next_period_start(1797120000) == 1798761600.0

    def test_reset_is_utc_not_local(self, monkeypatch):
        """timegm, not mktime — a non-UTC host must not shift the reset."""
        monkeypatch.setattr(time, "timezone", 19800)  # IST
        assert quota.next_period_start(1784030400) == 1785542400.0


class TestPlans:
    def test_new_user_is_free(self, store):
        assert store.get_plan(_user(store)) == "free"

    def test_unknown_user_is_free(self, store):
        assert store.get_plan("nobody") == "free"

    def test_set_plan_grants_pro(self, store):
        uid = _user(store)
        store.set_plan(uid, "pro", time.time() + 3600)
        assert store.get_plan(uid) == "pro"

    def test_expired_pro_reads_as_free(self, store):
        uid = _user(store)
        store.set_plan(uid, "pro", time.time() - 1)
        assert store.get_plan(uid) == "free"

    def test_pro_without_expiry_never_lapses(self, store):
        uid = _user(store)
        store.set_plan(uid, "pro", None)
        assert store.get_plan(uid) == "pro"

    def test_free_plan_is_metered_pro_is_not(self, free_limit):
        assert quota.limit_for("free") == 3
        assert quota.limit_for("pro") is None


class TestConsume:
    def test_each_run_costs_one_credit(self, store, free_limit):
        uid = _user(store)
        quota.consume_quota(uid)
        quota.consume_quota(uid)
        assert quota.get_quota(uid).used == 2
        assert quota.get_quota(uid).remaining == 1

    def test_402_once_the_cap_is_hit(self, store, free_limit):
        uid = _user(store)
        for _ in range(3):
            quota.consume_quota(uid)
        with pytest.raises(HTTPException) as e:
            quota.consume_quota(uid)
        assert e.value.status_code == 402
        assert e.value.detail["reason"] == "quota_exceeded"

    def test_402_carries_the_prices_the_modal_renders(self, store, free_limit):
        uid = _user(store)
        for _ in range(3):
            quota.consume_quota(uid)
        with pytest.raises(HTTPException) as e:
            quota.consume_quota(uid)
        pricing = e.value.detail["pricing"]
        assert pricing["monthly_usd"] == 20
        assert pricing["yearly_usd"] == 100
        assert e.value.detail["resets_at"] > time.time()

    def test_pro_is_never_capped(self, store, free_limit):
        uid = _user(store)
        store.set_plan(uid, "pro", time.time() + 3600)
        for _ in range(25):
            quota.consume_quota(uid)
        q = quota.get_quota(uid)
        assert q.used == 25 and q.limit is None and q.remaining is None


class TestStreamGate:
    """has_quota_remaining backs the /api/stream refusal — the cap on the LLM
    run itself, checked before the model is invoked, consuming nothing."""

    def test_free_user_has_quota_until_cap(self, store, free_limit):
        uid = _user(store)
        ok, state = quota.has_quota_remaining(uid)
        assert ok is True and state.remaining == 3

    def test_exhausted_free_user_is_refused(self, store, free_limit):
        uid = _user(store)
        for _ in range(3):
            quota.consume_quota(uid)
        ok, state = quota.has_quota_remaining(uid)
        assert ok is False and state.remaining == 0

    def test_check_consumes_nothing(self, store, free_limit):
        uid = _user(store)
        for _ in range(5):
            quota.has_quota_remaining(uid)
        assert quota.get_quota(uid).used == 0

    def test_pro_is_always_allowed(self, store, free_limit):
        uid = _user(store)
        store.set_plan(uid, "pro", time.time() + 3600)
        ok, state = quota.has_quota_remaining(uid)
        assert ok is True and state.limit is None

    def test_exceeded_detail_matches_the_402(self, store, free_limit):
        uid = _user(store)
        for _ in range(3):
            quota.consume_quota(uid)
        _, state = quota.has_quota_remaining(uid)
        detail = quota.quota_exceeded_detail(state)
        assert detail["reason"] == "quota_exceeded"
        assert detail["limit"] == 3
        assert detail["pricing"]["yearly_usd"] == 100

    def test_refund_returns_the_credit(self, store, free_limit):
        uid = _user(store)
        lease = quota.consume_quota(uid)
        assert quota.get_quota(uid).used == 1
        lease.refund()
        assert quota.get_quota(uid).used == 0

    def test_refund_is_idempotent(self, store, free_limit):
        uid = _user(store)
        lease = quota.consume_quota(uid)
        lease.refund()
        lease.refund()
        assert quota.get_quota(uid).used == 0

    def test_committed_lease_does_not_refund(self, store, free_limit):
        uid = _user(store)
        lease = quota.consume_quota(uid)
        lease.commit()
        lease.refund()
        assert quota.get_quota(uid).used == 1

    def test_refund_never_goes_negative(self, store, free_limit):
        uid = _user(store)
        store.refund(uid, quota.current_period())
        assert quota.get_quota(uid).used == 0

    def test_last_credit_is_not_double_spent(self, store, free_limit):
        """The check and the increment share a transaction, so two callers
        racing on the final credit can't both win."""
        uid = _user(store)
        for _ in range(2):
            quota.consume_quota(uid)
        # Two "concurrent" attempts against a single remaining credit.
        first, _ = store.try_consume(uid, quota.current_period(), 3)
        second, _ = store.try_consume(uid, quota.current_period(), 3)
        assert first is True
        assert second is False

    def test_quota_is_per_user(self, store, free_limit):
        a, b = _user(store, "usr_a"), _user(store, "usr_b")
        for _ in range(3):
            quota.consume_quota(a)
        with pytest.raises(HTTPException):
            quota.consume_quota(a)
        quota.consume_quota(b)  # b is unaffected
        assert quota.get_quota(b).used == 1

    def test_usage_resets_between_months(self, store, free_limit):
        uid = _user(store)
        store.try_consume(uid, "2026-07", 3)
        store.try_consume(uid, "2026-07", 3)
        assert store.get_usage(uid, "2026-07") == 2
        assert store.get_usage(uid, "2026-08") == 0


class TestMigration:
    def test_adds_plan_columns_to_a_preexisting_users_table(self):
        """A DB created before plans existed must gain the columns, not crash —
        CREATE TABLE IF NOT EXISTS would silently skip the new schema."""
        path = tempfile.mktemp(suffix=".db")
        with sqlite3.connect(path) as c:
            c.execute(
                "CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT UNIQUE, "
                "password_hash TEXT, name TEXT, github_id TEXT, github_login TEXT, "
                "avatar_url TEXT, created_at REAL NOT NULL)"
            )
            c.execute(
                "INSERT INTO users (id, email, created_at) VALUES ('old', 'o@x.com', 1.0)"
            )
        s = JobStore(path)  # runs _migrate
        assert s.get_plan("old") == "free"
        assert s.get_user_by_id("old")["email"] == "o@x.com"
        s.set_plan("old", "pro", None)
        assert s.get_plan("old") == "pro"
