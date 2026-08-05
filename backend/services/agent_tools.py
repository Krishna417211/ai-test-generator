"""
agent_tools.py — the tools the writer agent is allowed to use.

Every tool here is a thin wrapper over something this codebase already does in
the scripted pipeline. That is the whole design: the scripted `WriterAgent` path
decides *for* the model when to look at source, when to check a selector and when
to validate; agent mode hands the model the same capabilities and lets it decide
the order. Nothing here can reach the network, the filesystem or a shell — the
toolbox is closed over data the caller already fetched, so a prompt-injected
instruction inside a crawled page or a repo file has nothing to reach for.

Tools:
  list_files      — the file tree the extractor kept
  search_source   — keyword search over that source, ranked
  read_file       — one file's contents
  find_anchors    — query the grounding index (the real ids/testids/classes)
  validate_code   — run the real static validator on a candidate file
  write_file      — add a file to the suite being built

There is deliberately no `finish` tool: a turn that calls no tool is the model
saying it is done, which is the API's own end_turn signal and one less thing for
the model to get wrong.
"""

import re
import logging
from dataclasses import dataclass

from services.validator import validate_files

logger = logging.getLogger(__name__)

# A single tool result is injected straight back into the conversation, so an
# unbounded one (a 400KB bundle, a search that matched everything) blows the
# context window a few turns later and the run dies far from the cause. Caps are
# per-result and generous enough that a real page component arrives whole.
MAX_FILE_CHARS = 20_000
MAX_SEARCH_RESULTS = 12
MAX_SNIPPETS_PER_FILE = 4
MAX_ANCHORS = 200

# Files the model writes are capped too — a "test file" of 200KB is a runaway
# generation, not a suite, and accepting it wastes the rest of the budget.
MAX_WRITE_CHARS = 60_000

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text or "")]


# ─────────────────────────────────────────────
# Tool schemas (Anthropic tool-use format)
# ─────────────────────────────────────────────

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "list_files",
        "description": (
            "List every source file available, with its size. Start here to see "
            "the shape of the project before reading anything."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_source",
        "description": (
            "Keyword search across all available source. Returns the best-matching "
            "files with the matching lines. Use this to find a page, a form, a "
            "route or a component by name before reading whole files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Words to look for, e.g. 'login form email password'.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read one source file in full. Prefer search_source first — reading "
            "everything wastes the context you need for writing tests."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Exact path from list_files."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "find_anchors",
        "description": (
            "Query the grounding index: the ids, data-testids, names, roles, "
            "aria-labels and classes that PROVABLY exist in the app's source or "
            "live DOM. A selector you write must come from here — anything else "
            "is a guess and will be flagged as unverified."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["testid", "id", "name", "role", "label", "class", "text"],
                    "description": "Restrict to one kind of anchor. Omit for all kinds.",
                },
                "contains": {
                    "type": "string",
                    "description": "Only anchors whose value contains this substring.",
                },
            },
        },
    },
    {
        "name": "validate_code",
        "description": (
            "Run the real static validator on a file BEFORE writing it: syntax, "
            "page-object field-initialisation order, and imports that no file in "
            "the suite provides. Cheap — use it on every code file you write."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["filename", "content"],
        },
    },
    {
        "name": "run_test",
        "description": (
            "Run the test files written so far against the real deployed app and "
            "report what passed and what failed, with the actual error messages. "
            "This is ground truth — a selector that resolved in the source can "
            "still fail on the live page. Run it once you have a page object and "
            "a spec, fix what it reports, and run it again. If it isn't available "
            "on this server it will say so; carry on without it."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "write_file",
        "description": (
            "Add a file to the test suite. Writing the same filename twice "
            "replaces the earlier version. When every file is written, stop "
            "calling tools and reply with a one-paragraph summary of the suite."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Path relative to the suite root, e.g. 'pages/LoginPage.ts'.",
                },
                "description": {
                    "type": "string",
                    "description": "One line: what this file covers.",
                },
                "content": {"type": "string"},
            },
            "required": ["filename", "description", "content"],
        },
    },
]


@dataclass
class ToolCallRecord:
    """One tool invocation, kept so the UI can show what the agent actually did."""
    name: str
    summary: str
    ok: bool = True


class Toolbox:
    """Executes tool calls against data the caller already has in hand.

    Holds the suite being built (`written`), so the loop can pull the result out
    when the model stops. Closed over its inputs — see the module docstring for
    why there is no network or filesystem access in here.
    """

    def __init__(
        self,
        source_files: dict[str, str],
        framework_key: str,
        src_index=None,
        dom_index=None,
        base_url: str = "",
    ):
        self.source_files = source_files or {}
        self.framework_key = framework_key
        self.src_index = src_index
        self.dom_index = dom_index
        self.base_url = base_url
        self.written: dict[str, dict] = {}      # filename -> {description, content}
        self.calls: list[ToolCallRecord] = []
        # The last real execution, if the agent ran one. This is the only
        # evidence anywhere in the pipeline that the suite actually passes, so
        # it is kept for the caller rather than left in the conversation.
        self.last_run: dict | None = None
        # Pre-tokenise once. Search is called many times over the same corpus and
        # re-splitting every file per call is the whole cost of the tool.
        self._index: dict[str, set] = {
            path: set(_tokens(path)) | set(_tokens(content))
            for path, content in self.source_files.items()
        }

    # ── dispatch ──────────────────────────────────────────────────────────

    async def run_async(self, name: str, args: dict) -> str:
        """Dispatch, awaiting the tools that need to. What the loop calls.

        Only `run_test` does I/O; keeping the rest synchronous means the bulk of
        the toolbox stays trivially testable without an event loop.
        """
        if name in ASYNC_TOOLS:
            try:
                return await getattr(self, f"_tool_{name}")(args or {})
            except Exception as e:                  # pragma: no cover - defensive
                logger.warning(f"Agent tool {name} failed: {e!r}")
                self.calls.append(ToolCallRecord(name, f"failed: {e}", ok=False))
                return f"Tool {name} failed: {e}"
        return self.run(name, args)

    def run(self, name: str, args: dict) -> str:
        """Execute one synchronous tool call and return the text sent to the model.

        Never raises: a tool error is information the model can act on (bad path,
        malformed input), and turning it into an exception would kill a run that
        the model could have recovered from by itself on the next turn.
        """
        try:
            handler = getattr(self, f"_tool_{name}", None)
            if handler is None:
                self.calls.append(ToolCallRecord(name, "unknown tool", ok=False))
                return f"No such tool: {name}"
            return handler(args or {})
        except Exception as e:                      # pragma: no cover - defensive
            logger.warning(f"Agent tool {name} failed: {e!r}")
            self.calls.append(ToolCallRecord(name, f"failed: {e}", ok=False))
            return f"Tool {name} failed: {e}"

    # ── tools ─────────────────────────────────────────────────────────────

    def _tool_list_files(self, args: dict) -> str:
        if not self.source_files:
            return (
                "No repo source is available in this run — the suite is being "
                "written from the live DOM. Use find_anchors instead."
            )
        lines = [
            f"  {path} ({len(content)} chars)"
            for path, content in sorted(self.source_files.items())
        ]
        self.calls.append(ToolCallRecord("list_files", f"{len(lines)} files"))
        return f"{len(lines)} files:\n" + "\n".join(lines)

    def _tool_search_source(self, args: dict) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            return "search_source needs a non-empty query."
        terms = set(_tokens(query))
        if not terms:
            return "No searchable words in that query."

        scored = []
        for path, vocab in self._index.items():
            overlap = terms & vocab
            if overlap:
                # Path matches count double: a query for "login" wants
                # LoginPage.tsx ahead of a file that merely mentions logging in.
                score = len(overlap) + len(terms & set(_tokens(path)))
                scored.append((score, path))
        scored.sort(key=lambda t: (-t[0], t[1]))

        if not scored:
            return f"No file matched {query!r}. Try list_files to see what exists."

        out = []
        for _, path in scored[:MAX_SEARCH_RESULTS]:
            snippets = []
            for i, line in enumerate((self.source_files[path] or "").splitlines(), 1):
                if any(t in line.lower() for t in terms):
                    snippets.append(f"    {i}: {line.strip()[:160]}")
                    if len(snippets) >= MAX_SNIPPETS_PER_FILE:
                        break
            out.append(f"  {path}\n" + ("\n".join(snippets) or "    (matched on path)"))

        self.calls.append(
            ToolCallRecord("search_source", f"{query!r} → {len(scored)} files"))
        return (
            f"{len(scored)} file(s) matched; showing "
            f"{min(len(scored), MAX_SEARCH_RESULTS)}:\n" + "\n".join(out)
        )

    def _tool_read_file(self, args: dict) -> str:
        path = (args.get("path") or "").strip()
        content = self.source_files.get(path)
        if content is None:
            # A near-miss is the common case (the model guesses a plausible path),
            # and naming the closest real files saves a whole wasted turn.
            near = [p for p in self.source_files if path and path.lower() in p.lower()]
            hint = f" Did you mean: {', '.join(near[:5])}?" if near else ""
            self.calls.append(ToolCallRecord("read_file", f"{path} (missing)", ok=False))
            return f"No file at {path!r}.{hint} Use list_files to see what exists."

        self.calls.append(ToolCallRecord("read_file", path))
        if len(content) > MAX_FILE_CHARS:
            return (
                f"{path} (first {MAX_FILE_CHARS} of {len(content)} chars):\n"
                + content[:MAX_FILE_CHARS]
                + "\n… truncated — search_source for a specific symbol to see the rest."
            )
        return f"{path}:\n{content}"

    def _tool_find_anchors(self, args: dict) -> str:
        kind = (args.get("kind") or "").strip().lower()
        contains = (args.get("contains") or "").strip().lower()

        rows: list[tuple[str, str, str]] = []       # (kind, value, where)
        for index in (self.src_index, self.dom_index):
            if index is None:
                continue
            origin = index.source
            for anchor in index.anchors:
                if kind and anchor.kind != kind:
                    continue
                if contains and contains not in (anchor.value or "").lower():
                    continue
                where = origin
                prov = index.provenance(anchor)
                if prov:
                    where = f"{prov[0].file}:{prov[0].line}"
                rows.append((anchor.kind, anchor.value, where))

        if not rows:
            what = f"kind={kind} " if kind else ""
            what += f"containing {contains!r}" if contains else ""
            return (
                f"No anchors found {what}— nothing in this app proves that selector "
                f"exists. Call find_anchors with no arguments to see everything real."
            )

        rows.sort()
        shown = rows[:MAX_ANCHORS]
        body = "\n".join(f"  {k}={v!r}  ← {w}" for k, v, w in shown)
        more = f"\n… and {len(rows) - len(shown)} more" if len(rows) > len(shown) else ""
        self.calls.append(
            ToolCallRecord("find_anchors", f"{kind or 'all'} → {len(rows)} anchors"))
        return f"{len(rows)} real anchor(s):\n{body}{more}"

    def _tool_validate_code(self, args: dict) -> str:
        filename = (args.get("filename") or "").strip()
        content = args.get("content") or ""
        if not filename:
            return "validate_code needs a filename."

        # Validate the candidate *alongside* the files already written, so the
        # cross-file import check is real: a spec importing '../pages/LoginPage'
        # is only valid if that page object is actually part of this suite.
        candidates = [
            _Shim(name, data["content"])
            for name, data in self.written.items() if name != filename
        ] + [_Shim(filename, content)]

        results = {r.filename: r for r in validate_files(candidates, self.framework_key)}
        mine = results.get(filename)
        if mine is None:                            # pragma: no cover - defensive
            return f"Validator returned nothing for {filename}."
        if mine.ok:
            note = "" if mine.checked else " (no parser for this file type — not deeply checked)"
            self.calls.append(ToolCallRecord("validate_code", f"{filename} ok"))
            return f"{filename}: VALID{note}"
        self.calls.append(
            ToolCallRecord("validate_code", f"{filename} invalid", ok=False))
        return f"{filename}: INVALID — {mine.error}\nFix it and validate again."

    def _tool_write_file(self, args: dict) -> str:
        filename = (args.get("filename") or "").strip().lstrip("/")
        content = args.get("content") or ""
        description = (args.get("description") or "").strip()

        if not filename:
            return "write_file needs a filename."
        if not content.strip():
            return f"Refused to write {filename}: content was empty."
        if len(content) > MAX_WRITE_CHARS:
            return (
                f"Refused to write {filename}: {len(content)} chars exceeds the "
                f"{MAX_WRITE_CHARS} limit. Split it into smaller files."
            )

        replaced = filename in self.written
        self.written[filename] = {"description": description, "content": content}
        self.calls.append(
            ToolCallRecord("write_file", f"{filename} ({len(content)} chars)"))
        verb = "Replaced" if replaced else "Wrote"
        return (
            f"{verb} {filename} ({len(content)} chars). "
            f"Suite now has {len(self.written)} file(s): "
            f"{', '.join(sorted(self.written))}"
        )


    async def _tool_run_test(self, args: dict) -> str:
        # Imported here so a deployment with execution disabled never pulls in
        # the runner, and so the import cost lands on the one run that uses it.
        from services import test_runner

        if not self.written:
            return "Nothing to run yet — write at least one spec file first."

        files = {name: data["content"] for name, data in self.written.items()}
        try:
            result = await test_runner.run_suite(
                files, base_url=self.base_url, framework_key=self.framework_key)
        except test_runner.RunnerUnavailable as e:
            # Not a failure of the suite, and must not read as one. The agent is
            # told to carry on rather than trying to "fix" tests that never ran.
            self.calls.append(ToolCallRecord("run_test", f"unavailable: {e}", ok=False))
            return (
                f"Could not run the tests: {e}\n"
                f"This says nothing about whether your suite is correct — carry "
                f"on writing it, and rely on find_anchors and validate_code."
            )

        self.last_run = result.as_dict()

        if result.timed_out:
            self.calls.append(ToolCallRecord("run_test", "timed out", ok=False))
            return (
                "The run was killed for exceeding the time limit. Usually one "
                "test waits on something that never happens — check for a "
                "selector that never appears, or a navigation that never lands."
            )

        head = (
            f"Ran {result.total} test(s) against {self.base_url}: "
            f"{result.passed} passed, {result.failed} failed"
            + (f", {result.skipped} skipped" if result.skipped else "")
            + f" in {result.duration_ms}ms."
        )
        self.calls.append(ToolCallRecord(
            "run_test",
            f"{result.passed}/{result.total} passed",
            ok=result.ok,
        ))
        if not result.failures:
            return head + (
                "\nEverything passed against the real app."
                if result.total else
                "\nNo tests were collected — check the spec actually declares tests."
            )

        detail = "\n\n".join(
            f"FAILED: {f.test}  ({f.file})\n{f.message}"
            for f in result.failures[:8]
        )
        more = (f"\n\n… and {len(result.failures) - 8} more failure(s)"
                if len(result.failures) > 8 else "")
        return (
            f"{head}\n\n{detail}{more}\n\n"
            f"These are real failures against the live app. Fix them with "
            f"selectors you have confirmed via find_anchors, then run_test again."
        )


# Tools whose handler is a coroutine — see Toolbox.run_async.
ASYNC_TOOLS = {"run_test"}


@dataclass
class _Shim:
    """Minimal stand-in for GeneratedFile — validate_files only reads these two."""
    filename: str
    content: str
