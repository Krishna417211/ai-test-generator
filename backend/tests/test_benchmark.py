"""
Tests for services/benchmark.py — the grounding/fragility leaderboard harness.

The properties that matter: a suite grounded to real selectors scores high, a
hallucinated one scores low, the pooled rate weighs big repos more than small
ones, and a case with no selectors is excluded from the rate rather than gifted
a perfect one.
"""

from services.benchmark import run_benchmark, format_markdown


GOOD_CASE = {
    "name": "well-grounded",
    "source": {
        "App.tsx": '<form id="login"><input data-testid="email"/><button id="go">Go</button></form>',
    },
    "suite": [{
        "filename": "login.spec.ts",
        "content": (
            "page.getByTestId('email').fill('a@b.c');\n"
            "page.locator('#go').click();\n"
            "page.locator('#login').isVisible();\n"
        ),
    }],
}

BAD_CASE = {
    "name": "hallucinated",
    "source": {"App.tsx": '<div>nothing to select here</div>'},
    "suite": [{
        "filename": "login.spec.ts",
        "content": "page.locator('#ghost').click();\npage.locator('.phantom').click();\n",
    }],
}

EMPTY_CASE = {
    "name": "no-selectors",
    "source": {"App.tsx": "<div/>"},
    "suite": [{"filename": "readme.spec.ts", "content": "// nothing located here"}],
}


def test_good_case_scores_high_bad_case_low():
    report = run_benchmark([GOOD_CASE, BAD_CASE])
    rows = {r.name: r for r in report.rows}
    assert rows["well-grounded"].rate == 1.0
    assert rows["hallucinated"].rate == 0.0


def test_empty_case_excluded_from_rate_not_gifted_100():
    report = run_benchmark([EMPTY_CASE])
    row = report.rows[0]
    assert row.rate is None
    assert report.mean_verified_rate is None      # nothing measurable
    assert report.pooled_verified_rate is None


def test_pooled_rate_weighs_by_selector_count():
    # A big well-grounded repo + a small hallucinated one: pooled rate should sit
    # close to the big repo, while the naive mean-of-rates would be near 0.5.
    big = {
        "name": "big",
        "source": {"A.tsx": "".join(f'<button id="b{i}">x</button>' for i in range(10))},
        "suite": [{"filename": "s.ts",
                   "content": "".join(f"page.locator('#b{i}').click();\n" for i in range(10))}],
    }
    report = run_benchmark([big, BAD_CASE])
    assert report.pooled_verified_rate == round(10 / 12, 3)   # 10 of 12 selectors
    assert report.pooled_verified_rate > report.mean_verified_rate


def test_grade_distribution_and_markdown():
    report = run_benchmark([GOOD_CASE, BAD_CASE])
    dist = report.grade_distribution()
    assert sum(dist.values()) == 2
    md = format_markdown(report)
    assert "| Repo |" in md
    assert "well-grounded" in md and "hallucinated" in md
    assert "verified-selector rate" in md


def test_dom_case_uses_live_dom():
    case = {
        "name": "dom-grounded",
        "source": {},   # empty source — only the DOM can verify
        "dom_html": '<button data-testid="submit">Go</button>',
        "suite": [{"filename": "s.ts", "content": "page.getByTestId('submit').click();\n"}],
    }
    report = run_benchmark([case])
    assert report.rows[0].rate == 1.0


def test_report_as_dict_is_serializable():
    import json
    report = run_benchmark([GOOD_CASE, BAD_CASE, EMPTY_CASE])
    blob = json.dumps(report.as_dict())
    assert '"cases": 3' in blob
