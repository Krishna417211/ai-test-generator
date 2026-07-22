"""
test_security_scanner.py — Unit tests for the passive security scanner.

Network is stubbed with httpx.MockTransport and DNS is stubbed so the SSRF
guard passes for the fake public hosts under test.
"""

import asyncio
import time

import httpx
import pytest

from services import security_scanner
from services.security_scanner import (
    SecurityScanner, ScanError, OutOfScopeError, UnscannableResponseError,
    validate_target, _looks_sensitive, _redirect_guard, _assert_in_scope,
    _brand_label, _evaluate_tls,
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
            validate_target(url)

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
            validate_target(url)
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
        assert validate_target("example.com").startswith("https://")

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
        assert validate_target(url) == "https://" + url

    def test_existing_http_scheme_is_preserved(self, monkeypatch):
        monkeypatch.setattr(
            security_scanner.socket, "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
        )
        assert validate_target("http://example.com") == "http://example.com"

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

# The real class, captured once at import — before any test can patch it.
#
# This used to read `real = httpx.AsyncClient` inside _install, i.e. whatever the
# attribute happened to be at call time. A second _install in the same test then
# captured the FIRST fake and wrapped it, so the outer handler was never reached
# and the scan silently replayed the previous test's responses. A test comparing
# two scans got two identical results and looked like a product bug.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _install(monkeypatch, handler):
    real = _REAL_ASYNC_CLIENT
    def fake(*a, **k):
        for key in ("timeout", "follow_redirects", "headers", "verify"):
            k.pop(key, None)
        return real(transport=httpx.MockTransport(handler), timeout=5, follow_redirects=True)
    monkeypatch.setattr(security_scanner.httpx, "AsyncClient", fake)
    monkeypatch.setattr(
        security_scanner.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    # TLS inspection opens its own real socket; stub it to a healthy cert so scan
    # tests stay hermetic and deterministic. Cases that need a bad cert call
    # _evaluate_tls directly (see TestTlsCertificate).
    async def _fake_tls(self, host, port=443):
        return (time.time() + 400 * 86_400, "TLSv1.3")
    monkeypatch.setattr(security_scanner.SecurityScanner, "_fetch_tls_info", _fake_tls)


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


# ── false positives ──────────────────────────
#
# Every test below pins a case the scanner used to get wrong. They matter more
# than the positive tests: a missed finding is a gap, but a *false* finding is
# advice that is actively wrong, and enough of them teach the reader to
# disregard the report entirely — including the true findings in it.

def _page(monkeypatch, *, headers=None, body="<html>ok</html>", url="https://x.example"):
    """Scan one page with everything else clean (no exposed files)."""
    def handler(req):
        if req.url.path == "/":
            return httpx.Response(200, headers=headers or {}, text=body)
        return httpx.Response(404)
    _install(monkeypatch, handler)
    return asyncio.run(SecurityScanner().scan(url))


def _titles(r):
    return [f["title"] for f in r.findings]


def _cookies(monkeypatch, cookies, url="https://x.example"):
    """Scan a page that sets several cookies.

    Repeated Set-Cookie headers have to go in as a list of pairs — httpx.Headers
    is immutable-ish and has no .add(), and a dict would silently keep only the
    last cookie, quietly turning a multi-cookie test into a one-cookie one.
    """
    headers = [("set-cookie", c) for c in cookies]
    def handler(req):
        if req.url.path == "/":
            return httpx.Response(200, headers=headers, text="<html>ok</html>")
        return httpx.Response(404)
    _install(monkeypatch, handler)
    return asyncio.run(SecurityScanner().scan(url))


class TestTransportFalsePositive:
    def test_http_that_upgrades_to_https_is_not_flagged(self, monkeypatch):
        """A site that 301s http -> https is doing the right thing and must get
        no transport finding. The old check read resp.history for an https hop,
        but httpx stores the *pre*-redirect (http) URL there, so it flagged every
        site that redirects correctly with 'HTTP does not redirect to HTTPS'."""
        def handler(req):
            if req.url.scheme == "http" and req.url.path == "/":
                return httpx.Response(301, headers={"location": "https://x.example/"})
            if req.url.path == "/":
                return httpx.Response(200, headers=SECURE_HEADERS, text="<html>ok</html>")
            return httpx.Response(404)
        _install(monkeypatch, handler)
        r = asyncio.run(SecurityScanner().scan("http://x.example"))
        assert not any("HTTP" in t for t in _titles(r)), _titles(r)
        assert r.final_url.startswith("https://")

    def test_plain_http_site_still_flagged(self, monkeypatch):
        """The real problem — http that stays http — must still be caught."""
        r = _page(monkeypatch, url="http://plain.example")
        assert any("Site served over plain HTTP" in t for t in _titles(r))


class TestTlsCertificate:
    """The tls category now inspects the certificate, not just the URL scheme."""
    NOW = 1_700_000_000.0

    def _titles(self, findings):
        return [f.title for f in findings]

    def test_expired_cert_flagged_high(self):
        f = _evaluate_tls(self.NOW - 86_400, "TLSv1.3", self.NOW)
        assert any("expired" in t for t in self._titles(f))
        assert f[0].severity == "high"

    def test_expiring_soon_flagged_low(self):
        f = _evaluate_tls(self.NOW + 3 * 86_400, "TLSv1.3", self.NOW)
        assert any("expires very soon" in t for t in self._titles(f))
        assert all(x.severity == "low" for x in f)

    def test_healthy_cert_no_finding(self):
        assert _evaluate_tls(self.NOW + 200 * 86_400, "TLSv1.3", self.NOW) == []

    def test_healthy_cert_tls12_no_finding(self):
        assert _evaluate_tls(self.NOW + 200 * 86_400, "TLSv1.2", self.NOW) == []

    def test_weak_protocol_flagged(self):
        f = _evaluate_tls(self.NOW + 200 * 86_400, "TLSv1", self.NOW)
        assert any("Outdated TLS protocol" in t for t in self._titles(f))

    def test_expired_and_weak_both_reported(self):
        f = _evaluate_tls(self.NOW - 86_400, "TLSv1.1", self.NOW)
        titles = self._titles(f)
        assert any("expired" in t for t in titles)
        assert any("Outdated" in t for t in titles)

    def test_http_scan_skips_cert_check(self, monkeypatch):
        # Over http there's no cert to inspect; _fetch_tls_info must not even be
        # consulted. Install a counting spy AFTER _install so it isn't clobbered.
        def handler(req):
            return httpx.Response(200, text="<html>ok</html>") if req.url.path == "/" \
                else httpx.Response(404)
        _install(monkeypatch, handler)
        called = {"n": 0}
        async def spy(self, host, port=443):
            called["n"] += 1
            return (time.time() + 400 * 86_400, "TLSv1.3")
        monkeypatch.setattr(security_scanner.SecurityScanner, "_fetch_tls_info", spy)
        asyncio.run(SecurityScanner().scan("http://plain.example"))
        assert called["n"] == 0


class TestHeaderStrength:
    """A header can be present and protect nothing. These pin the value-strength
    checks: present-but-weak must be caught (false negative), and a strong value
    must never be flagged (false positive)."""

    # ── HSTS ──
    def test_hsts_max_age_zero_is_flagged(self, monkeypatch):
        r = _page(monkeypatch, headers={"strict-transport-security": "max-age=0"})
        assert any("HSTS is disabled" in t for t in _titles(r))

    def test_hsts_too_short_is_flagged(self, monkeypatch):
        r = _page(monkeypatch, headers={"strict-transport-security": "max-age=3600"})
        assert any("HSTS max-age is too short" in t for t in _titles(r))

    def test_hsts_no_max_age_is_flagged(self, monkeypatch):
        r = _page(monkeypatch, headers={"strict-transport-security": "includeSubDomains"})
        assert any("no max-age" in t for t in _titles(r))

    def test_strong_hsts_not_flagged(self, monkeypatch):
        r = _page(monkeypatch, headers={
            "strict-transport-security": "max-age=63072000; includeSubDomains; preload"})
        assert not any("HSTS" in t and "not set" not in t for t in _titles(r))

    def test_hsts_only_judged_on_https(self, monkeypatch):
        # Over http, HSTS is meaningless — no weak-HSTS finding, and no "not set".
        r = _page(monkeypatch, headers={"strict-transport-security": "max-age=0"},
                  url="http://plain.example")
        assert not any("HSTS" in t for t in _titles(r))

    # ── CSP ──
    def test_csp_unsafe_inline_is_flagged(self, monkeypatch):
        r = _page(monkeypatch, headers={
            "content-security-policy": "default-src 'self'; script-src 'self' 'unsafe-inline'"})
        assert any("unsafe script sources" in t for t in _titles(r))

    def test_csp_wildcard_script_is_flagged(self, monkeypatch):
        r = _page(monkeypatch, headers={
            "content-security-policy": "script-src *"})
        assert any("unsafe script sources" in t for t in _titles(r))

    def test_csp_unsafe_inline_with_nonce_not_flagged(self, monkeypatch):
        # A nonce makes browsers ignore 'unsafe-inline' — flagging it is a false positive.
        r = _page(monkeypatch, headers={
            "content-security-policy": "script-src 'self' 'nonce-abc123' 'unsafe-inline'"})
        assert not any("unsafe script sources" in t for t in _titles(r))

    def test_csp_strict_dynamic_not_flagged(self, monkeypatch):
        r = _page(monkeypatch, headers={
            "content-security-policy": "script-src 'strict-dynamic' 'unsafe-inline' https:"})
        assert not any("unsafe script sources" in t for t in _titles(r))

    def test_strong_csp_not_flagged(self, monkeypatch):
        r = _page(monkeypatch, headers={
            "content-security-policy": "default-src 'self'; script-src 'self'; object-src 'none'"})
        assert not any("unsafe script sources" in t for t in _titles(r))

    def test_csp_script_src_falls_back_to_default_src(self, monkeypatch):
        # No script-src, but default-src is unsafe -> scripts are unrestricted.
        r = _page(monkeypatch, headers={
            "content-security-policy": "default-src 'self' 'unsafe-inline'"})
        assert any("unsafe script sources" in t for t in _titles(r))


class TestClickjackingFalsePositive:
    def test_csp_frame_ancestors_satisfies_clickjacking(self, monkeypatch):
        """frame-ancestors supersedes X-Frame-Options; browsers ignore XFO when
        a CSP sets it. Telling a site that took the modern advice that it has no
        clickjacking protection is simply false."""
        r = _page(monkeypatch, headers={
            "content-security-policy": "default-src 'self'; frame-ancestors 'none'",
        })
        assert not any("Clickjacking" in t for t in _titles(r))

    def test_xfo_still_flagged_when_neither_present(self, monkeypatch):
        r = _page(monkeypatch, headers={"content-security-policy": "default-src 'self'"})
        assert any("Clickjacking" in t for t in _titles(r))

    def test_xfo_not_flagged_when_xfo_present(self, monkeypatch):
        r = _page(monkeypatch, headers={"x-frame-options": "DENY"})
        assert not any("Clickjacking" in t for t in _titles(r))


class TestMixedContentFalsePositive:
    def test_plain_anchor_link_is_not_mixed_content(self, monkeypatch):
        """<a href="http://…"> is a link the user may click, not a subresource
        the page loads. Browsers neither block nor warn about it."""
        r = _page(monkeypatch, body='<html><a href="http://example.com">docs</a></html>')
        assert not any("Mixed content" in t for t in _titles(r))

    def test_canonical_link_is_not_mixed_content(self, monkeypatch):
        r = _page(monkeypatch, body='<html><link rel="canonical" href="http://x.example/"></html>')
        assert not any("Mixed content" in t for t in _titles(r))

    @pytest.mark.parametrize("body", [
        '<script src="http://cdn.x/x.js"></script>',
        '<img src="http://cdn.x/a.png">',
        '<iframe src="http://cdn.x/f"></iframe>',
        '<link rel="stylesheet" href="http://cdn.x/a.css">',
        '<object data="http://cdn.x/o.swf"></object>',
    ])
    def test_real_subresources_are_flagged(self, monkeypatch, body):
        r = _page(monkeypatch, body=f"<html>{body}</html>")
        assert any("Mixed content" in t for t in _titles(r)), body

    def test_evidence_names_the_resource(self, monkeypatch):
        r = _page(monkeypatch, body='<html><script src="http://cdn.x/x.js"></script></html>')
        f = next(f for f in r.findings if "Mixed content" in f["title"])
        assert "http://cdn.x/x.js" in f["evidence"]

    def test_http_page_is_not_flagged_for_mixed_content(self, monkeypatch):
        """Mixed content is only a concept on an HTTPS page."""
        r = _page(monkeypatch, body='<html><img src="http://x/a.png"></html>',
                  url="http://plain.example")
        assert not any("Mixed content" in t for t in _titles(r))


class TestPasswordFormFalsePositive:
    def test_password_in_a_different_form_is_not_flagged(self, monkeypatch):
        """The http:// action and the password field must be in the SAME form.
        Checking them independently flagged an https login page that happened to
        also carry a plain-http newsletter box."""
        body = """
        <html>
          <form action="http://track.example/subscribe"><input type="email"></form>
          <form action="https://x.example/login"><input type="password"></form>
        </html>
        """
        r = _page(monkeypatch, body=body)
        assert not any("Password form" in t for t in _titles(r))

    def test_password_in_the_http_form_is_flagged(self, monkeypatch):
        body = '<html><form action="http://x.example/login"><input type="password"></form></html>'
        r = _page(monkeypatch, body=body)
        assert any("Password form" in t for t in _titles(r))

    def test_reported_once_even_with_several_bad_forms(self, monkeypatch):
        body = (
            '<html>'
            '<form action="http://a.example/l"><input type="password"></form>'
            '<form action="http://b.example/l"><input type="password"></form>'
            '</html>'
        )
        r = _page(monkeypatch, body=body)
        assert sum("Password form" in t for t in _titles(r)) == 1


class TestCookieNoise:
    def test_analytics_cookie_not_flagged_for_httponly(self, monkeypatch):
        """_ga is read by JavaScript by design — HttpOnly would break it, so
        demanding it is advice we'd want the reader to ignore."""
        r = _page(monkeypatch, headers={"set-cookie": "_ga=GA1.2.3; Secure; SameSite=Lax"})
        assert not any("HttpOnly" in t for t in _titles(r))

    @pytest.mark.parametrize("name", ["session", "sessionid", "auth_token", "jwt", "sid", "PHPSESSID"])
    def test_session_cookies_are_flagged_for_httponly(self, monkeypatch, name):
        r = _page(monkeypatch, headers={"set-cookie": f"{name}=x; Secure; SameSite=Lax"})
        assert any("HttpOnly" in t for t in _titles(r)), name

    def test_cookies_are_grouped_into_one_finding_per_issue(self, monkeypatch):
        """Previously one finding per cookie per flag, none of which deduplicated
        because the title carried the cookie name."""
        r = _cookies(monkeypatch, ["a=1", "b=2", "c=3", "d=4", "e=5"])
        assert sum("Secure flag" in t for t in _titles(r)) == 1
        assert sum("SameSite" in t for t in _titles(r)) == 1

    def test_grouped_finding_names_the_cookies(self, monkeypatch):
        r = _cookies(monkeypatch, ["alpha=1", "beta=2"])
        f = next(f for f in r.findings if "Secure flag" in f["title"])
        assert "alpha" in f["evidence"] and "beta" in f["evidence"]


class TestConfigJsonFalsePositive:
    def test_firebase_web_config_is_not_a_secret(self):
        """apiKey in a Firebase web config is a project identifier that is meant
        to ship to browsers — not a credential."""
        assert not _looks_sensitive("json", '{"apiKey":"AIzaSyABC","authDomain":"x.firebaseapp.com"}')

    def test_pwa_manifest_is_not_a_secret(self):
        assert not _looks_sensitive("json", '{"name":"App","icons":[],"start_url":"/"}')

    @pytest.mark.parametrize("blob", [
        '{"client_secret":"abc"}',
        '{"password":"hunter2"}',
        '{"aws_secret_access_key":"x"}',
        '{"private_key":"-----BEGIN"}',
    ])
    def test_real_secrets_still_detected(self, blob):
        assert _looks_sensitive("json", blob)


class TestScoring:
    def test_missing_headers_alone_do_not_sink_the_grade(self, monkeypatch):
        """Six missing headers was -48 under a flat sum, grading a
        headers-only problem D — the same band as a site leaking its .env."""
        r = _page(monkeypatch)   # no security headers at all, nothing else wrong
        assert r.score >= 65
        assert r.grade in ("A", "B", "C")

    def test_category_deduction_is_capped(self, monkeypatch):
        r = _cookies(monkeypatch, ["session=1", "auth=2", "jwt=3", "sid=4", "token=5"])
        cookie_findings = [f for f in r.findings if f["category"] == "cookies"]
        assert cookie_findings                      # they are still reported
        assert r.score >= 100 - 35 - 20             # headers cap + cookies cap

    def test_exposed_env_still_reaches_F_alone(self, monkeypatch):
        """The cap must not rescue a site that is leaking its secrets."""
        def handler(req):
            if req.url.path == "/.env":
                return httpx.Response(200, text="SECRET_KEY=abc\nDB_PASSWORD=xyz")
            if req.url.path == "/":
                return httpx.Response(200, headers=SECURE_HEADERS, text="<html>ok</html>")
            return httpx.Response(404)
        _install(monkeypatch, handler)
        r = asyncio.run(SecurityScanner().scan("https://x.example"))
        assert r.grade == "F"

    def test_clean_site_is_still_100(self, monkeypatch):
        r = _page(monkeypatch, headers=SECURE_HEADERS)
        assert r.score == 100 and r.findings == []

    def test_checks_run_counts_real_checks(self, monkeypatch):
        """It used to report a number several checks returned 1 for regardless
        of how many they actually ran."""
        r = _page(monkeypatch, headers=SECURE_HEADERS)
        from services.security_scanner import SENSITIVE_PATHS, SECURITY_HEADERS
        # transport is one honest check now (is the final page https), not two —
        # see _check_transport: the old "does http redirect" sub-check could not be
        # answered from this request and was removed.
        # transport 1 + certificate 2 (expiry, protocol) + headers
        # (presence + 2 value-strength checks) + cookies 3 + exposed files
        # + disclosure 4 + cors 1 + content 2.
        expected = (
            1 + 2 + (len(SECURITY_HEADERS) + 2) + 3
            + len(SENSITIVE_PATHS) + 4 + 1 + 2
        )
        assert r.checks_run == expected


class TestScopeGate:
    """Third-party sites are refused before the network is touched."""

    @pytest.mark.parametrize("host", [
        "google.com", "www.google.com", "mail.google.com", "google.co.uk",
        "github.com", "www.wikipedia.org", "aws.amazon.com", "api.stripe.com",
        "facebook.com", "openai.com",
    ])
    def test_third_party_refused(self, host):
        with pytest.raises(OutOfScopeError) as exc:
            _assert_in_scope(host)
        assert "your own" in str(exc.value).lower()

    @pytest.mark.parametrize("host", [
        "whitehouse.gov", "army.mil", "example.bank", "india.gov.in",
    ])
    def test_gov_mil_bank_refused(self, host):
        with pytest.raises(OutOfScopeError):
            _assert_in_scope(host)

    @pytest.mark.parametrize("host", [
        "my-app.com", "staging.my-app.io", "shop.acme.co.uk", "x.example",
        "localhost.mycorp.dev",
    ])
    def test_ordinary_sites_pass(self, host):
        _assert_in_scope(host)   # no raise

    @pytest.mark.parametrize("host", [
        "myapp.github.io", "myproject.gitlab.io", "myapp.vercel.app",
        "myapp.netlify.app", "myapp.herokuapp.com", "myapp.pages.dev",
        "myapp.web.app", "myapp.fly.dev", "myapp.onrender.com",
    ])
    def test_user_deployments_on_hosting_suffixes_are_in_scope(self, host):
        """The whole point of the tool. `myapp.github.io` brand-matches "github"
        but belongs to the user — refusing it would block the exact case Testra
        exists to serve, while telling them to go scan something they own."""
        _assert_in_scope(host)   # no raise

    @pytest.mark.parametrize("host, brand", [
        ("www.google.com", "google"), ("google.co.uk", "google"),
        ("my-app.com", "my-app"), ("shop.acme.co.uk", "acme"),
        ("api.stripe.com", "stripe"),
    ])
    def test_brand_label_ignores_public_suffix(self, host, brand):
        assert _brand_label(host) == brand

    def test_refusal_happens_before_dns(self, monkeypatch):
        """No lookup for a host we won't scan either way."""
        def boom(*a, **k):
            raise AssertionError("DNS must not be consulted for an out-of-scope host")
        monkeypatch.setattr(security_scanner.socket, "getaddrinfo", boom)
        with pytest.raises(OutOfScopeError):
            validate_target("https://www.google.com")


class TestUnscannableResponse:
    """A finding needs a page it is true of.

    Measured before this existed: www.wikipedia.org answered our scanner with
    403, and the scan reported "grade C, 6 findings — no CSP, no HSTS, no
    X-Frame-Options...". Every one of those described the block page. It also
    explains why so many unrelated sites produced an identical finding list: a
    bare error page is missing the same six headers everywhere.
    """

    def _scan(self, monkeypatch, status=200, headers=None, body="<html>ok</html>"):
        def handler(req):
            if req.url.path == "/":
                return httpx.Response(status, headers=headers or {}, text=body)
            return httpx.Response(404)
        _install(monkeypatch, handler)
        return asyncio.run(SecurityScanner().scan("https://x.example"))

    @pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
    def test_non_2xx_is_refused_not_graded(self, monkeypatch, status):
        with pytest.raises(UnscannableResponseError) as exc:
            self._scan(monkeypatch, status=status)
        assert str(status) in str(exc.value)

    def test_429_explains_the_rate_limit(self, monkeypatch):
        with pytest.raises(UnscannableResponseError) as exc:
            self._scan(monkeypatch, status=429)
        assert "rate-limited" in str(exc.value)

    def test_cloudflare_challenge_header_is_refused(self, monkeypatch):
        with pytest.raises(UnscannableResponseError) as exc:
            self._scan(monkeypatch, headers={"cf-mitigated": "challenge"})
        assert "challenge" in str(exc.value).lower()

    @pytest.mark.parametrize("body", [
        "<html><title>Just a moment...</title></html>",
        "<html>Enable JavaScript and cookies to continue</html>",
        "<html>Request unsuccessful. Incapsula incident ID: 123</html>",
        "<html>Pardon Our Interruption</html>",
        "<html>DDoS protection by Cloudflare</html>",
    ])
    def test_bot_challenge_page_with_200_is_refused(self, monkeypatch, body):
        """Challenge interstitials return 200 with the real page nowhere in it."""
        with pytest.raises(UnscannableResponseError):
            self._scan(monkeypatch, body=body)

    @pytest.mark.parametrize("body", [
        "<html><h1>Handling 403 Access Denied errors in nginx</h1><p>...</p></html>",
        "<html><article>Why your S3 bucket returns Access Denied</article></html>",
        '<html><script>const MSGS={403:"Access Denied"}</script></html>',
    ])
    def test_a_page_that_merely_mentions_a_block_phrase_still_scans(self, monkeypatch, body):
        """Refusing the scan is the heaviest thing this check can do — it
        withholds the whole report, not one finding. An article about Access
        Denied errors is a real page and must be graded like one.

        "access denied" was a marker for exactly one commit. It never caught a
        real block page (those are 403s, already handled) and would have refused
        every security blog that discusses them.
        """
        r = self._scan(monkeypatch, body=body)
        assert r.grade   # scanned, not refused

    def test_a_weak_marker_in_a_long_real_page_is_not_a_challenge(self, monkeypatch):
        """'Just a moment' is a loading spinner on a thousand real sites. On a
        full-sized page it is content, not a Cloudflare interstitial."""
        body = "<html><body>Just a moment while we load your dashboard." + \
               ("<p>real content</p>" * 800) + "</body></html>"
        assert len(body) > 8000
        r = self._scan(monkeypatch, body=body)
        assert r.grade

    def test_a_weak_marker_in_a_stub_page_is_still_caught(self, monkeypatch):
        """The real Cloudflare interstitial: the phrase AND nothing else."""
        with pytest.raises(UnscannableResponseError):
            self._scan(monkeypatch, body="<html><title>Just a moment...</title></html>")

    def test_a_real_page_still_scans(self, monkeypatch):
        r = self._scan(monkeypatch, headers=SECURE_HEADERS)
        assert r.grade == "A"

    def test_204_and_201_are_gradeable(self, monkeypatch):
        r = self._scan(monkeypatch, status=201, headers=SECURE_HEADERS)
        assert r.grade == "A"


class TestUserAgent:
    def test_ua_is_browser_shaped_and_self_identifying(self):
        """Browser-shaped because big sites serve a degraded header set to a bare
        bot token — measured on google.com, the old UA saw no HSTS where a real
        visitor is served one, so the scan reported a header as missing that is
        actually there. Still carries our token: identifiable and blockable."""
        ua = security_scanner.USER_AGENT
        assert ua.startswith("Mozilla/5.0")
        assert "Testra-SecurityScanner" in ua

    def test_scan_sends_that_ua(self, monkeypatch):
        seen = {}
        def handler(req):
            seen["ua"] = req.headers.get("user-agent")
            return httpx.Response(200, headers=SECURE_HEADERS, text="<html>ok</html>")
        # _install strips the headers kwarg, so pass the client its UA the way
        # scan() does and assert on what actually left the socket.
        def fake(*a, **k):
            for key in ("timeout", "follow_redirects", "verify"):
                k.pop(key, None)
            return _REAL_ASYNC_CLIENT(
                transport=httpx.MockTransport(handler), timeout=5,
                follow_redirects=True, headers=k.pop("headers", None))
        monkeypatch.setattr(security_scanner.httpx, "AsyncClient", fake)
        monkeypatch.setattr(
            security_scanner.socket, "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
        )
        async def _fake_tls(self, host, port=443):
            return (time.time() + 400 * 86_400, "TLSv1.3")
        monkeypatch.setattr(security_scanner.SecurityScanner, "_fetch_tls_info", _fake_tls)
        asyncio.run(SecurityScanner().scan("https://x.example"))
        assert seen["ua"] == security_scanner.USER_AGENT


class TestHardeningGrade:
    """Hardening findings describe layers a site could add; vulnerability
    findings mean something is broken now. The grade must tell them apart."""

    def test_hardening_only_site_floors_at_C(self, monkeypatch):
        """Every header missing AND every cookie flag missing — still nothing an
        attacker can use. Under the old flat caps this was 51, a D: one band off
        failing, for a site with no vulnerability. Measured on google.com."""
        r = _cookies(monkeypatch, ["a=1", "session=2"])   # no security headers either
        assert {f["category"] for f in r.findings} <= security_scanner.HARDENING_CATEGORIES
        assert r.score >= 70
        assert r.grade == "C"

    def test_hardening_findings_are_still_all_reported(self, monkeypatch):
        """Capping the score must not quietly drop the advice."""
        r = _cookies(monkeypatch, ["session=2"])
        titles = _titles(r)
        assert "No Content-Security-Policy" in titles
        assert any("HttpOnly" in t for t in titles)
        assert any("SameSite" in t for t in titles)

    def test_exposed_env_still_reaches_F(self, monkeypatch):
        """The hardening budget must not rescue a site that is actually broken."""
        def handler(req):
            if req.url.path == "/.env":
                return httpx.Response(200, text="SECRET_KEY=abc\nDB_PASSWORD=xyz")
            if req.url.path == "/":
                return httpx.Response(200, text="<html>ok</html>")   # no headers
            return httpx.Response(404)
        _install(monkeypatch, handler)
        r = asyncio.run(SecurityScanner().scan("https://x.example"))
        assert r.grade == "F"

    def test_real_vulnerability_outranks_hardening(self, monkeypatch):
        """A wildcard-CORS-with-credentials site must score below a site whose
        only faults are missing headers — it has an account-takeover vector."""
        clean_ish = _page(monkeypatch)     # hardening only
        def handler(req):
            if req.url.path == "/":
                return httpx.Response(200, headers={
                    "access-control-allow-origin": "*",
                    "access-control-allow-credentials": "true",
                }, text="<html>ok</html>")
            return httpx.Response(404)
        _install(monkeypatch, handler)
        vulnerable = asyncio.run(SecurityScanner().scan("https://x.example"))
        assert vulnerable.score < clean_ish.score

    def test_clean_site_is_still_100(self, monkeypatch):
        r = _page(monkeypatch, headers=SECURE_HEADERS)
        assert r.score == 100 and r.grade == "A"


class TestFixVideos:
    def test_findings_carry_a_youtube_search(self, monkeypatch):
        r = _page(monkeypatch)
        assert r.findings
        for f in r.findings:
            assert f["video_url"].startswith("https://www.youtube.com/results?search_query=")

    def test_link_is_a_search_never_a_video_id(self, monkeypatch):
        """The whole point: we can't know real 11-char video ids, so we must
        never emit a /watch?v= link. A search built from our own query cannot be
        fabricated and cannot rot into someone else's video."""
        r = _page(monkeypatch)
        for f in r.findings:
            assert "/watch?v=" not in f["video_url"]
            assert "youtu.be/" not in f["video_url"]

    def test_query_is_url_encoded(self):
        url = security_scanner._youtube("fix mixed content https website tutorial")
        assert " " not in url
        assert "fix+mixed+content" in url or "fix%20mixed%20content" in url


class TestScoreDifferentiates:
    """The scanner used to give nearly every site on the internet the same score.

    HARDENING_BUDGET was applied as a hard clamp, and a missing CSP (20) plus a
    missing HSTS (10) hit the 30-point budget on its own — so every finding after
    those two was free. Since almost every site is missing at least CSP and HSTS,
    almost every site scored exactly 70/C regardless of how much else was wrong:

        missing 2 -> 70/C     missing 5           -> 70/C
        missing 3 -> 70/C     missing 6 + cookies -> 70/C

    A score that can't tell those sites apart reads as a broken tool. These pin
    the fix: strictly worse hardening must cost strictly more, while a site whose
    only faults are hardening still can't be graded below C.
    """

    def _score(self, items):
        fs = [security_scanner.Finding(sev, cat, title, "", "") for sev, cat, title in items]
        return SecurityScanner()._build_result("u", "u", fs, 10)

    TWO = [("high", "headers", "CSP"), ("medium", "headers", "HSTS")]
    THREE = TWO + [("medium", "headers", "XFO")]
    FIVE = THREE + [("low", "headers", "XCTO"), ("low", "headers", "RP")]
    SIX_PLUS_COOKIES = FIVE + [
        ("info", "headers", "PP"),
        ("medium", "cookies", "Secure"), ("medium", "cookies", "HttpOnly"),
        ("low", "cookies", "SameSite"),
    ]

    def test_more_missing_headers_scores_strictly_worse(self):
        s2 = self._score(self.TWO).score
        s3 = self._score(self.THREE).score
        s5 = self._score(self.FIVE).score
        s6 = self._score(self.SIX_PLUS_COOKIES).score
        assert s2 > s3 > s5 > s6, (s2, s3, s5, s6)

    def test_hardening_alone_never_drops_below_C(self):
        # The guarantee the budget exists for: defence-in-depth gaps alone must
        # not make a site look nearly-failing when nothing is exploitable.
        for items in (self.TWO, self.THREE, self.FIVE, self.SIX_PLUS_COOKIES):
            r = self._score(items)
            assert r.grade == "C", (items, r.score, r.grade)
            assert r.score >= 65

    def test_clean_site_is_still_perfect(self):
        r = self._score([])
        assert (r.score, r.grade) == (100, "A")

    def test_a_real_vulnerability_still_outranks_all_hardening(self):
        # An exposed .env must sink the grade to F no matter how tidy the headers.
        r = self._score([("critical", "exposure", "Exposed .env")])
        assert r.grade == "F"
        assert r.score < self._score(self.SIX_PLUS_COOKIES).score
