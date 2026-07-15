"""test_admin.py — The admin role: who gets it, who doesn't, and what it can do.

The bar for this file is different from the rest of the suite. Everything here
guards a privilege boundary, so the interesting tests are the *negative* ones —
a bug that grants admin to the wrong account is worse than one that denies it to
the right one, and only the former is silent.
"""

import time
import asyncio
import tempfile

import pytest
from fastapi import HTTPException

from config import Settings
from services import admin, auth
from services.store import JobStore


def _store() -> JobStore:
    return JobStore(tempfile.mktemp(suffix=".db"))


def _user(s: JobStore, uid="usr_1", email="ada@example.com", *, verified=True, **kw):
    s.create_user({
        "id": uid, "email": email, "name": kw.pop("name", None),
        "created_at": kw.pop("created_at", time.time()),
        "email_verified": verified, **kw,
    })
    return s.get_user_by_id(uid)


@pytest.fixture
def settings(monkeypatch):
    """A throwaway Settings with a known allowlist, wired into both modules.

    admin.py and auth.py each import `settings` by value, so patching one object
    isn't enough — both names have to point at this instance.
    """
    s = Settings(admin_emails="ada@example.com, Root@Example.COM")
    monkeypatch.setattr(admin, "settings", s)
    monkeypatch.setattr(auth, "settings", s)
    return s


@pytest.fixture
def store(monkeypatch):
    s = _store()
    monkeypatch.setattr(auth, "store", s)
    return s


class TestAllowlistParsing:
    def test_emails_are_split_and_lowercased(self, settings):
        assert settings.admin_email_list == ["ada@example.com", "root@example.com"]

    def test_blank_list_disables_admin(self):
        s = Settings(admin_emails="")
        assert s.admin_email_list == []
        assert not s.admin_enabled
        assert not s.is_admin_email("anyone@example.com")

    def test_whitespace_and_empty_entries_ignored(self):
        s = Settings(admin_emails=" a@x.com , , b@x.com ,")
        assert s.admin_email_list == ["a@x.com", "b@x.com"]

    def test_match_is_case_insensitive(self, settings):
        """users.email is stored lowercased; .env is typed by a human."""
        assert settings.is_admin_email("ADA@EXAMPLE.COM")
        assert settings.is_admin_email("root@example.com")

    def test_none_and_empty_are_not_admin(self, settings):
        assert not settings.is_admin_email(None)
        assert not settings.is_admin_email("")

    def test_non_listed_email_is_not_admin(self, settings):
        assert not settings.is_admin_email("mallory@example.com")

    def test_substring_of_an_admin_email_is_not_admin(self, settings):
        """Guards against a membership test that ever becomes `in` on a string."""
        assert not settings.is_admin_email("ada@example.com.evil.com")
        assert not settings.is_admin_email("da@example.com")


class TestIsAdmin:
    def test_listed_and_verified_is_admin(self, settings, store):
        assert auth.is_admin(_user(store))

    def test_listed_but_unverified_is_not_admin(self, settings, store):
        """The whole reason is_admin checks email_verified.

        With require_email_verification off, a stranger can sign up as an admin's
        address and be handed a session. The allowlist must not treat that
        unproven claim on the address as the grant.
        """
        assert not auth.is_admin(_user(store, verified=False))

    def test_unlisted_email_is_not_admin(self, settings, store):
        assert not auth.is_admin(_user(store, uid="usr_2", email="bob@example.com"))

    def test_user_with_no_email_is_not_admin(self, settings, store):
        """A GitHub account with no public email — nothing to match on."""
        assert not auth.is_admin(_user(store, uid="usr_3", email=None))

    def test_public_user_exposes_the_flag(self, settings, store):
        assert auth.public_user(_user(store))["is_admin"] is True
        assert auth.public_user(
            _user(store, uid="usr_2", email="bob@example.com")
        )["is_admin"] is False


class TestRequireAdmin:
    def _ctx(self, user):
        return {"user": auth.public_user(user), "user_id": user["id"]}

    def test_admin_passes_through(self, settings, store):
        ctx = self._ctx(_user(store))
        assert asyncio.run(admin.require_admin(ctx)) is ctx

    def test_non_admin_is_rejected(self, settings, store):
        ctx = self._ctx(_user(store, uid="usr_2", email="bob@example.com"))
        with pytest.raises(HTTPException) as e:
            asyncio.run(admin.require_admin(ctx))
        assert e.value.status_code == 404

    def test_rejection_is_404_not_403(self, settings, store):
        """404 by design: a 403 would confirm the console exists to someone who
        has a session but no role. Locking this in — a well-meaning 'clearer'
        error code here is an information leak."""
        ctx = self._ctx(_user(store, uid="usr_2", email="bob@example.com"))
        with pytest.raises(HTTPException) as e:
            asyncio.run(admin.require_admin(ctx))
        assert e.value.status_code == 404
        assert "admin" not in str(e.value.detail).lower()

    def test_unverified_admin_email_is_rejected(self, settings, store):
        ctx = self._ctx(_user(store, verified=False))
        with pytest.raises(HTTPException) as e:
            asyncio.run(admin.require_admin(ctx))
        assert e.value.status_code == 404

    def test_empty_allowlist_rejects_everyone(self, store, monkeypatch):
        s = Settings(admin_emails="")
        monkeypatch.setattr(admin, "settings", s)
        monkeypatch.setattr(auth, "settings", s)
        ctx = self._ctx(_user(store))
        with pytest.raises(HTTPException):
            asyncio.run(admin.require_admin(ctx))


class TestSuspension:
    def test_new_user_is_not_suspended(self, store):
        assert _user(store)["suspended"] is False

    def test_suspend_sets_flag_reason_and_time(self, store):
        _user(store)
        store.set_suspended("usr_1", True, reason="spam")
        row = store.get_user_by_id("usr_1")
        assert row["suspended"] is True
        assert row["suspended_reason"] == "spam"
        assert row["suspended_at"] == pytest.approx(time.time(), abs=5)

    def test_unsuspend_clears_flag_and_timestamp(self, store):
        _user(store)
        store.set_suspended("usr_1", True, reason="spam")
        store.set_suspended("usr_1", False)
        row = store.get_user_by_id("usr_1")
        assert row["suspended"] is False
        assert row["suspended_at"] is None

    def test_long_reason_is_truncated(self, store):
        _user(store)
        store.set_suspended("usr_1", True, reason="x" * 900)
        assert len(store.get_user_by_id("usr_1")["suspended_reason"]) == 500


class TestSuspendedUserIsLockedOut:
    """require_user must reject a suspended account on every request.

    Checking only at login would leave anyone already holding a token working
    until it expired — which is exactly the population worth suspending.
    """

    class _Req:
        def __init__(self, token):
            self.headers = {"Authorization": f"Bearer {token}"}
            self.query_params = {}

    def _session(self, store, user_id="usr_1"):
        store.create_session("tok", {"user_id": user_id}, 3600)
        return "tok"

    def test_active_user_passes(self, settings, store):
        _user(store)
        ctx = asyncio.run(auth.require_user(self._Req(self._session(store))))
        assert ctx["user_id"] == "usr_1"

    def test_suspended_user_is_rejected_mid_session(self, settings, store):
        _user(store)
        token = self._session(store)
        asyncio.run(auth.require_user(self._Req(token)))     # works before
        store.set_suspended("usr_1", True, reason="abuse")
        with pytest.raises(HTTPException) as e:
            asyncio.run(auth.require_user(self._Req(token)))
        assert e.value.status_code == 403

    def test_rejection_is_403_not_401(self, settings, store):
        """401 would send the client to /login, where a correct password would
        mint a session the next request rejects again. The account is locked,
        not logged out — the status has to say so."""
        _user(store)
        token = self._session(store)
        store.set_suspended("usr_1", True)
        with pytest.raises(HTTPException) as e:
            asyncio.run(auth.require_user(self._Req(token)))
        assert e.value.status_code == 403
        assert "suspend" in str(e.value.detail).lower()

    def test_unsuspending_restores_access(self, settings, store):
        _user(store)
        token = self._session(store)
        store.set_suspended("usr_1", True)
        store.set_suspended("usr_1", False)
        assert asyncio.run(auth.require_user(self._Req(token)))["user_id"] == "usr_1"

    def test_login_refuses_a_suspended_account(self, settings, monkeypatch):
        """Refused at the door too, not only on the next request.

        require_user already makes the token useless, so this is about honesty:
        a login that "succeeds" and then 403s on the very next page reads as a
        broken app rather than as the lock it is.
        """
        import main
        with pytest.raises(HTTPException) as e:
            asyncio.run(main._issue_login({
                "id": "usr_1", "email": "ada@example.com",
                "email_verified": True, "suspended": True,
            }))
        assert e.value.status_code == 403
        assert "suspend" in str(e.value.detail).lower()


class TestUserListing:
    def _seed(self, s):
        _user(s, "usr_1", "ada@example.com", name="Ada", created_at=100)
        _user(s, "usr_2", "bob@example.com", name="Bob", created_at=200)
        _user(s, "usr_3", "carol@test.org", name="Carol", created_at=300)
        return s

    def test_lists_newest_first(self, store):
        users = self._seed(store).list_users()
        assert [u["id"] for u in users] == ["usr_3", "usr_2", "usr_1"]

    def test_search_by_email(self, store):
        users = self._seed(store).list_users(query="example.com")
        assert {u["id"] for u in users} == {"usr_1", "usr_2"}

    def test_search_by_name_is_case_insensitive(self, store):
        assert [u["id"] for u in self._seed(store).list_users(query="ada")] == ["usr_1"]

    def test_search_by_github_login(self, store):
        _user(store, "usr_9", "z@x.com", github_login="octocat")
        assert [u["id"] for u in store.list_users(query="octocat")] == ["usr_9"]

    def test_paging(self, store):
        self._seed(store)
        assert [u["id"] for u in store.list_users(limit=2)] == ["usr_3", "usr_2"]
        assert [u["id"] for u in store.list_users(limit=2, offset=2)] == ["usr_1"]

    def test_count_matches_query_not_page(self, store):
        self._seed(store)
        assert store.count_users() == 3
        assert store.count_users(query="example.com") == 2

    def test_wildcards_in_query_are_literal(self, store):
        """A search for "%" must not match every row — LIKE metacharacters in
        user input are text, not syntax."""
        self._seed(store)
        assert store.list_users(query="%") == []
        assert store.list_users(query="_") == []

    def test_underscore_matches_itself(self, store):
        _user(store, "usr_u", "a_b@x.com")
        assert [u["id"] for u in store.list_users(query="a_b")] == ["usr_u"]


class TestPlatformAnalytics:
    def test_totals_on_empty_instance(self, store):
        t = store.platform_totals()
        assert t["users"] == 0 and t["generations"] == 0 and t["users_pro"] == 0

    def test_counts_users_and_generations(self, store):
        _user(store, "usr_1")
        _user(store, "usr_2", "b@x.com")
        store.record_generation("usr_1", source="repo", framework="playwright",
                                language="typescript", test_count=3, file_count=2)
        t = store.platform_totals()
        assert t["users"] == 2
        assert t["generations"] == 1
        assert t["generations_24h"] == 1

    def test_lapsed_pro_is_not_counted_as_paying(self, store):
        """Mirrors get_plan: an expired subscription is a free user, and counting
        it as Pro would overstate the paying base on the overview."""
        _user(store, "usr_1")
        store.set_plan("usr_1", "pro", expires_at=time.time() - 60)
        assert store.platform_totals()["users_pro"] == 0

    def test_live_pro_is_counted(self, store):
        _user(store, "usr_1")
        store.set_plan("usr_1", "pro", expires_at=time.time() + 86_400)
        assert store.platform_totals()["users_pro"] == 1

    def test_never_expiring_pro_is_counted(self, store):
        _user(store, "usr_1")
        store.set_plan("usr_1", "pro", expires_at=None)
        assert store.platform_totals()["users_pro"] == 1

    def test_counts_suspended(self, store):
        _user(store, "usr_1")
        store.set_suspended("usr_1", True)
        assert store.platform_totals()["users_suspended"] == 1

    def test_breakdown_groups_and_orders(self, store):
        _user(store, "usr_1")
        for fw in ("playwright", "playwright", "cypress"):
            store.record_generation("usr_1", source="r", framework=fw,
                                    language="ts", test_count=1, file_count=1)
        assert store.platform_breakdown("framework") == [
            {"name": "playwright", "count": 2},
            {"name": "cypress", "count": 1},
        ]

    def test_breakdown_rejects_arbitrary_columns(self, store):
        """The column is interpolated into SQL, so the allowlist is load-bearing."""
        with pytest.raises(ValueError):
            store.platform_breakdown("id; DROP TABLE users")

    def test_series_is_zero_filled(self, store):
        series = store.platform_series(days=7)
        assert len(series) == 7
        assert all(row["generate"] == 0 and row["signups"] == 0 for row in series)

    def test_series_counts_all_users_not_one(self, store):
        _user(store, "usr_1")
        _user(store, "usr_2", "b@x.com")
        store.record_generation("usr_1", source="r", framework="p", language="ts",
                                test_count=1, file_count=1)
        store.record_generation("usr_2", source="r", framework="p", language="ts",
                                test_count=1, file_count=1)
        assert sum(row["generate"] for row in store.platform_series(days=2)) == 2

    def test_recent_failures_excludes_successes(self, store):
        _user(store, "usr_1")
        store.record_generation("usr_1", source="r", framework="p", language="ts",
                                test_count=1, file_count=1, status="success")
        store.record_generation("usr_1", source="r", framework="p", language="ts",
                                test_count=0, file_count=0, status="error",
                                error="boom")
        failures = store.recent_failures()
        assert len(failures) == 1
        assert failures[0]["error"] == "boom"
        assert failures[0]["email"] == "ada@example.com"


class TestAdminUserView:
    """The row the console lists an account by.

    Worth its own tests because it reads two dicts with confusingly similar
    keys: activity_totals says "generations"/"scans", while ACTIVITY_TABLES and
    activity_series say "generate"/"scan". Getting it wrong doesn't raise — it
    silently renders 0 for everybody, which is exactly what shipped the first
    time and is only visible by looking at the page.
    """

    def _view(self, store, monkeypatch, user_id="usr_1"):
        import main
        from services import quota
        monkeypatch.setattr(main, "store", store)
        monkeypatch.setattr(quota, "store", store)
        return main._admin_user_view(store.get_user_by_id(user_id))

    def test_counts_are_not_silently_zero(self, settings, store, monkeypatch):
        _user(store)
        for _ in range(3):
            store.record_generation("usr_1", source="r", framework="playwright_js",
                                    language="typescript", test_count=4, file_count=2)
        store.record_scan("usr_1", url="https://x.com", grade="A", score=90, findings=1)

        view = self._view(store, monkeypatch)
        assert view.generations == 3
        assert view.scans == 1

    def test_reports_effective_plan_not_stored_plan(self, settings, store, monkeypatch):
        _user(store)
        store.set_plan("usr_1", "pro", expires_at=time.time() - 60)
        assert self._view(store, monkeypatch).plan == "free"

    def test_flags_admins(self, settings, store, monkeypatch):
        _user(store)
        assert self._view(store, monkeypatch).is_admin is True

    def test_reports_usage_for_the_current_period(self, settings, store, monkeypatch):
        from services.quota import current_period
        _user(store)
        store.set_usage("usr_1", current_period(), 4)
        assert self._view(store, monkeypatch).usage_this_period == 4


class TestUsageOverride:
    def test_set_usage_overwrites_counter(self, store):
        _user(store)
        store.try_consume("usr_1", "2026-07", limit=5)
        store.try_consume("usr_1", "2026-07", limit=5)
        assert store.get_usage("usr_1", "2026-07") == 2
        store.set_usage("usr_1", "2026-07", 0)
        assert store.get_usage("usr_1", "2026-07") == 0

    def test_set_usage_creates_a_missing_row(self, store):
        _user(store)
        store.set_usage("usr_1", "2026-07", 4)
        assert store.get_usage("usr_1", "2026-07") == 4

    def test_negative_usage_is_clamped(self, store):
        _user(store)
        assert store.set_usage("usr_1", "2026-07", -5) == 0


class TestSessionCounting:
    def test_counts_only_this_users_live_sessions(self, store):
        _user(store, "usr_1")
        _user(store, "usr_2", "b@x.com")
        store.create_session("a", {"user_id": "usr_1"}, 3600)
        store.create_session("b", {"user_id": "usr_1"}, 3600)
        store.create_session("c", {"user_id": "usr_2"}, 3600)
        assert store.count_sessions_for_user("usr_1") == 2

    def test_expired_sessions_are_not_counted(self, store):
        _user(store)
        store.create_session("a", {"user_id": "usr_1"}, -1)
        assert store.count_sessions_for_user("usr_1") == 0
