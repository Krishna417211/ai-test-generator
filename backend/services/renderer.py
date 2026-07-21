"""
renderer.py — fetch a page's *rendered* HTML, so DOM grounding works on the
JavaScript apps most real products actually are.

The problem this solves: grounding.fetch_dom reads static HTML and never runs it.
For a React/Vue/Angular/Next/Svelte app the served body is a mount element plus
<script> tags — the real DOM (buttons, inputs, ids, test-ids) only exists after
the JavaScript runs. So DOM grounding saw a near-empty shell and honestly
declined (grounding.dom_index_is_useful), and the product could only ever verify
selectors against source for exactly the apps whose selectors most need a live
check.

render_html runs the page in a headless Chromium and returns the DOM the browser
actually builds. Two properties keep it safe and deployable:

  • **SSRF-safe.** The initial URL clears the scanner's scope + private-address
    guard (validate_target), and a request interceptor re-checks *every* request
    the page makes — navigations, redirects, subresources — aborting any whose
    host resolves to a private/loopback/link-local address. A page cannot use a
    redirect or an <img> to make the browser reach an internal service any more
    than the httpx path can.

  • **Optional, with graceful fallback.** Playwright and a browser binary are not
    required to run the product. If the import is missing, or the browser can't
    launch, or a page fails to render, render_html falls back to the same static
    fetch grounding used before and reports mode="static". A deploy without a
    browser behaves exactly as it did — it just never gets the rendered upgrade.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse

import httpx

from services import security_scanner as scanner

logger = logging.getLogger(__name__)

# A launch failure (no browser binary, missing system libs) is permanent for the
# life of the process, so we latch it once and stop paying the launch cost on
# every subsequent scan. A per-page timeout is NOT latched — one slow site must
# not disable rendering for every site after it.
_browser_unavailable = False

# Chromium needs these in most container/CI sandboxes; harmless elsewhere.
_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


def _looks_like_launch_failure(err: Exception) -> bool:
    msg = str(err).lower()
    return any(s in msg for s in (
        "executable doesn't exist", "playwright install", "look for the browser",
        "failed to launch", "browsertype.launch",
    ))


async def _guard_route(route) -> None:
    """Abort any request the page makes to a non-public host (SSRF via the browser)."""
    host = urlparse(route.request.url).hostname
    if host:
        try:
            scanner._assert_public_host(host)
        except scanner.ScanError:
            await route.abort()
            return
    await route.continue_()


async def _render_with_browser(target: str, timeout: float) -> str:
    from playwright.async_api import async_playwright  # lazy: keeps it optional

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=_LAUNCH_ARGS)
        try:
            context = await browser.new_context(user_agent=scanner.USER_AGENT)
            page = await context.new_page()
            await page.route("**/*", _guard_route)
            await page.goto(target, wait_until="domcontentloaded",
                            timeout=timeout * 1000)
            # Best-effort settle for late client rendering; a page that never goes
            # idle (polling, websockets) must not fail the whole render, so the
            # DOM we already have is good enough if this times out.
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            return await page.content()
        finally:
            await browser.close()


async def _fetch_static(target: str, timeout: float) -> str:
    """The pre-existing behaviour: SSRF-guarded static fetch, no JS executed."""
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True,
        headers={"User-Agent": scanner.USER_AGENT},
        event_hooks={"response": [scanner._redirect_guard]},
    ) as client:
        resp = await client.get(target)
        return resp.text


async def render_html(url: str, *, timeout: float = 15.0) -> tuple[str, str]:
    """Return (html, mode) for `url`. mode is "rendered" (browser ran the page)
    or "static" (fell back). Raises ScanError for a private/out-of-scope URL —
    the same refusal the scanner and the static path already give.
    """
    global _browser_unavailable
    target = scanner.validate_target(url)  # scope + DNS/SSRF; raises ScanError

    if not _browser_unavailable:
        try:
            html = await _render_with_browser(target, timeout)
            return html, "rendered"
        except ImportError:
            _browser_unavailable = True
            logger.info("Playwright not installed — DOM rendering falls back to "
                        "static fetch.")
        except Exception as e:
            if _looks_like_launch_failure(e):
                _browser_unavailable = True
                logger.info(f"Headless browser unavailable ({e}) — DOM rendering "
                            "falls back to static fetch for this process.")
            else:
                logger.warning(f"Browser render failed for {target}: {e} — using "
                               "static fetch.")

    return await _fetch_static(target, timeout), "static"
