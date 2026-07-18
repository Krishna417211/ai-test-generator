"""
validator.py — Static validation of generated test files.

Full sandboxed E2E execution requires Docker + a running target app. Where that
is unavailable, this performs the next-best trust check: verifying the generated
test code is actually well-formed, runnable code (not truncated or malformed
LLM output). This is what powers the self-heal loop and the "validated" signal.

  • Python   → compiled with py_compile (real syntax check)
  • JavaScript → `node --check` if Node is available (real syntax check)
  • TypeScript / other → structural checks (balanced brackets, non-truncation)
"""

import os
import shutil
import tempfile
import subprocess
from dataclasses import dataclass


@dataclass
class FileValidation:
    filename: str
    ok: bool
    error: str = ""
    # Whether anything was actually verified, or we just had no toolchain to
    # check this file type with. Config and docs pass by default, so counting
    # them as passes inflates the number we report: "8/8 files passed syntax
    # validation" reads as eight checked files when two of them were a README
    # and a YAML nobody parsed. Callers reporting a rate must count only
    # checked=True, or they are quoting a statistic they didn't measure.
    checked: bool = True


def _error_line(text: str) -> str:
    """Pull the most informative line from a compiler/interpreter stderr dump."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines:
        if "Error" in ln:
            return ln[:200]
    return (lines[-1] if lines else "").strip()[:200]


def _validate_python(content: str) -> tuple[bool, str]:
    import py_compile
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tf:
        tf.write(content)
        path = tf.name
    try:
        py_compile.compile(path, doraise=True)
        return True, ""
    except py_compile.PyCompileError as e:
        return False, _error_line(str(e)) or "syntax error"
    finally:
        os.unlink(path)


def _validate_js(content: str) -> tuple[bool, str]:
    if not shutil.which("node"):
        return _validate_structural(content)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tf:
        tf.write(content)
        path = tf.name
    try:
        r = subprocess.run(["node", "--check", path], capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return True, ""
        return False, _error_line(r.stderr) or "syntax error"
    except Exception:
        return _validate_structural(content)
    finally:
        os.unlink(path)


def _validate_structural(content: str) -> tuple[bool, str]:
    """Bracket-balance + non-truncation check (best effort, no toolchain).

    Comments are skipped, and that is not a refinement — it is the difference
    between this check working and inverting. An apostrophe in prose ("we don't
    wait here") used to open a string that never closed, so from that point on
    every real string literal was read as code and every bracket inside one was
    counted. A single contraction in a comment could fail an entire valid file,
    and the writer prompt asks the model for comments explaining *why* — English
    ones are full of apostrophes.

    This is the only check .ts files get (there is no tsc here), and .ts is the
    default output, so a false failure here is not cosmetic: it is reported to
    the user as "N/M files passed syntax validation" and it drives the self-heal
    loop, which would spend LLM calls "fixing" code that was already correct.
    """
    if len(content.strip()) < 20:
        return False, "suspiciously short / empty output"
    pairs = {"}": "{", ")": "(", "]": "["}
    stack: list[str] = []
    in_str: str | None = None
    in_line_comment = False
    in_block_comment = False
    escaped = False
    i = 0
    n = len(content)

    while i < n:
        ch = content[i]
        nxt = content[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
        elif in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 1
        elif in_str:
            # Escapes are only meaningful inside a string, and tracking them with
            # a flag (rather than looking back one character) keeps a literal
            # backslash — '\\' — from being read as escaping the closing quote.
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_str:
                in_str = None
        elif ch == "/" and nxt == "/":
            in_line_comment = True
            i += 1
        elif ch == "/" and nxt == "*":
            in_block_comment = True
            i += 1
        elif ch in "\"'`":
            in_str = ch
        elif ch in "{([":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack[-1] != pairs[ch]:
                return False, "unbalanced brackets"
            stack.pop()
        i += 1

    if in_block_comment:
        return False, "unterminated block comment (output may be truncated)"
    if stack:
        return False, "unbalanced brackets (unclosed block — output may be truncated)"
    return True, ""


def validate_files(files, framework_key: str = "") -> list[FileValidation]:
    """Validate each generated file; skips non-code files (yaml/md/json)."""
    results: list[FileValidation] = []
    for f in files:
        name = f.filename.lower()
        checked = True
        if name.endswith(".py"):
            ok, err = _validate_python(f.content)
        elif name.endswith((".js", ".jsx")):
            ok, err = _validate_js(f.content)
        elif name.endswith((".ts", ".tsx")):
            ok, err = _validate_structural(f.content)
        else:
            ok, err = True, ""  # config/docs — no deep validation
            checked = False
        results.append(FileValidation(f.filename, ok, err, checked=checked))
    return results
