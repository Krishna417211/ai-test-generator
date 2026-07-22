"""
test_scan_route.py — the /api/scan endpoint's passive/active branching.

Covers the wiring, not the scanners themselves (those have their own suites):
passive is the default; active needs both authorization and an available ZAP
daemon, and degrades to passive with a note when the daemon isn't there.
"""

import json

import pytest
from fastapi.testclient import TestClient

import main
from services.security_scanner import ScanResult
from services.llm_router import Tier


async def _emit_steps(progress):
    for step in ("target", "checks", "score"):
        await progress.start(step)
        await progress.done(step, "ok")


def _fake_result(findings=None, grade="C", score=70):
    return ScanResult(
        url="https://x.example", final_url="https://x.example",
        score=score, grade=grade, findings=findings or [],
        counts={"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        checks_run=42,
    )


@pytest.fixture
def client(monkeypatch):
    # No auth, no metering, no LLM, no DB: isolate the route's own logic.
    from services.auth import require_user
    main.app.dependency_overrides[require_user] = lambda: {"user_id": "usr_test"}
    monkeypatch.setattr(main, "tier_for_user", lambda uid: Tier.FREE)
    monkeypatch.setattr(main, "precheck_target", lambda url: url)

    class _Store:
        def record_scan(self, **kw):
            pass
    monkeypatch.setattr(main, "store", _Store())
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def _events(resp) -> list[dict]:
    assert resp.status_code == 200, resp.text
    return [json.loads(ln) for ln in resp.text.splitlines() if ln.strip()]


def _final(resp) -> dict:
    """The ScanResponse carried by the stream's terminal result event."""
    result = [e for e in _events(resp) if e.get("type") == "result"]
    assert result, f"no result event in stream: {resp.text}"
    return result[-1]["data"]


def _error(resp) -> dict:
    """The error event a streamed HTTPException becomes (HTTP stays 200)."""
    errs = [e for e in _events(resp) if e.get("type") == "error"]
    assert errs, f"no error event in stream: {resp.text}"
    return errs[-1]


def _passive_returns(monkeypatch, result):
    class _S:
        async def scan(self, url, progress=None):
            if progress:
                await _emit_steps(progress)
            return result
    monkeypatch.setattr(main, "SecurityScanner", _S)


class TestPassive:
    def test_default_is_passive(self, monkeypatch, client):
        _passive_returns(monkeypatch, _fake_result())
        body = _final(client.post("/api/scan", json={"url": "https://x.example", "ai_summary": False}))
        assert body["mode"] == "passive"
        assert body["scan_note"] == ""
        assert body["grade"] == "C"


class TestActiveGating:
    def test_active_without_authorization_is_rejected(self, monkeypatch, client):
        _passive_returns(monkeypatch, _fake_result())
        err = _error(client.post("/api/scan", json={
            "url": "https://x.example", "ai_summary": False,
            "active": True, "authorized": False}))
        assert err["status"] == 400
        assert "confirm you own" in err["detail"].lower()

    def test_active_unavailable_falls_back_to_passive_with_note(self, monkeypatch, client):
        # zap_allow_active off -> active can't run -> passive + note.
        monkeypatch.setattr(main.settings, "zap_allow_active", False)
        _passive_returns(monkeypatch, _fake_result())
        body = _final(client.post("/api/scan", json={
            "url": "https://x.example", "ai_summary": False,
            "active": True, "authorized": True}))
        assert body["mode"] == "passive"
        assert "isn't available" in body["scan_note"]

    def test_active_available_uses_zap(self, monkeypatch, client):
        monkeypatch.setattr(main.settings, "zap_allow_active", True)
        active_result = _fake_result(
            findings=[{"severity": "high", "category": "vulnerability",
                       "title": "Cross Site Scripting (Reflected)", "description": "",
                       "remediation": "", "evidence": "param=q", "video_url": ""}],
            grade="F", score=30)

        class _Zap:
            def __init__(self, *a, **k):
                pass
            async def wait_until_ready(self, **k):
                return "ok", ""      # ("ok", "") == usable; see ZapScanner.diagnose
            async def scan(self, url, *, active=False, authorized=False, progress=None):
                assert active and authorized
                if progress:
                    await _emit_steps(progress)
                return active_result
        monkeypatch.setattr(main, "ZapScanner", _Zap)

        body = _final(client.post("/api/scan", json={
            "url": "https://x.example", "ai_summary": False,
            "active": True, "authorized": True}))
        assert body["mode"] == "active"
        assert body["grade"] == "F"
        assert any("Cross Site Scripting" in f["title"] for f in body["findings"])

    def test_daemon_reason_is_named_in_the_note(self, monkeypatch, client):
        """A degraded scan must say *which* thing was wrong.

        "Active scanning isn't available" alone is the message that made a key
        mismatch in production take a day to find — the fix differs per reason,
        so the reason travels with the result.
        """
        monkeypatch.setattr(main.settings, "zap_allow_active", True)
        _passive_returns(monkeypatch, _fake_result())

        class _Zap:
            def __init__(self, *a, **k):
                pass
            async def wait_until_ready(self, **k):
                return "bad_key", "the ZAP daemon rejected our API key"
        monkeypatch.setattr(main, "ZapScanner", _Zap)

        body = _final(client.post("/api/scan", json={
            "url": "https://x.example", "ai_summary": False,
            "active": True, "authorized": True}))
        assert body["mode"] == "passive"
        assert "rejected our API key" in body["scan_note"]


class TestCapabilities:
    """The endpoint the UI asks before offering an active scan."""

    def test_reports_unconfigured(self, monkeypatch, client):
        monkeypatch.setattr(main.settings, "zap_address", "")
        body = client.get("/api/scan/capabilities").json()
        assert body["active_available"] is False
        assert body["code"] == "not_configured"

    def test_reports_switched_off(self, monkeypatch, client):
        monkeypatch.setattr(main.settings, "zap_address", "http://zap:8090")
        monkeypatch.setattr(main.settings, "zap_allow_active", False)
        body = client.get("/api/scan/capabilities").json()
        assert body["active_available"] is False
        assert body["code"] == "disabled"

    def test_reports_ready(self, monkeypatch, client):
        monkeypatch.setattr(main.settings, "zap_address", "http://zap:8090")
        monkeypatch.setattr(main.settings, "zap_allow_active", True)

        class _Zap:
            def __init__(self, *a, **k):
                pass
            def diagnose(self):
                return "ok", ""
        monkeypatch.setattr(main, "ZapScanner", _Zap)
        body = client.get("/api/scan/capabilities").json()
        assert body["active_available"] is True
