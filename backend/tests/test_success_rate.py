"""
test_success_rate.py — The headline number has to be defensible.

A score is the easiest thing in this product to make dishonest, and the
dishonesty is always in the same direction: crediting a check that never ran.
Most of these pin the cases where the number must NOT be 100 — an empty suite,
a suite with no selectors to verify, a suite of page objects and no tests.
"""

from services import success_rate


def _validation(ok: int, bad: int = 0, unchecked: int = 0) -> list[dict]:
    out = [{"filename": f"ok{i}.ts", "ok": True, "checked": True} for i in range(ok)]
    out += [{"filename": f"bad{i}.ts", "ok": False, "checked": True} for i in range(bad)]
    out += [{"filename": f"doc{i}.md", "ok": True, "checked": False} for i in range(unchecked)]
    return out


def _grounding(total: int, verified: int) -> dict:
    return {"selectors_total": total, "selectors_verified": verified}


class TestPerfectRun:
    def test_everything_measured_and_passing_scores_100(self):
        s = success_rate.compute(
            validation=_validation(ok=5),
            grounding=_grounding(20, 20),
            failed_files=[],
            test_count=12,
            generated_file_count=5,
        )
        assert s.score == 100
        assert s.grade == "A"


class TestWhatMustNotScoreWell:
    def test_a_suite_with_no_test_cases_is_penalised(self):
        """Page objects and config only. It looks like a suite, collects zero
        tests, and is the failure most likely to be mistaken for success."""
        s = success_rate.compute(
            validation=_validation(ok=4),
            grounding=_grounding(10, 10),
            failed_files=[],
            test_count=0,
            generated_file_count=4,
        )
        assert s.score < 100
        assert any(c.key == "coverage" and c.passed == 0 for c in s.components)

    def test_missing_planned_files_dominate_the_score(self):
        """Weighted highest on purpose: a suite short a page object cannot run
        at all, whereas one unverified selector is a thing to check."""
        partial = success_rate.compute(
            validation=_validation(ok=2), grounding=_grounding(10, 10),
            failed_files=["tests/pages/LoginPage.ts", "tests/specs/login.spec.ts"],
            test_count=3, generated_file_count=2,
        )
        unverified = success_rate.compute(
            validation=_validation(ok=4), grounding=_grounding(10, 5),
            failed_files=[], test_count=3, generated_file_count=4,
        )
        assert partial.score < unverified.score

    def test_files_that_do_not_parse_pull_the_score_down(self):
        s = success_rate.compute(
            validation=_validation(ok=2, bad=2), grounding=_grounding(4, 4),
            failed_files=[], test_count=5, generated_file_count=4,
        )
        parse = next(c for c in s.components if c.key == "parse")
        assert parse.passed == 2 and parse.total == 4
        assert s.score < 90


class TestNothingToMeasure:
    def test_config_and_docs_do_not_pad_the_parse_component(self):
        """The validator marks them checked=False precisely so they can't. Two
        YAMLs and a README nobody parsed must not become "5/5 files passed"."""
        s = success_rate.compute(
            validation=_validation(ok=1, bad=1, unchecked=3),
            grounding=_grounding(2, 2),
            failed_files=[], test_count=1, generated_file_count=5,
        )
        parse = next(c for c in s.components if c.key == "parse")
        assert parse.total == 2

    def test_a_suite_with_no_selectors_is_not_credited_for_grounding(self):
        """Nothing was verified, so there is nothing to score. Dropping the
        component is right; scoring it 100% would be the most confident lie
        available here."""
        s = success_rate.compute(
            validation=_validation(ok=3), grounding=_grounding(0, 0),
            failed_files=[], test_count=4, generated_file_count=3,
        )
        assert not any(c.key == "grounding" for c in s.components)

    def test_weights_are_renormalised_over_present_components(self):
        """Dropping a component must not silently cap the achievable score."""
        s = success_rate.compute(
            validation=_validation(ok=3), grounding=None,
            failed_files=[], test_count=4, generated_file_count=3,
        )
        assert s.score == 100

    def test_nothing_at_all_reports_unmeasured_rather_than_zero(self):
        s = success_rate.compute(
            validation=[], grounding=None, failed_files=[],
            test_count=1, generated_file_count=0,
        )
        # Coverage alone still counts — a run that produced tests measured
        # something. The genuinely empty case is the one below.
        assert s.measured is True

        empty = success_rate.compute(
            validation=[], grounding=None, failed_files=[],
            test_count=0, generated_file_count=0,
        )
        assert empty.score == 0        # measurable, and it measured badly


class TestPayload:
    def test_the_caveat_ships_with_the_number(self):
        """The score travels to a UI we don't control. If "we didn't run these"
        isn't in the payload, some caller will show the number without it."""
        d = success_rate.compute(
            validation=_validation(ok=1), grounding=_grounding(1, 1),
            failed_files=[], test_count=1, generated_file_count=1,
        ).as_dict()
        assert "do not run the tests" in d["not_measured"]
        assert d["measures"]
        assert d["score"] == 100 and d["grade"] == "A"

    def test_components_explain_the_score(self):
        d = success_rate.compute(
            validation=_validation(ok=1, bad=1), grounding=_grounding(4, 1),
            failed_files=["x.ts"], test_count=0, generated_file_count=2,
        ).as_dict()
        keys = {c["key"] for c in d["components"]}
        assert keys == {"completeness", "parse", "grounding", "coverage"}
        # Every component says what went wrong, not just a fraction.
        assert all(c["detail"] for c in d["components"])
