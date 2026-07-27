"""test_step_contract.py — the step ids are a cross-language contract.

`backend/services/progress.py` emits `{"type":"step","id":...}` NDJSON events and
`frontend/src/flows.ts` lights a card per id. ARCHITECTURE.md calls this a
contract precisely because a drifted id is *silent* in the browser: the card
simply never lights, with no error anywhere.

Progress already guards one direction (it rejects an unknown id rather than
emitting an event nothing listens for). Nothing guarded the other: a frontend
step declared `driver: "server"` claims the backend emits it, and the crawl-first
change added one — `crawl` — that no backend code emits and that KNOWN_STEPS does
not contain. This test is the missing half.
"""

import re
from pathlib import Path

import pytest

from services.progress import KNOWN_STEPS

_FLOWS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "flows.ts"

# id + driver, in declaration order.
_STEP_RE = re.compile(r'id:\s*"([^"]+)",\s*(?://[^\n]*\n\s*)*'
                      r'(?:/\*.*?\*/\s*)?(?://[^\n]*\n\s*)*driver:\s*"([^"]+)"',
                      re.DOTALL)


def _frontend_steps() -> list[tuple[str, str]]:
    if not _FLOWS.exists():
        pytest.skip("frontend/src/flows.ts not available in this checkout")
    steps = _STEP_RE.findall(_FLOWS.read_text())
    assert steps, "parsed no steps out of flows.ts — the regex has drifted"
    return steps


def test_every_server_driven_step_is_one_the_backend_can_emit():
    """A `driver: "server"` card waits for an NDJSON event. If progress.py can't
    emit that id, the card hangs forever and nothing reports why."""
    declared = {sid for sid, driver in _frontend_steps() if driver == "server"}
    unknown = sorted(declared - set(KNOWN_STEPS))
    assert not unknown, (
        f"flows.ts marks {unknown} as server-driven, but progress.py's "
        f"KNOWN_STEPS is {sorted(KNOWN_STEPS)} — the backend cannot emit them, "
        f"so those cards never light. Either emit the step or set driver to "
        f"'client'/'static'."
    )


def test_flows_declares_a_known_driver_for_every_step():
    bad = [(sid, d) for sid, d in _frontend_steps()
           if d not in {"server", "client", "static"}]
    assert not bad, f"unknown driver values in flows.ts: {bad}"


def test_step_ids_are_unique_within_each_driver_contract():
    """Two cards sharing an id would both light on one event."""
    server_ids = [sid for sid, d in _frontend_steps() if d == "server"]
    dupes = {s for s in server_ids if server_ids.count(s) > 1}
    assert not dupes, f"duplicate server-driven step ids in flows.ts: {sorted(dupes)}"
