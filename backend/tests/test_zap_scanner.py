"""
test_zap_scanner.py — active-scan integration (OWASP ZAP).

The unit tests are hermetic: they pin the translation layer (ZAP alert -> Finding,
risk -> severity, grade) and the consent/availability gates without a daemon. The
last test really drives ZAP and is skipped unless ZAP_ADDRESS points at a live
one — exactly the environments (CI, a dev box) where no daemon exists.
"""

import os
import httpx
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


def _raiser(exc):
    """A _call stand-in that always fails the same way."""
    def call(path, **kw):
        raise exc
    return call


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


class TestAvailabilityReason:
    """"Active scanning isn't available" with no reason is unfalsifiable from
    outside: a missing daemon, one still booting, and a rejected API key all look
    identical, yet each needs a different fix. A key mismatch between the backend
    and the sidecar is exactly what broke this in production. Each cause is named."""

    def test_unconfigured_says_so(self):
        assert "no ZAP daemon is configured" in ZapScanner(address="", api_key="").availability()

    def test_connect_error_suggests_not_running_or_booting(self, monkeypatch):
        def boom(path, **kw):
            raise httpx.ConnectError("refused")
        z = ZapScanner(address="http://zap:8090", api_key="k")
        monkeypatch.setattr(z, "_call", boom)
        assert "isn't reachable" in z.availability()

    def test_closed_connection_is_reported_as_key_mismatch(self, monkeypatch):
        # ZAP answers a bad/missing API key by closing the connection, not with
        # a 403 — measured against ZAP 2.17.
        def boom(path, **kw):
            raise httpx.RemoteProtocolError("Server disconnected")
        z = ZapScanner(address="http://zap:8090", api_key="")
        monkeypatch.setattr(z, "_call", boom)
        msg = z.availability()
        assert "API key" in msg and "ZAP_API_KEY" in msg

    def test_403_is_reported_as_key_mismatch(self, monkeypatch):
        def boom(path, **kw):
            raise httpx.HTTPStatusError(
                "forbidden", request=httpx.Request("GET", "http://zap/"),
                response=httpx.Response(403, request=httpx.Request("GET", "http://zap/")))
        z = ZapScanner(address="http://zap:8090", api_key="wrong")
        monkeypatch.setattr(z, "_call", boom)
        assert "rejected our API key" in z.availability()

    def test_available_is_true_only_when_no_reason(self, monkeypatch):
        z = ZapScanner(address="http://zap:8090", api_key="k")
        monkeypatch.setattr(z, "_call", lambda path, **kw: {"version": "2.17.0"})
        assert z.availability() is None
        assert z.available() is True


class TestDiagnoseCodes:
    """diagnose() adds a machine-readable code beside the human reason.

    The code is what lets a caller distinguish a failure that will fix itself
    (the daemon is still booting) from one that never will (the key is wrong).
    """

    def test_ok_when_reachable(self, monkeypatch):
        z = ZapScanner(address="http://zap:8090", api_key="k")
        monkeypatch.setattr(z, "_call", lambda path, **kw: {"version": "2.17.0"})
        assert z.diagnose() == ("ok", "")

    def test_unreachable_is_retryable(self, monkeypatch):
        z = ZapScanner(address="http://zap:8090", api_key="k")
        monkeypatch.setattr(z, "_call", _raiser(httpx.ConnectError("refused")))
        code, reason = z.diagnose()
        assert code == "unreachable" and "isn't reachable" in reason

    def test_bad_key_is_not_retryable(self, monkeypatch):
        z = ZapScanner(address="http://zap:8090", api_key="wrong")
        monkeypatch.setattr(z, "_call", _raiser(httpx.RemoteProtocolError("disconnected")))
        assert z.diagnose()[0] == "bad_key"

    def test_unconfigured_has_its_own_code(self):
        assert ZapScanner(address="", api_key="").diagnose()[0] == "not_configured"


class TestWaitUntilReady:
    """A scan requested while the sidecar is still booting must wait it out
    rather than silently downgrade to the passive audit — ZAP takes about a
    minute to answer after a deploy or an OOM restart."""

    def test_waits_out_a_booting_daemon(self, monkeypatch):
        z = ZapScanner(address="http://zap:8090", api_key="k")
        calls = {"n": 0}

        def flaky(path, **kw):
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ConnectError("still booting")
            return {"version": "2.17.0"}
        monkeypatch.setattr(z, "_call", flaky)

        assert _run(z.wait_until_ready(timeout=30, poll=0)) == ("ok", "")
        assert calls["n"] == 3

    def test_gives_up_at_the_deadline(self, monkeypatch):
        z = ZapScanner(address="http://zap:8090", api_key="k")
        monkeypatch.setattr(z, "_call", _raiser(httpx.ConnectError("refused")))
        code, reason = _run(z.wait_until_ready(timeout=0, poll=0))
        assert code == "unreachable" and reason

    def test_does_not_wait_on_a_bad_key(self, monkeypatch):
        """Waiting cannot fix a configuration mismatch — returning at once is
        the difference between a clear error and a minute of false hope."""
        z = ZapScanner(address="http://zap:8090", api_key="wrong")
        calls = {"n": 0}

        def rejected(path, **kw):
            calls["n"] += 1
            raise httpx.RemoteProtocolError("disconnected")
        monkeypatch.setattr(z, "_call", rejected)

        code, _ = _run(z.wait_until_ready(timeout=60, poll=0))
        assert code == "bad_key"
        assert calls["n"] == 1
