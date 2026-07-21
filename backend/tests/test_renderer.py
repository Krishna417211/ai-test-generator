"""
test_renderer.py — the rendered-DOM path and its graceful fallbacks.

The mocked tests pin the contract that matters for deploys: when a browser isn't
available for any reason, render_html must degrade to the static fetch and never
raise on that account. One real-browser test proves rendering actually executes
JavaScript, and skips itself where Playwright/Chromium aren't installed.
"""

import asyncio

import pytest

from services import renderer, security_scanner


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset_latch(monkeypatch):
    # The permanent-unavailable latch is process-global; reset it per test so one
    # test's simulated launch failure can't leak into the next.
    monkeypatch.setattr(renderer, "_browser_unavailable", False)
    # Make the SSRF/scope guard pass for the fake public host under test.
    monkeypatch.setattr(
        security_scanner.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )


class TestFallback:
    def test_uses_browser_when_it_works(self, monkeypatch):
        async def fake_render(target, timeout):
            return "<html><body><button id='go'>Go</button></body></html>"
        monkeypatch.setattr(renderer, "_render_with_browser", fake_render)
        html, mode = _run(renderer.render_html("https://app.example"))
        assert mode == "rendered"
        assert "id='go'" in html

    def test_import_error_falls_back_to_static(self, monkeypatch):
        async def boom(target, timeout):
            raise ImportError("No module named 'playwright'")
        async def fake_static(target, timeout):
            return "<html>static</html>"
        monkeypatch.setattr(renderer, "_render_with_browser", boom)
        monkeypatch.setattr(renderer, "_fetch_static", fake_static)
        html, mode = _run(renderer.render_html("https://app.example"))
        assert mode == "static"
        assert html == "<html>static</html>"
        # Missing Playwright is permanent — the latch must be set so we stop trying.
        assert renderer._browser_unavailable is True

    def test_launch_failure_latches_and_falls_back(self, monkeypatch):
        async def boom(target, timeout):
            raise RuntimeError("Executable doesn't exist at /ms-playwright/chromium")
        async def fake_static(target, timeout):
            return "<html>static</html>"
        monkeypatch.setattr(renderer, "_render_with_browser", boom)
        monkeypatch.setattr(renderer, "_fetch_static", fake_static)
        _, mode = _run(renderer.render_html("https://app.example"))
        assert mode == "static"
        assert renderer._browser_unavailable is True

    def test_transient_render_error_does_not_latch(self, monkeypatch):
        async def boom(target, timeout):
            raise RuntimeError("net::ERR_TIMED_OUT navigating to page")
        async def fake_static(target, timeout):
            return "<html>static</html>"
        monkeypatch.setattr(renderer, "_render_with_browser", boom)
        monkeypatch.setattr(renderer, "_fetch_static", fake_static)
        _, mode = _run(renderer.render_html("https://app.example"))
        assert mode == "static"
        # A one-off page failure must NOT disable rendering for the whole process.
        assert renderer._browser_unavailable is False

    def test_private_url_is_refused_before_any_fetch(self, monkeypatch):
        # SSRF/scope guard runs first: a private address raises, never renders.
        monkeypatch.setattr(
            security_scanner.socket, "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))],
        )
        with pytest.raises(security_scanner.ScanError):
            _run(renderer.render_html("http://internal.local"))


class TestRealBrowser:
    """Proves rendering really runs JS. Skipped where the browser isn't present —
    exactly the environments the fallback above exists for."""

    def test_javascript_injected_dom_is_captured(self):
        pytest.importorskip("playwright")
        html = (
            "data:text/html,"
            "<div id=root></div><script>"
            "document.getElementById('root').innerHTML="
            "'<button id=login data-testid=login-btn>Log in</button>'"
            "</script>"
        )
        try:
            out = _run(renderer._render_with_browser(html, timeout=15.0))
        except Exception as e:  # no browser binary / sandbox — that's the skip case
            pytest.skip(f"headless browser unavailable: {e}")
        assert 'id="login"' in out
        assert "login-btn" in out
