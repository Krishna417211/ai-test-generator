"""test_filter_agent.py — Guards against fabricated project analysis.

The filter agent sends a file tree to an LLM and asks it to describe the project.
Two things made it confidently describe apps that did not exist:

  1. A project with no browser-renderable files extracted to an empty tree, and
     the agent asked the LLM to analyze it anyway.
  2. The prompt illustrated its JSON schema with realistic sample values
     ("src/pages/Login.tsx", "/dashboard", a JWT note). Given nothing real to
     describe, the model returned the sample.

Together they turned a pure FastAPI backend into a React app with a login page.
"""

import asyncio

import pytest

from agents import filter_agent
from agents.filter_agent import FilterAgent, NoTestableUIError

# A real API-only project: every file is server-side Python, nothing renders.
BACKEND_ONLY_FILES = {
    "requirements.txt": "fastapi==0.110.0\nuvicorn==0.29.0\n",
    "app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
    "app/routers/events.py": "from fastapi import APIRouter\nrouter = APIRouter()\n",
    "app/models/event.py": "class Event: ...\n",
    "run.py": "import uvicorn\n",
}


class TestNoTestableUI:
    def test_backend_only_project_raises(self):
        with pytest.raises(NoTestableUIError) as exc:
            asyncio.run(FilterAgent().run(BACKEND_ONLY_FILES))
        assert exc.value.scanned == len(BACKEND_ONLY_FILES)

    def test_error_names_the_detected_stack_and_is_actionable(self):
        with pytest.raises(NoTestableUIError) as exc:
            asyncio.run(FilterAgent().run(BACKEND_ONLY_FILES))
        msg = str(exc.value)
        assert "FastAPI" in msg, "should tell the user what it detected"
        # Must point at a way forward, not just state the failure.
        assert ".tsx" in msg or ".jsx" in msg

    def test_empty_input_raises(self):
        with pytest.raises(NoTestableUIError):
            asyncio.run(FilterAgent().run({}))

    def test_never_calls_the_llm_when_there_is_nothing_to_describe(self, monkeypatch):
        """The guard must short-circuit before the LLM — an empty tree is exactly
        the condition under which the model invents a project."""
        called = False

        async def _fail(*args, **kwargs):
            nonlocal called
            called = True
            return "{}"

        monkeypatch.setattr(filter_agent.router, "complete", _fail)
        with pytest.raises(NoTestableUIError):
            asyncio.run(FilterAgent().run(BACKEND_ONLY_FILES))
        assert not called, "LLM was called with an empty file tree"

    def test_project_with_ui_still_passes_the_guard(self, monkeypatch):
        """The guard must not fire on a real UI project."""

        async def _ok(*args, **kwargs):
            return '{"project_summary": "s", "key_pages": [], "key_components": [], "routes": [], "testing_challenges": []}'

        monkeypatch.setattr(filter_agent.router, "complete", _ok)
        result = asyncio.run(
            FilterAgent().run(
                {
                    "package.json": '{"dependencies": {"react": "^18.0.0"}}',
                    "src/pages/Home.tsx": "export default function Home() { return <div/>; }",
                }
            )
        )
        assert result.file_count > 0


class TestPromptIsNotCopyable:
    """The schema must be described, not demonstrated. Any realistic-looking
    sample value is a string the model can echo for an unrelated codebase.

    Asserts on the prompt actually sent to the provider — not on module source,
    which also contains the explanatory comments naming these same strings."""

    def _prompt(self, monkeypatch) -> str:
        captured = {}

        async def _capture(prompt, **kwargs):
            captured["prompt"] = prompt
            return '{"project_summary": "s", "key_pages": [], "key_components": [], "routes": [], "testing_challenges": []}'

        monkeypatch.setattr(filter_agent.router, "complete", _capture)
        asyncio.run(
            FilterAgent().run(
                {
                    "package.json": '{"dependencies": {"react": "^18.0.0"}}',
                    "src/pages/Home.tsx": "export default function Home() { return <div/>; }",
                }
            )
        )
        prompt = captured["prompt"]
        assert "GROUNDING RULES" in prompt
        return prompt

    @pytest.mark.parametrize(
        "leaked",
        [
            "src/pages/Login.tsx",
            "src/components/Navbar.tsx",
            "/dashboard",
            "/profile",
            "/forgot-password",
            "infinite scroll",
            "JWT auth",
        ],
    )
    def test_no_plausible_sample_values_in_prompt(self, monkeypatch, leaked):
        assert leaked not in self._prompt(monkeypatch), (
            f"{leaked!r} is concrete enough for the model to copy verbatim into "
            f"an analysis of a project that has no such thing. Use a "
            f"<placeholder> describing the field instead."
        )

    def test_prompt_forbids_inventing_paths(self, monkeypatch):
        prompt = self._prompt(monkeypatch)
        assert "MUST appear verbatim in FILE TREE" in prompt
        assert "Never invent a path" in prompt
