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
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urldefrag, parse_qsl

from services.grounding import GroundIndex
from services import grounding
from services import security_scanner as scanner

logger = logging.getLogger(__name__)

# Route-shaped string literals inside JavaScript: "products.html", '/cart.html',
# `checkout.html?step=2`. Anchored on the .html extension because that is what
# makes a literal a *page* rather than an asset, an API path or a CSS class.
#
# This exists because `a[href]` alone cannot crawl a JS-navigated app, which is
# most of them. A login screen is the common case: it is the entry point of
# nearly every app worth testing, it links nowhere, and its navigation happens in
# script (`location.replace('products.html')`) or in markup the script injects
# only once you are past it. Discovering nothing but the login page is how a
# crawl-grounded suite ends up testing one form and inventing the rest.
_ROUTE_LITERAL_RE = re.compile(
    r"""["'`](?!https?://)((?:\.{0,2}/)?[\w][\w\-./]*\.html(?:\?[^"'`\s<>]*)?)["'`]"""
)

# Same-origin scripts whose text is scanned for the literals above. Capped, and
# each URL is fetched at most once per crawl: an app's bundle is referenced by
# every page, and re-reading it per page would multiply the crawl's cost for
# identical results.
_MAX_SCRIPT_FETCHES = 12
_MAX_SCRIPT_BYTES = 600_000

# How many pages of the same *shape* (same path, same query keys) to render.
# A catalogue's product.html?id=1..500 are one template: the second adds a little
# (ids that vary per item), the fiftieth adds nothing but spends the page budget
# that checkout.html needed. Measured on a 12-product demo store: without this
# the crawl rendered 13 product pages and never reached checkout at all.
_MAX_PER_SHAPE = 2

# A route literal lifted out of a JS template — "product.html?id=${p.id}" — is a
# pattern, not a URL. Following it fetches a 404 and teaches the model a selector
# for a page that does not exist.
_UNRESOLVED_TEMPLATE_RE = re.compile(r"\$\{|<%|\{\{")

# Links that would end the session. A crawl that signs in and then follows the
# "Log out" link in the nav spends the rest of its budget re-rendering the login
# page — the exact failure signing in was meant to fix, and a confusing one
# because the crawl still "worked". Never visited, in any mode: there is nothing
# behind a logout worth grounding a suite on.
_SESSION_ENDING_RE = re.compile(r"(log|sign)[-_]?out|/logout\b|/signout\b", re.IGNORECASE)

# Field guesses for a login form, best first. Only used for the parts the caller
# did not name explicitly.
_USER_SELECTORS = (
    "input[type=email]", "input[name=email]", "#email",
    "input[name=username]", "#username", "input[type=text][name*=user i]",
)
_PASS_SELECTORS = ("input[type=password]", "input[name=password]", "#password")
_SUBMIT_SELECTORS = (
    "[data-testid*=login i]", "[data-testid*=signin i]", "button[type=submit]",
    "input[type=submit]", "form button",
)


class CrawlUnavailable(RuntimeError):
    """Raised when a live crawl cannot run (no Playwright / no browser).

    Distinct from a crawl that runs but finds a thin site: this means the crawler
    itself is unavailable, so the caller should fall back to static grounding.
    """


class CrawlLoginFailed(RuntimeError):
    """Raised when a sign-in step was requested but did not take.

    Deliberately fatal rather than a silent downgrade to an anonymous crawl. A
    caller that asked to sign in did so because the app's content is behind the
    wall; crawling the login page instead would ground the whole suite on one
    form while reporting success, and the user would only find out when every
    generated test failed.
    """


@dataclass
class LoginSpec:
    """How to get past an app's sign-in screen before crawling it.

    Selectors are optional: with none given the common shapes are tried (see
    `_USER_SELECTORS`). Naming them explicitly is the escape hatch for a form the
    guesses miss.

    Credentials live in this object for the duration of one crawl and are never
    logged or persisted — see the redaction in `main.analyze_crawl`.
    """
    url: str                       # the sign-in page (absolute, or relative to the seed)
    username: str
    password: str
    username_selector: str = ""
    password_selector: str = ""
    submit_selector: str = ""

    def __repr__(self) -> str:                      # never let a password reach a log
        return f"LoginSpec(url={self.url!r}, username={self.username!r}, password=***)"


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


def _canonical_host(url: str) -> str:
    """Hostname with a leading `www.` removed.

    `example.com` and `www.example.com` are the same site by universal
    convention, and most sites redirect one to the other. Comparing raw
    hostnames made that redirect look like leaving the origin, so the seed page
    was skipped, nothing was indexed, and the user was told their site "exposed
    too few elements" — blaming their app for a `www` redirect.
    """
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _same_host(a: str, b: str) -> bool:
    return _canonical_host(a) == _canonical_host(b)


def _normalize(url: str) -> str:
    """Drop the fragment; keep path+query. `/x#a` and `/x#b` are one page."""
    return urldefrag(url)[0].rstrip("/") or urldefrag(url)[0]


def _shape(url: str) -> str:
    """The template a URL is an instance of: its path plus its query *keys*.

    `product.html?id=1` and `product.html?id=2` share a shape and are rendered
    from one template, so the crawl only needs a couple of them (see
    `_MAX_PER_SHAPE`).
    """
    parsed = urlparse(url)
    keys = sorted(k for k, _ in parse_qsl(parsed.query))
    return f"{parsed.path}?{','.join(keys)}"


async def _script_routes(page, context, seed: str, fetched: dict[str, str]) -> list[str]:
    """Route literals found in the page's own JavaScript.

    Reads inline `<script>` text directly and fetches same-origin `src=` scripts
    through the browser's request context (so cookies and headers match the
    crawl). `fetched` is the per-crawl cache: a bundle referenced from every page
    is read once.

    Best-effort by construction — a script that will not load yields no routes
    and never fails the crawl.
    """
    try:
        found = await page.evaluate(
            """() => {
                const out = {inline: [], src: []};
                for (const s of document.querySelectorAll('script')) {
                    if (s.src) out.src.push(s.src);
                    else if (s.textContent) out.inline.push(s.textContent);
                }
                return out;
            }"""
        )
    except Exception as e:
        logger.debug("crawl: could not read scripts on %s (%s)", page.url, e)
        return []

    texts: list[str] = list(found.get("inline") or [])
    for src in (found.get("src") or []):
        if not _same_host(src, seed):
            continue                       # a CDN's bundle is not this app's routes
        if src in fetched:
            texts.append(fetched[src])
            continue
        if len(fetched) >= _MAX_SCRIPT_FETCHES:
            continue
        body = ""
        try:
            scanner.validate_target(src)   # same SSRF gate as every navigation
            resp = await context.request.get(src)
            if resp.ok:
                body = (await resp.text())[:_MAX_SCRIPT_BYTES]
        except Exception as e:
            logger.debug("crawl: could not fetch script %s (%s)", src, e)
        fetched[src] = body
        if body:
            texts.append(body)

    routes: list[str] = []
    for text in texts:
        for m in _ROUTE_LITERAL_RE.finditer(text):
            routes.append(m.group(1))
    return routes


async def _first_present(page, explicit: str, candidates: tuple[str, ...]):
    """The caller's selector if it matches, else the first candidate that does."""
    for sel in ([explicit] if explicit else []) + list(candidates):
        if not sel:
            continue
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                return loc, sel
        except Exception:
            continue
    return None, ""


async def _sign_in(page, login: LoginSpec, seed: str, nav_timeout_ms: int) -> str:
    """Drive the app's own sign-in form, and prove it worked.

    Returns the URL the app landed on after signing in — the natural place to
    start the crawl, since it is where the app itself sends a signed-in user.

    Nothing here is special-cased to one site: it fills the fields it can find,
    submits, and then checks the app's *own* verdict — did we leave the login
    page. That check is the whole value. Without it a wrong password produces a
    crawl of the login screen that reports success.
    """
    login_url = scanner.validate_target(urljoin(seed, login.url))
    await page.goto(login_url, wait_until="domcontentloaded")

    user_loc, user_sel = await _first_present(page, login.username_selector, _USER_SELECTORS)
    pass_loc, pass_sel = await _first_present(page, login.password_selector, _PASS_SELECTORS)
    if user_loc is None or pass_loc is None:
        raise CrawlLoginFailed(
            "Could not find the sign-in fields on that page. Check the login URL, "
            "or name the field selectors explicitly."
        )
    await user_loc.fill(login.username)
    await pass_loc.fill(login.password)
    logger.info("crawl: signing in at %s (user=%s, pass=%s)", login_url, user_sel, pass_sel)

    submit_loc, _ = await _first_present(page, login.submit_selector, _SUBMIT_SELECTORS)
    before = _normalize(page.url)
    try:
        if submit_loc is not None:
            await submit_loc.click()
        else:
            await pass_loc.press("Enter")     # a form with no button still submits
        # Most apps navigate; a client-rendered one may just swap the view, so a
        # timeout here is not itself a failure — the landing check below decides.
        await page.wait_for_load_state("networkidle", timeout=nav_timeout_ms)
    except Exception as e:
        logger.info("crawl: no navigation settled after sign-in (%s)", e)

    if _normalize(page.url) == before:
        # Still on the login page: the app rejected us. Its own error message is
        # the most useful thing we can hand back, so look for one.
        detail = ""
        try:
            for sel in ("[role=alert]", "#error", ".error"):
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    detail = (await loc.inner_text()).strip()[:200]
                    if detail:
                        break
        except Exception:
            pass
        raise CrawlLoginFailed(
            "Signing in did not work — the site stayed on the login page"
            + (f': "{detail}"' if detail else ".")
            + " Check the credentials."
        )

    logger.info("crawl: signed in, landed on %s", page.url)
    return page.url


async def _discover(page, context, url: str, seed: str, fetched: dict[str, str]) -> list[str]:
    """Every same-origin page this one points at, from markup *and* script.

    Markup first (a real link is the strongest signal a route exists), then the
    literals in the app's JavaScript for everything a rendered page cannot show.
    """
    hrefs: list[str] = []
    try:
        hrefs = await page.eval_on_selector_all(
            "a[href], area[href], [data-href], form[action]",
            """els => els.map(e => e.getAttribute('href')
                              || e.getAttribute('data-href')
                              || e.getAttribute('action'))""",
        )
    except Exception as e:
        logger.debug("crawl: could not read links on %s (%s)", url, e)

    return [h for h in hrefs if h] + await _script_routes(page, context, seed, fetched)


async def crawl(
    seed_url: str,
    *,
    max_pages: int = 20,
    wait_until: str = "networkidle",
    nav_timeout_ms: int = 15_000,
    login: LoginSpec | None = None,
) -> CrawlResult:
    """Render the seed, follow same-origin links, and merge every page's rendered
    DOM into one `GroundIndex`.

    With `login`, the app's sign-in form is driven first and the rest of the
    crawl runs in that authenticated browser context. This is usually the
    difference between grounding a suite on a login screen and grounding it on
    the app: a guarded route bounces an anonymous crawler straight back to the
    wall, so without it the index contains one page no matter how many routes the
    site has.

    Raises `CrawlUnavailable` if the browser can't be driven, `CrawlLoginFailed`
    if a requested sign-in didn't take, and `ScanError` (from the scanner) if the
    seed is private/out-of-scope.
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
    script_cache: dict[str, str] = {}      # script url → body, read once per crawl
    shape_counts: dict[str, int] = {}      # route template → pages already rendered

    try:
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
            except Exception as e:
                raise CrawlUnavailable(f"Could not launch a browser: {e}") from e
            context = await browser.new_context(user_agent=scanner.USER_AGENT)
            page = await context.new_page()
            page.set_default_navigation_timeout(nav_timeout_ms)

            if login is not None:
                # Seed the queue with where the app itself put us after sign-in.
                # That page is the app's real entry point for a signed-in user —
                # usually a dashboard or catalogue that links to everything else,
                # whereas the requested seed may still be the public landing page.
                landed = _normalize(await _sign_in(page, login, safe_seed, nav_timeout_ms))
                if _same_host(landed, safe_seed) and landed not in queue:
                    queue.insert(0, landed)

            while queue and len(pages) < max_pages:
                url = queue.pop(0)
                if url in seen:
                    continue
                seen.add(url)

                # Enough instances of this template already. Not recorded as
                # `unvisited` — that list is for routes the budget ran out on,
                # and these were a deliberate skip.
                shape = _shape(url)
                if shape_counts.get(shape, 0) >= _MAX_PER_SHAPE:
                    continue

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
                        # The FIRST page is special: the user handed us this URL,
                        # and wherever the site redirects it is by definition the
                        # site's canonical home (example.com → shop.example.com,
                        # a country/locale host, a marketing domain → the app).
                        # Refusing that indexed nothing and blamed the user's
                        # site. Re-anchor to where it actually lives — but only
                        # through the same scope/SSRF gate, which is what still
                        # stops a redirect to a third party (github.com and
                        # friends are refused by _assert_in_scope).
                        if not pages:
                            try:
                                safe_seed = scanner.validate_target(page.url)
                                logger.info("crawl: seed redirected to %s — following it "
                                            "as the site's canonical origin", safe_seed)
                            except scanner.ScanError as e:
                                logger.info("crawl: seed redirected off-site to %s (%s) "
                                            "— skipped", page.url, e)
                                continue
                        else:
                            logger.info("crawl: %s redirected off-origin to %s — skipped",
                                        url, page.url)
                            continue
                    # Where we ACTUALLY landed. An auth-guarded app bounces every
                    # route to its login screen, so without this the crawl indexes
                    # that one screen once per guarded route and reports "9 routes
                    # rendered" for a site it only ever saw one page of — inflating
                    # the anchor provenance the whole suite is grounded on, and
                    # telling the user a number that is not true.
                    final = _normalize(page.url)
                    if final != url:
                        if final in seen:
                            logger.info("crawl: %s redirected to already-seen %s — skipped",
                                        url, final)
                            continue
                        seen.add(final)
                    html = await page.content()
                    hrefs = await _discover(page, context, url, safe_seed, script_cache)
                except Exception as e:
                    logger.warning("crawl: skipping %s (%s)", url, e)
                    continue

                idx = grounding.build_dom_index(html)
                merged.anchors |= idx.anchors
                shape_counts[_shape(final)] = shape_counts.get(_shape(final), 0) + 1
                # Recorded under the url that was actually rendered, not the one
                # requested — the report must name the page these anchors came from.
                pages.append(CrawledPage(url=final, path=urlparse(final).path or "/",
                                         n_anchors=len(idx.anchors)))

                # Relative links resolve against the page's FINAL url, not the one
                # queued. Two things break otherwise: a redirect (the queued url
                # is no longer the document's base), and `_normalize` having
                # stripped the trailing slash — urljoin treats the last segment of
                # a slashless url as a *file*, so "products.html" off
                # "example.com/my-app" resolves to "example.com/products.html"
                # and every discovered route 404s on a sub-path deployment.
                base = page.url
                for href in hrefs:
                    if not href:
                        continue
                    absu = _normalize(urljoin(base, href))
                    if not absu.startswith(("http://", "https://")):
                        continue
                    if not _same_host(absu, safe_seed):
                        continue
                    if absu in seen or absu in queue:
                        continue
                    if _SESSION_ENDING_RE.search(absu):
                        continue        # following this would sign the crawl out
                    if _UNRESOLVED_TEMPLATE_RE.search(absu):
                        continue        # a JS template, not a real route
                    if shape_counts.get(_shape(absu), 0) >= _MAX_PER_SHAPE:
                        continue        # already have enough of this template
                    if len(pages) + len(queue) >= max_pages:
                        unvisited.append(absu)
                    else:
                        queue.append(absu)

            await context.close()
            await browser.close()
    except (CrawlUnavailable, CrawlLoginFailed, scanner.ScanError):
        # Each of these is a specific, actionable answer — no browser, bad
        # credentials, out-of-scope target. Folding them into the generic
        # "crawl failed to run" below would replace all three with a shrug.
        raise
    except Exception as e:
        raise CrawlUnavailable(f"Crawl failed to run: {e}") from e

    return CrawlResult(seed=safe_seed, index=merged, pages=pages,
                       unvisited=sorted(set(unvisited)))
