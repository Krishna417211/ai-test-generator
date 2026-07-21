"""
Tests for services/grounding.py — source + DOM selector verification.

The properties that matter:
  * a selector present in ground truth verifies and carries provenance;
  * a hallucinated selector does not verify and reports what was missing;
  * source and DOM are checked by the same rules (symmetry);
  * DOM fetching goes through the scanner's SSRF guard.
"""

from dataclasses import dataclass

import pytest

from services import grounding
from services.grounding import (
    Anchor, KIND_ID, KIND_TESTID, KIND_ROLE, KIND_CLASS,
    build_source_index, build_dom_index, parse_anchors,
    ground_selector, ground_suite,
)


@dataclass
class _File:
    filename: str
    content: str


SOURCE = {
    "src/Login.tsx": (
        '<form id="login-form">\n'
        '  <input name="email" data-testid="email-input" className="field lg" />\n'
        '  <button role="button" className="btn primary">Log in</button>\n'
        '</form>\n'
    ),
}


# ── source index + provenance ────────────────────────────────────────────────

def test_source_index_extracts_anchors_with_provenance():
    idx = build_source_index(SOURCE)
    assert idx.has(Anchor(KIND_ID, "login-form"))
    assert idx.has(Anchor(KIND_TESTID, "email-input"))
    assert idx.has(Anchor(KIND_CLASS, "primary"))
    prov = idx.provenance(Anchor(KIND_TESTID, "email-input"))
    assert prov and prov[0].file == "src/Login.tsx"
    assert prov[0].line == 2   # data-testid is on the second line


def test_verified_selector_carries_file_and_line():
    idx = build_source_index(SOURCE)
    g = ground_selector("getByTestId('email-input')", "spec.ts", idx)
    assert g.verified is True
    assert g.source == "source"
    assert g.provenance[0].as_dict() == {"file": "src/Login.tsx", "line": 2}


def test_hallucinated_selector_reports_missing():
    idx = build_source_index(SOURCE)
    g = ground_selector("#submit-btn", "spec.ts", idx)
    assert g.verified is False
    assert Anchor(KIND_ID, "submit-btn") in g.missing


def test_compound_selector_needs_all_anchors():
    idx = build_source_index(SOURCE)
    # role=button exists and class primary exists → verified.
    ok = ground_selector("button.primary", "s.ts", idx)
    assert ok.verified is True
    # class primary exists but id ghost does not → not verified.
    bad = ground_selector("#ghost.primary", "s.ts", idx)
    assert bad.verified is False


# ── DOM index (symmetry with source) ─────────────────────────────────────────

DOM = """
<html><body>
  <form id="login-form">
    <input name="email" data-testid="email-input" class="field lg">
    <button class="btn primary">Log in</button>
  </form>
</body></html>
"""


def test_dom_index_matches_source_rules():
    dom = build_dom_index(DOM)
    assert dom.has(Anchor(KIND_TESTID, "email-input"))
    assert dom.has(Anchor(KIND_ID, "login-form"))
    # implicit role: a <button> is role=button to getByRole
    assert dom.has(Anchor(KIND_ROLE, "button"))
    # text anchor from the button
    assert dom.has(grounding.Anchor(grounding.KIND_TEXT, "Log in"))


def test_dom_grounding_preferred_and_labeled():
    src = build_source_index({})           # empty source
    dom = build_dom_index(DOM)
    g = ground_selector("getByTestId('email-input')", "s.ts", src, dom_index=dom)
    assert g.verified is True
    assert g.source == "dom"               # verified by DOM, not source


def test_getbytext_checkable_against_dom():
    dom = build_dom_index(DOM)
    src = build_source_index({})
    g = ground_selector("getByText('Log in')", "s.ts", src, dom_index=dom)
    assert g.verified is True


# ── declining an unusable (client-rendered) DOM ──────────────────────────────

SPA_SHELL = '<html><body><div id="root"></div><script src="/assets/app.js"></script></body></html>'
NEXT_SHELL = '<html><body><div id="__next"></div><script src="/_next/x.js"></script></body></html>'
THIN_PLAIN = '<html><body><p>maintenance</p></body></html>'


def test_spa_shell_dom_is_not_useful():
    dom = build_dom_index(SPA_SHELL)
    assert grounding.dom_index_is_useful(dom) is False


def test_rendered_dom_is_useful():
    # the rich DOM fixture above clears the anchor threshold
    assert grounding.dom_index_is_useful(build_dom_index(DOM)) is True


def test_thin_dom_describes_spa_when_mount_present():
    for shell in (SPA_SHELL, NEXT_SHELL):
        msg = grounding.describe_thin_dom(shell)
        assert "client-rendered" in msg and "source" in msg


def test_thin_dom_describes_generic_when_no_mount():
    msg = grounding.describe_thin_dom(THIN_PLAIN)
    assert "too little markup" in msg and "client-rendered" not in msg


def test_thin_dom_rendered_says_it_ran_the_page():
    # When a browser DID run the page and it's still sparse, the message must not
    # claim we skipped executing it — that would be false.
    msg = grounding.describe_thin_dom(SPA_SHELL, rendered=True)
    assert "headless browser" in msg
    assert "without executing" not in msg


def test_report_carries_dom_note_in_dict():
    report = grounding.GroundingReport(checked_against=["source"])
    report.dom_note = grounding.describe_thin_dom(SPA_SHELL)
    d = report.as_dict()
    assert d["dom_note"] and d["checked_against"] == ["source"]


# ── selector parsing ─────────────────────────────────────────────────────────

def test_parse_anchors_covers_kinds():
    anchors = parse_anchors("getByRole('button')")
    assert Anchor(KIND_ROLE, "button") in anchors
    anchors = parse_anchors("input[name='email'].field#main")
    kinds = {a.kind for a in anchors}
    assert {"name", "class", "id"} <= kinds


# ── suite-level report ───────────────────────────────────────────────────────

def test_ground_suite_rate_and_unverified():
    files = [
        _File("login.spec.ts", """
            page.getByTestId('email-input').fill('a@b.c');
            page.locator('#login-form').isVisible();
            page.locator('#does-not-exist').click();
        """),
    ]
    report = ground_suite(files, SOURCE)
    assert report.total == 3
    assert report.verified == 2
    assert report.rate == round(2 / 3, 3)
    assert report.checked_against == ["source"]
    unv = report.unverified()
    assert len(unv) == 1 and unv[0].selector == "#does-not-exist"


def test_empty_suite_has_none_rate():
    report = ground_suite([], SOURCE)
    assert report.total == 0
    assert report.rate is None


# ── DOM fetch reuses the scanner's SSRF guard ────────────────────────────────

def test_fetch_dom_rejects_private_address(monkeypatch):
    import asyncio
    # validate_target should refuse a private host before any request is made.
    def boom(url):
        raise grounding.scanner.ScanError("private")
    monkeypatch.setattr(grounding.scanner, "validate_target", boom)
    with pytest.raises(grounding.scanner.ScanError):
        asyncio.run(grounding.fetch_dom("http://127.0.0.1"))
