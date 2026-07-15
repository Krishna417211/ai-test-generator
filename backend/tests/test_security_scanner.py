"""
test_security_scanner.py — Unit tests for the passive security scanner.

Network is stubbed with httpx.MockTransport and DNS is stubbed so the SSRF
guard passes for the fake public hosts under test.
"""

import asyncio

import httpx
import pytest

from services import security_scanner
from services.security_scanner import (
    SecurityScanner, ScanError, _validate_target, _looks_sensitive, _redirect_guard,
)


# ── SSRF guard (uses real DNS for localhost/link-local) ──

class TestSSRFGuard:
    @pytest.mark.parametrize("url", [
        "http://localhost",
        "http://127.0.0.1",
        "http://169.254.169.254",   # cloud metadata
        "http://[::1]",
    ])
    def test_blocks_internal(self, url):
        with pytest.raises(ScanError):
            _validate_target(url)

    # Asserting only on ScanError let a bug hide in plain sight: "ftp://x" was
    # rewritten to "https://ftp://x", so this raised for the wrong reason — DNS
    # failing on the host "ftp" — and the scheme check was never reached. Assert
    # the *reason*, not just the type.
    @pytest.mark.parametrize(
        "url, scheme",
        [
            ("ftp://example.com", "ftp"),
            ("file:///etc/passwd", "file"),
            ("gopher://example.com", "gopher"),
            ("ws://example.com", "ws"),
        ],
    )
    def test_rejects_non_http_scheme(self, url, scheme):
        with pytest.raises(ScanError) as exc:
            _validate_target(url)
        msg = str(exc.value)
        assert "Only http:// and https:// URLs" in msg, (
            f"expected a scheme rejection, got: {msg!r}"
        )
        assert scheme in msg
        assert "resolve host" not in msg

    def test_prepends_https_when_scheme_missing(self, monkeypatch):
        monkeypatch.setattr(
            security_scanner.socket, "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
        )
        assert _validate_target("example.com").startswith("https://")

    @pytest.mark.parametrize(
        "url",
        ["example.com", "example.com:8080", "example.com/path?q=1", "sub.example.co.uk"],
    )
    def test_bare_hosts_still_get_https(self, monkeypatch, url):
        """The stricter scheme regex must not mistake a port or a dotted host
        for a scheme and refuse to prepend https://."""
        monkeypatch.setattr(
            security_scanner.socket, "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
        )
        assert _validate_target(url) == "https://" + url

    def test_existing_http_scheme_is_preserved(self, monkeypatch):
        monkeypatch.setattr(
            security_scanner.socket, "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
        )
        assert _validate_target("http://example.com") == "http://example.com"

    def test_redirect_guard_blocks_internal_hop(self):
        # An open redirect pointing at cloud metadata must be rejected mid-scan.
        resp = httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data"},
            request=httpx.Request("GET", "https://safe.example"),
        )
        with pytest.raises(ScanError):
            asyncio.run(_redirect_guard(resp))

    def test_redirect_guard_allows_public_hop(self, monkeypatch):
        monkeypatch.setattr(
            security_scanner.socket, "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
        )
        resp = httpx.Response(
            302, headers={"location": "https://other.example/next"},
            request=httpx.Request("GET", "https://safe.example"),
        )
        asyncio.run(_redirect_guard(resp))  # no raise


# ── sensitive-file signature matching ────────

class TestLooksSensitive:
    def test_env_positive(self):
        assert _looks_sensitive("env", "SECRET_KEY=abc\nDB_PASSWORD=xyz")

    def test_env_negative_on_spa_html(self):
        assert not _looks_sensitive("env", "<html><body>Not found</body></html>")

    def test_gitconfig(self):
        assert _looks_sensitive("gitconfig", "[core]\n\trepositoryformatversion = 0")


# ── full scan via mocked transport ───────────

def _install(monkeypatch, handler):
    real = httpx.AsyncClient
    def fake(*a, **k):
        for key in ("timeout", "follow_redirects", "headers", "verify"):
            k.pop(key, None)
        return real(transport=httpx.MockTransport(handler), timeout=5, follow_redirects=True)
    monkeypatch.setattr(security_scanner.httpx, "AsyncClient", fake)
    monkeypatch.setattr(
        security_scanner.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )


SECURE_HEADERS = {
    "strict-transport-security": "max-age=63072000; includeSubDomains",
    "content-security-policy": "default-src 'self'",
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "geolocation=()",
    "set-cookie": "session=abc; Secure; HttpOnly; SameSite=Lax",
}


class TestScan:
    def test_secure_site_scores_high(self, monkeypatch):
        def handler(req):
            if req.url.path == "/":
                return httpx.Response(200, headers=SECURE_HEADERS, text="<html>ok</html>")
            return httpx.Response(404)  # nothing sensitive exposed
        _install(monkeypatch, handler)

        r = asyncio.run(SecurityScanner().scan("https://safe.example"))
        assert r.grade == "A"
        assert r.score == 100
        assert r.findings == []

    def test_insecure_site_flags_issues(self, monkeypatch):
        def handler(req):
            if req.url.path == "/.env":
                return httpx.Response(200, text="SECRET_KEY=abc123\nDB_PASSWORD=xyz")
            if req.url.path == "/":
                return httpx.Response(
                    200,
                    headers={
                        "server": "nginx/1.18.0",
                        "access-control-allow-origin": "*",
                        "access-control-allow-credentials": "true",
                        "set-cookie": "session=abc",  # no flags
                    },
                    text='<html><script src="http://cdn.evil/x.js"></script></html>',
                )
            return httpx.Response(404)
        _install(monkeypatch, handler)

        r = asyncio.run(SecurityScanner().scan("http://bad.example"))
        titles = [f["title"] for f in r.findings]
        sev = {f["title"]: f["severity"] for f in r.findings}

        assert any("HTTP" in t for t in titles)                       # plain HTTP
        assert "No Content-Security-Policy" in titles                 # missing CSP
        assert any(".env" in t for t in titles)                       # exposed secret file
        assert sev[next(t for t in titles if ".env" in t)] == "critical"
        assert any("CORS" in t for t in titles)                       # wildcard + credentials
        assert any("HttpOnly" in t for t in titles)                   # cookie flag
        assert r.grade == "F"

        # Findings must be sorted most-severe first.
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        sevs = [order[f["severity"]] for f in r.findings]
        assert sevs == sorted(sevs)

    def test_unreachable_host_raises(self, monkeypatch):
        def handler(req):
            raise httpx.ConnectError("boom")
        _install(monkeypatch, handler)
        with pytest.raises(ScanError):
            asyncio.run(SecurityScanner().scan("https://down.example"))
