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
            "generations": 0, "tests_written": 0, "scans": 0, "repos_published": 0,
        }

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
