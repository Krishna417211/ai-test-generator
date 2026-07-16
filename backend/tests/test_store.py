"""test_store.py — Unit tests for the SQLite-backed JobStore."""

import tempfile

from services.store import JobStore
from agents.filter_agent import FilterResult


def _fr() -> FilterResult:
    return FilterResult(
        project_summary="s", key_pages=[], key_components=[], routes=["/"],
        framework="Django", testing_challenges=[], files={"a": "b"},
        total_tokens=1, file_count=1,
    )


def _store() -> JobStore:
    return JobStore(tempfile.mktemp(suffix=".db"))


class TestJobStore:
    def test_create_and_get_rebuilds_filterresult(self):
        s = _store()
        s.create("j", {"filter_result": _fr(), "request": {}})
        got = s.get("j")
        assert isinstance(got["filter_result"], FilterResult)
        assert got["filter_result"].framework == "Django"
        assert got["filter_result"].file_count == 1

    def test_missing_returns_none(self):
        assert _store().get("nope") is None

    def test_update_patches_session(self):
        s = _store()
        s.create("j", {"filter_result": _fr()})
        s.update("j", streamed_raw="hello")
        assert s.get("j")["streamed_raw"] == "hello"

    def test_survives_restart(self):
        path = tempfile.mktemp(suffix=".db")
        JobStore(path).create("j", {"filter_result": _fr()})
        # A fresh instance (simulating a server restart) still sees the job.
        assert JobStore(path).get("j")["filter_result"].file_count == 1

    def test_count(self):
        s = _store()
        s.create("a", {"x": 1})
        s.create("b", {"x": 2})
        assert s.count() == 2

    def test_prune_removes_all_when_ttl_negative(self):
        s = _store()
        s.create("a", {"x": 1})
        removed = s.prune(max_age_seconds=-1)
        assert removed >= 1
        assert s.get("a") is None


# ─────────────────────────────────────────────
# Activity history (profile)
# ─────────────────────────────────────────────

class TestActivityHistory:
    """Generations and scans back the profile page. `usage` can't: it's a bare
    per-month counter. `jobs` can't either: it's pruned at 24h."""

    def test_generation_roundtrip(self):
        s = _store()
        s.record_generation("u1", "https://github.com/a/b", "playwright_js", "typescript", 9, 8)
        rows = s.list_generations("u1")
        assert len(rows) == 1
        assert rows[0]["source"] == "https://github.com/a/b"
        assert rows[0]["framework"] == "playwright_js"
        assert rows[0]["test_count"] == 9
        assert rows[0]["file_count"] == 8

    def test_scan_roundtrip(self):
        s = _store()
        s.record_scan("u1", "https://nurons.me/home", "D", 52, 6)
        rows = s.list_scans("u1")
        assert len(rows) == 1
        assert rows[0]["url"] == "https://nurons.me/home"
        assert rows[0]["grade"] == "D"
        assert rows[0]["score"] == 52
        assert rows[0]["findings"] == 6

    def test_history_is_scoped_to_its_owner(self):
        """The profile endpoint keys every read by the session's user id. If the
        store leaked across users, that endpoint would hand one account another
        account's repos and scanned URLs."""
        s = _store()
        s.record_generation("alice", "alice/repo", "playwright_js", "typescript", 1, 1)
        s.record_scan("alice", "https://alice.example", "A", 95, 0)
        s.record_generation("bob", "bob/repo", "cypress_js", "javascript", 2, 2)

        assert [g["source"] for g in s.list_generations("alice")] == ["alice/repo"]
        assert [g["source"] for g in s.list_generations("bob")] == ["bob/repo"]
        assert s.list_scans("bob") == []
        assert s.activity_totals("bob")["scans"] == 0
        assert s.activity_totals("alice")["generations"] == 1

    def test_newest_first(self):
        s = _store()
        for i in range(3):
            s.record_generation("u1", f"repo-{i}", "playwright_js", "typescript", 1, 1)
        assert [g["source"] for g in s.list_generations("u1")] == ["repo-2", "repo-1", "repo-0"]

    def test_list_is_capped_but_totals_are_not(self):
        """The headline count must not silently freeze at the display limit."""
        s = _store()
        for i in range(25):
            s.record_generation("u1", f"r{i}", "playwright_js", "typescript", 2, 1)
        assert len(s.list_generations("u1", limit=10)) == 10
        assert s.activity_totals("u1")["generations"] == 25
        assert s.activity_totals("u1")["tests_written"] == 50

    def test_totals_on_a_user_with_no_history(self):
        s = _store()
        assert s.activity_totals("nobody") == {
            "generations": 0, "tests_written": 0, "generations_failed": 0,
            "scans": 0, "repos_published": 0,
        }

    def test_failed_generations_are_counted_but_not_double_counted(self):
        """A failed run is still a generation that happened: it belongs in the
        history and in `generations`, but it wrote no tests."""
        s = _store()
        s.record_generation("u1", "r/ok", "playwright_js", "typescript", 5, 2)
        s.record_generation("u1", "r/bad", "playwright_js", "typescript", 0, 0,
                            status="failed", error="providers exhausted")
        totals = s.activity_totals("u1")
        assert totals["generations"] == 2          # both attempts
        assert totals["generations_failed"] == 1   # a subset, not an extra
        assert totals["tests_written"] == 5        # the failure contributed none

    def test_history_survives_the_job_prune(self):
        """Regression: jobs are pruned at 24h. If history lived in `jobs`, a
        profile would go blank every day."""
        s = _store()
        s.create("job-1", {"filter_result": _fr(), "request": {}})
        s.record_generation("u1", "kept/forever", "playwright_js", "typescript", 3, 2)
        s.record_scan("u1", "https://kept.example", "B", 80, 1)

        s.prune(max_age_seconds=-1)  # expire everything prunable

        assert s.get("job-1") is None, "job should have been pruned"
        assert len(s.list_generations("u1")) == 1, "generation history must outlive jobs"
        assert len(s.list_scans("u1")) == 1, "scan history must outlive jobs"

    def test_totals_count_published_repos(self):
        s = _store()
        s.record_published_repo("me/one", "u1", "https://github.com/me/one")
        s.record_published_repo("me/two", "u1", "https://github.com/me/two")
        s.record_published_repo("other/x", "u2", "https://github.com/other/x")
        assert s.activity_totals("u1")["repos_published"] == 2
        assert s.activity_totals("u2")["repos_published"] == 1


class TestActivityFeed:
    """The dashboard's unified timeline across the three activity tables."""

    def test_feed_merges_all_three_features_newest_first(self):
        s = _store()
        s.record_generation("u1", "r/gen", "playwright_js", "typescript", 4, 1)
        s.record_scan("u1", "https://scanned.example", "A", 95, 0)
        s.record_published_repo("me/pub", "u1", "https://github.com/me/pub",
                                test_count=3, files_pushed=20)
        feed = s.activity_feed("u1")
        assert len(feed) == 3
        assert {i["kind"] for i in feed} == {"generate", "scan", "publish"}
        stamps = [i["created_at"] for i in feed]
        assert stamps == sorted(stamps, reverse=True), "feed must be newest-first"

    def test_feed_shape_depends_on_kind(self):
        s = _store()
        s.record_generation("u1", "r/gen", "playwright_js", "typescript", 4, 1)
        s.record_scan("u1", "https://scanned.example", "A", 95, 2)
        by_kind = {i["kind"]: i for i in s.activity_feed("u1")}
        assert by_kind["generate"]["source"] == "r/gen"
        assert by_kind["scan"]["grade"] == "A"
        assert by_kind["scan"]["findings"] == 2

    def test_feed_never_leaks_another_users_activity(self):
        s = _store()
        s.record_generation("u1", "mine", "playwright_js", "typescript", 1, 1)
        s.record_generation("u2", "theirs", "playwright_js", "typescript", 1, 1)
        assert [i["source"] for i in s.activity_feed("u1")] == ["mine"]

    def test_feed_is_capped(self):
        s = _store()
        for i in range(12):
            s.record_generation("u1", f"r{i}", "playwright_js", "typescript", 1, 1)
        assert len(s.activity_feed("u1", limit=5)) == 5

    def test_series_is_zero_filled_across_the_window(self):
        """A quiet day must be a 0, not a missing row — the chart draws a
        continuous axis off this."""
        s = _store()
        s.record_generation("u1", "r/today", "playwright_js", "typescript", 2, 1)
        series = s.activity_series("u1", days=14)
        assert len(series) == 14
        assert [d["date"] for d in series] == sorted(d["date"] for d in series)
        assert sum(d["generate"] for d in series) == 1
        assert all(d["publish"] == 0 and d["scan"] == 0 for d in series)


class TestUserSettings:
    def test_unset_settings_fall_back_to_defaults(self):
        s = _store()
        assert s.get_settings("nobody") == JobStore.DEFAULT_SETTINGS

    def test_partial_save_leaves_other_fields_at_their_default(self):
        s = _store()
        got = s.save_settings("u1", framework="cypress")
        assert got["framework"] == "cypress"
        assert got["language"] == JobStore.DEFAULT_SETTINGS["language"]

    def test_save_upserts_rather_than_duplicating(self):
        s = _store()
        s.save_settings("u1", framework="cypress")
        s.save_settings("u1", framework="selenium", base_url="https://x.example")
        assert s.get_settings("u1")["framework"] == "selenium"
        assert s.get_settings("u1")["base_url"] == "https://x.example"

    def test_unknown_keys_are_ignored(self):
        s = _store()
        s.save_settings("u1", framework="cypress", plan="pro")   # plan is not settable here
        assert "plan" not in s.get_settings("u1")


class TestAccountDeletion:
    def test_delete_account_removes_the_user_and_their_activity(self):
        s = _store()
        s.create_user({"id": "u1", "email": "a@b.c", "created_at": 0})
        s.record_generation("u1", "r", "playwright_js", "typescript", 1, 1)
        s.record_scan("u1", "https://x.example", "A", 90, 0)
        s.record_published_repo("me/r", "u1", "https://github.com/me/r")
        s.save_settings("u1", framework="cypress")

        s.delete_account("u1")

        assert s.get_user_by_id("u1") is None
        assert s.activity_feed("u1") == []
        assert s.activity_totals("u1")["generations"] == 0

    def test_delete_account_leaves_other_users_alone(self):
        s = _store()
        s.create_user({"id": "u1", "email": "a@b.c", "created_at": 0})
        s.create_user({"id": "u2", "email": "d@e.f", "created_at": 0})
        s.record_generation("u2", "theirs", "playwright_js", "typescript", 1, 1)
        s.delete_account("u1")
        assert s.get_user_by_id("u2") is not None
        assert len(s.activity_feed("u2")) == 1


class TestUserSorting:
    """list_users' ORDER BY, which is string-concatenated and so must be
    allowlisted — the injection case below is the reason this class exists."""

    def _seed(self) -> JobStore:
        import time
        s = _store()
        now = time.time()
        rows = [
            ("usr_c", "carol@x.com", "Carol", False),
            ("usr_a", "alice@x.com", "Alice", True),
            ("usr_b", "bob@x.com", "Bob", False),
        ]
        for i, (uid, email, name, susp) in enumerate(rows):
            s.create_user({"id": uid, "email": email, "name": name,
                           "created_at": now + i, "email_verified": True,
                           "suspended": susp})
        return s

    def test_defaults_to_newest_first(self):
        s = self._seed()
        assert [u["email"] for u in s.list_users()] == [
            "bob@x.com", "alice@x.com", "carol@x.com"]

    def test_sorts_by_email_both_ways(self):
        s = self._seed()
        assert [u["email"] for u in s.list_users(sort="email", direction="asc")] == [
            "alice@x.com", "bob@x.com", "carol@x.com"]
        assert [u["email"] for u in s.list_users(sort="email", direction="desc")] == [
            "carol@x.com", "bob@x.com", "alice@x.com"]

    def test_suspended_desc_surfaces_locked_accounts(self):
        s = self._seed()
        first = s.list_users(sort="suspended", direction="desc")[0]
        assert first["email"] == "alice@x.com"

    def test_unknown_sort_key_falls_back_and_does_not_execute(self):
        """An unlisted column is ignored, not interpolated. If the key reached
        SQL this would drop the table (or raise); it must do neither."""
        s = self._seed()
        rows = s.list_users(sort="created_at; DROP TABLE users --", direction="asc")
        assert [u["email"] for u in rows] == ["carol@x.com", "alice@x.com", "bob@x.com"]
        assert s.count_users() == 3

    def test_evil_direction_is_not_interpolated(self):
        s = self._seed()
        rows = s.list_users(sort="email", direction="asc; DROP TABLE users --")
        # Anything that isn't exactly "asc" means DESC; the table survives.
        assert [u["email"] for u in rows][0] == "carol@x.com"
        assert s.count_users() == 3

    def test_ties_break_on_id_so_paging_is_stable(self):
        """Every row here has the same plan. Without the id tiebreaker the two
        pages could overlap or skip, which is invisible until it bites."""
        s = self._seed()
        page1 = s.list_users(sort="plan", direction="desc", limit=2, offset=0)
        page2 = s.list_users(sort="plan", direction="desc", limit=2, offset=2)
        seen = [u["id"] for u in page1] + [u["id"] for u in page2]
        assert len(seen) == len(set(seen)) == 3


class TestPlatformTrends:
    def test_previous_windows_exclude_the_current_one(self):
        """The *_prev counters must not double-count rows already in the live
        window — the boundary is half-open."""
        import time
        s = _store()
        now = time.time()
        # 2 signups inside the last 7d, 1 in the 7d before that, 1 ancient.
        for i, age_days in enumerate([1, 3, 10, 90]):
            s.create_user({"id": f"u{i}", "email": f"u{i}@x.com",
                           "created_at": now - age_days * 86_400,
                           "email_verified": True})
        t = s.platform_totals()
        assert t["users"] == 4
        assert t["users_new_7d"] == 2
        assert t["users_new_7d_prev"] == 1
