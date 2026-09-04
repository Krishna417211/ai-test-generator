"""
test_generate_flow.py — /api/stream followed by /api/generate, end to end.

These two endpoints are one user action split in half, and the seam between them
is where the expensive mistake lived: the stream asked the model for the whole
suite as a single JSON blob so the browser had something to render, that blob
came back truncated at the provider's output-token cap, and /api/generate threw
it away and generated the same suite again file-by-file. Every generation paid
for a large call whose output was never used.

The seam is only testable from here — each endpoint on its own looks correct.
"""

import json

import pytest
from fastapi.testclient import TestClient

import main
from services.llm_router import Tier

SPEC = (
    "import { test, expect } from '@playwright/test';\n"
    "test('logs in', async ({ page }) => {\n"
    "  await page.goto('login.html');\n"
    "  await expect(page.locator('#email')).toBeVisible();\n"
    "});\n"
)


class _Lease:
    """Stands in for a quota lease, recording which way it was settled."""

    def __init__(self):
        self.refunded = False
        self.committed = False

    def refund(self):
        self.refunded = True

    def commit(self):
        self.committed = True


@pytest.fixture
def calls(monkeypatch):
    """Record every provider call the flow makes, by context."""
    seen: list[str] = []

    async def fake_complete(**kwargs):
        seen.append(kwargs["context_hint"])
        if kwargs["context_hint"] == "writer_plan":
            return json.dumps({"files": [
                {"filename": "tests/login.spec.ts",
                 "description": "Login flow", "kind": "spec"},
                {"filename": "tests/pages/LoginPage.ts",
                 "description": "Login page object", "kind": "page_object"},
            ]})
        return SPEC

    import agents.writer_agent as wa
    monkeypatch.setattr(wa.router, "complete", fake_complete)
    return seen


@pytest.fixture
def lease():
    return _Lease()


@pytest.fixture
def client(monkeypatch, lease, tmp_path):
    from services.auth import require_user
    from services.quota import require_quota

    main.app.dependency_overrides[require_user] = lambda: {"user_id": "usr_test"}
    main.app.dependency_overrides[require_quota] = lambda: {
        "user_id": "usr_test", "quota_lease": lease,
    }
    monkeypatch.setattr(main, "tier_for_user", lambda uid: Tier.FREE)
    monkeypatch.setattr(main, "has_quota_remaining", lambda uid: (True, None))
    monkeypatch.setattr(main, "_record_generation", lambda *a, **k: None)

    # A real JobStore on a temp DB: session round-tripping through JSON is part
    # of what this is testing (the streamed suite has to survive it intact).
    from services.store import JobStore
    store = JobStore(db_path=str(tmp_path / "jobs.db"))
    monkeypatch.setattr(main, "store", store)

    yield TestClient(main.app), store
    main.app.dependency_overrides.clear()


def _seed_job(store):
    from agents.filter_agent import FilterResult
    fr = FilterResult(
        "A login page.", [], [], ["/login"], "static html", [],
        {"login.html": "<form><input id='email'></form>"}, 0, 1,
    )
    store.create("job1", {
        "user_id": "usr_test", "files": {}, "filter_result": fr,
        "request": {"test_flows": "log in"},
    })
    return "job1"


def _stream(client, job_id):
    resp = client.get(f"/api/stream/{job_id}", params={
        "framework": "playwright", "language": "typescript",
        "test_flows": "log in", "base_url": "https://app.example",
    })
    assert resp.status_code == 200, resp.text
    events = []
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def _generate(client, job_id):
    return client.post(f"/api/generate/{job_id}", json={
        "framework": "playwright", "language": "typescript",
        "test_flows": "log in", "base_url": "https://app.example",
        "include_ci": True,
    })


def test_the_suite_is_generated_once_across_both_requests(client, calls):
    c, store = client
    job = _seed_job(store)

    _stream(c, job)
    after_stream = list(calls)
    assert after_stream.count("writer_plan") == 1
    assert after_stream.count("writer_file") == 2

    resp = _generate(c, job)
    assert resp.status_code == 200, resp.text

    assert calls == after_stream, (
        "finalising re-generated a suite the stream had already written "
        f"(extra calls: {calls[len(after_stream):]})"
    )


def test_the_finalised_suite_contains_the_streamed_files(client, calls):
    c, store = client
    job = _seed_job(store)
    _stream(c, job)

    body = _generate(c, job).json()
    names = [f["filename"] for f in body["files"]]

    assert any(n.endswith("tests/login.spec.ts") for n in names), names
    assert any(n.endswith("tests/pages/LoginPage.ts") for n in names), names
    # Scaffolding the model never wrote, added on finalise.
    assert any(n.endswith("playwright.config.ts") for n in names), names
    assert body["test_count"] >= 1
    assert not body["failed_files"]


def test_the_stream_reports_progress_rather_than_going_quiet(client, calls):
    """A run with nothing to show looks hung, and users reload into a second one."""
    c, store = client
    events = _stream(c, _seed_job(store))

    statuses = [e["message"] for e in events if e["type"] == "provider_status"]
    assert any("Planning" in s for s in statuses), statuses
    assert any("tests/login.spec.ts" in s for s in statuses), statuses
    assert any(e["type"] == "chunk" for e in events)
    assert events[-1]["type"] == "done"


def test_the_streamed_suite_survives_the_session_round_trip(client, calls):
    c, store = client
    job = _seed_job(store)
    _stream(c, job)

    session = store.get(job)
    parsed = json.loads(session["streamed_raw"])
    assert [f["filename"] for f in parsed["files"]] == [
        "tests/login.spec.ts", "tests/pages/LoginPage.ts",
    ]
    assert session["streamed_failed"] == []
    # Present, but None here: provenance is recorded by the real router, which
    # this test replaces. What matters at this seam is that the key travels —
    # /api/generate makes no calls of its own now, so if the stream doesn't
    # store who wrote the suite, the results screen has no honest answer.
    assert "streamed_provenance" in session


def test_a_run_on_the_operators_keys_still_costs_a_credit(client, calls, lease):
    """No user key saved, so the operator paid — the credit must be charged."""
    c, store = client
    job = _seed_job(store)
    _stream(c, job)
    _generate(c, job)

    assert lease.committed and not lease.refunded


def test_a_stream_that_produced_nothing_still_yields_a_suite(client, calls):
    """The reuse path is an optimisation, not a dependency.

    If the stream stored nothing usable — it died mid-run, or an older job row
    predates the change — finalising must still generate rather than hand back an
    empty archive. This is also the control for the test above: with the reuse
    broken, generation demonstrably happens twice, which is exactly what that
    test asserts must not happen when reuse works.
    """
    c, store = client
    job = _seed_job(store)
    _stream(c, job)
    after_stream = list(calls)

    store.update(job, streamed_raw="")          # simulate a stream that stored nothing

    body = _generate(c, job).json()
    regenerated = calls[len(after_stream):]

    assert "writer_plan" in regenerated, "finalising did not fall back to generating"
    assert any(f["filename"].endswith("login.spec.ts") for f in body["files"])
