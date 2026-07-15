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
        expected = 2 + len(SECURITY_HEADERS) + 3 + len(SENSITIVE_PATHS) + 4 + 1 + 2
        assert r.checks_run == expected


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
