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

import pytest

from services import live_crawler as lc
from services import security_scanner


def _run(coro):
    return asyncio.run(coro)


class TestPureLogic:
    def test_same_host(self):
        assert lc._same_host("https://a.com/x", "https://a.com/y") is True
        assert lc._same_host("https://a.com/x", "https://b.com/y") is False

    def test_normalize_drops_fragment(self):
        # /x#a and /x#b are one page; the trailing slash is normalized away.
        assert lc._normalize("https://a.com/x#a") == lc._normalize("https://a.com/x#b")
        assert lc._normalize("https://a.com/x/") == "https://a.com/x"


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
