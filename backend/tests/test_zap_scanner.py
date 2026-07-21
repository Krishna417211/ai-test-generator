"""
test_zap_scanner.py — active-scan integration (OWASP ZAP).

The unit tests are hermetic: they pin the translation layer (ZAP alert -> Finding,
risk -> severity, grade) and the consent/availability gates without a daemon. The
last test really drives ZAP and is skipped unless ZAP_ADDRESS points at a live
one — exactly the environments (CI, a dev box) where no daemon exists.
"""

import os
import asyncio

import pytest

from services import security_scanner as sc
from services.zap_scanner import (
    ZapScanner, ZapUnavailableError, ZapAuthorizationError,
    _map_alert, _grade, _dedupe,
)
from services.security_scanner import Finding


def _run(coro):
    return asyncio.run(coro)


# ── translation layer (pure) ─────────────────

class TestMapping:
    def test_risk_maps_to_severity_and_fields(self):
        f = _map_alert({
            "risk": "High", "alert": "Cross Site Scripting (Reflected)",
            "description": "Reflected XSS.", "solution": "Encode output.",
            "param": "q", "evidence": "<script>alert(1)</script>",
            "url": "http://x/search", "reference": "https://owasp.org/xss\nhttps://cwe/79",
        })
        assert f.severity == "high"
        assert f.category == "vulnerability"
        assert f.title.startswith("Cross Site Scripting")
        assert "param=q" in f.evidence and "<script>" in f.evidence
        assert f.remediation == "Encode output."
        assert f.video_url == "https://owasp.org/xss"   # first reference line only

    def test_unknown_risk_defaults_to_info(self):
        assert _map_alert({"risk": "Nonsense", "alert": "x"}).severity == "info"

    def test_informational_maps_to_info(self):
        assert _map_alert({"risk": "Informational", "alert": "x"}).severity == "info"


class TestGrade:
    def test_any_high_is_F(self):
        score, grade = _grade({"high": 1, "medium": 0, "low": 0})
        assert grade == "F"

    def test_clean_is_A(self):
        assert _grade({"critical": 0, "high": 0, "medium": 0, "low": 0})[1] == "A"

    def test_mediums_pull_the_grade_down_without_failing(self):
        score, grade = _grade({"high": 0, "medium": 2, "low": 1})
        assert grade in ("B", "C") and grade != "F"


class TestDedupe:
    def test_collapses_per_url_repeats_and_sorts(self):
        items = [
            Finding("low", "vulnerability", "Server version", "", ""),
            Finding("high", "vulnerability", "XSS", "", ""),
            Finding("high", "vulnerability", "XSS", "", ""),   # same alert, other URL
            Finding("medium", "vulnerability", "No CSP", "", ""),
        ]
        out = _dedupe(items)
        assert [f.title for f in out] == ["XSS", "No CSP", "Server version"]  # sorted, deduped


# ── consent + availability gates ─────────────

class TestGates:
    def test_no_address_is_unavailable(self):
        z = ZapScanner(address="", api_key="")
        assert z.available() is False

    def test_scan_without_address_raises_unavailable(self):
        z = ZapScanner(address="", api_key="")
        with pytest.raises(ZapUnavailableError):
            _run(z.scan("https://example.com"))

    def test_active_requires_master_switch(self, monkeypatch):
        monkeypatch.setattr("services.zap_scanner.settings.zap_allow_active", False)
        z = ZapScanner(address="http://127.0.0.1:9", api_key="")
        with pytest.raises(ZapAuthorizationError):
            _run(z.scan("https://example.com", active=True, authorized=True))

    def test_active_requires_explicit_authorization(self, monkeypatch):
        monkeypatch.setattr("services.zap_scanner.settings.zap_allow_active", True)
        z = ZapScanner(address="http://127.0.0.1:9", api_key="")
        with pytest.raises(ZapAuthorizationError):
            _run(z.scan("https://example.com", active=True, authorized=False))

    def test_private_target_refused_by_ssrf_guard(self, monkeypatch):
        # Reaches validate_target (after the auth gate) and is refused there —
        # ZAP inherits the passive scanner's scope + SSRF guard.
        monkeypatch.setattr("services.zap_scanner.settings.zap_allow_active", True)
        z = ZapScanner(address="http://127.0.0.1:9", api_key="")
        with pytest.raises(sc.ScanError):
            _run(z.scan("http://127.0.0.1:7000", active=True, authorized=True))


# ── live daemon (skipped without one) ────────

@pytest.mark.skipif(not os.getenv("ZAP_ADDRESS"),
                    reason="no live ZAP daemon (set ZAP_ADDRESS to run)")
def test_live_scan_finds_alerts(monkeypatch):
    # Allow the loopback test target through the SSRF guard for this local run.
    monkeypatch.setattr(sc, "_assert_public_host", lambda host: None)
    monkeypatch.setattr("services.zap_scanner.settings.zap_allow_active", True)
    z = ZapScanner()
    target = os.getenv("ZAP_TEST_TARGET", "http://127.0.0.1:7000/")
    r = _run(z.scan(target, active=True, authorized=True))
    assert r.checks_run >= 1
    assert isinstance(r.findings, list)
