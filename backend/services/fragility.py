"""
fragility.py — predict which selectors will break before the suite is ever run.

The failure that makes people abandon a generated E2E suite is not a selector
that is *wrong today* — validation catches those. It is a selector that is
*right today and brittle tomorrow*: `div > div:nth-child(3) > button` matches
now and breaks the first time someone adds a wrapper. A human reviewer spots
these on sight; this module encodes that judgement so we can spot them at
generation time, flag them, and prefer a sturdier locator.

Why a weighted feature model and not an LLM call
------------------------------------------------
Brittleness is a small, stable, explainable function of a selector's *shape* —
does it lean on position, on depth, on a hashed class, or on a stable contract
like `data-testid`/role? That is exactly what a linear scorer over named
features does well and cheaply, with no token cost and a reason attached to
every score. The weights below are hand-set from the well-known locator
hierarchy (Playwright/Testing-Library guidance), but they are deliberately
*data* (`FEATURE_WEIGHTS`), not code: the benchmark harness (services metrics)
and the file-importance work can later re-fit them from real pass/break data
without touching the logic here.

Score is 0.0 (rock solid) → 1.0 (will break). It is a *risk* estimate, named as
one — never dressed up as a probability we measured.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Optional


# Each feature maps a shape we can detect to how much risk it adds. Positive
# weights add fragility; negative weights (stable contracts) subtract it. Kept
# as a plain dict so it can be serialized, inspected, and re-fit from data.
FEATURE_WEIGHTS: dict[str, float] = {
    "absolute_xpath": 0.95,     # /html/body/div[2]/... — breaks on any structural edit
    "nth_child": 0.70,          # :nth-child / :nth-of-type / [n] — positional
    "deep_chain": 0.45,         # 4+ combinators — couples the test to layout depth
    "hashed_class": 0.65,       # css-1a2b3c, sc-bdfBwe, jsx-1234 — build-generated, unstable
    "index_access": 0.40,       # .nth(3), .eq(2), [2] — order-dependent
    "text_match": 0.25,         # getByText / :has-text — breaks on copy changes
    "bare_tag": 0.55,           # a plain tag like "button" or "div" — matches too much
    "long_class_chain": 0.30,   # .a.b.c.d stacked classes — styling, not identity
    # Stable contracts pull the score back down.
    "test_id": -0.85,           # data-testid / data-cy / getByTestId — purpose-built for tests
    "role": -0.55,              # getByRole / [role=] — accessibility contract
    "aria_label": -0.45,        # getByLabel / aria-label
    "stable_id": -0.35,         # #login-form — an id that does not look generated
}

BIAS = 0.35  # a bare, featureless selector sits at medium risk, not zero


@dataclass
class SelectorRisk:
    selector: str
    file: str
    score: float                       # 0.0 solid → 1.0 fragile
    grade: str                         # solid | ok | risky | brittle
    reasons: list[str] = field(default_factory=list)
    suggestion: Optional[str] = None   # a sturdier locator, when we can name one

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class FragilityReport:
    """Suite-level view: the counts a UI shows, plus the worst offenders."""
    total: int = 0
    brittle: int = 0                   # score >= 0.6
    risky: int = 0                     # 0.4 <= score < 0.6
    mean_score: float = 0.0
    items: list[SelectorRisk] = field(default_factory=list)

    @property
    def grade(self) -> str:
        """One letter for the whole suite, from the mean risk."""
        if not self.total:
            return "—"
        m = self.mean_score
        if m < 0.25:
            return "A"
        if m < 0.40:
            return "B"
        if m < 0.55:
            return "C"
        if m < 0.70:
            return "D"
        return "F"

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "brittle": self.brittle,
            "risky": self.risky,
            "mean_score": round(self.mean_score, 3),
            "grade": self.grade,
            # Only the worst handful are worth surfacing; the rest are noise.
            "worst": [i.as_dict() for i in self.top(8)],
        }

    def top(self, n: int) -> list[SelectorRisk]:
        return sorted(self.items, key=lambda i: i.score, reverse=True)[:n]


# ── feature detectors ────────────────────────────────────────────────────────
# Each returns (fired: bool, evidence: str). Kept small and independent so the
# weight table stays the single source of truth for how much each one matters.

_HASHED_CLASS = re.compile(
    r"\b(?:css-[a-z0-9]{5,}|sc-[a-zA-Z]{6,}|jsx-\d{3,}|[a-z]+-[a-f0-9]{6,}|_[a-zA-Z0-9]{5,}_)\b"
)


def _features(sel: str) -> list[tuple[str, str]]:
    """Detect which fragility features a selector exhibits."""
    fired: list[tuple[str, str]] = []
    s = sel.strip()

    if s.startswith("/") or s.startswith("(//") or re.match(r"^//", s):
        # XPath. Absolute (rooted at /html or /) is the brittlest thing there is.
        if re.match(r"^/html|^/\w", s) and "//" not in s[:2]:
            fired.append(("absolute_xpath", s))
    if re.search(r":nth-(?:child|of-type)\(|\[\s*\d+\s*\]", s):
        fired.append(("nth_child", "positional selector"))
    if re.search(r"\.nth\(\s*\d+|\.eq\(\s*\d+|\.first\(\)|\.last\(\)", s):
        fired.append(("index_access", "index-based access"))

    combinators = len(re.findall(r"\s*>\s*|\s+(?=[.#\[a-zA-Z])", s))
    if combinators >= 4:
        fired.append(("deep_chain", f"{combinators} levels deep"))

    if _HASHED_CLASS.search(s):
        fired.append(("hashed_class", "build-generated class name"))

    if re.search(r"getByText\(|has-text\(|:text\(|:contains\(", s):
        fired.append(("text_match", "matches on visible text"))

    stacked = len(re.findall(r"\.[a-zA-Z][\w-]*", s))
    if stacked >= 4 and "#" not in s:
        fired.append(("long_class_chain", f"{stacked} stacked classes"))

    if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9]*", s) and s.lower() in _COMMON_TAGS:
        fired.append(("bare_tag", f"bare <{s}> matches many elements"))

    # Stable contracts.
    if re.search(r"getByTestId\(|data-testid|data-cy", s):
        fired.append(("test_id", "uses a test id"))
    if re.search(r"getByRole\(|\[role=|role=", s):
        fired.append(("role", "uses an ARIA role"))
    if re.search(r"getByLabel\(|aria-label", s):
        fired.append(("aria_label", "uses an accessible label"))
    if re.match(r"^#[a-zA-Z][\w-]*$", s) and not _HASHED_CLASS.search(s):
        fired.append(("stable_id", "stable id"))

    return fired


_COMMON_TAGS = {
    "div", "span", "a", "p", "button", "input", "li", "ul", "img", "form",
    "section", "header", "footer", "nav", "table", "tr", "td", "h1", "h2", "h3",
}


def score_selector(sel: str, file: str = "") -> SelectorRisk:
    """Score a single selector's break-risk and, when possible, suggest better."""
    fired = _features(sel)
    raw = BIAS + sum(FEATURE_WEIGHTS.get(name, 0.0) for name, _ in fired)
    score = max(0.0, min(1.0, raw))

    reasons = [ev for _, ev in fired if FEATURE_WEIGHTS.get(_, 0) > 0]
    grade = (
        "solid" if score < 0.25 else
        "ok" if score < 0.40 else
        "risky" if score < 0.60 else
        "brittle"
    )
    return SelectorRisk(
        selector=sel, file=file, score=round(score, 3), grade=grade,
        reasons=reasons or (["no stable anchor"] if score >= 0.4 else []),
        suggestion=_suggest(sel, fired),
    )


def _suggest(sel: str, fired: list[tuple[str, str]]) -> Optional[str]:
    """Name a sturdier locator when the shape tells us one exists."""
    names = {n for n, _ in fired}
    if names & {"test_id", "role", "aria_label", "stable_id"}:
        return None  # already anchored to something stable
    if "absolute_xpath" in names or "nth_child" in names or "deep_chain" in names:
        return "prefer getByRole()/getByTestId() over a positional or deep path"
    if "hashed_class" in names or "long_class_chain" in names:
        return "styling classes are build-generated; add a data-testid to the element"
    if "text_match" in names:
        return "text is fine for intent, but pair it with a role: getByRole('button', { name })"
    if "bare_tag" in names:
        return "scope the tag to a role or test id — a bare tag matches too much"
    return None


# ── selector extraction from generated test code ─────────────────────────────
# We score what the tests will actually query, so we pull the argument out of
# the locator calls the frameworks use rather than guessing from raw CSS.

# The selector argument may itself contain the *other* quote char
# (cy.get('[data-testid="row"]')), so we match the opening quote and capture up
# to the same quote via a backreference rather than a quote-excluding class.
_LOCATOR_CALLS = re.compile(
    r"""(?:locator|querySelector(?:All)?|\$x?|cy\.get|cy\.xpath|waitForSelector)
        \s*\(\s*(["'`])((?:(?!\1).)+)\1""",
    re.VERBOSE,
)
# Selenium: find_element(..., "selector") — the locator string is the LAST arg,
# after a By.* strategy, so we anchor on the trailing quoted string.
_SELENIUM_CALLS = re.compile(
    r"""find_element[s_a-z]*\([^)]*?(["'`])((?:(?!\1).)+)\1\s*\)""",
    re.VERBOSE,
)
_SEMANTIC_CALLS = re.compile(
    r"""(getBy(?:Role|TestId|Label|Text)\s*\(\s*["'`][^"'`]*["'`])""",
)


def extract_selectors(content: str) -> list[str]:
    """Every distinct thing the suite locates by, framework-agnostic."""
    found: list[str] = []
    found += [m.group(2) for m in _LOCATOR_CALLS.finditer(content)]
    found += [m.group(2) for m in _SELENIUM_CALLS.finditer(content)]
    found += _SEMANTIC_CALLS.findall(content)
    # Dedupe but keep order so the first occurrence's file wins in reports.
    seen: set[str] = set()
    out = []
    for f in found:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def analyze(files: list) -> FragilityReport:
    """Score a whole generated suite.

    `files` is any iterable of objects with `.filename` and `.content`
    (GeneratedFile from the writer agent), so this stays decoupled from it.
    """
    report = FragilityReport()
    scores: list[float] = []
    for f in files:
        for sel in extract_selectors(getattr(f, "content", "")):
            risk = score_selector(sel, getattr(f, "filename", ""))
            report.items.append(risk)
            scores.append(risk.score)
            if risk.score >= 0.6:
                report.brittle += 1
            elif risk.score >= 0.4:
                report.risky += 1
    report.total = len(scores)
    report.mean_score = round(sum(scores) / len(scores), 3) if scores else 0.0
    return report
