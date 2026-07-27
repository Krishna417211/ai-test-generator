"""
test_live_crawler.py — the multi-page crawl that grounds a suite against a
deployed site's *rendered* DOM.

The network-free tests pin the guarantees that matter and can't be flaky:
same-origin scoping, fragment normalization, the SSRF guard on the seed, and
the CrawlUnavailable fallback when no browser is present. One real-browser test
proves the crawl actually renders JS and merges anchors across routes, and skips
itself where Playwright/Chromium aren't installed — the same environments the
fallback exists for.
"""

import asyncio
import http.server
import threading

import pytest

from services import live_crawler as lc
from services import security_scanner


def _run(coro):
    return asyncio.run(coro)


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves an app shaped like every auth-guarded site: a login page that links
    nowhere, and guarded routes that bounce back to it in script."""

    # `auth=1` in the cookie is this fixture's whole session model: the login
    # form sets it, and the guarded pages check it — so a crawl that really
    # signed in sees different HTML from one that did not.
    PAGES = {
        "/": '<script src="/app.js"></script><div id=root>redirecting</div>',
        "/login.html": (
            '<form id=loginForm method=post action="/login.html">'
            '<input id=email name=email>'
            '<input id=password name=password type=password>'
            '<button data-testid=login-btn>Sign in</button></form>'
            '<script src="/app.js"></script>'
        ),
        # Guarded: bounces to the login page, exactly like requireAuth().
        "/products.html": '<div data-testid=product-grid>Shop</div>'
                          '<a href="/logout">Log out</a>',
        "/cart.html": '<div data-testid=cart-total>0.00</div>',
        # Referenced only from JS — unreachable via a[href].
        "/app.js": 'const routes = ["/products.html", "/cart.html"];'
                   'if (location.pathname === "/") location.replace("/login.html");',
    }
    GUARDED = ("/products.html", "/cart.html")
    BOUNCE = '<script>location.replace("/login.html")</script>'

    REQUESTED: list = []

    def _signed_in(self) -> bool:
        return "auth=1" in self.headers.get("Cookie", "")

    def do_GET(self):
        _Handler.REQUESTED.append(self.path)
        if self.path == "/logout":
            # Visiting this ends the session — the thing the crawl must not do.
            self.send_response(200)
            self.send_header("Set-Cookie", "auth=; Path=/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self.PAGES.get(self.path)
        if body is None:
            self.send_error(404)
            return
        if self.path in self.GUARDED and not self._signed_in():
            body = self.BOUNCE
        ctype = "application/javascript" if self.path.endswith(".js") else "text/html"
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        # The login form posts here. Only the right password sets the session.
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        ok = "password=secret" in body
        self.send_response(303)
        if ok:
            self.send_header("Set-Cookie", "auth=1; Path=/")
            self.send_header("Location", "/products.html")
        else:
            self.send_header("Location", "/login.html")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def guarded_site():
    _Handler.REQUESTED = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/"
    finally:
        srv.shutdown()
        srv.server_close()


class TestPureLogic:
    def test_same_host(self):
        assert lc._same_host("https://a.com/x", "https://a.com/y") is True
        assert lc._same_host("https://a.com/x", "https://b.com/y") is False

    def test_normalize_drops_fragment(self):
        # /x#a and /x#b are one page; the trailing slash is normalized away.
        assert lc._normalize("https://a.com/x#a") == lc._normalize("https://a.com/x#b")
        assert lc._normalize("https://a.com/x/") == "https://a.com/x"


class TestHostMatching:
    """`example.com` and `www.example.com` are one site.

    Comparing raw hostnames made the near-universal apex↔www redirect look like
    leaving the origin: the seed page was skipped, nothing was indexed, and the
    user was told their site "exposed too few elements" — blaming their app for
    a `www` redirect.
    """

    def test_www_and_apex_are_the_same_site(self):
        assert lc._same_host("https://www.example.com/", "https://example.com/")
        assert lc._same_host("https://example.com/", "https://www.example.com/")

    def test_comparison_is_case_insensitive(self):
        assert lc._same_host("https://WWW.Example.COM/x", "https://example.com/")

    def test_a_third_party_host_is_still_refused(self):
        assert not lc._same_host("https://evil.test/", "https://example.com/")

    def test_a_different_subdomain_is_not_assumed_same(self):
        """Only the seed's own redirect may re-anchor to another subdomain —
        link-following must not wander from app.x to admin.x on its own."""
        assert not lc._same_host("https://shop.example.com/", "https://example.com/")


class TestRouteLiterals:
    """Route discovery from the app's own JavaScript.

    `a[href]` alone cannot crawl a JS-navigated app, and the entry point of most
    apps worth testing is a login screen that links nowhere — so without this the
    crawl indexes one page and the suite is grounded on a fraction of the site.
    """

    def test_finds_page_literals(self):
        js = """
          const nav = `<a href="products.html">Shop</a>`;
          if (!Auth.isIn()) location.replace('login.html');
          go("/checkout.html?step=2");
        """
        found = lc._ROUTE_LITERAL_RE.findall(js)
        assert "products.html" in found
        assert "login.html" in found
        assert "/checkout.html?step=2" in found

    def test_ignores_absolute_and_non_page_literals(self):
        # An off-site page is not this app's route (the crawl is same-origin, and
        # the host check would drop it anyway — but not matching keeps the
        # candidate list honest). Assets and API paths are not pages at all.
        js = """
          fetch("https://cdn.other.com/vendor.html");
          import "./styles.css"; track("/api/v1/events"); cls("html-wrapper");
        """
        found = lc._ROUTE_LITERAL_RE.findall(js)
        assert not any(f.startswith("http") for f in found)
        assert "./styles.css" not in found
        assert "/api/v1/events" not in found


class TestGuards:
    def test_private_seed_is_refused_before_any_browser(self, monkeypatch):
        # SSRF/scope guard runs first: a private seed raises, never launches.
        monkeypatch.setattr(
            security_scanner.socket, "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))],
        )
        with pytest.raises(security_scanner.ScanError):
            _run(lc.crawl("http://internal.local"))

    def test_missing_playwright_raises_crawl_unavailable(self, monkeypatch):
        # A public host passes the guard; with Playwright import failing, the
        # crawl must signal CrawlUnavailable so callers fall back to Mode 1.
        monkeypatch.setattr(
            security_scanner.socket, "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
        )
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name.startswith("playwright"):
                raise ImportError("No module named 'playwright'")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(lc.CrawlUnavailable):
            _run(lc.crawl("https://app.example"))


class TestRealBrowser:
    """Proves the crawl really renders JS and merges anchors across routes.
    Skipped where the browser isn't present — exactly the environments the
    CrawlUnavailable fallback exists for."""

    def test_renders_js_and_indexes_anchors(self, monkeypatch):
        pytest.importorskip("playwright")
        # A data: URL whose real anchors only exist after JS runs. A static fetch
        # would see an empty #root; the crawl must see the injected button.
        seed = (
            "data:text/html,"
            "<div id=root></div><script>"
            "document.getElementById('root').innerHTML="
            "'<button data-testid=go>Go</button>'"
            "</script>"
        )
        # This test is about rendering, not scope: stub the SSRF/scope guard to
        # pass the data: seed through unchanged (it has no resolvable host).
        monkeypatch.setattr(security_scanner, "validate_target", lambda url: url)
        try:
            res = _run(lc.crawl(seed, max_pages=1))
        except lc.CrawlUnavailable:
            pytest.skip("no browser available in this environment")
        from services.grounding import Anchor, KIND_TESTID
        assert res.n_pages == 1
        assert Anchor(KIND_TESTID, "go") in res.index.anchors

    def test_seed_redirect_to_another_host_is_followed(self, monkeypatch, guarded_site):
        """A site whose canonical home is a different host must still be crawled.

        `127.0.0.1` and `localhost` are distinct hostnames, so this reproduces
        the apex→www / apex→shop case: refusing the seed's own redirect indexed
        nothing and told the user their site was empty.
        """
        pytest.importorskip("playwright")
        monkeypatch.setattr(security_scanner, "validate_target", lambda url: url)
        port = guarded_site.rstrip("/").rsplit(":", 1)[1]
        # Enter via 127.0.0.1; the app's own script sends us to localhost.
        seed = f"http://127.0.0.1:{port}/"
        _Handler.PAGES["/"] = (
            f'<script>location.replace("http://localhost:{port}/login.html")</script>'
        )
        try:
            res = _run(lc.crawl(seed, max_pages=5))
        except lc.CrawlUnavailable:
            pytest.skip("no browser available in this environment")
        finally:
            _Handler.PAGES["/"] = ('<script src="/app.js"></script>'
                                   '<div id=root>redirecting</div>')

        from services.grounding import Anchor, KIND_TESTID
        assert res.n_pages >= 1, "seed redirect to another host was refused"
        assert Anchor(KIND_TESTID, "login-btn") in res.index.anchors

    def test_auth_guarded_site_is_reported_as_one_page(self, monkeypatch, guarded_site):
        """Routes that bounce to the login screen are not separate pages.

        The crawl reached them only because the route literals in app.js were
        discovered (no a[href] on the login page points anywhere) — so this pins
        both halves: script-derived discovery finds the guarded routes, and the
        redirect dedupe stops the one login page they all land on from being
        counted, and its anchors credited, once per route.
        """
        pytest.importorskip("playwright")
        monkeypatch.setattr(security_scanner, "validate_target", lambda url: url)
        try:
            res = _run(lc.crawl(guarded_site, max_pages=10))
        except lc.CrawlUnavailable:
            pytest.skip("no browser available in this environment")

        from services.grounding import Anchor, KIND_TESTID
        # Discovery worked: the guarded routes exist ONLY as literals in app.js,
        # so requesting them proves the crawl read the app's JavaScript. Without
        # that this assertion passes vacuously — the crawl would simply have
        # stopped at the login page having found nothing to follow.
        assert "/products.html" in _Handler.REQUESTED
        assert "/cart.html" in _Handler.REQUESTED
        # …and dedupe worked: they all bounced to the one login page, which is
        # counted (and its anchors credited) exactly once.
        assert [p.path for p in res.pages] == ["/login.html"], (
            f"expected only the login page to be indexed, got {[p.path for p in res.pages]}"
        )
        assert Anchor(KIND_TESTID, "login-btn") in res.index.anchors


class TestAuthenticatedCrawl:
    """Signing in before the walk — the difference between grounding a suite on a
    login form and grounding it on the app."""

    def _login(self, password="secret"):
        return lc.LoginSpec(url="/login.html", username="a@b.test", password=password)

    def test_signing_in_reaches_the_guarded_pages(self, monkeypatch, guarded_site):
        pytest.importorskip("playwright")
        monkeypatch.setattr(security_scanner, "validate_target", lambda url: url)
        try:
            res = _run(lc.crawl(guarded_site, max_pages=10, login=self._login()))
        except lc.CrawlUnavailable:
            pytest.skip("no browser available in this environment")

        from services.grounding import Anchor, KIND_TESTID
        paths = {p.path for p in res.pages}
        assert "/products.html" in paths and "/cart.html" in paths
        # Anchors that only exist behind the wall — the anonymous crawl above
        # cannot produce these, so they are the proof the sign-in took.
        assert Anchor(KIND_TESTID, "product-grid") in res.index.anchors
        assert Anchor(KIND_TESTID, "cart-total") in res.index.anchors

    def test_wrong_password_fails_loudly(self, monkeypatch, guarded_site):
        """Never a silent downgrade to an anonymous crawl.

        The caller supplied credentials because the content is behind them; a
        suite quietly grounded on the login page would be the wrong output
        reported as success.
        """
        pytest.importorskip("playwright")
        monkeypatch.setattr(security_scanner, "validate_target", lambda url: url)
        try:
            with pytest.raises(lc.CrawlLoginFailed):
                _run(lc.crawl(guarded_site, max_pages=5, login=self._login("wrong")))
        except lc.CrawlUnavailable:
            pytest.skip("no browser available in this environment")

    def test_crawl_never_follows_a_logout_link(self, monkeypatch, guarded_site):
        """products.html links to /logout. Following it would end the session and
        spend the rest of the budget re-rendering the login page."""
        pytest.importorskip("playwright")
        monkeypatch.setattr(security_scanner, "validate_target", lambda url: url)
        try:
            res = _run(lc.crawl(guarded_site, max_pages=10, login=self._login()))
        except lc.CrawlUnavailable:
            pytest.skip("no browser available in this environment")
        assert "/logout" not in _Handler.REQUESTED
        # The session survived the whole walk: guarded anchors are still being
        # collected at the end, which is only true if we never signed ourselves
        # out. (The login page itself may legitimately be indexed — this
        # fixture's "/" bounces there unconditionally, as some apps do — so its
        # presence is not the signal; the guarded content is.)
        from services.grounding import Anchor, KIND_TESTID
        assert Anchor(KIND_TESTID, "cart-total") in res.index.anchors

    def test_password_is_not_in_the_repr(self):
        spec = lc.LoginSpec(url="/login", username="a@b.test", password="hunter2")
        assert "hunter2" not in repr(spec)
        assert "a@b.test" in repr(spec)      # the username is not the secret
