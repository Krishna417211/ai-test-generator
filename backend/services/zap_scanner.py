"""
zap_scanner.py — active vulnerability scanning via an OWASP ZAP daemon.

The passive scanner (security_scanner.py) reads a site's *configuration*. It can
tell you a CSP is missing; it can never tell you a form is *actually* injectable,
because proving that means sending a payload and observing the app mishandle it.
ZAP does exactly that — it spiders the app and runs active checks (reflected XSS,
SQL injection, path traversal, …) — and this module drives it and maps its alerts
back into the same Finding/ScanResult shape the passive scanner and the frontend
already speak, so the UI needs no new vocabulary.

Three properties matter, and they mirror the rest of this codebase:

  • **Consent is mandatory.** Active scanning sends attack traffic. scan() refuses
    unless the caller both enables it (settings.zap_allow_active) and asserts
    authorization for this specific target (authorized=True). The same scope +
    SSRF guard the passive scanner uses runs first, so ZAP can't be pointed at an
    internal address or a third party's site any more than the passive path can.

  • **Optional.** No ZAP daemon configured (settings.zap_address empty) means the
    feature is unavailable — available() says so and scan() raises a clear error.
    Nothing else in the product depends on it.

  • **Reuse, not reinvention.** ZAP owns the detection logic; we own only the
    translation. Risk → severity, alert → Finding, and a grade derived the same
    way (any High/critical is an F, because an exploitable bug is not a C).
"""

from __future__ import annotations

import time
import asyncio
import logging
from dataclasses import asdict
from typing import Optional
from urllib.parse import urlparse

import httpx

from config import settings
from services import security_scanner as scanner
from services.security_scanner import Finding, ScanResult
from services.progress import NullProgress, Progress

logger = logging.getLogger(__name__)

# ZAP's risk vocabulary → the severity words the rest of the app uses. ZAP has no
# "critical"; its "High" is the top band, which we keep as "high" (grade logic
# treats high the same weight-class for the F floor below).
# How often to report progress while waiting on a long ZAP scan. Short enough
# that no proxy or browser sees an idle stream, long enough not to spam the UI.
_HEARTBEAT_SECONDS = 10.0

_RISK_TO_SEVERITY = {
    "High": "high",
    "Medium": "medium",
    "Low": "low",
    "Informational": "info",
    "Info": "info",
}


class ZapUnavailableError(Exception):
    """No ZAP daemon is configured or reachable."""


class ZapAuthorizationError(Exception):
    """Active scanning was requested without the required authorization."""


def _map_alert(alert: dict) -> Finding:
    """Translate one ZAP alert into the app's Finding shape.

    ZAP alert fields we use: risk, alert (name), description, solution, evidence,
    param, url, reference. category is always "vulnerability" — these are not the
    passive scanner's hardening categories, and labelling them so keeps the two
    kinds of finding distinguishable in the UI and the store.
    """
    severity = _RISK_TO_SEVERITY.get(alert.get("risk", ""), "info")
    name = alert.get("alert") or alert.get("name") or "Issue"
    evidence_bits = []
    if alert.get("param"):
        evidence_bits.append(f"param={alert['param']}")
    if alert.get("evidence"):
        evidence_bits.append(str(alert["evidence"])[:200])
    if alert.get("url"):
        evidence_bits.append(f"at {alert['url']}")
    return Finding(
        severity=severity,
        category="vulnerability",
        title=name,
        description=(alert.get("description") or "").strip(),
        remediation=(alert.get("solution") or "").strip(),
        evidence=" · ".join(evidence_bits),
        # ZAP references are curated URLs, not a model's guess — safe to pass
        # through. Take the first line if several are newline-joined.
        video_url=(alert.get("reference") or "").splitlines()[0].strip()
        if alert.get("reference") else "",
    )


def _grade(counts: dict) -> tuple[int, str]:
    """A score/grade from ZAP counts. An active finding is a real bug, so the
    scale is blunter than the passive scanner's: any high is an F, mediums cost
    a lot, lows a little. This is a summary for humans, not a precise metric."""
    score = 100
    score -= 40 * counts.get("critical", 0)
    score -= 40 * counts.get("high", 0)
    score -= 12 * counts.get("medium", 0)
    score -= 3 * counts.get("low", 0)
    score = max(0, min(100, score))
    if counts.get("critical") or counts.get("high"):
        return score, "F"
    if score >= 90: return score, "A"
    if score >= 80: return score, "B"
    if score >= 65: return score, "C"
    if score >= 50: return score, "D"
    return score, "F"


class ZapScanner:
    """Drives a ZAP daemon over its REST API. Stateless between scans."""

    def __init__(self, address: Optional[str] = None, api_key: Optional[str] = None,
                 timeout: float = 30.0):
        self.address = (address if address is not None else settings.zap_address).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.zap_api_key
        self.http_timeout = timeout

    # ── daemon plumbing ──────────────────────

    def _call(self, path: str, **params) -> dict:
        if self.api_key:
            params["apikey"] = self.api_key
        resp = httpx.get(f"{self.address}{path}", params=params, timeout=self.http_timeout)
        resp.raise_for_status()
        return resp.json()

    async def _acall(self, path: str, **params) -> dict:
        """_call off the event loop — the ZAP REST client is blocking, and a long
        active scan must not freeze the async server while it polls."""
        return await asyncio.to_thread(self._call, path, **params)

    def available(self) -> bool:
        """True if a daemon is configured and answers. Never raises."""
        return self.availability() is None

    def availability(self) -> Optional[str]:
        """None if ZAP is usable, else a short reason it isn't. Never raises.

        The reason matters. "Active scanning isn't available" with nothing after
        it is unfalsifiable from the outside — it looks identical whether the
        daemon is missing, still booting, or rejecting our API key, and that made
        a simple key mismatch in production take far too long to identify. Each
        of those has a different fix, so each gets named.
        """
        if not self.address:
            return "no ZAP daemon is configured on the server"
        try:
            self._call("/JSON/core/view/version/")
            return None
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if code in (401, 403):
                return ("the ZAP daemon rejected our API key — the backend and the "
                        "daemon are configured with different ZAP_API_KEY values")
            return f"the ZAP daemon answered HTTP {code}"
        except httpx.RemoteProtocolError:
            # ZAP does not answer 403 to a bad API key — it closes the connection
            # without sending anything, which surfaces here as a protocol error.
            # Measured against ZAP 2.17: a wrong key and a missing key both give
            # "Empty reply from server", while a correct key returns 200. So a
            # disconnect on this endpoint is, in practice, the key being wrong —
            # which is exactly how this failed in production.
            return ("the ZAP daemon rejected our API key (it closed the connection) "
                    "— the backend and the daemon have different ZAP_API_KEY values")
        except httpx.ConnectError:
            return ("the ZAP daemon isn't reachable — it may not be running, or is "
                    "still starting up (it takes about a minute)")
        except httpx.TimeoutException:
            return "the ZAP daemon timed out — it may still be starting up"
        except Exception as e:
            return f"the ZAP daemon could not be reached ({type(e).__name__})"

    # ── the scan ─────────────────────────────

    async def scan(
        self,
        url: str,
        *,
        active: bool = False,
        authorized: bool = False,
        progress: Optional[Progress] = None,
        poll_interval: float = 2.0,
        max_wait: Optional[float] = None,
    ) -> ScanResult:
        """Spider `url`, optionally run active checks, and return a ScanResult.

        active=True sends attack traffic and therefore requires BOTH the master
        switch (settings.zap_allow_active) and an explicit authorized=True from
        the caller asserting the user owns this target. Passive-only (active=
        False) still spiders and reports what ZAP's passive rules see, without
        sending payloads.
        """
        say = progress or NullProgress()
        if not self.address:
            raise ZapUnavailableError(
                "Active scanning isn't configured. Set zap_address to a running "
                "ZAP daemon to enable it."
            )
        if active:
            if not settings.zap_allow_active:
                raise ZapAuthorizationError(
                    "Active scanning is disabled on this server (zap_allow_active)."
                )
            if not authorized:
                raise ZapAuthorizationError(
                    "Active scanning sends attack traffic and needs explicit "
                    "confirmation that you own or are authorized to test the target."
                )

        await say.start("target")
        # Same scope + SSRF guard as the passive scanner: refuses third-party and
        # private/internal targets before ZAP touches the network.
        target = scanner.validate_target(url)
        if not self.available():
            raise ZapUnavailableError(f"ZAP daemon at {self.address} is not reachable.")
        await say.done("target", f"{urlparse(target).netloc} — scanning via ZAP")

        deadline = time.monotonic() + (max_wait if max_wait is not None
                                       else settings.zap_timeout_seconds)

        # Seed, then spider to discover URLs.
        await say.start("checks")
        await self._acall("/JSON/core/action/accessUrl/", url=target)
        sid = (await self._acall("/JSON/spider/action/scan/", url=target, recurse="true"))["scan"]
        await self._await_status("/JSON/spider/view/status/", sid, deadline, poll_interval,
                                 say, "spider")
        found = len((await self._acall("/JSON/spider/view/results/", scanId=sid))["results"])

        if active:
            aid = (await self._acall("/JSON/ascan/action/scan/", url=target, recurse="true"))["scan"]
            await self._await_status("/JSON/ascan/view/status/", aid, deadline, poll_interval,
                                     say, "active scan")
            mode = "active"
        else:
            mode = "spider"
        await say.done("checks", f"{found} URLs discovered · {mode} scan complete")

        # Collect and translate alerts.
        await say.start("score")
        raw = (await self._acall("/JSON/core/view/alerts/", baseurl=target)).get("alerts", [])
        findings = _dedupe([_map_alert(a) for a in raw])
        counts = {s: 0 for s in ("critical", "high", "medium", "low", "info")}
        for f in findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        score, grade = _grade(counts)
        await say.done("score", f"Grade {grade} · {len(findings)} findings")

        return ScanResult(
            url=url,
            final_url=target,
            score=score,
            grade=grade,
            findings=[asdict(f) for f in findings],
            counts=counts,
            checks_run=found,
        )

    async def _await_status(self, path: str, scan_id, deadline: float, poll: float,
                            say: Optional[Progress] = None, label: str = "") -> None:
        """Poll a ZAP 0–100 status endpoint until 100 or the deadline passes.

        `say` is pinged periodically while we wait. That is not cosmetic: an
        active scan runs for minutes, and this loop previously emitted nothing
        for its whole duration. A streamed response with no bytes on the wire for
        minutes looks hung to the user and is liable to be cut by a proxy or the
        browser long before ZAP finishes — so the scan has to keep talking while
        it works, and the percentage is genuinely useful besides.
        """
        last_beat = 0.0
        while True:
            status = int((await self._acall(path, scanId=scan_id))["status"])
            if status >= 100:
                return
            if time.monotonic() > deadline:
                logger.warning("ZAP scan exceeded its time budget; returning partial results.")
                return
            now = time.monotonic()
            if say is not None and now - last_beat >= _HEARTBEAT_SECONDS:
                last_beat = now
                # Re-assert the step as running. The UI treats a repeated running
                # event as "still going", and it puts bytes on the wire.
                await say.start("checks")
                logger.info(f"ZAP {label or 'scan'} at {status}%")
            await asyncio.sleep(poll)


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """One row per (severity, title); ZAP repeats an alert per affected URL."""
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    seen, out = set(), []
    for f in findings:
        key = (f.severity, f.title)
        if key not in seen:
            seen.add(key)
            out.append(f)
    out.sort(key=lambda f: order.get(f.severity, 9))
    return out
