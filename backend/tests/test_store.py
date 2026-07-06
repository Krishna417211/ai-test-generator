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
