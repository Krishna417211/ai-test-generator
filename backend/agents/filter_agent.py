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

from services.file_extractor import ExtractionResult, FileExtractor
from services.llm_router import router

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert frontend engineer and QA architect.
Your job is to analyze a web project's file structure and identify
the most important files for end-to-end (E2E) UI testing.

Focus on:
- User-facing routes and pages
- Interactive components (forms, buttons, navigation)
- Authentication flows
- Key UI patterns and data flows

Be precise and practical. Output only valid JSON when asked."""


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
    ) -> FilterResult:
        """
        Phase 1a: Local extraction (fast, deterministic)
        Phase 1b: LLM analysis for project understanding
        """

        # ── Phase 1a: Smart local extraction ──
        extraction: ExtractionResult = self.extractor.extract(raw_files)
        logger.info(
            f"Extraction complete: {extraction.file_count} files, "
            f"~{extraction.total_tokens:,} tokens, framework={extraction.framework}"
        )

        # ── Phase 1b: LLM project analysis ──
        file_tree_summary = self._build_file_tree_summary(extraction.files)
        analysis = await self._analyze_with_llm(
            file_tree_summary=file_tree_summary,
            framework=extraction.framework,
            user_description=user_description,
            warnings=extraction.warnings,
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
    ) -> dict:
        """
        Ask the LLM to analyze the project and return structured JSON.
        """
        prompt = f"""
Analyze this {framework} web project file structure and return a JSON object.

FILE TREE:
{file_tree_summary}

USER'S TESTING GOALS:
{user_description or "No specific goals provided — cover all main user flows."}

{"WARNINGS: " + "; ".join(warnings) if warnings else ""}

Return ONLY a valid JSON object (no markdown, no explanation) with this shape:
{{
  "project_summary": "2-3 sentence description of what this app does and its main features",
  "key_pages": [
    {{
      "path": "src/pages/Login.tsx",
      "description": "User login page with email/password form",
      "test_priority": "high",
      "suggested_tests": ["successful login", "invalid credentials", "forgot password link"]
    }}
  ],
  "key_components": [
    {{
      "path": "src/components/Navbar.tsx",
      "description": "Top navigation with links and user menu"
    }}
  ],
  "routes": ["/", "/login", "/dashboard", "/profile"],
  "testing_challenges": [
    "App uses JWT auth — tokens need to be seeded in localStorage before protected route tests",
    "Product grid uses infinite scroll — need special waits for dynamic content"
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
