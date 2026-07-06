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
    """Bracket-balance + non-truncation check (best effort, no toolchain)."""
    if len(content.strip()) < 20:
        return False, "suspiciously short / empty output"
    pairs = {"}": "{", ")": "(", "]": "["}
    stack: list[str] = []
    in_str = None
    prev = ""
    for ch in content:
        if in_str:
            if ch == in_str and prev != "\\":
                in_str = None
        elif ch in "\"'`":
            in_str = ch
        elif ch in "{([":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack[-1] != pairs[ch]:
                return False, "unbalanced brackets"
            stack.pop()
        prev = ch
    if stack:
        return False, "unbalanced brackets (unclosed block — output may be truncated)"
    return True, ""


def validate_files(files, framework_key: str = "") -> list[FileValidation]:
    """Validate each generated file; skips non-code files (yaml/md/json)."""
    results: list[FileValidation] = []
    for f in files:
        name = f.filename.lower()
        if name.endswith(".py"):
            ok, err = _validate_python(f.content)
        elif name.endswith((".js", ".jsx")):
            ok, err = _validate_js(f.content)
        elif name.endswith((".ts", ".tsx")):
            ok, err = _validate_structural(f.content)
        else:
            ok, err = True, ""  # config/docs — no deep validation
        results.append(FileValidation(f.filename, ok, err))
    return results
