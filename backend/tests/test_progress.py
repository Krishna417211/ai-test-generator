"""test_progress.py — the NDJSON progress channel behind the long POSTs.

The bar here is the split that streaming forces (see services/progress.py): a
failure decided *before* the first byte must stay a real HTTP status, and one
decided after must arrive in-band carrying that status, because the client
rebuilds the same exception from either. Get that wrong and a 402 stops opening
the upgrade modal, or a 404 arrives as a 200 with a body nobody reads.

The endpoint tests assert the *step ids*, not just that something streamed:
`frontend/src/flows.ts` keys its cards off these strings, and a renamed id is
invisible on both sides — the card simply never lights up.
"""

import asyncio
import json
import tempfile
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from services.progress import (
    KNOWN_STEPS, NullProgress, Progress, ndjson,
)
from services.store import JobStore


def _events(collected: list) -> list[dict]:
    return collected


async def _collect(work) -> list[dict]:
    """Drive the streaming response and decode every line it emits."""
    response = ndjson(work)
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
    return [json.loads(line) for line in "".join(chunks).splitlines() if line.strip()]


class TestProgress:
    def test_start_and_done_emit_the_step_id_and_detail(self):
        seen: list[dict] = []

        async def run():
            p = Progress(lambda e: _append(seen, e))
            await p.start("fetch")
            await p.done("fetch", "9 files read")

        asyncio.run(run())
        assert seen == [
            {"type": "step", "id": "fetch", "state": "running"},
            {"type": "step", "id": "fetch", "state": "done", "detail": "9 files read"},
        ]

    def test_skip_carries_the_reason(self):
        seen: list[dict] = []
        asyncio.run(_skip(seen))
        assert seen[0]["state"] == "skipped"
        assert seen[0]["detail"] == "CI/CD not requested"

    def test_an_unknown_step_id_is_rejected(self):
        """A typo'd id is silent in the browser — the card just never lights.
        It must not be silent here."""
        with pytest.raises(ValueError, match="Unknown progress step"):
            asyncio.run(Progress(_noop).start("fetchh"))

    def test_every_flow_id_is_registered(self):
        # The ids main.py actually emits, spelled out independently of the
        # constants so a rename has to be made deliberately in both places.
        for step in ("fetch", "extract", "agent1",
                     "target", "checks", "score", "plan", "save",
                     "read", "filter", "suite", "push", "record"):
            assert step in KNOWN_STEPS

    def test_null_progress_swallows_everything(self):
        """The agents take a Progress unconditionally; off-request callers pass
        this rather than every call site guarding on None."""
        async def run():
            p = NullProgress()
            await p.start("fetch")
            await p.done("fetch", "x")
            await p.skip("suite", "y")
        asyncio.run(run())  # must not raise


async def _append(sink: list, event: dict) -> None:
    sink.append(event)


async def _noop(event: dict) -> None:
    return None


async def _skip(sink: list) -> None:
    await Progress(lambda e: _append(sink, e)).skip("suite", "CI/CD not requested")


class TestNdjsonStream:
    def test_steps_then_result_in_order(self):
        async def work(p: Progress):
            await p.start("fetch")
            await p.done("fetch", "9 files read")
            return {"job_id": "j1"}

        events = asyncio.run(_collect(work))
        assert [e["type"] for e in events] == ["step", "step", "result"]
        assert events[-1]["data"] == {"job_id": "j1"}

    def test_http_exception_becomes_an_in_band_error_with_its_status(self):
        """The status line is long gone by the time work fails, so the status
        has to travel in the body for the client to rebuild the same error."""
        async def work(p: Progress):
            await p.start("fetch")
            raise HTTPException(404, "Repository not found")

        events = asyncio.run(_collect(work))
        assert events[-1] == {"type": "error", "status": 404, "detail": "Repository not found"}

    def test_a_structured_402_detail_survives_intact(self):
        """The quota payload is an object, not a string — the upgrade modal is
        built from its fields, so it must not be stringified on the way out."""
        payload = {"message": "out of credits", "used": 5, "limit": 5, "pricing": {"monthly_usd": 20}}

        async def work(p: Progress):
            raise HTTPException(402, payload)

        events = asyncio.run(_collect(work))
        assert events[-1]["status"] == 402
        assert events[-1]["detail"] == payload

    def test_an_unexpected_exception_is_a_500_and_leaks_nothing(self):
        async def work(p: Progress):
            raise RuntimeError("psycopg2: password authentication failed for user 'admin'")

        events = asyncio.run(_collect(work))
        assert events[-1]["status"] == 500
        assert "password" not in json.dumps(events[-1])

    def test_a_run_ends_with_exactly_one_terminal_event(self):
        async def work(p: Progress):
            await p.start("fetch")
            return {"ok": True}

        events = asyncio.run(_collect(work))
        terminal = [e for e in events if e["type"] in ("result", "error")]
        assert len(terminal) == 1

    def test_every_line_is_standalone_json(self):
        """The client splits on newlines, so a step's detail containing one
        would desynchronise the parse."""
        async def work(p: Progress):
            await p.done("fetch", "line one\nline two")
            return {}

        response = ndjson(work)

        async def drain():
            out = ""
            async for c in response.body_iterator:
                out += c if isinstance(c, str) else c.decode()
            return out

        raw = asyncio.run(drain())
        for line in raw.splitlines():
            if line.strip():
                json.loads(line)  # must not raise


@pytest.fixture
def client(monkeypatch):
    """A TestClient with a real store and a signed-in user."""
    import main
    from services import auth

    store = JobStore(tempfile.mktemp(suffix=".db"))
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(auth, "store", store)

    store.create_user({
        "id": "usr_1", "email": "u@example.com", "created_at": time.time(),
        "email_verified": True,
    })
    token = auth.create_login_session("usr_1")
    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c, store


def _lines(response) -> list[dict]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


class TestScanRoute:
    """/api/scan is the simplest streamed endpoint — one service call behind it."""

    def test_streams_the_scan_steps_then_the_result(self, client, monkeypatch):
        import main

        class FakeResult:
            url = "https://x.test"
            final_url = "https://x.test/"
            score = 90
            grade = "A"
            counts = {"high": 0}
            checks_run = 7
            findings: list = []

        class FakeScanner:
            async def scan(self, url, progress=None):
                await progress.start("target")
                await progress.done("target", "x.test reachable")
                await progress.start("checks")
                await progress.done("checks", "7 checks run · 0 issues found")
                await progress.start("score")
                await progress.done("score", "Grade A · 90/100")
                return FakeResult()

        monkeypatch.setattr(main, "SecurityScanner", FakeScanner)
        c, _ = client
        r = c.post("/api/scan", json={"url": "https://x.test", "ai_summary": False})

        assert r.status_code == 200
        events = _lines(r)
        step_ids = [e["id"] for e in events if e["type"] == "step" and e["state"] == "running"]
        # The ids the frontend's scan flow renders cards for.
        assert step_ids == ["target", "checks", "score", "plan", "save"]
        assert events[-1]["type"] == "result"
        assert events[-1]["data"]["grade"] == "A"

    def test_the_plan_step_always_closes_even_with_no_ai_summary(self, client, monkeypatch):
        """A clean site never asks the model. The step must still resolve, or it
        spins forever with nothing left to close it."""
        import main

        class FakeResult:
            url = final_url = "https://x.test/"
            score, grade, checks_run = 100, "A", 7
            counts: dict = {}
            findings: list = []

        class FakeScanner:
            async def scan(self, url, progress=None):
                return FakeResult()

        monkeypatch.setattr(main, "SecurityScanner", FakeScanner)
        c, _ = client
        events = _lines(c.post("/api/scan", json={"url": "https://x.test", "ai_summary": True}))
        plan = [e for e in events if e.get("id") == "plan"]
        assert [e["state"] for e in plan] == ["running", "done"]

    def test_an_unreachable_site_arrives_as_an_in_band_400(self, client, monkeypatch):
        import main
        from services.security_scanner import ScanError

        class FakeScanner:
            async def scan(self, url, progress=None):
                await progress.start("target")
                raise ScanError("Could not reach the site: nope")

        monkeypatch.setattr(main, "SecurityScanner", FakeScanner)
        c, _ = client
        r = c.post("/api/scan", json={"url": "https://x.test", "ai_summary": False})

        # 200: the stream opened before the scan failed. The real status rides
        # in the body, which is what the client re-raises from.
        assert r.status_code == 200
        assert _lines(r)[-1]["status"] == 400

    @pytest.mark.parametrize("url", [
        "https://www.google.com", "https://github.com", "https://whitehouse.gov",
    ])
    def test_a_third_party_site_is_a_real_400_and_never_streams(self, url, monkeypatch, client):
        """The other side of the split in progress.py's docstring: this one IS
        decidable from the URL alone, so it must not become a 200 with an error
        line inside. The scanner must never be constructed either — refusing a
        target we won't scan should cost no work and no network."""
        import main

        class ExplodingScanner:
            def __init__(self, *a, **k):
                raise AssertionError("must not scan an out-of-scope target")

        monkeypatch.setattr(main, "SecurityScanner", ExplodingScanner)
        c, _ = client
        r = c.post("/api/scan", json={"url": url, "ai_summary": False})

        assert r.status_code == 400
        assert "application/x-ndjson" not in r.headers.get("content-type", "")
        # However it's phrased, the refusal has to point somewhere actionable.
        assert "own" in r.json()["detail"].lower()

    def test_the_scan_route_still_accepts_an_ordinary_site(self, monkeypatch, client):
        """The gate must not swallow the normal case."""
        import main

        class FakeResult:
            url = final_url = "https://my-app.test/"
            score, grade, checks_run = 100, "A", 7
            counts: dict = {}
            findings: list = []

        class FakeScanner:
            async def scan(self, url, progress=None):
                return FakeResult()

        monkeypatch.setattr(main, "SecurityScanner", FakeScanner)
        c, _ = client
        r = c.post("/api/scan", json={"url": "https://my-app.test", "ai_summary": False})
        assert r.status_code == 200
        assert _lines(r)[-1]["type"] == "result"


class TestAnalyzeRoute:
    def test_a_malformed_url_is_a_real_http_error_and_never_streams(self):
        """Decidable before any work, so it must stay a real status rather than
        becoming a 200 carrying an error line.

        The status is 422, not the handler's 400: GenerateRequest.validate_github_url
        rejects the URL during request parsing, so the handler never runs. Both
        are pre-flight — which is the property under test. Asserting "not 200 +
        NDJSON" rather than an exact code keeps this honest about which layer
        answers, while still failing if the rejection ever slides into the
        stream (where the client would see a 200 and a body it has to unpack)."""
        import main
        from services import auth

        store = JobStore(tempfile.mktemp(suffix=".db"))
        with TestClient(main.app) as c:
            import unittest.mock as mock
            with mock.patch.object(main, "store", store), mock.patch.object(auth, "store", store):
                store.create_user({"id": "usr_2", "email": "b@example.com",
                                   "created_at": time.time(), "email_verified": True})
                token = auth.create_login_session("usr_2")
                r = c.post(
                    "/api/analyze",
                    json={"repo_url": "not-a-github-url", "framework": "playwright",
                          "language": "typescript", "test_flows": "", "base_url": "http://x"},
                    headers={"Authorization": f"Bearer {token}"},
                )
        assert r.status_code in (400, 422)
        assert r.headers["content-type"].startswith("application/json")
        assert "x-ndjson" not in r.headers["content-type"]
