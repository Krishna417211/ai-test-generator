"""
progress.py — server→client step progress for the long-running POSTs.

`/api/analyze`, `/api/scan` and `/api/publish-zip` each do 10–60s of work behind
a single request, and the client had no way to know which part was running. This
module streams the answer.

Why NDJSON over the endpoint's own response, rather than SSE like
`/api/stream/{job_id}`: all three are POSTs and two carry a multipart upload,
while EventSource can only issue a GET with no headers and no body. Streaming
the POST's own response body keeps one request, the Authorization header and the
file upload exactly as they are — only the response *shape* changes.

The cost is that the status line goes out before the work starts, so a failure
mid-flight can no longer BE an HTTP status. That splits error handling in two,
and the split is load-bearing:

  * Anything decidable up front — a malformed URL, a missing GitHub token, an
    exhausted quota — MUST be raised before the first byte is yielded, so it
    stays a real 400/403/402 and the client keeps reacting to it as it always
    has (`QuotaExceededError` → the upgrade modal).
  * Anything that can only fail once the work is underway is reported in-band as
    {"type": "error", "status": N, "detail": ...} and re-raised on the client as
    the same exception the non-streaming call would have thrown.

Event shapes (one JSON object per line):

    {"type": "step",   "id": "fetch", "state": "running"}
    {"type": "step",   "id": "fetch", "state": "done", "detail": "240 files"}
    {"type": "result", "data": {...}}
    {"type": "error",  "status": 404, "detail": "..."}

A run always ends with exactly one `result` or one `error`.
"""

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Awaitable, Callable

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

MEDIA_TYPE = "application/x-ndjson"

# Every step id the server may emit, grouped by the flow that owns it. These are
# the contract with the frontend: `frontend/src/flows.ts` keys its step cards off
# the same strings, and a card whose id is never emitted simply never lights up.
# That failure is silent in the browser, so it is made loud here instead —
# `Progress` rejects an unknown id rather than emitting an event no card is
# listening for.
GENERATE_STEPS = ("fetch", "extract", "agent1")
SCAN_STEPS = ("target", "checks", "score", "plan", "save")
PUBLISH_STEPS = ("read", "filter", "suite", "push", "record")

KNOWN_STEPS = frozenset(GENERATE_STEPS + SCAN_STEPS + PUBLISH_STEPS)


class Progress:
    """Reports step transitions to the client. Handed to the work function.

    `detail` is shown to the user next to the step, so it should be a fact from
    this run ("18 of 240 files kept"), not a restatement of what the step is.
    """

    def __init__(self, emit: Callable[[dict], Awaitable[None]]):
        self._emit = emit

    @staticmethod
    def _check(step_id: str) -> None:
        if step_id not in KNOWN_STEPS:
            # A programming error, not a runtime condition: these ids are static
            # strings, never derived from user input. Raising means the endpoint
            # test catches a typo instead of a user meeting a step that never
            # finishes.
            raise ValueError(
                f"Unknown progress step {step_id!r}. Add it to progress.py and "
                f"to the matching flow in frontend/src/flows.ts."
            )

    async def start(self, step_id: str) -> None:
        self._check(step_id)
        await self._emit({"type": "step", "id": step_id, "state": "running"})

    async def done(self, step_id: str, detail: str = "") -> None:
        self._check(step_id)
        await self._emit({"type": "step", "id": step_id, "state": "done", "detail": detail})

    async def skip(self, step_id: str, detail: str) -> None:
        """Mark a step as deliberately not run, and say why.

        A step that simply never reports is indistinguishable from one that
        hung, so every branch that bypasses work has to say so: publish without
        CI/CD, or a repo with no UI to test. `detail` is required here — a
        skipped step with no reason is the confusing thing this prevents.
        """
        self._check(step_id)
        await self._emit({"type": "step", "id": step_id, "state": "skipped", "detail": detail})


class NullProgress(Progress):
    """A Progress that reports nothing.

    Lets the agents take a `progress` argument unconditionally instead of
    guarding every call site with `if progress`. They run off a request too —
    from tests, and from publish's CI path — and those callers have nobody to
    report to.
    """

    def __init__(self) -> None:
        super().__init__(self._drop)

    @staticmethod
    async def _drop(event: dict) -> None:
        return None


class _Sentinel:
    pass


_END = _Sentinel()


async def _drain(work: Callable[[Progress], Awaitable[Any]]) -> AsyncIterator[str]:
    """Run `work`, yielding its progress events then its result, as NDJSON."""
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: dict) -> None:
        await queue.put(event)

    async def runner() -> None:
        try:
            result = await work(Progress(emit))
            await queue.put({"type": "result", "data": result})
        except HTTPException as e:
            # The work raised the same HTTPException it would have raised
            # un-streamed; carry the status across so the client can rebuild it.
            await queue.put({"type": "error", "status": e.status_code, "detail": e.detail})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"Streamed handler failed: {e}")
            await queue.put({
                "type": "error",
                "status": 500,
                "detail": "Something went wrong. Please try again.",
            })
        finally:
            await queue.put(_END)

    task = asyncio.create_task(runner())
    try:
        while True:
            event = await queue.get()
            if event is _END:
                break
            yield json.dumps(event) + "\n"
    finally:
        # The client hung up (or the server is shutting down) — stop the work
        # rather than leaving an LLM call running for a response nobody will
        # read. Cancellation is best-effort: the task may already be done.
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def ndjson(work: Callable[[Progress], Awaitable[Any]]) -> StreamingResponse:
    """Wrap `work` in a streaming NDJSON response.

    `work` receives a `Progress` and returns the JSON-serializable body that the
    endpoint would otherwise have returned. Raise HTTPException inside it for
    failures that can only be detected once the work has begun; raise *before*
    calling this for anything that should stay a plain HTTP error.
    """
    return StreamingResponse(
        _drain(work),
        media_type=MEDIA_TYPE,
        headers={
            "Cache-Control": "no-cache",
            # Caddy and nginx will otherwise buffer the whole body and deliver
            # every step at once at the end — which is exactly the spinner this
            # module exists to replace.
            "X-Accel-Buffering": "no",
        },
    )
