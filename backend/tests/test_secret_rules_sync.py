"""
test_secret_rules_sync.py — The CLI's copy of the credential rules cannot drift.

`backend/services/secret_rules.json` is canonical. `cli/src/secret-rules.json` is
a verbatim copy, and it exists for one uninteresting reason: npm publishes `cli/`
on its own and a package cannot read a file outside its own directory.

A copy that nothing checks is just duplication with extra steps — which is what
this replaced. Adding a rule to one language and not the other used to be silent,
and silent in the dangerous direction: the CLI would upload a credential it could
have stopped locally. So the copy is verified here, byte for byte, and the failure
message names the one command that fixes it.

The stronger guarantee sits in the `self_test` corpus inside that JSON, which
both `tests/test_secrets_guard.py` and `cli/test/secrets.test.js` execute. This
test proves they are reading the same rules; that corpus proves they reach the
same answers.
"""

import json
from pathlib import Path

import pytest

from services import secrets_guard

REPO = Path(__file__).resolve().parents[2]
CANONICAL = REPO / "backend" / "services" / "secret_rules.json"
CLI_COPY = REPO / "cli" / "src" / "secret-rules.json"


class TestCanonicalRules:
    def test_the_module_reads_the_canonical_file(self):
        # Guards against someone reintroducing a hard-coded list in the module
        # while leaving the JSON in place, unused and quietly wrong.
        assert secrets_guard.RULES_PATH == CANONICAL
        assert secrets_guard.RULES["version"] >= 1

    def test_rules_are_actually_wired_up(self):
        loaded = json.loads(CANONICAL.read_text(encoding="utf-8"))
        assert len(secrets_guard.CONTENT_RULES) == len(loaded["content_rules"])
        assert secrets_guard.SECRET_FILENAMES == frozenset(
            loaded["path_rules"]["secret_filenames"]
        )
        assert secrets_guard.SECRET_EXTENSIONS == frozenset(
            loaded["path_rules"]["secret_extensions"]
        )
        assert secrets_guard.SECRET_DIRECTORIES == frozenset(
            loaded["path_rules"]["secret_directories"]
        )


@pytest.mark.skipif(not CLI_COPY.exists(), reason="cli/ package not present")
class TestCliCopyInSync:
    def test_bytes_are_identical(self):
        assert CLI_COPY.read_bytes() == CANONICAL.read_bytes(), (
            "cli/src/secret-rules.json has drifted from the canonical rules in "
            "backend/services/secret_rules.json.\n\n"
            "Fix:  cd cli && npm run sync-rules\n\n"
            "Never hand-edit the copy — edit the canonical file and re-run that."
        )


class TestPortability:
    """The patterns must mean the same thing in Python and JavaScript.

    They are compiled by `re` here and by `RegExp` in the CLI, so a construct
    only one engine supports is a rule that silently does nothing on the other
    side. These check the boundaries of the portable subset the file documents.
    """

    PATTERNS = None

    @classmethod
    def setup_class(cls):
        loaded = json.loads(CANONICAL.read_text(encoding="utf-8"))
        cls.PATTERNS = [r["pattern"] for r in loaded["content_rules"]] + [
            loaded["path_rules"]["dotenv_pattern"],
            loaded["path_rules"]["cloud_key_pattern"],
        ]

    def test_no_python_only_constructs(self):
        # (?P<x>...) named groups, (?<=...) lookbehind and \Z have no JavaScript
        # equivalent (or differ), so any of them is a portability bug.
        banned = ["(?P<", "(?P=", "(?<=", "(?<!", r"\Z", r"\A", "(?#", r"\p{"]
        for pattern in self.PATTERNS:
            for token in banned:
                assert token not in pattern, (
                    f"{pattern!r} uses {token!r}, which does not port to "
                    "JavaScript RegExp — see the portability note in "
                    "secret_rules.json"
                )

    def test_every_pattern_compiles_in_python(self):
        import re
        for pattern in self.PATTERNS:
            re.compile(pattern)  # raises on a malformed pattern

    def test_flags_are_limited_to_case_insensitivity(self):
        loaded = json.loads(CANONICAL.read_text(encoding="utf-8"))
        for rule in loaded["content_rules"]:
            assert set(rule.get("flags", "")) <= {"i"}, (
                f"{rule['label']}: only the 'i' flag is portable here"
            )
