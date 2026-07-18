"""
filter_agent.py — Agent 1: File Filter

Takes the raw repo file tree and asks the LLM to produce a final,
curated JSON object of { filename: content } pairs.

The local file_extractor.py does most of the heavy lifting (fast, free).
The LLM's job here is to:
  1. Understand the project structure holistically
  2. Identify which files are most important for E2E testing
  3. Produce a structured summary of the project layout
  4. Flag any unusual patterns or potential testing challenges
"""

import json
import logging
from dataclasses import dataclass
from typing import Optional

from services.file_extractor import ExtractionResult, FileExtractor
from services.llm_router import router, Tier
from services.progress import NullProgress, Progress

logger = logging.getLogger(__name__)


def pluralize(n: int, word: str) -> str:
    return word if n == 1 else f"{word}s"

SYSTEM_PROMPT = """You are an expert frontend engineer and QA architect.
Your job is to analyze a web project's file structure and identify
the most important files for end-to-end (E2E) UI testing.

Focus on:
- User-facing routes and pages
- Interactive components (forms, buttons, navigation)
- Authentication flows
- Key UI patterns and data flows

Be precise and practical. Output only valid JSON when asked."""


class NoTestableUIError(Exception):
    """No browser-renderable files survived extraction.

    Raised instead of letting the pipeline continue: with an empty file tree the
    LLM has nothing to describe, and it answers by echoing the shape of the
    example in the prompt — inventing a login page, a /dashboard route and a JWT
    flow for a project that has none. Failing loudly beats a confident fiction.
    """

    def __init__(self, framework: str, scanned: int):
        self.framework = framework
        self.scanned = scanned
        super().__init__(
            f"No testable UI found. Testra writes browser-based E2E tests, but none "
            f"of the {scanned} file(s) scanned render in a browser "
            f"(detected: {framework}). Point it at a project with pages or "
            f"components — .jsx, .tsx, .vue, .svelte or .html."
        )


@dataclass
class FilterResult:
    project_summary: str           # Human-readable summary for the UI
    key_pages: list[dict]          # [{path, description, test_priority}]
    key_components: list[dict]     # [{path, description}]
    routes: list[str]              # detected routes/URLs
    framework: str
    testing_challenges: list[str]  # things the user should know
    files: dict[str, str]          # final filtered file contents (from extractor)
    total_tokens: int
    file_count: int                # number of files kept (read by the API layer)


class FilterAgent:
    """
    Agent 1: Understands the repo structure and surfaces insights
    before test generation begins.
    """

    def __init__(self, token_budget: int = 600_000):
        self.extractor = FileExtractor(token_budget=token_budget)

    async def run(
        self,
        raw_files: dict[str, str],
        user_description: str = "",
        progress: Optional[Progress] = None,
        tier: Tier = Tier.FREE,
    ) -> FilterResult:
        """
        Phase 1a: Local extraction (fast, deterministic)
        Phase 1b: LLM analysis for project understanding

        `progress`, when given, reports the two phases as they happen — they are
        the bulk of the wait on /api/analyze and the client cannot see the
        boundary between them from the outside. Optional so the agent stays
        callable off a request (tests, and publish's CI path).
        """
        say = progress or NullProgress()

        # ── Phase 1a: Smart local extraction ──
        await say.start("extract")
        extraction: ExtractionResult = self.extractor.extract(raw_files)
        logger.info(
            f"Extraction complete: {extraction.file_count} files, "
            f"~{extraction.total_tokens:,} tokens, framework={extraction.framework}"
        )

        # Stop before the LLM sees an empty tree — see NoTestableUIError.
        if extraction.file_count == 0:
            logger.warning(
                f"No UI files extracted from {len(raw_files)} raw file(s); "
                f"framework={extraction.framework}. Refusing to analyze."
            )
            raise NoTestableUIError(extraction.framework, len(raw_files))

        await say.done(
            "extract",
            f"{extraction.file_count} of {len(raw_files)} files kept · {extraction.framework}",
        )

        # ── Phase 1b: LLM project analysis ──
        await say.start("agent1")
        file_tree_summary = self._build_file_tree_summary(extraction.files)
        analysis = await self._analyze_with_llm(
            file_tree_summary=file_tree_summary,
            framework=extraction.framework,
            user_description=user_description,
            warnings=extraction.warnings,
            tier=tier,
        )
        routes = analysis.get("routes", [])
        pages = analysis.get("key_pages", [])
        await say.done(
            "agent1",
            f"{len(pages)} {pluralize(len(pages), 'page')}, "
            f"{len(routes)} {pluralize(len(routes), 'route')} found",
        )

        return FilterResult(
            project_summary=analysis.get("project_summary", ""),
            key_pages=analysis.get("key_pages", []),
            key_components=analysis.get("key_components", []),
            routes=analysis.get("routes", []),
            framework=extraction.framework,
            testing_challenges=analysis.get("testing_challenges", [])
                + extraction.warnings,
            files=extraction.files,
            total_tokens=extraction.total_tokens,
            file_count=extraction.file_count,
        )

    async def _analyze_with_llm(
        self,
        file_tree_summary: str,
        framework: str,
        user_description: str,
        warnings: list[str],
        tier: Tier = Tier.FREE,
    ) -> dict:
        """
        Ask the LLM to analyze the project and return structured JSON.
        """
        # The schema below deliberately uses <angle-bracket> placeholders rather
        # than realistic sample values. An earlier version illustrated the shape
        # with "src/pages/Login.tsx", routes ["/", "/login", "/dashboard"] and a
        # JWT/infinite-scroll challenge — and the model reproduced those verbatim
        # for projects that contained none of them. Describe the shape; never
        # supply content that is plausible enough to copy.
        prompt = f"""
Analyze this {framework} web project file structure and return a JSON object.

FILE TREE:
{file_tree_summary}

USER'S TESTING GOALS:
{user_description or "No specific goals provided — cover all main user flows."}

{"WARNINGS: " + "; ".join(warnings) if warnings else ""}

GROUNDING RULES — these override everything else:
- Every "path" you output MUST appear verbatim in FILE TREE above. Never invent a path.
- Every route MUST be one you can point to in the file contents shown. Do not guess
  conventional routes just because most apps have them.
- Describe only features you can see evidence of. If FILE TREE is small or unclear,
  return fewer items — a short, accurate answer is correct; a padded one is a failure.
- If you cannot ground an item in the files shown, omit it entirely.
- Return empty arrays rather than plausible-sounding filler.

Return ONLY a valid JSON object (no markdown, no explanation) with this shape:
{{
  "project_summary": "<2-3 sentences describing what THIS app does, based only on the files above>",
  "key_pages": [
    {{
      "path": "<exact path copied from FILE TREE>",
      "description": "<what this page does>",
      "test_priority": "<high|medium|low>",
      "suggested_tests": ["<flow grounded in this file's contents>"]
    }}
  ],
  "key_components": [
    {{
      "path": "<exact path copied from FILE TREE>",
      "description": "<what this component does>"
    }}
  ],
  "routes": ["<route found in the routing code above>"],
  "testing_challenges": [
    "<a real obstacle you can see in these files, or omit>"
  ]
}}

Include up to 15 key_pages and 10 key_components. Be specific about selectors and flows.
""".strip()

        try:
            raw = await router.complete(
                prompt=prompt,
                system_prompt=SYSTEM_PROMPT,
                temperature=0.1,
                context_hint="filter_agent",
                json_mode=True,
                tier=tier,
            )
            # Strip any markdown fencing the LLM might add
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw)

        except Exception as e:
            logger.error(f"Filter agent LLM call failed: {e}")
            # Return a minimal result so the pipeline can still continue
            return {
                "project_summary": f"Could not analyze project structure: {e}",
                "key_pages": [],
                "key_components": [],
                "routes": [],
                "testing_challenges": [str(e)],
            }

    def _build_file_tree_summary(self, files: dict[str, str]) -> str:
        """
        Build a compact file tree string that shows structure
        without sending all file contents to the LLM.
        For the filter agent, we just need the structure + key file previews.
        """
        lines = ["Project files:"]
        for path in sorted(files.keys()):
            size = len(files[path])
            lines.append(f"  {path}  ({size:,} bytes)")

        # Append content of the highest-priority files
        HIGH_PRIORITY = ["package.json", "App.jsx", "App.tsx", "router/index.js"]
        lines.append("\nKey file previews:")

        for path, content in files.items():
            name = path.split("/")[-1]
            if name in HIGH_PRIORITY or any(hp in path for hp in ["pages/", "router"]):
                preview = content[:800]
                lines.append(f"\n--- {path} ---\n{preview}")
                if len(content) > 800:
                    lines.append(f"  ... ({len(content) - 800} more chars)")

        return "\n".join(lines)
