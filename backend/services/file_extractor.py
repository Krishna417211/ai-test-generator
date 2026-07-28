"""
file_extractor.py — Intelligent file filtering for UI test generation

The core insight: we only want files that render in the browser.
Backend logic, DB migrations, server configs — all noise.

Scoring system: files get an importance score 1–10:
  10 = router files (App.jsx, router/index.js) — always include
   9 = page components (pages/, views/, screens/)
   8 = top-level layout components
   7 = form components, auth components
   6 = shared UI components
   5 = config files (package.json, vite.config.js)
   4 = utility components
   1 = everything else UI-adjacent

If token budget is tight, we truncate low-score files first.
"""

import os
import re
import io
import json
import zipfile
import logging
from dataclasses import dataclass, field
from pathlib import Path

from services import safe_paths, secrets_guard
from services.importance_model import learned_score

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# File classification rules
# ─────────────────────────────────────────────

# Extensions that can render in the browser
UI_EXTENSIONS = {
    ".html", ".htm",
    ".jsx", ".tsx",
    ".vue",
    ".svelte",
    ".blade.php",
    ".erb",
    ".ejs",
    ".hbs",                      # Handlebars
    ".component.html",           # Angular
}

# Pure config/metadata files we want (read-only, small)
CONFIG_FILES = {
    "package.json",
    "package-lock.json",         # for framework detection only, truncated
    "vite.config.js", "vite.config.ts",
    "next.config.js", "next.config.ts",
    "nuxt.config.js", "nuxt.config.ts",
    "angular.json",
    "svelte.config.js",
    # Only the example. A real .env is the app's live credentials, and this set
    # decides what gets pasted into a third-party model's prompt — see
    # services/secrets_guard.py, which drops it even if it were listed here.
    ".env.example",
    "tailwind.config.js", "tailwind.config.ts",
    "tsconfig.json",
}

# Router/entry files — highest priority
ROUTER_PATTERNS = [
    r"(^|/)App\.(jsx|tsx|js|ts|vue|svelte)$",   # anchor to a path boundary so
    r"(^|/)main\.(jsx|tsx|js|ts)$",             # "ChatApp.tsx" doesn't match
    r"router/(index|routes)\.(js|ts)$",
    r"routes\.(js|ts|rb|php)$",
    r"urls\.py$",
    r"web\.php$",
    r"app-routing\.module\.ts$",
    r"pages/_app\.(jsx|tsx|js|ts)$",
    r"src/index\.(jsx|tsx|js|ts)$",
]

# Directories that are purely server-side — always skip
SKIP_DIRECTORIES = {
    "node_modules", ".git", ".github", "dist", "build",
    "__pycache__", ".pytest_cache", "coverage", ".nyc_output",
    "migrations", "alembic", "seeds", "fixtures",
    ".next", ".nuxt", ".svelte-kit",
    "vendor",                    # PHP
    ".terraform", "infra",
    "docs", "storybook-static",
}

# File patterns to always skip
SKIP_PATTERNS = [
    r"\.test\.(js|ts|jsx|tsx)$",
    r"\.spec\.(js|ts|jsx|tsx)$",
    r"\.stories\.(js|ts|jsx|tsx|mdx)$",
    r"\.(min\.js|bundle\.js)$",
    r"\.map$",
    r"\.lock$",
    r"Dockerfile",
    r"\.yml$", r"\.yaml$",       # CI/CD, Docker Compose
    r"\.(sql|sqlite|db)$",
    r"\.(png|jpg|jpeg|gif|svg|ico|webp|woff|woff2|ttf|eot)$",
    r"\.(csv|xlsx|json)$",       # data files (not config)
]

# Directories with high-value UI content
PRIORITY_DIRS = [
    "pages", "app", "views", "screens",
    "components", "containers", "features",
    "layouts", "templates",
]


# ─────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────

@dataclass
class ScoredFile:
    path: str
    content: str
    size: int
    importance: int = 5           # 1–10
    token_estimate: int = 0

    def __post_init__(self):
        self.token_estimate = len(self.content) // 4  # rough: 1 token ≈ 4 chars


@dataclass
class ExtractionResult:
    files: dict[str, str]         # path → content
    framework: str                # detected frontend framework
    monorepo: bool
    monorepo_packages: list[str]
    file_count: int
    total_tokens: int
    skipped_count: int
    truncated: bool
    warnings: list[str] = field(default_factory=list)
    # Credential files held back before anything was sent to a model.
    # See services/secrets_guard.py.
    excluded_secrets: list[dict] = field(default_factory=list)


# ─────────────────────────────────────────────
# Framework detection
# ─────────────────────────────────────────────

def find_manifest(files: dict[str, str], name: str) -> str:
    """Content of the shallowest file called `name`, searched at any depth.

    Looking only at the repo root broke every split-layout project: a repo with
    frontend/package.json and backend/requirements.txt matched neither, so React
    detection was skipped entirely and the stack fell through to backend
    guesswork. Shallowest wins, so a root manifest still beats a nested one.
    """
    candidates = [p for p in files if p == name or p.endswith("/" + name)]
    if not candidates:
        return ""
    candidates.sort(key=lambda p: (p.count("/"), len(p)))
    return files[candidates[0]]


# A real Flask app imports flask at the start of a line. Substring-matching
# "from flask import" anywhere in a .py also matched this module's own detection
# code and its tests, so testgen-ai — a React app — detected itself as Flask.
_PY_IMPORT_RE = {
    "flask": re.compile(r"^\s*(from flask import|import flask\b)", re.M),
}


def detect_framework(files: dict[str, str]) -> str:
    """
    Detect the app's UI stack — SPA frameworks (React/Vue/…), server-rendered
    backends (Django/Flask/Rails/Laravel), and their template engines.

    Returns a human-readable name used in prompts and shown in the UI. Works
    best on the *raw* file set (so backend markers like manage.py and
    requirements.txt are visible), but degrades gracefully on any subset.
    """
    paths = list(files.keys())
    path_blob = " ".join(paths)

    def has_ext(*exts: str) -> bool:
        return any(p.lower().endswith(exts) for p in paths)

    # ── 1. JavaScript / SPA frameworks (package.json is authoritative) ──
    pkg_content = find_manifest(files, "package.json")
    if pkg_content:
        try:
            pkg = json.loads(pkg_content)
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        except json.JSONDecodeError:
            deps = {}

        if "next" in deps:
            return "Next.js (React)"
        if "nuxt" in deps or "@nuxt/kit" in deps:
            return "Nuxt (Vue)"
        if "@angular/core" in deps:
            return "Angular"
        if "react" in deps:
            if "@remix-run/react" in deps:
                return "Remix (React)"
            if "gatsby" in deps:
                return "Gatsby (React)"
            return "React (Vite/CRA)"
        if "vue" in deps:
            return "Vue 3"
        if "svelte" in deps:
            return "Svelte/SvelteKit"
        if any(k in deps for k in ("express", "koa", "@hapi/hapi", "fastify")):
            if has_ext(".ejs", ".hbs", ".pug", ".handlebars"):
                return "Node.js/Express (server-rendered)"
        # package.json present but no known UI framework — keep checking below.

    # ── 2. Python backends ──
    py_reqs = " ".join(
        find_manifest(files, name)
        for name in ("requirements.txt", "pyproject.toml", "Pipfile", "setup.py")
    ).lower()
    settings_blob = " ".join(
        c for p, c in files.items() if p.endswith("settings.py")
    ).lower()
    if (
        any(p == "manage.py" or p.endswith("/manage.py") for p in paths)
        or "django" in py_reqs
        or "installed_apps" in settings_blob
    ):
        return "Django (server-rendered templates)"
    if "flask" in py_reqs or any(
        _PY_IMPORT_RE["flask"].search(files.get(p, "")) for p in paths if p.endswith(".py")
    ):
        return "Flask (Jinja2 templates)"
    if "fastapi" in py_reqs:
        return "FastAPI (Jinja2 templates)" if has_ext(".html", ".j2") else "FastAPI (API)"

    # ── 3. Ruby on Rails ──
    gemfile = find_manifest(files, "Gemfile").lower()
    if "rails" in gemfile or has_ext(".erb") or any(p.endswith("config/routes.rb") for p in paths):
        return "Ruby on Rails (ERB templates)"

    # ── 4. PHP / Laravel ──
    composer = find_manifest(files, "composer.json").lower()
    if "laravel/framework" in composer or "laravel" in pkg_content.lower() or has_ext(".blade.php"):
        return "Laravel/Blade"

    # ── 5. Template-engine heuristics from HTML content ──
    html_blob = " ".join(
        c for p, c in files.items() if p.lower().endswith((".html", ".htm"))
    )[:20000]
    if "{% " in html_blob or "{%-" in html_blob:
        return "Django/Jinja templates"

    # ── 6. Extension-based frontend inference (last resort) ──
    if ".vue" in path_blob:
        return "Vue 3"
    if ".svelte" in path_blob:
        return "Svelte/SvelteKit"
    if find_manifest(files, "angular.json"):
        return "Angular"
    if has_ext(".jsx", ".tsx"):
        return "React (Vite/CRA)"

    # ── 7. Plain static site (no build step, no framework) ──
    # Hand-written .html served as files — GitHub Pages, S3, nginx. Nothing above
    # matched, so there is no SPA manifest, no backend, and no template syntax.
    #
    # This has to be named rather than left to the React guess below. The
    # detected stack is interpolated verbatim into both agents' prompts, and a
    # writer told "React" writes React: SPA navigation to /login, waits on
    # networkidle, a client-side router that isn't there. The pages of a static
    # site are real files (login.html), so every one of those routes 404s and the
    # whole suite fails against an app that works.
    if has_ext(".html", ".htm"):
        return "Static HTML/JS (multi-page)"

    return "Unknown (Node.js)" if pkg_content else "Unknown (likely React)"


def detect_monorepo(files: dict[str, str]) -> tuple[bool, list[str]]:
    """Detect monorepo structure and return sub-package names."""
    # A malformed package.json must not crash the whole analysis, so parse
    # defensively and treat unparseable content as an empty object.
    try:
        pkg = json.loads(find_manifest(files, "package.json") or "{}")
    except json.JSONDecodeError:
        pkg = {}

    if find_manifest(files, "lerna.json") or find_manifest(files, "nx.json"):
        workspaces = pkg.get("workspaces", [])
        if isinstance(workspaces, dict):
            workspaces = workspaces.get("packages", [])
        return True, workspaces if isinstance(workspaces, list) else []

    workspaces = pkg.get("workspaces", [])
    if workspaces:
        return True, workspaces if isinstance(workspaces, list) else []

    return False, []


# ─────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────

def score_file(path: str) -> int:
    """Assign an importance score to a file path (1–10)."""
    name = Path(path).name.lower()
    path_lower = path.lower()

    # Routers and entry points
    for pattern in ROUTER_PATTERNS:
        if re.search(pattern, path, re.IGNORECASE):
            return 10

    # Page-level components
    parts = path_lower.split("/")
    if any(d in parts for d in ["pages", "app", "views", "screens"]):
        return 9

    # Layouts
    if any(d in parts for d in ["layouts", "layout"]):
        return 8

    # Config files
    if name in CONFIG_FILES:
        return 5

    # Auth / forms (usually high test value)
    if any(kw in path_lower for kw in ["auth", "login", "signup", "form", "checkout", "cart"]):
        return 8

    # Shared components
    if any(d in parts for d in ["components", "containers", "features"]):
        return 6

    # Templates
    if any(d in parts for d in ["templates", "partials"]):
        return 7

    return 4


# ─────────────────────────────────────────────
# Main extractor
# ─────────────────────────────────────────────

class FileExtractor:
    """
    Takes a raw dict of {path: content} (from GitHub or ZIP),
    filters to UI-relevant files, scores them, and returns a
    trimmed set that fits within token budget.
    """

    def __init__(self, token_budget: int = 600_000):
        """
        token_budget: max tokens to send to LLM.
        Gemini Flash supports 1M, but we stay conservative.
        """
        self.token_budget = token_budget

    def extract(self, raw_files: dict[str, str]) -> ExtractionResult:
        warnings = []
        original_count = len(raw_files)

        # Step 0: credentials never reach the model. This runs before every
        # other step — including framework detection — because the whole point
        # is that no code path downstream of here has the opportunity to send a
        # secret anywhere. A prompt cannot be un-sent.
        raw_files, secrets = secrets_guard.scrub(raw_files)
        if secrets:
            logger.info(
                f"Withheld {len(secrets)} sensitive file(s) from the LLM context: "
                + ", ".join(f.path for f in secrets[:5])
            )
            warnings.append(secrets_guard.summarize(secrets))

        # Step 1: Filter out junk
        filtered = self._filter_files(raw_files)

        # Step 2: Score each file. The path-only heuristic is the prior; when a
        # trained importance model exists (services/importance_model.py), it
        # refines the score using the file's body too. With no model on disk,
        # learned_score returns the heuristic unchanged — so this is a no-op
        # until a model is trained.
        scored = [
            ScoredFile(
                path=path,
                content=content,
                size=len(content),
                importance=learned_score(path, content, fallback=score_file(path)),
            )
            for path, content in filtered.items()
        ]

        # Step 3: Sort by importance descending
        scored.sort(key=lambda f: f.importance, reverse=True)

        # Step 4: Fit within token budget
        selected: list[ScoredFile] = []
        total_tokens = 0
        truncated = False

        for f in scored:
            if total_tokens + f.token_estimate > self.token_budget:
                truncated = True
                # Instead of skipping entirely, summarize low-importance files
                if f.importance <= 5:
                    summary = self._summarize_file(f.path, f.content)
                    summary_file = ScoredFile(
                        path=f.path,
                        content=f"[SUMMARIZED]\n{summary}",
                        size=len(summary),
                        importance=f.importance,
                    )
                    if total_tokens + summary_file.token_estimate <= self.token_budget:
                        selected.append(summary_file)
                        total_tokens += summary_file.token_estimate
                continue
            selected.append(f)
            total_tokens += f.token_estimate

        if truncated:
            warnings.append(
                f"Repo is large — {original_count} files found, "
                f"{len(selected)} included in LLM context. "
                "Lower-importance files were summarized."
            )

        # Step 5: Framework detection — run on the RAW file set so backend
        # markers (manage.py, requirements.txt, Gemfile, …) are visible even
        # though the filter drops them from the LLM context.
        all_file_contents = {f.path: f.content for f in selected}
        framework = detect_framework(raw_files)
        is_monorepo, packages = detect_monorepo(all_file_contents)

        if is_monorepo:
            warnings.append(
                f"Monorepo detected with packages: {packages}. "
                "Showing files from all packages — consider filtering by sub-package."
            )

        return ExtractionResult(
            files={f.path: f.content for f in selected},
            framework=framework,
            monorepo=is_monorepo,
            monorepo_packages=packages,
            file_count=len(selected),
            total_tokens=total_tokens,
            skipped_count=original_count - len(selected),
            truncated=truncated,
            warnings=warnings,
            excluded_secrets=[f.as_dict() for f in secrets],
        )

    def _filter_files(self, raw_files: dict[str, str]) -> dict[str, str]:
        """Remove files that are definitely not useful for UI testing."""
        result = {}
        for path, content in raw_files.items():
            if self._should_skip(path):
                continue
            result[path] = content
        return result

    def _should_skip(self, path: str) -> bool:
        parts = path.split("/")

        # Skip if any path segment is a known junk directory
        for part in parts:
            if part.lower() in SKIP_DIRECTORIES:
                return True

        name = Path(path).name.lower()

        # Known config files are always wanted. This check must come BEFORE the
        # skip-patterns below, otherwise the `\.json$` data-file rule would drop
        # package.json / tsconfig.json and silently break framework detection.
        if name in CONFIG_FILES:
            return False

        # Skip by file pattern
        for pattern in SKIP_PATTERNS:
            if re.search(pattern, path, re.IGNORECASE):
                return True

        # Skip pure backend files (no UI extension and not a config)
        ext = Path(path).suffix.lower()

        # Match by filename suffix rather than Path.suffix so compound
        # extensions (.blade.php, .component.html) are recognised — Path.suffix
        # only returns the final segment (".php") and would miss them.
        is_ui_file = (
            any(name.endswith(ui_ext) for ui_ext in UI_EXTENSIONS)
            or name in CONFIG_FILES
        )
        # Allow .js and .ts files that look like router/page files
        if ext in {".js", ".ts"} and not is_ui_file:
            if any(
                kw in path.lower()
                for kw in ["router", "routes", "app.", "pages/", "views/", "screen"]
            ):
                is_ui_file = True

        if not is_ui_file:
            return True

        return False

    def _summarize_file(self, path: str, content: str) -> str:
        """
        Extract only the key testing-relevant parts of a file:
        - Element IDs and class names
        - Form fields
        - Button text
        - Route definitions
        - data-testid attributes
        """
        lines = []

        # Extract IDs
        ids = re.findall(r'id=["\']([^"\']+)["\']', content)
        if ids:
            lines.append(f"IDs: {', '.join(set(ids))}")

        # Extract class names (first 10 unique)
        classes = re.findall(r'class(?:Name)?=["\']([^"\']+)["\']', content)
        unique_classes = list(set(" ".join(classes).split()))[:10]
        if unique_classes:
            lines.append(f"Classes (sample): {', '.join(unique_classes)}")

        # Extract data-testid attributes
        testids = re.findall(r'data-testid=["\']([^"\']+)["\']', content)
        if testids:
            lines.append(f"data-testid: {', '.join(set(testids))}")

        # Extract button texts
        buttons = re.findall(r'<button[^>]*>([^<]+)</button>', content, re.IGNORECASE)
        if buttons:
            lines.append(f"Buttons: {', '.join(b.strip() for b in buttons[:5])}")

        # Extract input names
        inputs = re.findall(r'<input[^>]+name=["\']([^"\']+)["\']', content, re.IGNORECASE)
        if inputs:
            lines.append(f"Inputs: {', '.join(set(inputs))}")

        # Extract route paths (common patterns)
        routes = re.findall(r'path:["\s]+["\']([^"\']+)["\']', content)
        routes += re.findall(r'to=["\']([^"\']+)["\']', content)
        if routes:
            lines.append(f"Routes: {', '.join(set(routes[:10]))}")

        if not lines:
            # Nothing useful extracted; include first 50 lines
            first_lines = "\n".join(content.splitlines()[:50])
            return f"[First 50 lines of {path}]\n{first_lines}"

        return f"[Summary of {path}]\n" + "\n".join(lines)


# ─────────────────────────────────────────────
# ZIP file processing
# ─────────────────────────────────────────────

def extract_zip(zip_bytes: bytes) -> dict[str, str]:
    """
    Extract a ZIP file in memory and return {path: content} dict.
    Handles nested zip structures (repo downloaded from GitHub as .zip).
    """
    result = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()

        # GitHub ZIPs wrap everything in a top-level folder like "repo-main/".
        # Strip it — but NOT when that single top folder is a real project
        # directory (e.g. a zip containing only "src/..."), or we'd mangle paths.
        COMMON_ROOT_DIRS = {
            "src", "app", "lib", "source", "dist", "build", "public",
            "components", "pages", "views", "assets", "static", "tests", "test",
        }
        prefix = ""
        if names:
            first = names[0]
            if "/" in first:
                candidate_prefix = first.split("/")[0] + "/"
                top = candidate_prefix.rstrip("/").lower()
                if (
                    top not in COMMON_ROOT_DIRS
                    and all(n.startswith(candidate_prefix) or n == candidate_prefix for n in names)
                ):
                    prefix = candidate_prefix

        for name in names:
            if name.endswith("/"):
                continue  # Skip directories
            relative_path = name[len(prefix):]
            if not relative_path:
                continue
            # zip-slip guard: never accept absolute paths or ones that escape the
            # extraction root ("../"), even though we hold contents in a dict —
            # they later get written to disk when publishing to GitHub.
            if os.path.isabs(relative_path) or ".." in relative_path.replace("\\", "/").split("/"):
                logger.warning(f"Skipping unsafe path in ZIP (path traversal): {name}")
                continue
            try:
                content = zf.read(name).decode("utf-8", errors="replace")
                result[relative_path] = content
            except Exception as e:
                logger.warning(f"Failed to read {name} from ZIP: {e}")

    return result


# ─────────────────────────────────────────────
# Push preparation (for publishing a project to a fresh git repo)
# ─────────────────────────────────────────────

# Dependency / build / VCS junk that should never be committed. Unlike
# SKIP_DIRECTORIES (which also drops content dirs like "docs" for LLM
# filtering), this keeps everything a human would want in the repo.
PUSH_SKIP_DIRECTORIES = {
    "node_modules", ".git", "dist", "build", "__pycache__",
    ".pytest_cache", "coverage", ".nyc_output", ".next", ".nuxt",
    ".svelte-kit", "venv", ".venv", "vendor", ".terraform",
    ".idea", ".vscode", ".cache", ".parcel-cache", "target",
}

# Binary / non-source extensions we skip so blobs stay valid UTF-8 text
# (extract_zip decodes with errors="replace", which would corrupt binaries).
PUSH_SKIP_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp", ".tiff",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pdf", ".zip", ".gz", ".tar", ".rar", ".7z",
    ".mp4", ".mov", ".avi", ".mp3", ".wav",
    ".db", ".sqlite", ".sqlite3",
    ".pyc", ".pyo", ".class", ".o", ".so", ".dll", ".exe", ".bin",
}

# Skip individual files larger than this — GitHub blob pushes get slow/huge.
PUSH_MAX_FILE_BYTES = 1_000_000  # 1 MB


def filter_for_push(
    files: dict[str, str],
) -> tuple[dict[str, str], list[str], list[dict]]:
    """
    Filter a raw {path: content} map down to what belongs in a fresh repo.

    Drops credential files, dependency/build/VCS junk, binary assets, oversized
    files, and anything whose path can't be written safely.

    Returns (kept_files, warnings, excluded_secrets). The third value is
    separate from `warnings` because it is the one category the caller may want
    to render differently — a dropped node_modules is housekeeping; a withheld
    `.env` is something the user has to know about before they wonder why their
    deploy has no configuration.
    """
    kept: dict[str, str] = {}
    skipped_dirs = 0
    skipped_binary = 0
    skipped_large = 0
    unsafe_paths: list[str] = []

    # Credentials first, so a `.env` can never be reprieved by a later rule.
    files, secrets = secrets_guard.scrub(files)

    for path, content in files.items():
        parts = [p.lower() for p in path.replace("\\", "/").split("/")]
        if any(part in PUSH_SKIP_DIRECTORIES for part in parts):
            skipped_dirs += 1
            continue
        ext = Path(path).suffix.lower()
        if ext in PUSH_SKIP_EXTENSIONS:
            skipped_binary += 1
            continue
        if len(content.encode("utf-8", errors="replace")) > PUSH_MAX_FILE_BYTES:
            skipped_large += 1
            continue
        # An unwritable path (traversal, reserved device name, trailing dot)
        # would either escape the repo or make it un-clonable on Windows.
        # Sanitizing can rename, so check for a collision before accepting it.
        safe = safe_paths.sanitize(path)
        if safe is None:
            unsafe_paths.append(path)
            continue
        if safe != path and safe in kept:
            unsafe_paths.append(path)
            continue
        kept[safe] = content

    warnings: list[str] = []
    if secrets:
        logger.info(
            f"Withheld {len(secrets)} sensitive file(s) from the push: "
            + ", ".join(f.path for f in secrets[:5])
        )
        warnings.append(secrets_guard.summarize(secrets))
    if skipped_dirs:
        warnings.append(f"Skipped {skipped_dirs} build/dependency file(s) (node_modules, dist, etc.)")
    if skipped_binary:
        warnings.append(f"Skipped {skipped_binary} binary asset(s)")
    if skipped_large:
        warnings.append(f"Skipped {skipped_large} file(s) larger than 1 MB")
    if unsafe_paths:
        listed = ", ".join(unsafe_paths[:3])
        warnings.append(
            f"Skipped {len(unsafe_paths)} file(s) whose path can't be written to a "
            f"git repo ({listed}{'...' if len(unsafe_paths) > 3 else ''})"
        )
    return kept, warnings, [f.as_dict() for f in secrets]
