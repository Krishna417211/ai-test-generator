"""
success_rate.py — One number for "how well did this generation go?"

## What this is, and what it deliberately is not

It is a **generation success rate**: the share of the checks we actually perform
that the suite passed. Every input is something measured on this run —

  • completeness   did every planned file get written? (agents/writer_agent.py
                   records the ones that didn't in `failed_files`)
  • parse          did every code file compile / parse? (services/validator.py)
  • grounding      does every selector the model wrote exist in the user's real
                   source or live DOM? (services/grounding.py)
  • coverage       did the suite end up containing runnable test cases at all?

It is **not** a prediction that the tests pass against the running app. We never
execute them — that needs Docker and a live target, see validator.py's docstring
— so a number claiming a pass rate would be invented. That distinction is
carried in the payload itself (`measures`, `not_measured`) rather than left to
whoever writes the UI, because the number travels and the caveat has to travel
with it.

## Why weighted, and why these weights

The components are not equally load-bearing. A suite missing planned files
cannot run at all; a suite with one unverified selector is a suite with one
thing to check. So completeness and parseability dominate, grounding informs,
and coverage is a floor check. A component with nothing to measure (no
selectors, no code files) is dropped and its weight redistributed, rather than
being scored 100% — crediting a suite for a check that never ran is exactly the
inflation this module exists to avoid.
"""

from dataclasses import dataclass, field

# (key, label, weight). Weights are relative; they are normalised over whichever
# components actually had something to measure.
_WEIGHTS = {
    "completeness": 0.35,
    "parse": 0.30,
    "grounding": 0.25,
    "coverage": 0.10,
}

_GRADES = ((95, "A"), (85, "B"), (70, "C"), (50, "D"))


@dataclass
class Component:
    key: str
    label: str
    passed: int
    total: int
    weight: float
    detail: str = ""

    @property
    def rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "passed": self.passed,
            "total": self.total,
            "rate": round(self.rate, 3),
            "weight": round(self.weight, 3),
            "detail": self.detail,
        }


@dataclass
class SuiteScore:
    """The score plus everything needed to defend it."""

    score: int                      # 0–100, or -1 when nothing could be measured
    grade: str                      # A–F, or "—"
    components: list[Component] = field(default_factory=list)
    measured: bool = True

    def as_dict(self) -> dict:
        return {
            "score": self.score if self.measured else None,
            "grade": self.grade,
            "measured": self.measured,
            "components": [c.as_dict() for c in self.components],
            # Shipped with the number so no caller can show one without the
            # other. This is the sentence that keeps the score honest.
            "measures": (
                "Share of our automated checks this suite passed: planned files "
                "delivered, code parsed, selectors found in your app, and test "
                "cases produced."
            ),
            "not_measured": (
                "We do not run the tests against your app, so this is not a "
                "prediction that they pass. Run them to confirm."
            ),
        }


def _grade(score: int) -> str:
    for threshold, letter in _GRADES:
        if score >= threshold:
            return letter
    return "F"


def compute(
    *,
    validation: list[dict],
    grounding: dict | None,
    failed_files: list[str],
    test_count: int,
    generated_file_count: int,
) -> SuiteScore:
    """Score one generation run.

    `validation` is the per-file list from services/validator.py (entries carry
    `ok` and `checked`); `grounding` is Grounding.as_dict() from the writer.
    Both are already part of every response, so this adds no new measurement —
    it only aggregates what was measured.
    """
    grounding = grounding or {}
    components: list[Component] = []

    # ── completeness: planned files that actually got written ──
    planned = generated_file_count + len(failed_files)
    if planned:
        components.append(Component(
            key="completeness",
            label="Planned files delivered",
            passed=generated_file_count,
            total=planned,
            weight=_WEIGHTS["completeness"],
            detail=(
                f"{len(failed_files)} planned file(s) never generated"
                if failed_files else "every planned file was written"
            ),
        ))

    # ── parse: only files a real parser looked at ──
    # Config and docs are marked checked=False by the validator precisely so
    # they can't pad this. Older payloads have no `checked` key, hence the
    # `is not False` default.
    checked = [v for v in validation if v.get("checked") is not False]
    if checked:
        ok = sum(1 for v in checked if v.get("ok"))
        components.append(Component(
            key="parse",
            label="Code files that parse",
            passed=ok,
            total=len(checked),
            weight=_WEIGHTS["parse"],
            detail=(
                f"{len(checked) - ok} file(s) still fail to parse"
                if ok < len(checked) else "all parsed cleanly"
            ),
        ))

    # ── grounding: selectors traced back to the real app ──
    sel_total = int(grounding.get("selectors_total") or 0)
    if sel_total:
        verified = int(grounding.get("selectors_verified") or 0)
        components.append(Component(
            key="grounding",
            label="Selectors found in your app",
            passed=verified,
            total=sel_total,
            weight=_WEIGHTS["grounding"],
            detail=(
                f"{sel_total - verified} selector(s) we could not trace"
                if verified < sel_total else "every selector traced to your source"
            ),
        ))

    # ── coverage: a suite with no test cases collects nothing ──
    # Binary on purpose. "How many tests should there be" has no ground truth,
    # but "are there any" is decidable and is the difference between a suite and
    # a directory of page objects.
    components.append(Component(
        key="coverage",
        label="Test cases produced",
        passed=1 if test_count > 0 else 0,
        total=1,
        weight=_WEIGHTS["coverage"],
        detail=(
            f"{test_count} test case(s) found"
            if test_count else "no test cases — the suite would collect 0 tests"
        ),
    ))

    total_weight = sum(c.weight for c in components)
    if not components or total_weight <= 0:
        return SuiteScore(score=-1, grade="—", components=[], measured=False)

    weighted = sum(c.rate * c.weight for c in components) / total_weight
    score = int(round(weighted * 100))
    return SuiteScore(score=score, grade=_grade(score), components=components)


def from_writer_result(result) -> SuiteScore:
    """Convenience wrapper for a WriterResult (agents/writer_agent.py)."""
    return compute(
        validation=result.validation,
        grounding=result.grounding.as_dict(),
        failed_files=result.failed_files,
        test_count=result.test_count,
        generated_file_count=len(result.files),
    )
