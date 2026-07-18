"""
benchmark.py — measure the suite quality the product actually claims.

A demo convinces one person; a benchmark convinces a room. This harness runs the
grounding and fragility analysis over a set of (repo, generated-suite) cases and
reports the numbers the product stakes its trust on:

  • **verified-selector rate** — of every selector the model wrote, the fraction
    that provably resolves to a real element (source, or the live DOM). This is
    our stand-in for "does it work": we do not execute suites (see the module
    docstring in services/validator.py), so we report the thing we CAN prove
    rather than an invented pass rate.
  • **fragility grade** — how brittle the verified selectors are, so a suite
    can't score well by grounding everything to `nth-child`.

The harness is honest about denominators: a case that grounded nothing counts
as a case with no measurable rate, not a free 100%. Aggregates are computed over
cases that actually had selectors.

Two ways to feed it:
  • pre-generated suites (fast, free, deterministic) — the default the tests use
    and the one you run in CI to catch a regression in grounding itself;
  • the full pipeline (extract → filter → write → ground), which needs API keys
    and real time/money and is how you produce headline numbers over N repos.

Run:  python -m services.benchmark cases.json        # prints a markdown table
where cases.json is [{"name","source":{path:content},"suite":[{filename,content}]}].
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Optional

from services import grounding as grounding_svc
from services import fragility as fragility_svc


@dataclass
class _Suite:
    """Minimal stand-in for a generated file, so the harness needs no writer."""
    filename: str
    content: str


@dataclass
class BenchmarkRow:
    name: str
    selectors: int = 0
    verified: int = 0
    fragility_grade: str = "—"
    mean_fragility: float = 0.0
    brittle: int = 0

    @property
    def rate(self) -> Optional[float]:
        if not self.selectors:
            return None
        return round(self.verified / self.selectors, 3)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "selectors": self.selectors,
            "verified": self.verified,
            "verified_rate": self.rate,
            "fragility_grade": self.fragility_grade,
            "mean_fragility": self.mean_fragility,
            "brittle": self.brittle,
        }


@dataclass
class BenchmarkReport:
    rows: list = field(default_factory=list)   # [BenchmarkRow]

    @property
    def measurable(self) -> list:
        return [r for r in self.rows if r.rate is not None]

    @property
    def mean_verified_rate(self) -> Optional[float]:
        rows = self.measurable
        if not rows:
            return None
        return round(sum(r.rate for r in rows) / len(rows), 3)

    @property
    def total_selectors(self) -> int:
        return sum(r.selectors for r in self.rows)

    @property
    def total_verified(self) -> int:
        return sum(r.verified for r in self.rows)

    @property
    def pooled_verified_rate(self) -> Optional[float]:
        """Rate over ALL selectors, not the mean of per-repo rates — a big repo
        should weigh more than a tiny one in the headline number."""
        if not self.total_selectors:
            return None
        return round(self.total_verified / self.total_selectors, 3)

    def grade_distribution(self) -> dict:
        dist: dict = {}
        for r in self.rows:
            dist[r.fragility_grade] = dist.get(r.fragility_grade, 0) + 1
        return dist

    def as_dict(self) -> dict:
        return {
            "cases": len(self.rows),
            "mean_verified_rate": self.mean_verified_rate,
            "pooled_verified_rate": self.pooled_verified_rate,
            "total_selectors": self.total_selectors,
            "total_verified": self.total_verified,
            "grade_distribution": self.grade_distribution(),
            "rows": [r.as_dict() for r in self.rows],
        }


def evaluate_case(
    name: str,
    source_files: dict[str, str],
    suite_files: list,
    dom_index: Optional[grounding_svc.GroundIndex] = None,
) -> BenchmarkRow:
    """Score one (repo, suite) pair on grounding + fragility."""
    ground = grounding_svc.ground_suite(suite_files, source_files, dom_index=dom_index)
    frag = fragility_svc.analyze(suite_files)
    return BenchmarkRow(
        name=name,
        selectors=ground.total,
        verified=ground.verified,
        fragility_grade=frag.grade,
        mean_fragility=frag.mean_score,
        brittle=frag.brittle,
    )


def run_benchmark(cases: list[dict]) -> BenchmarkReport:
    """Evaluate a list of cases loaded from JSON (see module docstring)."""
    report = BenchmarkReport()
    for case in cases:
        suite = [_Suite(f["filename"], f["content"]) for f in case.get("suite", [])]
        dom_index = None
        if case.get("dom_html"):
            dom_index = grounding_svc.build_dom_index(case["dom_html"])
        report.rows.append(
            evaluate_case(case["name"], case.get("source", {}), suite, dom_index)
        )
    return report


def format_markdown(report: BenchmarkReport) -> str:
    """A leaderboard table plus the headline aggregates — paste into a README."""
    lines = [
        "| Repo | Selectors | Verified | Rate | Fragility |",
        "|------|-----------|----------|------|-----------|",
    ]
    for r in sorted(report.rows, key=lambda x: (x.rate is None, -(x.rate or 0))):
        rate = f"{r.rate:.0%}" if r.rate is not None else "—"
        lines.append(
            f"| {r.name} | {r.selectors} | {r.verified} | {rate} | "
            f"{r.fragility_grade} ({r.mean_fragility:.2f}) |"
        )
    pooled = report.pooled_verified_rate
    mean = report.mean_verified_rate
    lines += [
        "",
        f"**{len(report.rows)} repos** · "
        f"pooled verified-selector rate **{pooled:.0%}**"
        if pooled is not None else f"**{len(report.rows)} repos** · no measurable selectors",
    ]
    if mean is not None:
        lines[-1] += f" · mean per-repo **{mean:.0%}**"
    lines.append(f"Fragility grades: {report.grade_distribution()}")
    return "\n".join(lines)


def _main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python -m services.benchmark cases.json [--json]", file=sys.stderr)
        return 2
    with open(argv[1]) as fh:
        cases = json.load(fh)
    report = run_benchmark(cases)
    if "--json" in argv:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(format_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
