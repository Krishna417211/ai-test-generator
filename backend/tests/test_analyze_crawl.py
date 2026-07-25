"""
test_analyze_crawl.py — the crawl-first generate entrypoint (/api/analyze-crawl).

Pins the guarantees that define the flow: GitHub is hard-required, a hosted URL
is required, a crawl that isn't usable stops the run with an honest status (no
repo-file fallback), and a good crawl produces a job whose session carries the
live anchors with NO source files. The crawler and GitHub I/O are stubbed so the
route's own logic is what's under test — the crawl itself is covered by
test_live_crawler.py.
"""

import pytest
from fastapi.testclient import TestClient

import main
from services.llm_router import Tier
from services import grounding
from services import live_crawler as lc


def _index(n: int) -> grounding.GroundIndex:
    """A GroundIndex with n distinct anchors (>= MIN_USEFUL is 'useful')."""
    idx = grounding.GroundIndex(source="dom")
    idx.anchors = {grounding.Anchor(grounding.KIND_ID, f"el{i}") for i in range(n)}
    return idx


def _crawl_result(n_anchors: int):
    idx = _index(n_anchors)
    pages = [lc.CrawledPage(url="https://app.example/login", path="/login",
                            n_anchors=n_anchors)]
    return lc.CrawlResult(seed="https://app.example", index=idx, pages=pages,
                          unvisited=[])


class _FakeGH:
    def __init__(self, *a, **k):
        pass

    async def get_file_tree(self, info):
        return [{"path": "package.json"}]

    async def fetch_files(self, info, paths, concurrency=8):
        class F:
            def __init__(s, p, c):
                s.path, s.content = p, c
        return [F("package.json", '{"dependencies":{"react":"18"}}')]


@pytest.fixture
def client(monkeypatch):
    from services.auth import require_user
    # Connected GitHub session by default; individual tests override to drop it.
    main.app.dependency_overrides[require_user] = lambda: {
        "user_id": "usr_test", "session": {"github_token": "gho_x"}}
    monkeypatch.setattr(main, "tier_for_user", lambda uid: Tier.FREE)
    monkeypatch.setattr(main, "GitHubService", _FakeGH)
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def _payload(**kw):
    base = {"repo_url": "https://github.com/owner/repo",
            "live_url": "https://app.example"}
    base.update(kw)
    return base


def test_requires_github_connection(client, monkeypatch):
    from services.auth import require_user
    main.app.dependency_overrides[require_user] = lambda: {
        "user_id": "usr_test", "session": {}}          # not connected
    r = client.post("/api/analyze-crawl", json=_payload())
    assert r.status_code == 403
    assert "GitHub" in r.json()["detail"]


def test_requires_hosted_url(client):
    r = client.post("/api/analyze-crawl", json={"repo_url": "https://github.com/o/r"})
    assert r.status_code == 400
    assert "hosted" in r.json()["detail"].lower()


def test_thin_crawl_stops_with_422(client, monkeypatch):
    async def fake_crawl(url, **k):
        return _crawl_result(2)                        # below MIN_USEFUL
    monkeypatch.setattr(main.crawler_svc, "crawl", fake_crawl)
    r = client.post("/api/analyze-crawl", json=_payload())
    assert r.status_code == 422
    assert "too few elements" in r.json()["detail"]


def test_blocked_target_stops_with_400(client, monkeypatch):
    async def fake_crawl(url, **k):
        raise main.ScanError("Refusing to scan a private address")
    monkeypatch.setattr(main.crawler_svc, "crawl", fake_crawl)
    r = client.post("/api/analyze-crawl", json=_payload())
    assert r.status_code == 400
    assert "can't be crawled" in r.json()["detail"]


def test_browser_unavailable_stops_with_503(client, monkeypatch):
    async def fake_crawl(url, **k):
        raise lc.CrawlUnavailable("no browser")
    monkeypatch.setattr(main.crawler_svc, "crawl", fake_crawl)
    r = client.post("/api/analyze-crawl", json=_payload())
    assert r.status_code == 503


def test_good_crawl_creates_crawl_only_job(client, monkeypatch):
    async def fake_crawl(url, **k):
        return _crawl_result(12)
    monkeypatch.setattr(main.crawler_svc, "crawl", fake_crawl)
    r = client.post("/api/analyze-crawl", json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["crawl"]["n_anchors"] == 12
    assert body["analysis"]["routes"] == ["/login"]
    assert body["analysis"]["framework"] == "React (Vite/CRA)"

    # The session must carry the live anchors and NO source files — that is what
    # makes generation crawl-only downstream.
    session = main.store.get(body["job_id"])
    assert len(session["crawl_anchors"]) == 12
    assert session["filter_result"].files == {}
