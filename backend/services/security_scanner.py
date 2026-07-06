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

An SSRF guard blocks scans of localhost / private / link-local addresses.
"""

import re
import socket
import asyncio
import logging
import ipaddress
from dataclasses import dataclass, field, asdict
from urllib.parse import urlparse, urljoin

import httpx

logger = logging.getLogger(__name__)

SEVERITY_WEIGHTS = {"critical": 35, "high": 20, "medium": 10, "low": 4, "info": 0}
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

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

SECURITY_HEADERS = {
    "strict-transport-security": (
        "medium", "HSTS not set",
        "The Strict-Transport-Security header forces browsers to use HTTPS, "
        "preventing SSL-stripping and cookie-hijacking on the network.",
        'Add: Strict-Transport-Security: max-age=63072000; includeSubDomains; preload',
    ),
    "content-security-policy": (
        "high", "No Content-Security-Policy",
        "Without a CSP, injected scripts (XSS) run freely. A CSP is the single "
        "most effective defence-in-depth control against XSS and data injection.",
        "Add a Content-Security-Policy header, e.g. "
        "\"default-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'\".",
    ),
    "x-frame-options": (
        "medium", "Clickjacking protection missing (X-Frame-Options)",
        "The page can be embedded in an <iframe> on a malicious site and used "
        "for clickjacking.",
        "Add: X-Frame-Options: DENY  (or a CSP with frame-ancestors 'none').",
    ),
    "x-content-type-options": (
        "low", "MIME-sniffing not disabled (X-Content-Type-Options)",
        "Browsers may guess content types and execute non-script files as scripts.",
        "Add: X-Content-Type-Options: nosniff",
    ),
    "referrer-policy": (
        "low", "No Referrer-Policy",
        "Full URLs (which can contain tokens) may leak to third parties via the "
        "Referer header.",
        "Add: Referrer-Policy: strict-origin-when-cross-origin",
    ),
    "permissions-policy": (
        "info", "No Permissions-Policy",
        "Powerful browser features (camera, geolocation, etc.) are not explicitly "
        "restricted.",
        "Add a Permissions-Policy header disabling features you don't use, e.g. "
        "\"geolocation=(), camera=(), microphone=()\".",
    ),
}


class ScanError(Exception):
    """Raised when the target URL is invalid or cannot be scanned safely."""


@dataclass
class Finding:
    severity: str          # critical | high | medium | low | info
    category: str          # headers | tls | cookies | disclosure | exposure | cors | content
    title: str
    description: str
    remediation: str
    evidence: str = ""


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

def _validate_target(url: str) -> str:
    """Normalise + safety-check the URL. Rejects internal/private targets."""
    url = url.strip()
    if not url:
        raise ScanError("Please provide a URL to scan.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url  # assume https if scheme omitted

    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ScanError("Only http and https URLs can be scanned.")
    host = parsed.hostname
    if not host:
        raise ScanError("Could not parse a hostname from that URL.")

    # Resolve and reject private / loopback / link-local / reserved IPs (SSRF).
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
    return url


# ─────────────────────────────────────────────
# Scanner
# ─────────────────────────────────────────────

class SecurityScanner:
    def __init__(self, timeout: float = 12.0, max_body_bytes: int = 300_000):
        self.timeout = timeout
        self.max_body_bytes = max_body_bytes

    async def scan(self, url: str) -> ScanResult:
        target = _validate_target(url)
        findings: list[Finding] = []
        checks = 0

        headers = {"User-Agent": "Testra-SecurityScanner/1.0 (+passive-scan)"}
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, headers=headers, verify=True
        ) as client:
            try:
                resp = await client.get(target)
            except httpx.RequestError as e:
                raise ScanError(f"Could not reach the site: {e}")

            final_url = str(resp.url)
            h = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.text[: self.max_body_bytes]

            checks += self._check_transport(target, resp, findings)
            checks += self._check_security_headers(final_url, h, findings)
            checks += self._check_cookies(resp, final_url, findings)
            checks += self._check_disclosure(h, findings)
            checks += self._check_cors(h, findings)
            checks += self._check_content(final_url, body, findings)
            checks += await self._check_exposed_files(client, final_url, findings)

        return self._build_result(target, final_url, findings, checks)

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
            ))
        elif started_http and not redirected_to_https:
            findings.append(Finding(
                "medium", "tls", "HTTP does not redirect to HTTPS",
                "The site is available over HTTP without forcing an upgrade to HTTPS.",
                "Add a 301 redirect from http:// to https:// at the edge/server.",
            ))
        return 1

    def _check_security_headers(self, final_url, h, findings) -> int:
        is_https = final_url.lower().startswith("https://")
        for header, (sev, title, desc, fix) in SECURITY_HEADERS.items():
            if header == "strict-transport-security" and not is_https:
                continue  # HSTS is only meaningful over HTTPS
            if header not in h:
                findings.append(Finding(sev, "headers", title, desc, fix))
        return len(SECURITY_HEADERS)

    def _check_cookies(self, resp, final_url, findings) -> int:
        is_https = final_url.lower().startswith("https://")
        set_cookies = resp.headers.get_list("set-cookie") if hasattr(resp.headers, "get_list") else []
        if not set_cookies:
            raw = resp.headers.get("set-cookie")
            set_cookies = [raw] if raw else []
        for cookie in set_cookies:
            name = cookie.split("=", 1)[0].strip()
            low = cookie.lower()
            if is_https and "secure" not in low:
                findings.append(Finding(
                    "medium", "cookies", f"Cookie '{name}' missing Secure flag",
                    "The cookie can be sent over unencrypted HTTP and intercepted.",
                    "Set the Secure attribute on all cookies.",
                    evidence=cookie[:120],
                ))
            if "httponly" not in low:
                findings.append(Finding(
                    "medium", "cookies", f"Cookie '{name}' missing HttpOnly flag",
                    "JavaScript can read the cookie, so an XSS bug can steal the session.",
                    "Set HttpOnly on session/auth cookies.",
                    evidence=cookie[:120],
                ))
            if "samesite" not in low:
                findings.append(Finding(
                    "low", "cookies", f"Cookie '{name}' has no SameSite attribute",
                    "The cookie may be sent on cross-site requests, enabling CSRF.",
                    "Set SameSite=Lax (or Strict) on cookies.",
                    evidence=cookie[:120],
                ))
        return 1

    def _check_disclosure(self, h, findings) -> int:
        for header in ("server", "x-powered-by", "x-aspnet-version", "x-aspnetmvc-version"):
            val = h.get(header)
            # Only flag when a version number is leaked (e.g. "nginx/1.18.0").
            if val and re.search(r"\d+\.\d+", val):
                findings.append(Finding(
                    "low", "disclosure", f"Server/framework version disclosed ({header})",
                    "Exposing exact software versions helps attackers match known CVEs.",
                    f"Remove or obfuscate the '{header}' response header.",
                    evidence=f"{header}: {val}",
                ))
        return 1

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
            ))
        elif acao == "*":
            findings.append(Finding(
                "info", "cors", "CORS allows any origin (Access-Control-Allow-Origin: *)",
                "Acceptable for public, non-credentialed APIs, but risky if the endpoint "
                "ever returns user-specific data.",
                "Restrict CORS to known origins if the response is not fully public.",
            ))
        return 1

    def _check_content(self, final_url, body, findings) -> int:
        is_https = final_url.lower().startswith("https://")
        if is_https and re.search(r'(?:src|href)\s*=\s*["\']http://', body, re.I):
            findings.append(Finding(
                "medium", "content", "Mixed content: HTTP resources on an HTTPS page",
                "Insecure http:// resources on a secure page can be tampered with and "
                "are blocked by modern browsers.",
                "Load all scripts, styles, images and iframes over https://.",
            ))
        # Password field on a non-HTTPS action.
        if re.search(r'<form[^>]+action\s*=\s*["\']http://', body, re.I) and \
                re.search(r'type\s*=\s*["\']password', body, re.I):
            findings.append(Finding(
                "high", "content", "Password form submits over plain HTTP",
                "Credentials are sent unencrypted and can be captured on the network.",
                "Point the form action at an https:// endpoint.",
            ))
        return 1

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

        score = 100
        counts = {s: 0 for s in SEVERITY_WEIGHTS}
        for f in unique:
            counts[f.severity] = counts.get(f.severity, 0) + 1
            score -= SEVERITY_WEIGHTS.get(f.severity, 0)
        score = max(0, min(100, score))
        grade = _grade(score)

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
        return t.lstrip().startswith("{") and ("secret" in t.lower() or "password" in t.lower() or "key" in t.lower())
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
