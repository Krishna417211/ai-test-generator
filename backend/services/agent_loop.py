"""
agent_loop.py — the agentic path for writing a test suite.

The scripted pipeline in `agents/writer_agent.py` decides the order of work in
Python: plan the file list, generate each file, validate, heal, ground, heal
selectors. It works, and it stays the default. What it cannot do is *look
something up* — every fact the model gets was chosen before the first token, and
when the prompt's 6k-char source budget cuts the file it needed, the model
guesses and the heal pass cleans up after it.

This module inverts that. The model gets the same capabilities as tools (see
`services/agent_tools.py`) and decides for itself what to read, what to check,
and when it is done. Concretely it can search for the login form, read only that
file, ask the grounding index what selectors genuinely exist there, validate its
page object before committing it, and write it — rather than being handed a
fixed slice of the repo and asked to guess in one shot.

Runs on Claude or Gemini, whichever the tier prefers and has capacity when the
run *starts* — and then stays there, because both providers sign their tool
calls with opaque data that has to be replayed verbatim (see
services/agent_protocol.py). Losing that provider mid-run keeps whatever was
written rather than pretending another can pick it up.

Returns plain dicts, not `GeneratedFile`s, so that `writer_agent` can import this
module without this module importing `writer_agent` back.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from services.llm_router import router, Tier, AllProvidersExhausted
from services.agent_tools import TOOL_SCHEMAS, Toolbox, ToolCallRecord
from services import agent_protocol

logger = logging.getLogger(__name__)

# How many assistant turns before we stop and keep what was written. A suite of
# 6–10 files needs roughly two to four turns per file (search, read, ground,
# validate, write), and the model batches several tool calls per turn. Past this
# it is not converging, and each further turn re-sends the whole conversation —
# the cost per turn *grows*, so an unbounded loop gets expensive fastest exactly
# when it is going worst.
MAX_TURNS = 40

# The same wait-out-the-cooldown retry the scripted path uses. A conversation is
# far more expensive to abandon than a single call — every earlier turn is spent
# and unrecoverable — so this is patient where `complete()` would rotate away.
RATE_RETRY_MAX_ATTEMPTS = 6
RATE_RETRY_WAIT_SECONDS = 20


@dataclass
class AgentRunResult:
    files: list[dict] = field(default_factory=list)      # {filename, description, content}
    summary: str = ""
    turns: int = 0
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    stop_reason: str = "completed"                       # completed | max_turns | exhausted
    # Which provider(s) actually served the turns, in order. Agent mode rotates,
    # so "who wrote this" has an honest answer only as a list.
    providers: list[str] = field(default_factory=list)
    # The last real execution of the suite, if the agent ran one and the runner
    # was available. None means the tests were never run — which is the normal
    # case, and must never be presented as "no failures".
    last_run: Optional[dict] = None

    def as_dict(self) -> dict:
        """What the UI needs to show the agent's work — the trace, not the prose."""
        return {
            "turns": self.turns,
            "stop_reason": self.stop_reason,
            "tool_calls": [
                {"name": c.name, "summary": c.summary, "ok": c.ok}
                for c in self.tool_calls
            ],
            "files_written": len(self.files),
            "last_run": self.last_run,
            "providers": sorted(set(self.providers)),
        }


class AgentUnavailable(Exception):
    """Agent mode could not run at all — the caller should fall back to scripted."""


def _initial_prompt(
    project_summary: str,
    framework_key: str,
    language: str,
    test_flows: str,
    base_url: str,
    has_source: bool,
    anchor_hint: str,
    can_run: bool,
) -> str:
    # Only promise execution when the runner is genuinely available. Telling the
    # model to "run the tests" on a server that can't wastes turns on a tool that
    # answers "unavailable" every time.
    run_step = (
        "5. Run the suite with run_test against the real app, and fix what "
        "actually fails. A selector that exists in the source can still fail "
        "live. Keep going until it passes or you can explain why it can't.\n"
        if can_run else ""
    )
    last_step = (
        "6." if can_run else "5."
    )
    where = (
        "the repo source (list_files / search_source / read_file) and the "
        "grounding index (find_anchors)"
        if has_source
        else "the grounding index (find_anchors) — this run has no repo source, "
             "the anchors come from the live site's rendered DOM"
    )
    return f"""Write an end-to-end test suite for this application.

## PROJECT
{project_summary}

## TARGET
- Framework: {framework_key} ({language})
- Base URL: {base_url}

## WHAT TO TEST
{test_flows or "The main user-facing flows you can find evidence for."}

## WHAT YOU HAVE
You can investigate before you write, using {where}.

A sample of the anchors that provably exist:
{anchor_hint}

## HOW TO WORK
1. Investigate first. Find the pages and forms that the flows above actually
   touch. Do not write a test for a page you have not seen evidence of.
2. Ground every selector. Before you use an id, data-testid, name, role,
   aria-label or class, confirm it with find_anchors. If it isn't there, it does
   not exist — pick one that does, or test that page a different way.
3. Validate each code file with validate_code before write_file, and fix what it
   reports. An invalid file is worse than a missing one.
4. Write the suite with write_file: page objects first, then specs.
{run_step}{last_step} When the suite is complete, stop calling tools and reply with a short
   summary of what you covered and anything you deliberately left out.

Do not invent routes, credentials or fixtures. If something cannot be tested
from what the app actually exposes, say so in the summary instead of faking it.
"""


async def _turn_with_retry(
    messages: list[dict], system: str, tier: Tier, tools: list[dict],
    pinned: Optional[str] = None,
) -> tuple[list[dict], str, str]:
    """One assistant turn, waiting out a rate limit rather than losing the run."""
    last: Optional[Exception] = None
    for attempt in range(RATE_RETRY_MAX_ATTEMPTS + 1):
        try:
            return await router.agent_turn(
                messages=messages,
                system=system,
                tools=tools,
                tier=tier,
                context_hint="writer_agent_loop",
                pinned=pinned,
            )
        except AllProvidersExhausted as e:
            last = e
            # Out of credit is not a wait-and-retry situation — no amount of
            # waiting adds quota, and retrying just delays telling the truth.
            if getattr(e, "reason", "") == "quota_exhausted":
                raise
            if attempt < RATE_RETRY_MAX_ATTEMPTS:
                logger.info(
                    f"Agent turn rate-limited — waiting {RATE_RETRY_WAIT_SECONDS}s "
                    f"(attempt {attempt + 1}/{RATE_RETRY_MAX_ATTEMPTS})"
                )
                await asyncio.sleep(RATE_RETRY_WAIT_SECONDS)
                continue
            raise
    raise last or AllProvidersExhausted("agent turn failed", reason="failed")


async def run_agent(
    *,
    source_files: dict[str, str],
    framework_key: str,
    language: str,
    system_prompt: str,
    project_summary: str,
    test_flows: str,
    base_url: str,
    anchor_hint: str = "",
    src_index=None,
    dom_index=None,
    tier: Tier = Tier.PRO,
    max_turns: int = MAX_TURNS,
) -> AgentRunResult:
    """Drive the tool-use loop until the model stops, and return what it wrote."""

    # Imported here rather than at module scope so a build without the runner
    # (or with execution disabled) never pays for it.
    from services import test_runner
    can_run, why_not = test_runner.availability(framework_key)
    if not can_run:
        logger.info(f"Agent mode without execution: {why_not}")

    # Don't offer a tool that cannot work. A model given run_test on a server
    # with no Node spends turns calling it and being told no.
    tools = TOOL_SCHEMAS if can_run else [
        t for t in TOOL_SCHEMAS if t["name"] != "run_test"
    ]

    box = Toolbox(
        source_files=source_files,
        framework_key=framework_key,
        src_index=src_index,
        dom_index=dom_index,
        base_url=base_url,
    )

    # Normalized conversation (services/agent_protocol.py) — never a provider's
    # own shape, so any agent-capable provider can serve any turn.
    messages: list[dict] = [{
        "role": "user",
        "content": [{"type": agent_protocol.TEXT, "text": _initial_prompt(
            project_summary=project_summary,
            framework_key=framework_key,
            language=language,
            test_flows=test_flows,
            base_url=base_url,
            has_source=bool(source_files),
            anchor_hint=anchor_hint or "  (none extracted)",
            can_run=can_run,
        )}],
    }]

    summary = ""
    stop_reason = "completed"
    turns = 0
    nudged = False
    providers: list[str] = []
    # Whichever provider answers the first turn serves the whole conversation:
    # both sign their tool calls with opaque data that has to be replayed and
    # can't be translated (see services/agent_protocol.py).
    pinned: Optional[str] = None

    for turn in range(max_turns):
        turns = turn + 1
        try:
            content, _stop, provider = await _turn_with_retry(
                messages, system_prompt, tier, tools, pinned)
        except AllProvidersExhausted as e:
            # Keep whatever was written. A partial suite the caller can report
            # honestly beats discarding real work because the budget ran out on
            # the last turn — `failed_files` and the success rate already exist
            # to describe an incomplete suite.
            if box.written:
                logger.warning(f"Agent loop ran out of capacity mid-run: {e}")
                stop_reason = "exhausted"
                break
            raise AgentUnavailable(str(e)) from e

        pinned = pinned or provider
        providers.append(provider)
        logger.info(f"turn {turns} [{provider}]: {agent_protocol.describe(content)}")
        messages.append({"role": "assistant", "content": content})

        tool_uses = [b for b in content if b["type"] == agent_protocol.TOOL_USE]
        text = "".join(
            b["text"] for b in content if b["type"] == agent_protocol.TEXT)

        if not tool_uses:
            # No tool call: the model considers itself done.
            if not box.written and not nudged:
                # It stopped without producing anything — usually it described a
                # plan instead of executing it. One corrective turn is worth far
                # more than failing the run, but only one: if it does it twice,
                # something is wrong that repeating won't fix.
                nudged = True
                messages.append({
                    "role": "user",
                    "content": [{"type": agent_protocol.TEXT, "text": (
                        "You haven't written any files yet — nothing was saved. "
                        "Use write_file for each file of the suite now."
                    )}],
                })
                continue
            summary = text.strip()
            break

        results = []
        for block in tool_uses:
            output = await box.run_async(block["name"], block.get("input") or {})
            results.append({
                "type": agent_protocol.TOOL_RESULT,
                "id": block["id"],
                # Carried alongside the id because Gemini pairs a result to its
                # call by *name*, having issued no id of its own.
                "name": block["name"],
                "content": output,
            })
        messages.append({"role": "user", "content": results})
    else:
        stop_reason = "max_turns"
        logger.warning(f"Agent loop hit the {max_turns}-turn cap — keeping what it wrote")

    files = [
        {"filename": name, "description": data["description"], "content": data["content"]}
        for name, data in box.written.items()
    ]
    if not files:
        raise AgentUnavailable(
            "The agent finished without writing any files"
        )

    logger.info(
        f"Agent loop finished: {len(files)} files, {turns} turns, "
        f"{len(box.calls)} tool calls via {'/'.join(sorted(set(providers)))} "
        f"({stop_reason})"
    )
    return AgentRunResult(
        files=files,
        summary=summary,
        turns=turns,
        tool_calls=box.calls,
        stop_reason=stop_reason,
        last_run=box.last_run,
        providers=providers,
    )
