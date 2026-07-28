"""
secrets_guard.py — Keep credentials out of the LLM context and out of git.

Two distinct places in this app take a pile of the user's files and send them
somewhere they can never be recalled from:

  • the LLM context (agents/filter_agent.py → a third-party model provider)
  • a brand-new GitHub repository (services/git_publisher.py)

A `.env` in an uploaded ZIP is the normal case, not the exotic one — people zip
their working tree. Pushing it to a repo they just created, or pasting it into a
model prompt, leaks a live credential, and neither is undoable: the commit is in
the reflog even after a force-push, and the prompt is gone into someone else's
logs. So the filter runs by default and errs toward excluding.

Two detectors, because they catch different mistakes:

  Path    a file whose *identity* is a credential — .env, id_rsa, a .pem, an AWS
          credentials file. Never wanted in either destination, no exceptions
          worth the risk. Example/template variants (.env.example) are kept:
          they are documentation and contain placeholders by definition.

  Content a normal source file with a live key hard-coded in it. Only
          high-confidence markers are used — an issuer-prefixed token
          (AKIA…, ghp_…, sk_live_…) or a PEM private-key block. Generic
          "api_key =" assignments are deliberately NOT matched: they are
          overwhelmingly placeholders, and dropping half a codebase to catch one
          would make the whole feature something users switch off.

Excluded files are reported, never dropped silently — the user has to know their
`.env` did not reach the repo, or they will assume the deploy is configured.

## Where the rules live

Not here. `secret_rules.json`, next to this file, is the canonical set, and
`cli/src/secrets.js` reads a synchronised copy of the same file. The rules were
previously written out twice, once per language, which meant a rule added to one
side silently did not exist on the other. Now there is one list, one set of
patterns, and one shared `self_test` corpus that both implementations run — so a
case added in JSON is covered in both languages at once.

`tests/test_secret_rules_sync.py` fails CI if the CLI's copy drifts.
"""

import json
import re
from pathlib import Path

# ─────────────────────────────────────────────
# Rules, loaded from the canonical JSON
# ─────────────────────────────────────────────

RULES_PATH = Path(__file__).with_name("secret_rules.json")


def _load_rules() -> dict:
    """Read the rule set. A failure here is fatal, and deliberately so.

    Falling back to an empty rule set would leave the guard silently passing
    every credential file through — the one failure mode this module exists to
    prevent, arrived at by being helpful about a missing file. The file ships
    alongside this module and is copied into the image by `COPY . .`; if it is
    genuinely absent, the process should not start.
    """
    with RULES_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


RULES = _load_rules()

_PATH_RULES = RULES["path_rules"]

SECRET_EXTENSIONS = frozenset(_PATH_RULES["secret_extensions"])
SECRET_FILENAMES = frozenset(_PATH_RULES["secret_filenames"])
SECRET_DIRECTORIES = frozenset(_PATH_RULES["secret_directories"])
_PRIVATE_KEY_STEMS = tuple(_PATH_RULES["private_key_stems"])

# A dotenv that carries real values. `.env`, `.env.local`, `.env.production` …
# The example/template variants are the documented placeholders and are the one
# thing here we deliberately keep.
_DOTENV_RE = re.compile(_PATH_RULES["dotenv_pattern"], re.IGNORECASE)
_DOTENV_SAFE_SUFFIXES = tuple(_PATH_RULES["dotenv_safe_suffixes"])

# Google service-account / GCP key JSONs, which are named freely.
_GCP_KEY_RE = re.compile(_PATH_RULES["cloud_key_pattern"], re.IGNORECASE)


def _path_reason(path: str) -> str | None:
    """Why this path is a credential file, or None if it is ordinary."""
    # NOT lstrip("./") — that strips *characters*, so ".env" becomes "env" and
    # the single most important file this module exists to catch walks straight
    # through. Strip the "./" prefix as a prefix.
    norm = path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.lstrip("/")
    parts = norm.split("/")
    name = parts[-1]
    low = name.lower()

    for part in parts[:-1]:
        if part.lower() in SECRET_DIRECTORIES:
            return f"lives in {part}/ — a credential directory"

    if _DOTENV_RE.match(low):
        if any(low.endswith(s) for s in _DOTENV_SAFE_SUFFIXES):
            return None
        return "environment file — usually holds live secrets"

    if low in SECRET_FILENAMES:
        return "known credential file"

    if Path(low).suffix in SECRET_EXTENSIONS:
        return f"{Path(low).suffix} key/certificate file"

    if _GCP_KEY_RE.search(low):
        return "looks like a cloud service-account key"

    # id_rsa.pub is a public key and harmless, but id_rsa.bak / id_rsa.old are
    # the private one under another name.
    if low.startswith(_PRIVATE_KEY_STEMS) and not low.endswith(".pub"):
        return "private SSH key"

    return None


# ─────────────────────────────────────────────
# Content-based rules
# ─────────────────────────────────────────────

# Only issuer-prefixed tokens and PEM blocks. Each of these has a fixed,
# unmistakable prefix chosen by the issuer precisely so scanners can find it, so
# a match is evidence rather than a guess. Compiled from the canonical JSON —
# see the module docstring for why they are not written out here.
CONTENT_RULES: list[tuple[str, re.Pattern]] = [
    (
        rule["label"],
        re.compile(rule["pattern"], re.IGNORECASE if "i" in rule.get("flags", "") else 0),
    )
    for rule in RULES["content_rules"]
]

# Only scan the head of a file. A real key sits in a config block near the top,
# and reading 200 KB of minified vendor bundle per file — for every file, on
# every request — costs more than it catches.
_CONTENT_SCAN_BYTES = RULES["content_scan_bytes"]


def _content_reason(content: str) -> str | None:
    head = content[:_CONTENT_SCAN_BYTES]
    for label, pattern in CONTENT_RULES:
        if pattern.search(head):
            return f"contains what looks like {label}"
    return None


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

class Finding:
    """One excluded file and the reason, in words a user can act on."""

    __slots__ = ("path", "reason", "kind")

    def __init__(self, path: str, reason: str, kind: str):
        self.path = path
        self.reason = reason
        self.kind = kind          # "path" | "content"

    def __repr__(self) -> str:                       # pragma: no cover - debug aid
        return f"Finding({self.path!r}, {self.reason!r}, {self.kind!r})"

    def as_dict(self) -> dict:
        return {"path": self.path, "reason": self.reason, "kind": self.kind}


def inspect(path: str, content: str = "") -> Finding | None:
    """Classify one file. Returns None when it is safe to send/push."""
    reason = _path_reason(path)
    if reason:
        return Finding(path, reason, "path")
    reason = _content_reason(content)
    if reason:
        return Finding(path, reason, "content")
    return None


def scrub(files: dict[str, str]) -> tuple[dict[str, str], list[Finding]]:
    """Split `files` into (safe, excluded).

    The caller decides how loudly to report the exclusions; it must report them
    somehow. A user whose `.env` was quietly removed will push, deploy, and
    debug a missing-config error that we caused.
    """
    safe: dict[str, str] = {}
    excluded: list[Finding] = []
    for path, content in files.items():
        finding = inspect(path, content or "")
        if finding:
            excluded.append(finding)
        else:
            safe[path] = content
    return safe, excluded


def summarize(excluded: list[Finding], limit: int = 4) -> str:
    """One warning line naming the files, not just counting them.

    Naming them is the point: "1 sensitive file excluded" leaves the user
    guessing which, and the whole value of the message is that they can go and
    check the one we mean.
    """
    if not excluded:
        return ""
    names = [f.path for f in excluded[:limit]]
    more = len(excluded) - len(names)
    listed = ", ".join(names) + (f" +{more} more" if more > 0 else "")
    noun = "file" if len(excluded) == 1 else "files"
    return (
        f"Excluded {len(excluded)} sensitive {noun} ({listed}) — credentials are "
        "never pushed to a new repo or sent to the model."
    )
