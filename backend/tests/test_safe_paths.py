"""
test_safe_paths.py — Model-authored filenames are untrusted input.

Every path in a generated suite comes out of an LLM's JSON. These cover the ways
that goes wrong: escaping the suite directory, landing on a name Windows cannot
create, and — the quiet one — two planned files sharing a name, where
last-write-wins loses a spec and still reports the full count.
"""

import pytest

from services import safe_paths


class TestSanitize:
    def test_ordinary_paths_pass_through_unchanged(self):
        for p in ("tests/login.spec.ts", "tests/pages/LoginPage.ts",
                  ".github/workflows/e2e.yml", "conftest.py"):
            assert safe_paths.sanitize(p) == p, p

    def test_traversal_is_rejected_not_repaired(self):
        """There is no correct place to move a file that asked to be outside the
        root, and guessing one is how you overwrite the user's src/."""
        for p in ("../../etc/passwd", "tests/../../src/App.tsx", ".."):
            assert safe_paths.sanitize(p) is None, p

    def test_absolute_paths_are_relativised(self):
        # Unlike traversal, this one has an obvious intent: the model almost
        # always means "tests/x.ts" when it writes "/tests/x.ts".
        assert safe_paths.sanitize("/tests/login.spec.ts") == "tests/login.spec.ts"
        assert safe_paths.sanitize("C:/tests/login.spec.ts") == "tests/login.spec.ts"
        assert safe_paths.sanitize("./tests/a.ts") == "tests/a.ts"

    def test_git_directory_is_off_limits(self):
        """Writing here doesn't add a file — it corrupts the repository."""
        assert safe_paths.sanitize(".git/config") is None

    def test_github_workflows_are_allowed(self):
        """The CI workflow legitimately lives there; over-blocking would drop
        the feature the user ticked the box for."""
        assert safe_paths.sanitize(".github/workflows/e2e-tests.yml") is not None

    def test_windows_hostile_names_are_repaired(self):
        # Trailing dot/space: legal on Linux, silently trimmed by Windows, so
        # the repo has two names for one file and can't be cloned cleanly.
        assert safe_paths.sanitize("tests/login.spec.ts.") == "tests/login.spec.ts"
        assert safe_paths.sanitize("tests/login.spec.ts ") == "tests/login.spec.ts"
        # Reserved device names cannot be created at all on Windows.
        assert safe_paths.sanitize("tests/con.ts") == "tests/_con.ts"
        # Characters Windows forbids outright.
        assert safe_paths.sanitize('tests/a<b>c.ts') == "tests/abc.ts"

    def test_control_characters_are_stripped(self):
        assert safe_paths.sanitize("tests/a\x00b.ts") == "tests/ab.ts"

    def test_empty_and_nonsense_are_rejected(self):
        for p in ("", "   ", "/", "///", None):
            assert safe_paths.sanitize(p) is None, repr(p)

    def test_absurdly_long_names_are_trimmed_keeping_the_extension(self):
        long = "tests/" + "a" * 300 + ".spec.ts"
        out = safe_paths.sanitize(long)
        assert out is not None
        assert out.endswith(".ts")           # extension decides how it's parsed
        assert len(out.split("/")[-1]) <= safe_paths.MAX_SEGMENT


class TestDedupe:
    def test_free_name_is_returned_as_is(self):
        assert safe_paths.dedupe("tests/a.ts", set()) == "tests/a.ts"

    def test_collision_gets_a_suffix_before_the_extension(self):
        assert safe_paths.dedupe("tests/a.ts", {"tests/a.ts"}) == "tests/a-2.ts"
        assert safe_paths.dedupe(
            "tests/a.ts", {"tests/a.ts", "tests/a-2.ts"}) == "tests/a-3.ts"

    def test_extensionless_names_work(self):
        assert safe_paths.dedupe("Makefile", {"Makefile"}) == "Makefile-2"

    def test_compound_test_extensions_stay_intact(self):
        """`.spec.` is what the runner discovers on. `login.spec-2.ts` exists,
        parses, validates — and is never collected, so the duplicate spec
        silently doesn't run. That is the failure deduping exists to prevent."""
        assert safe_paths.dedupe(
            "tests/login.spec.ts", {"tests/login.spec.ts"}) == "tests/login-2.spec.ts"
        assert safe_paths.dedupe(
            "tests/test_login.py", {"tests/test_login.py"}) == "tests/test_login-2.py"

    def test_dotfiles_keep_their_leading_dot(self):
        assert safe_paths.dedupe(".gitignore", {".gitignore"}) == ".gitignore-2"


class TestMergeNewOnly:
    def test_existing_files_are_never_overwritten(self):
        """The file we'd destroy is the one thing in the request we didn't
        write, which makes this the worst of the collision cases."""
        dest = {"e2e/README.md": "the user's own notes"}
        added, skipped = safe_paths.merge_new_only(
            dest, {"e2e/README.md": "generated", "e2e/tests/a.ts": "spec"})

        assert dest["e2e/README.md"] == "the user's own notes"
        assert added == ["e2e/tests/a.ts"]
        assert skipped == ["e2e/README.md"]

    def test_skipped_list_is_the_caller_s_warning_source(self):
        _, skipped = safe_paths.merge_new_only({"a": "1"}, {"a": "2", "b": "3"})
        assert skipped == ["a"]


class TestDedupeExhaustion:
    def test_gives_up_loudly_rather_than_looping(self):
        taken = {"a.ts"} | {f"a-{n}.ts" for n in range(2, 1000)}
        with pytest.raises(ValueError):
            safe_paths.dedupe("a.ts", taken)
