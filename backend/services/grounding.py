"""
grounding.py — verify every selector a suite uses against something real.

This is the trust spine of the product. An LLM will happily write
`page.locator('#submit-btn')` for a button whose id is actually `submit`, and
the suite looks perfect until it is run. We refuse to ship that unexamined, so
every selector the writer emits is checked against a ground truth and, when it
misses, sent back to the model to fix (the self-heal loop in writer_agent).

There are two ground truths, and they answer different questions:

  • **Source grounding** — does this selector exist in the repo's markup? Works
    for any repo, live or not, and carries *provenance*: the file and line the
    element was declared on, so the UI can say "this came from LoginForm.tsx:42"
    instead of asking the user to trust us.

  • **DOM grounding** — does this selector match an element on the *deployed*
    page right now? Stronger evidence (it is what the browser will actually
    see), but only possible when the user gives us a URL they own. We fetch the
    live HTML through the security scanner's SSRF-guarded path — never executing
    anything, only reading the DOM.

Both are built the same way on purpose: extract the set of anchors that exist
(ids, test-ids, classes, form names, ARIA roles, accessible labels, link/button
text), then check each generated selector's key against that set. Symmetry
means a selector verified against source and a selector verified against the
live DOM are compared by the same rules, and the heal loop does not care which
truth rejected a selector.

No CSS engine is used or needed: the selectors we generate resolve to one of a
handful of anchor kinds, and membership in the extracted set is exactly the
question "would `querySelector` find this". This keeps the module dependency
-free (stdlib `html.parser` only) and identical in behaviour across source and
DOM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional

import httpx

from services import security_scanner as scanner


# The anchor kinds we can both extract from ground truth and parse out of a
# generated selector. Everything selector-shaped reduces to one of these.
KIND_ID = "id"
KIND_TESTID = "testid"
KIND_CLASS = "class"
KIND_NAME = "name"
KIND_ROLE = "role"
KIND_LABEL = "label"
KIND_TEXT = "text"


@dataclass(frozen=True)
class Anchor:
    kind: str
    value: str


@dataclass
class Provenance:
    file: str
    line: int

    def as_dict(self) -> dict:
        return {"file": self.file, "line": self.line}


@dataclass
class GroundIndex:
    """The set of anchors that genuinely exist, with where each came from.

    `where` is empty for DOM indexes (the provenance is "the live page"); source
    indexes fill it so the UI can cite a file and line for a verified selector.
    """
    source: str                                  # "source" | "dom"
    anchors: set = field(default_factory=set)    # {Anchor}
    where: dict = field(default_factory=dict)    # Anchor -> list[Provenance]

    def has(self, anchor: Anchor) -> bool:
        return anchor in self.anchors

    def provenance(self, anchor: Anchor) -> list[Provenance]:
        return self.where.get(anchor, [])


# ── extracting ground truth from repo source ─────────────────────────────────

_SOURCE_PATTERNS = {
    KIND_ID: re.compile(r'\bid=["\']([^"\']+)["\']'),
    KIND_TESTID: re.compile(r'data-(?:testid|cy)=["\']([^"\']+)["\']'),
    KIND_NAME: re.compile(r'\bname=["\']([^"\']+)["\']'),
    KIND_ROLE: re.compile(r'\brole=["\']([^"\']+)["\']'),
    KIND_LABEL: re.compile(r'aria-label=["\']([^"\']+)["\']'),
}
_CLASS_PATTERN = re.compile(r'class(?:Name)?=["\']([^"\']+)["\']')


def build_source_index(source_files: dict[str, str]) -> GroundIndex:
    """Index every anchor declared in the repo, remembering file:line."""
    idx = GroundIndex(source="source")
    for filename, content in source_files.items():
        for lineno, line in enumerate(content.splitlines(), start=1):
            for kind, pat in _SOURCE_PATTERNS.items():
                for val in pat.findall(line):
                    _add(idx, Anchor(kind, val), filename, lineno)
            for group in _CLASS_PATTERN.findall(line):
                for cls in group.split():
                    _add(idx, Anchor(KIND_CLASS, cls), filename, lineno)
    return idx


def _add(idx: GroundIndex, anchor: Anchor, filename: str, lineno: int) -> None:
    idx.anchors.add(anchor)
    # Keep the first couple of declaration sites; more than that is noise and a
    # test only needs one place to point at.
    sites = idx.where.setdefault(anchor, [])
    if len(sites) < 3 and not any(p.file == filename and p.line == lineno for p in sites):
        sites.append(Provenance(filename, lineno))


# ── extracting ground truth from a live DOM ──────────────────────────────────

class _DomAnchorParser(HTMLParser):
    """Collect the same anchor kinds from rendered HTML that we pull from source.

    Text anchors come from the elements users actually locate by text — links
    and buttons — not every text node, so `getByText('Log in')` is checkable
    without the index ballooning to every word on the page.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors: set = set()
        self._text_tag: Optional[str] = None
        self._text_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.anchors.add(Anchor(KIND_ID, a["id"]))
        for k in ("data-testid", "data-cy"):
            if a.get(k):
                self.anchors.add(Anchor(KIND_TESTID, a[k]))
        if a.get("name"):
            self.anchors.add(Anchor(KIND_NAME, a["name"]))
        if a.get("role"):
            self.anchors.add(Anchor(KIND_ROLE, a["role"]))
        if a.get("aria-label"):
            self.anchors.add(Anchor(KIND_LABEL, a["aria-label"]))
        for cls in (a.get("class") or "").split():
            self.anchors.add(Anchor(KIND_CLASS, cls))
        # Implicit roles: a button element IS role=button to getByRole.
        if tag in _IMPLICIT_ROLE:
            self.anchors.add(Anchor(KIND_ROLE, _IMPLICIT_ROLE[tag]))
        if tag in ("a", "button"):
            self._text_tag, self._text_buf = tag, []

    def handle_data(self, data):
        if self._text_tag:
            self._text_buf.append(data)

    def handle_endtag(self, tag):
        if tag == self._text_tag:
            text = " ".join("".join(self._text_buf).split())
            if text:
                self.anchors.add(Anchor(KIND_TEXT, text))
            self._text_tag = None


_IMPLICIT_ROLE = {
    "a": "link", "button": "button", "nav": "navigation", "main": "main",
    "header": "banner", "footer": "contentinfo", "table": "table",
    "img": "img", "h1": "heading", "h2": "heading", "h3": "heading",
}


def build_dom_index(html: str) -> GroundIndex:
    """Index the anchors present in a page's rendered HTML."""
    parser = _DomAnchorParser()
    parser.feed(html)
    return GroundIndex(source="dom", anchors=parser.anchors)


async def fetch_dom(url: str, *, timeout: float = 12.0) -> str:
    """Fetch a page's HTML through the scanner's SSRF-guarded, non-executing path.

    Reuses `validate_target` (scope + DNS/SSRF guard) and `_redirect_guard`
    rather than re-deriving them, so DOM grounding cannot be turned into a
    request to an internal address any more than the scanner can.
    """
    final_target = scanner.validate_target(url)  # raises ScanError on private/out-of-scope
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": scanner.USER_AGENT},
        event_hooks={"response": [scanner._redirect_guard]},
    ) as client:
        resp = await client.get(final_target)
        return resp.text


# ── parsing a generated selector into checkable anchors ──────────────────────

_SEL_PARSERS = [
    (KIND_TESTID, re.compile(r"getByTestId\(\s*['\"]([^'\"]+)['\"]")),
    (KIND_TESTID, re.compile(r"data-(?:testid|cy)=['\"]?([\w-]+)")),
    (KIND_ROLE, re.compile(r"getByRole\(\s*['\"]([^'\"]+)['\"]")),
    (KIND_LABEL, re.compile(r"getByLabel\(\s*['\"]([^'\"]+)['\"]")),
    (KIND_TEXT, re.compile(r"getByText\(\s*['\"]([^'\"]+)['\"]")),
    (KIND_ID, re.compile(r"#([a-zA-Z][\w-]*)")),
    (KIND_NAME, re.compile(r"\[name=['\"]?([\w-]+)")),
    (KIND_ROLE, re.compile(r"\[role=['\"]?([\w-]+)")),
    (KIND_CLASS, re.compile(r"\.([a-zA-Z][\w-]*)")),
]


def parse_anchors(selector: str) -> list[Anchor]:
    """Reduce a selector to the anchors that must exist for it to match.

    A compound selector can contribute several (`button.primary#go` → role,
    class, id); each is checked independently, and any miss is a miss.
    """
    anchors: list[Anchor] = []
    for kind, pat in _SEL_PARSERS:
        for val in pat.findall(selector):
            a = Anchor(kind, val)
            if a not in anchors:
                anchors.append(a)
    return anchors


# ── the report the writer agent and API consume ──────────────────────────────

@dataclass
class SelectorGrounding:
    selector: str
    file: str
    anchors: list = field(default_factory=list)      # [Anchor]
    verified: bool = False
    source: Optional[str] = None                      # which truth verified it
    provenance: list = field(default_factory=list)    # [Provenance]
    missing: list = field(default_factory=list)       # [Anchor] that weren't found

    def as_dict(self) -> dict:
        return {
            "selector": self.selector,
            "file": self.file,
            "verified": self.verified,
            "source": self.source,
            "provenance": [p.as_dict() for p in self.provenance],
            "missing": [{"kind": a.kind, "value": a.value} for a in self.missing],
        }


@dataclass
class GroundingReport:
    total: int = 0
    verified: int = 0
    checked_against: list = field(default_factory=list)   # ["source"] or ["source","dom"]
    items: list = field(default_factory=list)             # [SelectorGrounding]

    @property
    def rate(self) -> Optional[float]:
        # None (not 1.0) for a suite that anchored to nothing — an empty proof
        # is not a perfect one. Mirrors writer_agent.Grounding.selector_rate.
        if not self.total:
            return None
        return round(self.verified / self.total, 3)

    def unverified(self) -> list:
        return [i for i in self.items if not i.verified]

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "verified": self.verified,
            "rate": self.rate,
            "checked_against": self.checked_against,
            "items": [i.as_dict() for i in self.items],
        }


def ground_selector(
    selector: str,
    file: str,
    source_index: GroundIndex,
    dom_index: Optional[GroundIndex] = None,
) -> SelectorGrounding:
    """Verify one selector against source, then (if present) the live DOM.

    A selector is verified when *all* its anchors are found in at least one
    ground truth. DOM evidence is preferred when available because it is what
    the browser will actually see; source is the fallback and the provenance
    carrier.
    """
    anchors = parse_anchors(selector)
    result = SelectorGrounding(selector=selector, file=file, anchors=anchors)
    if not anchors:
        # Nothing checkable (e.g. a pure text/aria call we couldn't parse) —
        # count it as neither verified nor a failure by leaving it unverified
        # but with no missing anchors, so the heal loop won't chase it.
        return result

    for index in (dom_index, source_index):   # DOM first: stronger evidence
        if index is None:
            continue
        missing = [a for a in anchors if not index.has(a)]
        if not missing:
            result.verified = True
            result.source = index.source
            for a in anchors:
                result.provenance.extend(index.provenance(a))
            return result

    # Not fully verified anywhere. Report what source lacked (its misses carry
    # provenance intent); prefer source's view since it is always present.
    result.missing = [a for a in anchors if not source_index.has(a)]
    return result


def ground_suite(
    files: list,
    source_files: dict[str, str],
    dom_index: Optional[GroundIndex] = None,
) -> GroundingReport:
    """Ground every selector in a generated suite against source (+ DOM)."""
    from services.fragility import extract_selectors   # local import: avoid cycle

    source_index = build_source_index(source_files)
    report = GroundingReport(
        checked_against=["source"] + (["dom"] if dom_index else [])
    )
    for f in files:
        for sel in extract_selectors(getattr(f, "content", "")):
            g = ground_selector(sel, getattr(f, "filename", ""), source_index, dom_index)
            if not g.anchors:
                continue  # unparseable — not part of the denominator
            report.items.append(g)
            report.total += 1
            if g.verified:
                report.verified += 1
    return report
