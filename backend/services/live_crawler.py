"""
live_crawler.py — render *and* crawl a deployed site to build a real DOM index.

The existing DOM grounding (services/grounding.py) fetches ONE page's *static*
HTML and never runs JavaScript (see `fetch_dom`). For a client-rendered app the
served HTML is a near-empty shell — `#root` and a few `<script>` tags — so
`build_dom_index` sees almost nothing and grounding declines (the whole
`MIN_USEFUL_DOM_ANCHORS` / `describe_thin_dom` apology exists for exactly this).

This module closes that gap. It drives a headless browser so the page's own
JavaScript runs, follows the app's in-site links, and merges every rendered page
into a single `GroundIndex` — the same shape the writer/heal loop already
consumes. Nothing downstream changes: a richer, JS-rendered, multi-page index is
still just a `GroundIndex(source="dom")`.

Design choices that matter:

  • **Reuses `grounding.build_dom_index`** for extraction, so "what an anchor is"
    stays defined in exactly one place — this module only *supplies rendered HTML*
    it could not get before, page by page.
  • **Reuses the scanner's SSRF guard** (`validate_target`) on the seed *and* on
    every discovered link before visiting it, so a crawl cannot be pointed at an
    internal address any more than static grounding can.
  • **Same-origin only.** It follows links within the seed host and ignores the
    rest — a test suite grounds against the site under test, not the web.

It degrades safely: if Playwright is not installed or the browser cannot launch,
`crawl` raises `CrawlUnavailable`, and callers fall back to static grounding.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urldefrag

from services.grounding import GroundIndex
from services import grounding
from services import security_scanner as scanner

logger = logging.getLogger(__name__)


class CrawlUnavailable(RuntimeError):
    """Raised when a live crawl cannot run (no Playwright / no browser).

    Distinct from a crawl that runs but finds a thin site: this means the crawler
    itself is unavailable, so the caller should fall back to static grounding.
    """


@dataclass
class CrawledPage:
    url: str
    path: str
    n_anchors: int


@dataclass
class CrawlResult:
    """The merged ground truth of a rendered, multi-page crawl."""
    seed: str
    index: GroundIndex                                   # merged anchors, source="dom"
    pages: list[CrawledPage] = field(default_factory=list)
    # Internal routes discovered but not visited (hit the page cap).
    unvisited: list[str] = field(default_factory=list)

    @property
    def n_pages(self) -> int:
        return len(self.pages)

    @property
    def n_anchors(self) -> int:
        return len(self.index.anchors)


def _same_host(a: str, b: str) -> bool:
    return urlparse(a).hostname == urlparse(b).hostname


def _normalize(url: str) -> str:
    """Drop the fragment; keep path+query. `/x#a` and `/x#b` are one page."""
    return urldefrag(url)[0].rstrip("/") or urldefrag(url)[0]


async def crawl(
    seed_url: str,
    *,
    max_pages: int = 20,
    wait_until: str = "networkidle",
    nav_timeout_ms: int = 15_000,
) -> CrawlResult:
    """Render the seed, follow same-origin links, and merge every page's rendered
    DOM into one `GroundIndex`.

    Raises `CrawlUnavailable` if the browser can't be driven, and `ScanError`
    (from the scanner) if the seed is private/out-of-scope.
    """
    # SSRF/scope guard on the seed — same gate static grounding passes through.
    safe_seed = scanner.validate_target(seed_url)

    try:
        from playwright.async_api import async_playwright
    except Exception as e:  # pragma: no cover - import guard
        raise CrawlUnavailable(f"Playwright is not installed: {e}") from e

    merged = GroundIndex(source="dom")
    pages: list[CrawledPage] = []
    seen: set[str] = set()
    queue: list[str] = [_normalize(safe_seed)]
    unvisited: list[str] = []

    try:
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
            except Exception as e:
                raise CrawlUnavailable(f"Could not launch a browser: {e}") from e
            context = await browser.new_context(user_agent=scanner.USER_AGENT)
            page = await context.new_page()
            page.set_default_navigation_timeout(nav_timeout_ms)

            while queue and len(pages) < max_pages:
                url = queue.pop(0)
                if url in seen:
                    continue
                seen.add(url)

                # Every hop re-passes the SSRF guard: a link (or a redirect the
                # app injected) can point anywhere, so it is validated exactly
                # like the seed before we navigate to it.
                try:
                    scanner.validate_target(url)
                except scanner.ScanError:
                    continue

                try:
                    await page.goto(url, wait_until=wait_until)
                    # A same-host link can still *redirect* off-origin — an OAuth
                    # entry like /api/auth/github/login lands on github.com. The
                    # pre-nav guard cannot see that; only the final URL can. If we
                    # left the seed host, this is a third party's page — indexing
                    # it would poison the ground truth with their anchors, so skip
                    # it and don't harvest its links.
                    if not _same_host(page.url, safe_seed):
                        logger.info("crawl: %s redirected off-origin to %s — skipped",
                                    url, page.url)
                        continue
                    html = await page.content()
                    hrefs = await page.eval_on_selector_all(
                        "a[href]", "els => els.map(e => e.getAttribute('href'))"
                    )
                except Exception as e:
                    logger.warning("crawl: skipping %s (%s)", url, e)
                    continue

                idx = grounding.build_dom_index(html)
                merged.anchors |= idx.anchors
                pages.append(CrawledPage(url=url, path=urlparse(url).path or "/",
                                         n_anchors=len(idx.anchors)))

                for href in hrefs:
                    if not href:
                        continue
                    absu = _normalize(urljoin(url, href))
                    if not absu.startswith(("http://", "https://")):
                        continue
                    if not _same_host(absu, safe_seed):
                        continue
                    if absu in seen or absu in queue:
                        continue
                    if len(pages) + len(queue) >= max_pages:
                        unvisited.append(absu)
                    else:
                        queue.append(absu)

            await context.close()
            await browser.close()
    except CrawlUnavailable:
        raise
    except Exception as e:
        raise CrawlUnavailable(f"Crawl failed to run: {e}") from e

    return CrawlResult(seed=safe_seed, index=merged, pages=pages,
                       unvisited=sorted(set(unvisited)))
