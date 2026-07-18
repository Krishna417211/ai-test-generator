"""
security_scanner.py — Passive production security scan for a deployed URL.

Given a URL the user owns, this fetches the site and inspects its *configuration*
for the common issues that bite web apps in production. It is intentionally
NON-INTRUSIVE — it reads headers, cookies, TLS/redirect behaviour, and probes a
short curated list of files that should never be public (.env, .git/config, …).
It sends no attack payloads and does not brute-force anything.

Checks:
  • HTTPS enforced + HSTS
  • Security headers (CSP, X-Frame-Options, X-Content-Type-Options,
    Referrer-Policy, Permissions-Policy)
  • Cookie flags (Secure, HttpOnly, SameSite)
  • Server / framework version disclosure
  • CORS misconfiguration (ACAO: * with credentials)
  • Accidentally exposed sensitive files
  • Mixed content / insecure form actions (light HTML inspection)

An SSRF guard blocks scans of localhost / private / link-local addresses, and a
scope gate blocks third-party sites the user plainly does not own.
"""

import re
import socket
import asyncio
import logging
import ipaddress
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import urlparse, urljoin, quote_plus

from services.progress import NullProgress, Progress

import httpx

logger = logging.getLogger(__name__)

SEVERITY_WEIGHTS = {"critical": 35, "high": 20, "medium": 10, "low": 4, "info": 0}
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

# Browser-shaped, but still says who it is.
#
# The shape is not cosmetic and not evasion — it is a correctness fix. Large
# sites vary their response by User-Agent, and a bare bot token gets a degraded
# one. Measured against www.google.com, same client, same method, UA the only
# variable:
#
#   Testra-SecurityScanner/1.0        -> no HSTS, no Permissions-Policy
#   Mozilla/5.0 … Chrome/124 + token  -> HSTS: max-age=31536000, PP: unload=()
#
# So the old UA made the scanner report headers as MISSING that every real
# visitor is actually served. The question this tool answers is "what protects
# the people using this site?", and the honest instrument for that is the
# request a person's browser makes. The token stays appended so we remain
# identifiable in logs and blockable by anyone who would rather we went away.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 Testra-SecurityScanner/1.0 (+passive-scan)"
)

# The most a single category may cost, however many findings it produces.
#
# Without a cap the score is a plain sum, so one misconfiguration counted N
# times sinks everything: five cookies each missing three flags is -120 on a
# 100-point scale, which graded any site with a few analytics cookies F no
# matter how good the rest of it was. Capping per category keeps each *kind* of
# problem worth about what it's actually worth.
#
# `exposure` is uncapped on purpose: a world-readable .env is not one issue
# among several, and it should be able to take the grade to F by itself.
CATEGORY_CAPS = {
    "tls": 35,
    "headers": 35,
    "cookies": 20,
    "cors": 20,
    "content": 25,
    "disclosure": 8,
    "exposure": 100,
}

# Hardening is not the same kind of thing as a vulnerability, and the grade has
# to stop pretending it is.
#
# `headers`, `cookies` and `disclosure` are defence-in-depth: they describe
# layers a site could add, not something an attacker can use today. `tls`,
# `cors`, `content` and `exposure` are the opposite — each one means something
# is actually broken right now (traffic in the clear, any origin reading
# authenticated responses, credentials posted over HTTP, secrets on disk).
#
# Under the old flat caps a site whose ONLY faults were hardening ones could
# still reach 51 — a D, one band off failing — while nothing about it was
# exploitable. Real measurement, www.google.com: headers -35, cookies -14 = 51.
# Calling that a near-fail is not a defensible reading of the evidence, and a
# grade that says "nearly failing" about a site with no actual vulnerability
# teaches people to disregard the grade.
#
# So hardening findings share one budget. They are all still reported, in full,
# at their own severities — they just cannot, together, take a site below C on
# their own. Anything genuinely broken deducts outside this budget and can still
# sink the grade to F by itself.
HARDENING_CATEGORIES = {"headers", "cookies", "disclosure"}
HARDENING_BUDGET = 30


def _youtube(query: str) -> str:
    """A YouTube *search* URL for a fix — never a specific video.

    Deliberately a search, not a hand-picked video ID. The two alternatives both
    end badly: hard-coded IDs rot silently into 404s or, worse, into whatever
    unrelated video later occupies that slot; and asking an LLM for one is
    strictly worse, because a model cannot know real 11-character YouTube IDs
    and will invent plausible ones instead (this repo already had to walk that
    back once — see commit 7e53fef, "Stop fabricating project analysis").

    A search URL is built entirely from a string we own, so it cannot be
    fabricated, cannot 404, and always matches the finding it hangs off. On
    security advice, "here are current videos about this exact problem" is
    honest in a way a confidently wrong link is not.
    """
    return "https://www.youtube.com/results?search_query=" + quote_plus(query)


# ── HTML matching ────────────────────────────
#
# Sub-resource matching is deliberately per-tag. The old pattern was
# `(?:src|href)=["']http://`, which also matched <a href="http://...> — an
# ordinary outbound link, which a browser neither loads as part of the page nor
# blocks nor warns about. It is not mixed content, and matching it made this
# check fire on virtually every page that links anywhere.
_MIXED_SRC_RE = re.compile(
    r"<\s*(?:script|img|iframe|embed|source|audio|video|track|input)\b"
    r"[^>]*?\bsrc\s*=\s*[\"']http://([^\"'\s>]+)",
    re.I,
)
_MIXED_OBJECT_RE = re.compile(
    r"<\s*object\b[^>]*?\bdata\s*=\s*[\"']http://([^\"'\s>]+)", re.I
)
# Only rel=stylesheet counts; <link rel="canonical" href="http://…"> is metadata,
# not something the browser fetches into the page.
_MIXED_CSS_RE = re.compile(
    r"<\s*link\b(?=[^>]*\brel\s*=\s*[\"']?stylesheet\b)"
    r"[^>]*?\bhref\s*=\s*[\"']http://([^\"'\s>]+)",
    re.I,
)

# Each <form> as its own block, so a form's action can be judged against the
# fields inside *that* form rather than anything else on the page.
_FORM_RE = re.compile(r"<\s*form\b[^>]*>.*?(?:<\s*/\s*form\s*>|\Z)", re.I | re.S)
_FORM_ACTION_HTTP_RE = re.compile(r"\baction\s*=\s*[\"']http://([^\"'\s>]+)", re.I)
_PASSWORD_INPUT_RE = re.compile(r"<\s*input\b[^>]*\btype\s*=\s*[\"']?password\b", re.I)

# Cookies whose whole job needs JavaScript to read them (analytics, consent,
# locale) are not session cookies, and HttpOnly on them would break the feature.
# Only flag it for cookies that look like they carry authentication — which is
# what this check's own remediation has always said.
# Matching these anywhere in the name, not only at a separator: the most common
# session cookies in the wild jam the word into a prefix — PHPSESSID,
# JSESSIONID, ASP.NET_SessionId — and anchoring to a boundary missed every one
# of them, which is the opposite of the intended failure direction. 'sid' is the
# exception and stays anchored, because as a bare substring it appears inside
# ordinary words.
_SESSION_COOKIE_RE = re.compile(
    r"sess|auth|token|jwt|login|remember|identity"
    r"|(^|[_.\-])sid($|[_.\-])",
    re.I,
)

# Files that should never be reachable in production. (severity, matcher)
SENSITIVE_PATHS: list[tuple[str, str, str]] = [
    ("/.env", "critical", "env"),
    ("/.env.local", "critical", "env"),
    ("/.env.production", "critical", "env"),
    ("/.git/config", "critical", "gitconfig"),
    ("/.git/HEAD", "high", "githead"),
    ("/.aws/credentials", "critical", "aws"),
    ("/config.json", "medium", "json"),
    ("/.DS_Store", "low", "dsstore"),
    ("/phpinfo.php", "high", "phpinfo"),
    ("/server-status", "medium", "serverstatus"),
    ("/wp-config.php", "critical", "wpconfig"),
    ("/.htpasswd", "high", "htpasswd"),
]

# header -> (severity, title, description, remediation, youtube search query)
SECURITY_HEADERS = {
    "strict-transport-security": (
        "medium", "HSTS not set",
        "The Strict-Transport-Security header forces browsers to use HTTPS, "
        "preventing SSL-stripping and cookie-hijacking on the network.",
        'Add: Strict-Transport-Security: max-age=63072000; includeSubDomains; preload',
        "HSTS strict transport security header explained how to add",
    ),
    "content-security-policy": (
        "high", "No Content-Security-Policy",
        "Without a CSP, injected scripts (XSS) run freely. A CSP is the single "
        "most effective defence-in-depth control against XSS and data injection.",
        "Add a Content-Security-Policy header, e.g. "
        "\"default-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'\".",
        "content security policy CSP tutorial how to add header",
    ),
    "x-frame-options": (
        "medium", "Clickjacking protection missing (X-Frame-Options)",
        "The page can be embedded in an <iframe> on a malicious site and used "
        "for clickjacking.",
        "Add: X-Frame-Options: DENY  (or a CSP with frame-ancestors 'none').",
        "clickjacking X-Frame-Options frame-ancestors fix tutorial",
    ),
    "x-content-type-options": (
        "low", "MIME-sniffing not disabled (X-Content-Type-Options)",
        "Browsers may guess content types and execute non-script files as scripts.",
        "Add: X-Content-Type-Options: nosniff",
        "X-Content-Type-Options nosniff MIME sniffing explained",
    ),
    "referrer-policy": (
        "low", "No Referrer-Policy",
        "Full URLs (which can contain tokens) may leak to third parties via the "
        "Referer header.",
        "Add: Referrer-Policy: strict-origin-when-cross-origin",
        "Referrer-Policy header tutorial web security",
    ),
    "permissions-policy": (
        "info", "No Permissions-Policy",
        "Powerful browser features (camera, geolocation, etc.) are not explicitly "
        "restricted.",
        "Add a Permissions-Policy header disabling features you don't use, e.g. "
        "\"geolocation=(), camera=(), microphone=()\".",
        "Permissions-Policy header tutorial",
    ),
}


# ── Scope gate: sites the user evidently does not own ──
#
# Two honest reasons to refuse these, neither of which is "they are too secure
# to scan":
#
#  1. Consent. This scan is passive on headers, but _check_exposed_files probes
#     a dozen paths (/.env, /.git/config, /wp-config.php) against the target.
#     Aimed at your own deployment that is an audit. Aimed at a stranger's, it
#     is unsolicited recon, and Testra should not be the one pointing it.
#  2. The report is worthless anyway. You cannot act on a finding about
#     someone else's site — there is no fix you are able to ship.
#
# This list is deliberately NOT a security judgement and NOT load-bearing for
# correctness. It cannot be complete (the internet is not enumerable), and the
# false-positive bugs it looks like it addresses are fixed properly elsewhere in
# this module — by USER_AGENT and _unscannable_response. It is a scope guard for
# the obvious cases: the handful of domains people paste to kick the tyres.
THIRD_PARTY_DOMAINS = {
    "google", "youtube", "gmail", "facebook", "instagram", "whatsapp", "meta",
    "amazon", "aws", "microsoft", "bing", "azure", "apple", "icloud", "netflix",
    "twitter", "linkedin", "reddit", "tiktok", "snapchat", "pinterest", "twitch",
    "discord", "telegram", "zoom", "slack", "dropbox", "adobe", "oracle", "ibm",
    "salesforce", "sap", "uber", "airbnb", "spotify", "github", "gitlab",
    "bitbucket", "stripe", "paypal", "cloudflare", "wikipedia", "wikimedia",
    "mozilla", "openai", "anthropic", "yahoo", "baidu", "alibaba", "aliexpress",
    "samsung", "intel", "nvidia", "cisco", "vmware", "dell", "sony", "walmart",
    "ebay", "flipkart", "paytm", "phonepe", "irctc", "hdfcbank", "icicibank",
    "axisbank",
}
# Bare English words are deliberately absent from that set ("x", "live",
# "office", "target"): as a brand label each is a coin-flip, and blocking
# someone's own target.dev to spare them a report about Target is a worse
# outcome than the scan we failed to prevent. The gate is allowed to miss.

# Hosting suffixes: everything under these belongs to a USER, not to the company
# whose name is in the domain.
#
# This has to be checked before the brand match, and is the reason the gate is
# not a plain substring test. `myapp.github.io` brand-matches "github" and
# `myapp.vercel.app` matches "vercel" — but those are exactly the deployments
# this tool exists to scan. Blocking a user's own GitHub Pages site and telling
# them to "go scan a site you own" would be both wrong and insulting.
HOSTING_SUFFIXES = (
    ".github.io", ".gitlab.io", ".vercel.app", ".netlify.app", ".netlify.com",
    ".herokuapp.com", ".pages.dev", ".workers.dev", ".web.app",
    ".firebaseapp.com", ".onrender.com", ".fly.dev", ".surge.sh",
    ".azurewebsites.net", ".cloudfront.net", ".amplifyapp.com", ".replit.app",
    ".railway.app", ".streamlit.app", ".glitch.me", ".appspot.com",
)

# Whole categories where an unsolicited scan is worst-behaved: government,
# military, and the banking gTLD (registry-restricted to verified banks).
#
# Matched as a LABEL anywhere but the first, not as a trailing suffix: most of
# the world's government domains put it in the middle — india.gov.in, gov.uk,
# defence.gov.au — and a plain `.endswith(".gov")` sees none of them.
BLOCKED_LABELS = {"gov", "mil", "bank"}


class ScanError(Exception):
    """Raised when the target URL is invalid or cannot be scanned safely."""


class OutOfScopeError(ScanError):
    """Raised when the target is a third-party site the user does not own."""


class UnscannableResponseError(ScanError):
    """Raised when the site answered, but not with a page we can honestly grade."""


def _brand_label(host: str) -> str:
    """The label most likely to identify the brand, ignoring the public suffix.

    No PSL dependency here, so this is a heuristic on purpose: for 'google.com'
    and 'google.co.uk' alike it returns 'google', by walking in from the right
    past the short suffix-ish labels. It only ever feeds the scope gate, where a
    miss costs a scan we would rather not have run and never a wrong finding.
    """
    parts = [p for p in host.lower().strip(".").split(".") if p]
    if len(parts) < 2:
        return ""
    # Walk left past public-suffix-shaped labels: "co.uk", "com.au", "co.in".
    i = len(parts) - 1
    while i > 0 and len(parts[i]) <= 3 and parts[i] not in THIRD_PARTY_DOMAINS:
        i -= 1
    return parts[i]


def _assert_in_scope(host: str) -> None:
    """Refuse targets the user obviously does not own. See THIRD_PARTY_DOMAINS."""
    host = host.lower().rstrip(".")
    # User deployments first — see HOSTING_SUFFIXES. A site here is the user's
    # own, whatever brand name the suffix happens to contain.
    if any(host.endswith(s) for s in HOSTING_SUFFIXES):
        return
    labels = host.split(".")
    if BLOCKED_LABELS & set(labels[1:]):
        raise OutOfScopeError(
            f"'{host}' is a government, military or banking domain, and Testra will "
            "not scan those. Point it at a site you own and can fix."
        )
    if _brand_label(host) in THIRD_PARTY_DOMAINS:
        # The brand label is matched but deliberately not echoed: .title() turns
        # "github" into "Github" and "ibm" into "Ibm", and a refusal that
        # misspells the company in the same breath reads like a bug. The host is
        # already in the message and says the same thing correctly.
        raise OutOfScopeError(
            f"'{host}' is a third-party site, not one of yours — so Testra won't "
            "scan it. There'd be nothing you could act on in the report anyway: "
            "you can't ship a fix to someone else's site. Point this at your own "
            "deployment, e.g. https://your-app.com."
        )


@dataclass
class Finding:
    severity: str          # critical | high | medium | low | info
    category: str          # headers | tls | cookies | disclosure | exposure | cors | content
    title: str
    description: str
    remediation: str
    evidence: str = ""
    # A YouTube *search* for how to fix this specific issue. Always built by
    # _youtube() from a query we control — never a video ID, and never anything
    # a model produced. Empty means "no useful search for this one".
    video_url: str = ""


@dataclass
class ScanResult:
    url: str
    final_url: str
    score: int
    grade: str
    findings: list[dict]
    counts: dict
    summary: str = ""
    checks_run: int = 0


# ─────────────────────────────────────────────
# SSRF guard
# ─────────────────────────────────────────────

def _assert_public_host(host: str) -> None:
    """Resolve `host` and reject private/loopback/link-local/reserved/multicast IPs (SSRF)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ScanError(f"Could not resolve host '{host}'. Is the URL correct?")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ScanError(
                "Refusing to scan an internal/private address. "
                "Point this at your public production URL."
            )


def precheck_target(url: str) -> str:
    """Normalise the URL and apply every check that needs no network.

    Split from validate_target so the route can settle targeting before it opens
    the NDJSON stream (see progress.py: an up-front-decidable failure must stay a
    real 400, not an in-band error after a 200). Everything here is a string
    decision — scheme, hostname, scope — so the route pays nothing for it and
    resolves no DNS. The lookup stays in validate_target, on the scanner's side,
    where it happens once, immediately before the request it guards.
    """
    url = url.strip()
    if not url:
        raise ScanError("Please provide a URL to scan.")
    # Only bare hosts get a scheme prepended. Testing for `^https?://` instead
    # meant any *other* scheme was treated as scheme-less: "ftp://evil.com"
    # became "https://ftp://evil.com", whose hostname parses as "ftp" — so the
    # user got "Could not resolve host 'ftp'" and the scheme check below was
    # unreachable.
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
        url = "https://" + url  # assume https if scheme omitted

    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ScanError(
            f"Only http:// and https:// URLs can be scanned — got '{parsed.scheme}://'."
        )
    host = parsed.hostname
    if not host:
        raise ScanError("Could not parse a hostname from that URL.")

    # Scope before DNS: refusing a third party is a decision about the target we
    # can make from the name alone, and there's no reason to touch the network
    # (or leak that we were asked) for a host we will not scan either way.
    _assert_in_scope(host)
    return url


def validate_target(url: str) -> str:
    """precheck_target plus the DNS-dependent SSRF guard. Used by the scanner."""
    url = precheck_target(url)
    host = urlparse(url).hostname
    if host:
        _assert_public_host(host)
    return url


# ── Is this response actually the site? ──
#
# The bug this exists to kill: the scanner graded whatever came back, without
# ever asking whether it was the page. Measured, www.wikipedia.org answered our
# scanner with 403 — and the scan cheerfully reported "grade C, 6 findings: no
# CSP, no HSTS, no X-Frame-Options…". Every one of those was a description of a
# bot-block page. Wikipedia sends those headers on the real thing.
#
# That single hole explains both halves of the complaint that started this:
# reports full of things that are not true of the site, and the SAME six
# findings on site after site — because a WAF block page is a bare error page
# everywhere, and bare error pages are missing the same six headers everywhere.
#
# A finding needs a page it is true of. Without one the only honest output is to
# say we could not see the site — never a grade.
# Phrases that are a vendor's block page and essentially nothing else. Long
# enough, and specific enough, that a page containing one IS one.
_CHALLENGE_MARKERS = (
    "enable javascript and cookies to continue",
    "checking your browser before accessing",
    "attention required! | cloudflare",
    "request unsuccessful. incapsula incident id",
    "pardon our interruption",          # PerimeterX / HUMAN
    "ddos protection by",
)

# Short, generic phrases that a real page can legitimately contain: a loading
# spinner says "Just a moment", and an article about S3 permissions says "Access
# Denied" several times. Matching those bare refused the whole scan of an
# ordinary site — a worse failure than the false findings this check exists to
# prevent, because it withholds the entire report rather than one wrong line.
#
# So they need corroboration: a block page is a stub, while a real page that
# merely mentions the phrase is a real page, with a real page's worth of markup
# around it. "access denied" is gone entirely rather than demoted — the WAFs
# that serve it do so with a 403, which the status check above already catches,
# so it was only ever able to fire on a false positive.
_CHALLENGE_MARKERS_WEAK = ("just a moment", "verify you are human")
_CHALLENGE_STUB_BYTES = 8_000


def _unscannable_response(resp: "httpx.Response") -> Optional[str]:
    """Why this response can't be graded, or None if it looks like the real page."""
    code = resp.status_code
    if code == 429:
        return (
            "the site rate-limited the scan (HTTP 429). Its headers here are the "
            "rate-limiter's, not your app's. Wait a minute and try again."
        )
    if not (200 <= code < 300):
        return (
            f"the site answered HTTP {code} instead of serving a page. Grading that "
            "would describe an error page rather than your site — check the URL "
            "points at a page that loads, and that it isn't blocking automated "
            "requests."
        )
    # A 200 can still be a bot wall: challenge interstitials return 200 with the
    # real page nowhere in sight.
    if resp.headers.get("cf-mitigated", "").lower() == "challenge":
        return (
            "Cloudflare served a bot challenge instead of the page, so the headers "
            "here are the challenge's rather than your site's. Allow-list the "
            "scanner, or scan an origin URL that isn't behind the challenge."
        )
    body = resp.text
    head = body[:8000].lower()
    hit = next((m for m in _CHALLENGE_MARKERS if m in head), None)
    if hit is None and len(body) < _CHALLENGE_STUB_BYTES:
        # Only worth consulting the ambiguous phrases once the page is already
        # stub-shaped. See _CHALLENGE_MARKERS_WEAK.
        hit = next((m for m in _CHALLENGE_MARKERS_WEAK if m in head), None)
    if hit:
        return (
            "a bot-protection challenge answered instead of the page "
            f"(matched \"{hit}\"), so these headers are the challenge's, not "
            "your site's. Allow-list the scanner or scan an origin URL that "
            "isn't behind it."
        )
    return None


async def _redirect_guard(response: "httpx.Response") -> None:
    """
    Re-validate every redirect hop so an open redirect can't bounce the scanner
    onto an internal address (SSRF via 3xx). Runs as an httpx response hook,
    which fires for each redirect BEFORE it is followed.
    """
    if not response.is_redirect:
        return
    location = response.headers.get("location")
    if not location:
        return
    nxt = urljoin(str(response.url), location)
    host = urlparse(nxt).hostname
    if host:
        _assert_public_host(host)


# ─────────────────────────────────────────────
# Scanner
# ─────────────────────────────────────────────

class SecurityScanner:
    def __init__(self, timeout: float = 12.0, max_body_bytes: int = 300_000):
        self.timeout = timeout
        self.max_body_bytes = max_body_bytes

    async def scan(self, url: str, progress: Optional[Progress] = None) -> ScanResult:
        say = progress or NullProgress()

        await say.start("target")
        target = validate_target(url)
        findings: list[Finding] = []
        checks = 0

        headers = {"User-Agent": USER_AGENT}
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, max_redirects=5,
            headers=headers, verify=True,
            event_hooks={"response": [_redirect_guard]},
        ) as client:
            try:
                resp = await client.get(target)
            except httpx.RequestError as e:
                raise ScanError(f"Could not reach the site: {e}")

            final_url = str(resp.url)
            # Before anything is measured: is this the site, or a wall in front
            # of it? Every check below reads headers and body, and on a block
            # page all of them are true about the block page and false about the
            # site. There is no partial credit to salvage here — the honest move
            # is to stop and say we could not see it.
            why = _unscannable_response(resp)
            if why:
                raise UnscannableResponseError(f"Couldn't scan {urlparse(final_url).netloc} — {why}")

            # Only now is the target proven fair game: validate_target vets the
            # URL, but the redirect guard vets every hop, and it runs on this
            # request. Reporting "target" done before the fetch would call a
            # host safe that a redirect could still have taken elsewhere.
            hops = len(resp.history)
            await say.done(
                "target",
                f"{urlparse(final_url).netloc} reachable"
                + (f" · {hops} redirect{'s' if hops != 1 else ''} followed" if hops else ""),
            )

            await say.start("checks")
            h = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.text[: self.max_body_bytes]

            checks += self._check_transport(target, resp, findings)
            checks += self._check_security_headers(final_url, h, findings)
            checks += self._check_cookies(resp, final_url, findings)
            checks += self._check_disclosure(h, findings)
            checks += self._check_cors(h, findings)
            checks += self._check_content(final_url, body, findings)
            checks += await self._check_exposed_files(client, final_url, findings)
            await say.done(
                "checks",
                f"{checks} checks run · {len(findings)} "
                f"{'issue' if len(findings) == 1 else 'issues'} found",
            )

        await say.start("score")
        result = self._build_result(target, final_url, findings, checks)
        await say.done("score", f"Grade {result.grade} · {result.score}/100")
        return result

    # ── individual checks ────────────────────

    def _check_transport(self, target, resp, findings) -> int:
        is_https = str(resp.url).lower().startswith("https://")
        # Did an http:// request get upgraded to https via redirect?
        started_http = target.lower().startswith("http://")
        redirected_to_https = any(
            str(r.url).lower().startswith("https://") for r in resp.history
        )
        if not is_https:
            findings.append(Finding(
                "high", "tls", "Site served over plain HTTP",
                "Traffic is unencrypted and can be read or modified on the network. "
                "Credentials and cookies are exposed.",
                "Serve the site over HTTPS and redirect all HTTP traffic to HTTPS. "
                "Most hosts (Vercel, Netlify, Cloudflare) provide free TLS certificates.",
                evidence=f"Final URL: {resp.url}",
                video_url=_youtube("how to enable HTTPS TLS certificate website free"),
            ))
        elif started_http and not redirected_to_https:
            findings.append(Finding(
                "medium", "tls", "HTTP does not redirect to HTTPS",
                "The site is available over HTTP without forcing an upgrade to HTTPS.",
                "Add a 301 redirect from http:// to https:// at the edge/server.",
                video_url=_youtube("redirect http to https 301 tutorial"),
            ))
        return 2

    def _check_security_headers(self, final_url, h, findings) -> int:
        is_https = final_url.lower().startswith("https://")
        # CSP frame-ancestors supersedes X-Frame-Options: where both are present
        # browsers obey the CSP and ignore the older header, so a site using the
        # modern directive is protected and must not be told otherwise. This
        # check's own remediation text has always offered frame-ancestors as the
        # alternative — it just never looked for it, so every site that took the
        # advice kept being flagged for ignoring it.
        csp = h.get("content-security-policy", "").lower()
        has_frame_ancestors = "frame-ancestors" in csp

        for header, (sev, title, desc, fix, video_q) in SECURITY_HEADERS.items():
            if header == "strict-transport-security" and not is_https:
                continue  # HSTS is only meaningful over HTTPS
            if header == "x-frame-options" and has_frame_ancestors:
                continue
            if header not in h:
                findings.append(Finding(
                    sev, "headers", title, desc, fix, video_url=_youtube(video_q),
                ))
        return len(SECURITY_HEADERS)

    def _check_cookies(self, resp, final_url, findings) -> int:
        """Cookie flags, reported per *problem* rather than per cookie.

        One finding per cookie per flag was the single loudest source of noise
        here: a page setting five cookies produced up to fifteen findings that
        all said the same thing, none of which deduplicated (the title carried
        the cookie name), and which together were worth more than the entire
        rest of the scan. Grouping says the same thing once and names the
        cookies as evidence.
        """
        is_https = final_url.lower().startswith("https://")
        set_cookies = resp.headers.get_list("set-cookie") if hasattr(resp.headers, "get_list") else []
        if not set_cookies:
            raw = resp.headers.get("set-cookie")
            set_cookies = [raw] if raw else []

        no_secure, no_httponly, no_samesite = [], [], []
        for cookie in set_cookies:
            name = cookie.split("=", 1)[0].strip()
            low = cookie.lower()
            if is_https and "secure" not in low:
                no_secure.append(name)
            # HttpOnly only where it's actually indicated. An analytics or
            # consent cookie is read by JavaScript by design — HttpOnly would
            # break it, so demanding it there is advice we'd want ignored, and
            # advice a reader learns to ignore devalues the findings that matter.
            if "httponly" not in low and _SESSION_COOKIE_RE.search(name):
                no_httponly.append(name)
            if "samesite" not in low:
                no_samesite.append(name)

        if no_secure:
            findings.append(Finding(
                "medium", "cookies", "Cookies missing the Secure flag",
                "These cookies can be sent over unencrypted HTTP and intercepted.",
                "Set the Secure attribute on every cookie on an HTTPS site.",
                evidence=", ".join(no_secure[:10]),
                video_url=_youtube("secure httponly samesite cookie flags explained"),
            ))
        if no_httponly:
            findings.append(Finding(
                "medium", "cookies", "Session cookies missing the HttpOnly flag",
                "JavaScript can read these cookies, so a single XSS bug is enough "
                "to steal the session.",
                "Set HttpOnly on session/auth cookies.",
                evidence=", ".join(no_httponly[:10]),
                video_url=_youtube("httponly cookie flag session hijacking XSS fix"),
            ))
        if no_samesite:
            findings.append(Finding(
                "low", "cookies", "Cookies with no SameSite attribute",
                "These cookies may be sent on cross-site requests, enabling CSRF.",
                "Set SameSite=Lax (or Strict) on cookies.",
                evidence=", ".join(no_samesite[:10]),
                video_url=_youtube("samesite cookie attribute CSRF explained"),
            ))
        return 3

    def _check_disclosure(self, h, findings) -> int:
        headers = ("server", "x-powered-by", "x-aspnet-version", "x-aspnetmvc-version")
        for header in headers:
            val = h.get(header)
            # Only flag when a version number is leaked (e.g. "nginx/1.18.0").
            if val and re.search(r"\d+\.\d+", val):
                findings.append(Finding(
                    "low", "disclosure", f"Server/framework version disclosed ({header})",
                    "Exposing exact software versions helps attackers match known CVEs.",
                    f"Remove or obfuscate the '{header}' response header.",
                    evidence=f"{header}: {val}",
                    video_url=_youtube("hide server version header nginx apache security"),
                ))
        return len(headers)

    def _check_cors(self, h, findings) -> int:
        acao = h.get("access-control-allow-origin")
        acac = h.get("access-control-allow-credentials", "").lower() == "true"
        if acao == "*" and acac:
            findings.append(Finding(
                "high", "cors", "Insecure CORS: wildcard origin with credentials",
                "Any website can make authenticated cross-origin requests and read the "
                "response — a serious account-takeover vector.",
                "Never combine Access-Control-Allow-Origin: * with credentials. "
                "Echo back a strict allow-list of trusted origins instead.",
                evidence="Access-Control-Allow-Origin: * + Allow-Credentials: true",
                video_url=_youtube("CORS misconfiguration wildcard credentials vulnerability fix"),
            ))
        elif acao == "*":
            findings.append(Finding(
                "info", "cors", "CORS allows any origin (Access-Control-Allow-Origin: *)",
                "Acceptable for public, non-credentialed APIs, but risky if the endpoint "
                "ever returns user-specific data.",
                "Restrict CORS to known origins if the response is not fully public.",
                video_url=_youtube("CORS explained access-control-allow-origin tutorial"),
            ))
        return 1

    def _check_content(self, final_url, body, findings) -> int:
        is_https = final_url.lower().startswith("https://")

        # Sub-resources only — see _MIXED_*_RE. Anchors are not mixed content.
        if is_https:
            resources = [
                *_MIXED_SRC_RE.findall(body),
                *_MIXED_OBJECT_RE.findall(body),
                *_MIXED_CSS_RE.findall(body),
            ]
            if resources:
                # Name them. "Mixed content somewhere on this page" is a fact the
                # reader then has to go and rediscover by hand.
                shown = ", ".join(f"http://{r}" for r in dict.fromkeys(resources))[:200]
                findings.append(Finding(
                    "medium", "content", "Mixed content: HTTP resources on an HTTPS page",
                    "Insecure http:// resources on a secure page can be tampered with "
                    "in transit and are blocked by modern browsers.",
                    "Load all scripts, styles, images and iframes over https://.",
                    evidence=shown,
                    video_url=_youtube("fix mixed content https website tutorial"),
                ))

        # A password field only matters against the action of the form it's
        # actually in. Testing "any http:// form" AND "any password field"
        # independently flagged pages where the two were unrelated — e.g. an
        # https login form beside a plain-http newsletter signup.
        for form in _FORM_RE.findall(body):
            action = _FORM_ACTION_HTTP_RE.search(form)
            if action and _PASSWORD_INPUT_RE.search(form):
                findings.append(Finding(
                    "high", "content", "Password form submits over plain HTTP",
                    "Credentials are sent unencrypted and can be captured by anyone "
                    "on the network path.",
                    "Point the form action at an https:// endpoint.",
                    evidence=f"<form action=\"http://{action.group(1)}\">",
                    video_url=_youtube("secure login form https password transmission"),
                ))
                break  # one finding is enough; the fix is the same for all of them
        return 2

    async def _check_exposed_files(self, client, final_url, findings) -> int:
        base = f"{urlparse(final_url).scheme}://{urlparse(final_url).netloc}"

        async def probe(path, sev, kind):
            try:
                r = await client.get(urljoin(base + "/", path.lstrip("/")))
            except httpx.RequestError:
                return None
            if r.status_code != 200:
                return None
            if not _looks_sensitive(kind, r.text):
                return None
            return Finding(
                sev, "exposure", f"Exposed sensitive file: {path}",
                "A file that should never be public is reachable and may leak secrets, "
                "source code, or credentials.",
                f"Block access to {path} at the server/CDN and remove it from the web root. "
                "If it contained secrets, rotate them immediately.",
                evidence=f"HTTP 200 at {base}{path}",
                video_url=_youtube(f"exposed {path.lstrip('/')} file web server fix secure"),
            )

        results = await asyncio.gather(
            *(probe(p, s, k) for p, s, k in SENSITIVE_PATHS), return_exceptions=True
        )
        for r in results:
            if isinstance(r, Finding):
                findings.append(r)
        return len(SENSITIVE_PATHS)

    # ── scoring ──────────────────────────────

    def _build_result(self, url, final_url, findings, checks) -> ScanResult:
        # De-dup identical findings, then sort by severity.
        seen = set()
        unique: list[Finding] = []
        for f in findings:
            key = (f.severity, f.title)
            if key not in seen:
                seen.add(key)
                unique.append(f)
        unique.sort(key=lambda f: SEVERITY_ORDER.get(f.severity, 9))

        # Deduct per finding, but never more than CATEGORY_CAPS allows for any
        # one category. A flat sum made the score bottom out on breadth rather
        # than seriousness: six missing headers alone is -48, so a site whose
        # only fault was headers landed on D, indistinguishable from one leaking
        # its .env. Capping keeps the categories comparable, and leaves room for
        # a genuinely critical finding to be what decides the grade.
        score = 100
        counts = {s: 0 for s in SEVERITY_WEIGHTS}
        spent: dict[str, int] = {}
        hardening_spent = 0
        for f in unique:
            counts[f.severity] = counts.get(f.severity, 0) + 1
            weight = SEVERITY_WEIGHTS.get(f.severity, 0)
            cap = CATEGORY_CAPS.get(f.category, 100)
            used = spent.get(f.category, 0)
            deduct = max(0, min(weight, cap - used))
            # Hardening categories also draw on one shared budget, so a site
            # with nothing exploitable can't be graded down as if it had.
            if f.category in HARDENING_CATEGORIES:
                deduct = min(deduct, HARDENING_BUDGET - hardening_spent)
                deduct = max(0, deduct)
                hardening_spent += deduct
            spent[f.category] = used + deduct
            score -= deduct
        score = max(0, min(100, score))
        grade = _grade(score)

        # A critical finding sets the grade rather than voting on it. On the
        # arithmetic alone one exposed .env costs 35 and lands on 65 — a "C",
        # the same band as a site whose only fault is missing headers — and
        # nobody reading "C" goes and rotates their credentials. Every critical
        # in this scanner means the same thing: secrets are already public.
        if counts.get("critical"):
            grade = "F"

        return ScanResult(
            url=url,
            final_url=final_url,
            score=score,
            grade=grade,
            findings=[asdict(f) for f in unique],
            counts=counts,
            checks_run=checks,
        )


def _grade(score: int) -> str:
    if score >= 90: return "A"
    if score >= 80: return "B"
    if score >= 65: return "C"
    if score >= 50: return "D"
    return "F"


def _looks_sensitive(kind: str, text: str) -> bool:
    """Confirm a 200 response actually looks like the sensitive file (not an SPA 200)."""
    t = text[:2000]
    if kind == "env":
        return bool(re.search(r"^[A-Z0-9_]+=", t, re.M)) and "<html" not in t.lower()
    if kind == "gitconfig":
        return "[core]" in t or "[remote" in t
    if kind == "githead":
        return t.strip().startswith("ref:")
    if kind == "aws":
        return "aws_access_key_id" in t.lower()
    if kind == "json":
        if not t.lstrip().startswith("{"):
            return False
        # Matching a bare "key" substring made this fire on files that are meant
        # to be public and contain no secret at all: Firebase web configs
        # (apiKey is a project identifier, not a credential), PWA manifests
        # ("icons"/"start_url"), i18n bundles. Require a key name that is
        # actually secret-shaped, as a JSON key rather than anywhere in the text.
        return bool(re.search(
            r'"(client_secret|api_secret|secret_key|secret|password|passwd|'
            r'private_key|access_token|refresh_token|aws_secret_access_key)"\s*:',
            t, re.I,
        ))
    if kind == "dsstore":
        return "Bud1" in t[:8] or "\x00\x00\x00\x01Bud1" in t
    if kind == "phpinfo":
        return "phpinfo()" in t or "PHP Version" in t
    if kind == "serverstatus":
        return "Apache Server Status" in t or "Server Version" in t
    if kind == "wpconfig":
        return "DB_PASSWORD" in t or "wp-config" in t.lower()
    if kind == "htpasswd":
        return bool(re.search(r"^[^:]+:\$?", t, re.M)) and "<html" not in t.lower()
    return False
