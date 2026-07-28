"""
safe_paths.py — Make a model-authored filename safe to write and to push.

Every path in a generated suite comes out of an LLM's JSON, which means it is
untrusted input that happens to look structured. The failure modes are not
hypothetical:

  ../../etc/passwd            escapes the suite directory
  /tmp/spec.ts                absolute — lands outside the repo entirely
  .git/config                 overwrites git's own metadata on the push
  tests/spec .ts / spec.ts.   trailing space/dot: fine on Linux, unusable on
                              Windows, so the repo can't be cloned there
  CON.spec.ts                 reserved device name on Windows, same result
  tests/login.spec.ts (x2)    a duplicate silently overwrites the first file,
                              and the suite quietly loses a spec

`sanitize` repairs what can be repaired and rejects the rest. It never invents a
name for a file it cannot make safe: dropping one file and saying so beats
writing it somewhere the user did not expect.

The counterpart is `merge_new_only`, for the other half of the problem — a
generated path that is safe but already taken by one of the user's own files.
Overwriting there is worse than any of the above, because the file we would
destroy is the one thing in the request we didn't create.
"""

import posixpath
import re

# Anything unprintable, plus the characters Windows forbids in a filename. NUL
# is the dangerous one (it truncates the path in C APIs); the rest just make the
# repo unusable for half the people who clone it.
_ILLEGAL_CHARS_RE = re.compile(r'[\x00-\x1f\x7f<>:"|?*\\]')

# Windows reserved device names — a file called `con.ts` cannot be created on
# Windows at all, whatever the extension.
_RESERVED_STEMS = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

# Paths git owns. Writing here doesn't produce a file — it corrupts the repo.
# `.github` is deliberately absent: the CI workflow legitimately lives there.
_FORBIDDEN_ROOTS = {".git"}

MAX_SEGMENT = 100
MAX_PATH = 200


def _clean_segment(seg: str) -> str:
    seg = _ILLEGAL_CHARS_RE.sub("", seg)
    # Trailing dots and spaces are silently stripped by Windows, which turns
    # "spec.ts." into "spec.ts" — two different names in git, one on disk.
    seg = seg.rstrip(". ")
    seg = seg.strip()
    if len(seg) > MAX_SEGMENT:
        # Trim the stem, keep the extension: the extension is what decides how
        # the file is parsed, run and validated.
        stem, dot, ext = seg.rpartition(".")
        if dot and len(ext) <= 12:
            seg = stem[: MAX_SEGMENT - len(ext) - 1] + "." + ext
        else:
            seg = seg[:MAX_SEGMENT]
    stem = seg.split(".", 1)[0].lower()
    if stem in _RESERVED_STEMS:
        seg = "_" + seg
    return seg


def sanitize(raw: str) -> str | None:
    """Return a safe repo-relative path, or None if the name is unusable.

    Rejection (rather than repair) is the right answer whenever repairing would
    change *where* the file lands: a path that escaped the root has no correct
    home to be moved to, and guessing one is how you end up writing to the
    user's src/.
    """
    if not raw or not raw.strip():
        return None

    path = raw.strip().replace("\\", "/")

    # Absolute paths and Windows drive letters are outside the repo by
    # construction. Strip the marker and keep the tail — the model almost always
    # means "tests/login.spec.ts" when it writes "/tests/login.spec.ts".
    path = re.sub(r"^[A-Za-z]:", "", path)
    path = path.lstrip("/")
    while path.startswith("./"):
        path = path[2:]

    if not path:
        return None

    segments = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            # A real escape attempt, or a model confused about its own root.
            # Either way there is no safe interpretation.
            return None
        cleaned = _clean_segment(seg)
        if not cleaned:
            return None
        segments.append(cleaned)

    if not segments:
        return None

    result = posixpath.join(*segments)
    if len(result) > MAX_PATH:
        return None
    if segments[0] in _FORBIDDEN_ROOTS:
        return None
    # posixpath.normpath is the last word on whether the result still escapes —
    # belt and braces after the ".." rejection above.
    if posixpath.normpath(result).startswith(("..", "/")):
        return None
    return result


def _split_name(path: str) -> tuple[str, str]:
    """Split into (stem, extension) at the FIRST dot, not the last.

    `login.spec.ts` must become `login-2.spec.ts`, never `login.spec-2.ts`. The
    `.spec.` is not decoration — it is what Playwright, Cypress and pytest match
    on to discover tests. Suffixing after it produces a file that exists, parses,
    validates, and is never collected by the runner: the duplicate spec silently
    does not run, which is the exact outcome deduping exists to prevent.
    """
    directory, _, name = path.rpartition("/")
    prefix = directory + "/" if directory else ""
    # A leading dot is part of the name (.gitignore), not an extension marker.
    lead = "." if name.startswith(".") else ""
    body = name[len(lead):]
    stem, dot, ext = body.partition(".")
    return prefix + lead + stem, (dot + ext) if dot else ""


def dedupe(path: str, taken: set[str]) -> str:
    """`path`, or the next free `name-2.ext` if it is already taken.

    Used when the model plans two files with the same name. Renaming keeps both
    — the alternative, last-write-wins, loses a spec and reports success.
    """
    if path not in taken:
        return path
    stem, ext = _split_name(path)
    for n in range(2, 1000):
        candidate = f"{stem}-{n}{ext}"
        if candidate not in taken:
            return candidate
    raise ValueError(f"Could not find a free name for {path}")


def merge_new_only(
    dest: dict[str, str], additions: dict[str, str]
) -> tuple[list[str], list[str]]:
    """Add `additions` to `dest`, never overwriting an existing key.

    Returns (added, skipped). `skipped` holds paths that were already present —
    the caller must surface them, because a skipped file means the suite it
    belongs to is not the suite that was described to the user.
    """
    added: list[str] = []
    skipped: list[str] = []
    for path, content in additions.items():
        if path in dest:
            skipped.append(path)
            continue
        dest[path] = content
        added.append(path)
    return added, skipped
