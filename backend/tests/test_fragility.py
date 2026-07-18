"""
Tests for services/fragility.py — the selector break-risk scorer.

The contract we care about is *ordering*, not exact numbers: a stable contract
(test id, role) must always score lower than a positional or generated-class
locator. If a refit changes the weights, these tests should still hold as long
as the hierarchy is respected.
"""

from dataclasses import dataclass

from services import fragility
from services.fragility import score_selector, extract_selectors, analyze


@dataclass
class _File:
    filename: str
    content: str


# ── single-selector scoring: the hierarchy must hold ─────────────────────────

def test_test_id_is_solid():
    r = score_selector("getByTestId('login-button')")
    assert r.grade == "solid"
    assert r.score < 0.25
    assert r.suggestion is None  # already anchored


def test_role_beats_bare_tag():
    role = score_selector("getByRole('button', { name: 'Log in' })")
    tag = score_selector("button")
    assert role.score < tag.score


def test_absolute_xpath_is_brittle():
    r = score_selector("/html/body/div[2]/div/button")
    assert r.grade == "brittle"
    assert r.score >= 0.6
    assert r.suggestion is not None


def test_nth_child_is_risky_or_worse():
    r = score_selector("div > div:nth-child(3) > button")
    assert r.score >= 0.4
    assert any("positional" in reason for reason in r.reasons)


def test_hashed_class_flagged():
    r = score_selector(".css-1a2b3c4")
    assert r.score >= 0.4
    assert "data-testid" in (r.suggestion or "")


def test_hierarchy_ordering_end_to_end():
    # Strictly increasing risk down the well-known locator hierarchy.
    ladder = [
        "getByTestId('x')",
        "getByRole('button', { name: 'Go' })",
        "#login-form",
        ".btn.primary.large.rounded",
        "div > div > div:nth-child(2) > button",
        "/html/body/div/div/button",
    ]
    scores = [score_selector(s).score for s in ladder]
    assert scores == sorted(scores), scores


def test_stable_id_not_treated_as_hashed():
    stable = score_selector("#login-form")
    hashed = score_selector("#css-1a2b3c")
    assert stable.score < hashed.score


# ── extraction across frameworks ─────────────────────────────────────────────

def test_extract_playwright_and_cypress_and_selenium():
    content = """
      await page.locator('.card:nth-child(2)').click();
      await page.getByTestId('submit').click();
      cy.get('[data-testid="row"]').should('exist');
      driver.find_element(By.XPATH, "//button[@id='go']")
      await page.waitForSelector('#main');
    """
    sels = extract_selectors(content)
    assert ".card:nth-child(2)" in sels
    assert any("getByTestId" in s for s in sels)
    assert '[data-testid="row"]' in sels
    assert "#main" in sels


def test_extract_dedupes_preserving_order():
    content = "page.locator('#a'); page.locator('#b'); page.locator('#a');"
    assert extract_selectors(content) == ["#a", "#b"]


# ── suite-level report ───────────────────────────────────────────────────────

def test_analyze_counts_and_grade():
    files = [
        _File("login.spec.ts", """
            page.getByTestId('user').fill('a');
            page.getByRole('button', { name: 'Log in' }).click();
        """),
        _File("brittle.spec.ts", """
            page.locator('/html/body/div[2]/button').click();
            page.locator('div > div > div:nth-child(4) > a').click();
        """),
    ]
    report = analyze(files)
    assert report.total == 4
    assert report.brittle >= 1
    # Worst offender surfaces first in the serialized report.
    worst = report.as_dict()["worst"]
    assert worst[0]["score"] >= worst[-1]["score"]
    assert report.grade in {"A", "B", "C", "D", "F"}


def test_empty_suite_has_neutral_report():
    report = analyze([])
    assert report.total == 0
    assert report.grade == "—"
    assert report.as_dict()["mean_score"] == 0.0


def test_weights_are_data_not_hardcoded_logic():
    # The refit story depends on this: zeroing a weight must change the score.
    original = fragility.FEATURE_WEIGHTS["nth_child"]
    try:
        fragility.FEATURE_WEIGHTS["nth_child"] = 0.0
        low = score_selector("ul > li:nth-child(3)").score
    finally:
        fragility.FEATURE_WEIGHTS["nth_child"] = original
    high = score_selector("ul > li:nth-child(3)").score
    assert high > low
