"""
main.py — Testra FastAPI server

Endpoints:
  POST /api/analyze     — Phase 1: extract files + analyze project
  POST /api/generate    — Phase 2: generate test scripts
  GET  /api/stream      — SSE: stream generation in real-time
  GET  /api/status      — LLM provider health dashboard
  GET  /api/logs        — Recent API call log
  POST /api/upload-zip  — ZIP file upload + analyze
  POST /api/publish-zip — Push a project ZIP to a new GitHub repo (optional CI/CD)
  POST /api/scan        — Passive security audit of a deployed URL
  GET  /health          — Basic health check
"""

import io
import json
import time
import uuid
import secrets
import asyncio
import logging
import zipfile
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError

from config import settings
from logging_config import configure_logging, request_id_var
from ratelimit import rate_limit, analyze_limiter, generate_limiter
from agents.filter_agent import FilterAgent
from agents.writer_agent import WriterAgent
from models.schemas import (
    GenerateRequest, GenerateResponse, GeneratedFile,
    ProjectAnalysis, FilePreview, StatusResponse, ProviderStatus,
    PublishResponse, ScanRequest, ScanResponse,
    SignupRequest, LoginRequest, AuthResponse,
)
from services.file_extractor import extract_zip, FileExtractor, filter_for_push
from services.github_service import GitHubService, parse_github_url, RepoNotFoundError, RepoAccessError
from services.git_publisher import GitPublisher, GitPublishError
from services.security_scanner import SecurityScanner, ScanError
from services import github_oauth
from services import auth as auth_svc
from services.auth import require_user
from services.llm_router import router as llm_router
from services.store import store

configure_logging(settings.log_level)
logger = logging.getLogger(__name__)

_START_TIME = time.time()

app = FastAPI(
    title="Testra",
    description="Paste a GitHub URL, get production-ready E2E tests in seconds.",
    version="1.0.0",
)

_cors_origins = settings.cors_origin_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    # The CORS spec forbids credentials together with the "*" wildcard, so only
    # enable credentials when explicit origins are configured (CORS_ORIGINS env).
    allow_credentials=_cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """Attach a correlation ID to every request (logs + X-Request-ID header)."""
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    token = request_id_var.set(rid)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-ID"] = rid
    return response

# ─────────────────────────────────────────────
# Durable job store (SQLite) — jobs survive restarts and multiple workers.
# See services/store.py.
# ─────────────────────────────────────────────


# ─────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/metrics")
async def metrics():
    """Lightweight operational metrics for dashboards / uptime checks."""
    provider_status = llm_router.get_status()
    return {
        "app_env": settings.app_env,
        "uptime_seconds": round(time.time() - _START_TIME, 1),
        "jobs_stored": store.count(),
        "total_llm_calls": sum(p["total_calls"] for p in provider_status.values()),
        "providers": provider_status,
    }


# ─────────────────────────────────────────────
# Provider status
# ─────────────────────────────────────────────

@app.get("/api/status", response_model=StatusResponse)
async def get_status(ctx: dict = Depends(require_user)):
    status = llm_router.get_status()
    providers = [
        ProviderStatus(
            name=name,
            total_keys=info["total_keys"],
            available_keys=info["available_keys"],
            total_calls=info["total_calls"],
            healthy=info["healthy"],
        )
        for name, info in status.items()
    ]
    return StatusResponse(
        providers=providers,
        call_log=llm_router.get_call_log(limit=20),
    )


# ─────────────────────────────────────────────
# Phase 1: Analyze repo (GitHub URL)
# ─────────────────────────────────────────────

@app.post("/api/analyze", dependencies=[Depends(rate_limit(analyze_limiter))])
async def analyze_repo(payload: GenerateRequest, ctx: dict = Depends(require_user)):
    """
    Phase 1: Fetch the repo, extract UI files, run Filter Agent.
    Returns a project analysis + file tree preview.
    The client shows this to the user before generating tests.
    """
    if not payload.repo_url:
        raise HTTPException(400, "repo_url is required for this endpoint")

    try:
        repo_info = parse_github_url(payload.repo_url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    gh = GitHubService(token=payload.github_token)

    try:
        tree = await gh.get_file_tree(repo_info)
    except RepoNotFoundError as e:
        raise HTTPException(404, str(e))
    except RepoAccessError as e:
        raise HTTPException(403, str(e))

    # Fetch all files concurrently
    paths = [item["path"] for item in tree]
    logger.info(f"Fetching {len(paths)} files from {payload.repo_url}")
    raw_files_list = await gh.fetch_files(repo_info, paths, concurrency=15)
    raw_files = {f.path: f.content for f in raw_files_list}

    # Run Filter Agent
    agent = FilterAgent()
    result = await agent.run(raw_files, user_description=payload.test_flows)

    # Build response
    previews = [
        FilePreview(
            path=path,
            size=len(content),
            importance=5,   # simplified; extractor has the real score
            preview=content[:200],
        )
        for path, content in list(result.files.items())[:50]  # max 50 previews
    ]

    # Store session for Phase 2
    import uuid
    job_id = str(uuid.uuid4())
    store.create(job_id, {
        "user_id": ctx["user_id"],
        "files": result.files,
        "filter_result": result,
        "request": payload.model_dump(),
    })

    return {
        "job_id": job_id,
        "analysis": ProjectAnalysis(
            project_summary=result.project_summary,
            framework=result.framework,
            key_pages=result.key_pages,
            key_components=result.key_components,
            routes=result.routes,
            testing_challenges=result.testing_challenges,
            file_count=result.file_count,
            total_tokens=result.total_tokens,
            file_previews=previews,
        ).model_dump(),
    }


# ─────────────────────────────────────────────
# Phase 1b: Analyze ZIP upload
# ─────────────────────────────────────────────

@app.post("/api/upload-zip", dependencies=[Depends(rate_limit(analyze_limiter))])
async def upload_zip(
    file: UploadFile = File(...),
    framework: str = Form("playwright"),
    language: str = Form("typescript"),
    test_flows: str = Form(""),
    base_url: str = Form("http://localhost:3000"),
    ctx: dict = Depends(require_user),
):
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(400, "Only .zip files are supported")

    content = await file.read()
    if len(content) > 100 * 1024 * 1024:  # 100 MB limit
        raise HTTPException(413, "ZIP file too large (max 100 MB)")

    try:
        raw_files = extract_zip(content)
    except zipfile.BadZipFile:
        raise HTTPException(400, "Invalid ZIP file")

    logger.info(f"ZIP upload: {len(raw_files)} files extracted")

    agent = FilterAgent()
    result = await agent.run(raw_files, user_description=test_flows)

    import uuid
    job_id = str(uuid.uuid4())
    store.create(job_id, {
        "user_id": ctx["user_id"],
        "files": result.files,
        "filter_result": result,
        "request": {
            "framework": framework,
            "language": language,
            "test_flows": test_flows,
            "base_url": base_url,
        },
    })

    previews = [
        FilePreview(
            path=path,
            size=len(content),
            importance=5,
            preview=content[:200],
        )
        for path, content in list(result.files.items())[:50]
    ]

    return {
        "job_id": job_id,
        "analysis": ProjectAnalysis(
            project_summary=result.project_summary,
            framework=result.framework,
            key_pages=result.key_pages,
            key_components=result.key_components,
            routes=result.routes,
            testing_challenges=result.testing_challenges,
            file_count=result.file_count,
            total_tokens=result.total_tokens,
            file_previews=previews,
        ).model_dump(),
    }


# ─────────────────────────────────────────────
# GitHub OAuth: "Login with GitHub" so we can push on the user's behalf
# ─────────────────────────────────────────────

_OAUTH_STATE_TTL = 600          # 10 min to complete the login round-trip
_STATE_PREFIX = "oauthstate:"   # namespaces one-time states inside the session table


@app.get("/api/auth/config")
async def auth_config():
    """Tells the frontend whether GitHub login is available."""
    return {"github_oauth_enabled": settings.github_oauth_enabled}


@app.post("/api/auth/signup", response_model=AuthResponse,
          dependencies=[Depends(rate_limit(analyze_limiter))])
async def signup(payload: SignupRequest):
    """Register a new email/password account and return a session token."""
    email = auth_svc.validate_email(payload.email)
    auth_svc.validate_password(payload.password)
    if store.get_user_by_email(email):
        raise HTTPException(409, "An account with this email already exists. Try logging in.")

    user = {
        "id": auth_svc.new_user_id(),
        "email": email,
        "password_hash": auth_svc.hash_password(payload.password),
        "name": (payload.name or "").strip() or email.split("@")[0],
        "created_at": time.time(),
    }
    store.create_user(user)
    token = auth_svc.create_login_session(user["id"])
    logger.info(f"New signup: {email}")
    return AuthResponse(token=token, user=auth_svc.public_user(user))


@app.post("/api/auth/login", response_model=AuthResponse,
          dependencies=[Depends(rate_limit(analyze_limiter))])
async def login(payload: LoginRequest):
    """Log in with email + password and return a session token."""
    email = (payload.email or "").strip().lower()
    user = store.get_user_by_email(email)
    if not user or not auth_svc.verify_password(payload.password, user.get("password_hash")):
        # Same message for both cases — don't reveal whether the email exists.
        raise HTTPException(401, "Incorrect email or password.")
    token = auth_svc.create_login_session(user["id"])
    return AuthResponse(token=token, user=auth_svc.public_user(user))


@app.get("/api/auth/github/login")
async def github_login():
    """Start the OAuth flow — redirect the browser to GitHub's consent screen."""
    if not settings.github_oauth_enabled:
        raise HTTPException(503, "GitHub login is not configured on this server.")
    state = secrets.token_urlsafe(24)
    store.create_session(_STATE_PREFIX + state, {"kind": "oauth_state"}, _OAUTH_STATE_TTL)
    url = github_oauth.authorize_url(
        client_id=settings.github_client_id,
        redirect_uri=settings.oauth_callback_url,
        state=state,
    )
    return RedirectResponse(url, status_code=307)


@app.get("/api/auth/github/callback")
async def github_callback(code: str = "", state: str = "", error: str = ""):
    """GitHub redirects here after the user approves (or denies)."""
    frontend = settings.frontend_url.rstrip("/")

    def _fail(reason: str):
        return RedirectResponse(f"{frontend}/?login_error={reason}", status_code=307)

    if error or not code:
        return _fail(error or "access_denied")
    # One-time state check (CSRF protection).
    if not state or store.get_session(_STATE_PREFIX + state) is None:
        return _fail("invalid_state")
    store.delete_session(_STATE_PREFIX + state)

    try:
        token = await github_oauth.exchange_code(
            code=code,
            client_id=settings.github_client_id,
            client_secret=settings.github_client_secret,
            redirect_uri=settings.oauth_callback_url,
        )
        gh = await github_oauth.get_user(token)
    except github_oauth.OAuthError as e:
        logger.warning(f"OAuth callback failed: {e}")
        return _fail("exchange_failed")

    # Find or create the user account, linking by github_id first, then email.
    github_id = str(gh.get("id") or gh["login"])
    account = store.get_user_by_github(github_id)
    if not account and gh.get("email"):
        account = store.get_user_by_email(gh["email"])
    if account:
        store.update_user(
            account["id"], github_id=github_id, github_login=gh["login"],
            avatar_url=gh.get("avatar_url") or account.get("avatar_url"),
        )
        user_id = account["id"]
    else:
        user_id = auth_svc.new_user_id()
        store.create_user({
            "id": user_id, "email": gh.get("email"),
            "name": gh.get("name") or gh["login"],
            "github_id": github_id, "github_login": gh["login"],
            "avatar_url": gh.get("avatar_url"), "created_at": time.time(),
        })
        logger.info(f"New GitHub signup: {gh['login']}")

    # Session carries the GitHub access token so Publish can push on their behalf.
    session_id = auth_svc.create_login_session(user_id, github_token=token)
    return RedirectResponse(f"{frontend}/auth/callback?token={session_id}", status_code=307)


@app.get("/api/auth/me")
async def auth_me(ctx: dict = Depends(require_user)):
    """Return the currently logged-in user (401 if the token is missing/invalid)."""
    return {"authenticated": True, "user": ctx["user"]}


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = request.headers.get("Authorization", "")
    token = token[7:].strip() if token.lower().startswith("bearer ") else request.query_params.get("token", "")
    if token:
        store.delete_session(token)
    return {"ok": True}


# ─────────────────────────────────────────────
# Publish: push a project ZIP to a fresh GitHub repo (optionally with CI/CD)
# ─────────────────────────────────────────────

_DEFAULT_GITIGNORE = """\
# Dependencies
node_modules/
.pnp/
.venv/
venv/

# Build output
dist/
build/
.next/
.nuxt/
coverage/

# Environment
.env
.env.local
*.local

# Test artifacts
test-results/
playwright-report/
.pytest_cache/

# OS / IDE
.DS_Store
.idea/
.vscode/
"""


@app.post("/api/publish-zip", response_model=PublishResponse,
          dependencies=[Depends(rate_limit(analyze_limiter))])
async def publish_zip(
    file: UploadFile = File(...),
    github_token: str = Form(""),      # PAT fallback when GitHub OAuth isn't configured
    repo_name: str = Form(...),
    add_cicd: bool = Form(False),
    private: bool = Form(True),
    framework: str = Form("playwright"),
    language: str = Form("typescript"),
    test_flows: str = Form(""),
    base_url: str = Form("http://localhost:3000"),
    repo_description: str = Form(""),
    ctx: dict = Depends(require_user),
):
    """
    Extract a project ZIP and push it to a brand-new GitHub repo.

    Auth: the logged-in user's session. Pushing needs a GitHub token — it comes
    from the user's GitHub login (Continue with GitHub), or a PAT fallback when
    OAuth isn't configured on the server.

    If add_cicd is true, an E2E test suite + GitHub Actions workflow are
    generated and validated locally (WriterAgent self-heal loop) BEFORE the
    push — so what lands in git is already green, and we push exactly once.
    """
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(400, "Only .zip files are supported")
    if not repo_name.strip():
        raise HTTPException(400, "A repository name is required")

    # GitHub token: the logged-in user's linked GitHub token, else a PAT fallback.
    token = ctx["session"].get("github_token") or github_token.strip()
    if not token:
        raise HTTPException(
            403, "Connect your GitHub account to publish (use 'Continue with GitHub')."
        )

    content = await file.read()
    if len(content) > 100 * 1024 * 1024:  # 100 MB limit
        raise HTTPException(413, "ZIP file too large (max 100 MB)")

    try:
        raw_files = extract_zip(content)
    except zipfile.BadZipFile:
        raise HTTPException(400, "Invalid ZIP file")
    if not raw_files:
        raise HTTPException(400, "The ZIP archive contained no files")

    # Keep only what belongs in a repo (drop node_modules, binaries, huge files).
    push_files, warnings = filter_for_push(raw_files)
    if not push_files:
        raise HTTPException(400, "Nothing to push after filtering build/dependency files")

    cicd_added = False
    test_count = 0
    validation: list[dict] = []
    all_valid = True

    if add_cicd:
        # Generate a validated E2E test suite + CI workflow and fold it into the
        # push. WriterAgent's self-heal loop is the "validate locally until green"
        # step — it re-prompts the LLM to fix any file that fails static validation.
        filter_agent = FilterAgent()
        filter_result = await filter_agent.run(raw_files, user_description=test_flows)

        writer = WriterAgent()
        try:
            result = await writer.run(
                filter_result=filter_result,
                framework=framework,
                language=language,
                test_flows=test_flows,
                base_url=base_url,
                include_ci=True,
                self_heal=True,
                max_heal_attempts=4,
            )
        except Exception as e:
            logger.error(f"CI generation failed: {e}")
            raise HTTPException(500, f"CI/CD generation failed: {e}")

        test_count = result.test_count
        validation = result.validation
        all_valid = all(v.get("ok", False) for v in validation)
        if not all_valid:
            bad = sum(1 for v in validation if not v.get("ok", False))
            warnings.append(
                f"{bad} generated test file(s) still failed validation after auto-fix "
                "attempts — pushed anyway; review before relying on CI."
            )

        for gf in result.files:
            # Don't clobber the project's own README with the test-suite README.
            path = ("TESTING.md" if gf.filename == "README.md" and "README.md" in push_files
                    else gf.filename)
            push_files[path] = gf.content
        cicd_added = True

    # Ensure a .gitignore exists so the pushed repo stays clean.
    if ".gitignore" not in push_files:
        push_files[".gitignore"] = _DEFAULT_GITIGNORE

    publisher = GitPublisher(token=token)
    try:
        pub = await publisher.publish(
            repo_name=repo_name.strip(),
            files=push_files,
            private=private,
            description=repo_description,
            commit_message=("Add project + E2E tests & CI (via Testra)"
                            if cicd_added else "Initial commit (via Testra)"),
        )
    except GitPublishError as e:
        raise HTTPException(400, str(e))

    logger.info(f"Published {pub.files_pushed} files to {pub.full_name} (cicd={cicd_added})")

    return PublishResponse(
        success=True,
        repo_url=pub.repo_url,
        full_name=pub.full_name,
        branch=pub.branch,
        commit_sha=pub.commit_sha,
        files_pushed=pub.files_pushed,
        cicd_added=cicd_added,
        test_count=test_count,
        all_valid=all_valid,
        validation=validation,
        warnings=warnings + pub.warnings,
    )


# ─────────────────────────────────────────────
# Security scan: audit a deployed URL for production vulnerabilities
# ─────────────────────────────────────────────

async def _ai_scan_summary(result) -> str:
    """Ask the LLM for a short, prioritized action plan. Best-effort."""
    top = result.findings[:15]
    lines = [f"- [{f['severity'].upper()}] {f['title']}" for f in top]
    prompt = (
        f"A passive security scan of {result.final_url} scored {result.score}/100 "
        f"(grade {result.grade}). Findings:\n" + "\n".join(lines) +
        "\n\nWrite a concise (max 120 words) executive summary for the developer: the "
        "overall risk level, the 2-3 things to fix FIRST and why, and a one-line "
        "reassurance about what's already fine. Plain text, no markdown headers."
    )
    try:
        return (await llm_router.complete(
            prompt=prompt,
            system_prompt="You are a pragmatic application security engineer.",
            temperature=0.3,
            context_hint="security_scan",
        )).strip()
    except Exception as e:
        logger.warning(f"AI scan summary failed: {e}")
        return ""


def _fallback_summary(result) -> str:
    c = result.counts
    high = c.get("critical", 0) + c.get("high", 0)
    if high:
        return (f"Found {high} high-severity issue(s) that should be fixed before "
                f"relying on this in production. Overall grade: {result.grade}.")
    if sum(c.values()):
        return (f"No critical issues — mostly hardening improvements. "
                f"Overall grade: {result.grade}.")
    return "No issues detected by the passive checks. Nice work."


@app.post("/api/scan", response_model=ScanResponse,
          dependencies=[Depends(rate_limit(analyze_limiter))])
async def scan_url(payload: ScanRequest, ctx: dict = Depends(require_user)):
    """
    Passively audit a deployed URL for common production security issues and
    return prioritized findings with concrete fixes. Non-intrusive: it inspects
    headers/TLS/cookies and checks for accidentally-exposed files — no attacks.
    """
    scanner = SecurityScanner()
    try:
        result = await scanner.scan(payload.url)
    except ScanError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"Scan failed: {e}")
        raise HTTPException(500, f"Scan failed: {e}")

    summary = ""
    if payload.ai_summary and result.findings:
        summary = await _ai_scan_summary(result)
    if not summary:
        summary = _fallback_summary(result)

    logger.info(f"Scanned {result.final_url}: grade {result.grade}, "
                f"{len(result.findings)} findings")

    return ScanResponse(
        url=result.url,
        final_url=result.final_url,
        score=result.score,
        grade=result.grade,
        summary=summary,
        counts=result.counts,
        checks_run=result.checks_run,
        findings=result.findings,
    )


# ─────────────────────────────────────────────
# Phase 2: Generate tests (from session)
# ─────────────────────────────────────────────

@app.post("/api/generate/{job_id}", response_model=GenerateResponse,
          dependencies=[Depends(rate_limit(generate_limiter))])
async def generate_tests(job_id: str, payload: GenerateRequest,
                         ctx: dict = Depends(require_user)):
    """
    Phase 2: Given a job_id from Phase 1, generate the test scripts.
    """
    session = store.get(job_id)
    if not session:
        raise HTTPException(404, "Job not found. Please re-analyze the repo first.")
    if session.get("user_id") != ctx["user_id"]:
        raise HTTPException(403, "This job belongs to another account.")

    filter_result = session["filter_result"]
    agent = WriterAgent()

    try:
        result = await agent.run(
            filter_result=filter_result,
            framework=payload.framework.value,
            language=payload.language.value,
            test_flows=payload.test_flows or session["request"].get("test_flows", ""),
            base_url=payload.base_url,
            include_ci=payload.include_ci,
            pregenerated_raw=session.get("streamed_raw"),
            self_heal=payload.self_heal,
        )
    except Exception as e:
        logger.error(f"Test generation failed: {e}")
        raise HTTPException(500, f"Generation failed: {e}")

    return GenerateResponse(
        success=True,
        files=[
            GeneratedFile(
                filename=f.filename,
                content=f.content,
                description=f.description,
                size=len(f.content),
            )
            for f in result.files
        ],
        test_count=result.test_count,
        framework=result.framework,
        selector_warnings=result.selector_warnings,
        summary=result.summary,
        validation=result.validation,
    )


# ─────────────────────────────────────────────
# SSE: Streaming generation
# ─────────────────────────────────────────────

@app.get("/api/stream/{job_id}", dependencies=[Depends(rate_limit(generate_limiter))])
async def stream_generation(
    job_id: str,
    framework: str = "playwright",
    language: str = "typescript",
    test_flows: str = "",
    base_url: str = "http://localhost:3000",
    ctx: dict = Depends(require_user),
):
    """
    Server-Sent Events endpoint for real-time streaming output.
    Frontend connects here to show tokens as they're generated.
    (Auth token is passed as ?token= since EventSource can't set headers.)
    """
    session = store.get(job_id)
    if not session:
        raise HTTPException(404, "Job not found")
    if session.get("user_id") != ctx["user_id"]:
        raise HTTPException(403, "This job belongs to another account.")

    filter_result = session["filter_result"]
    agent = WriterAgent()

    async def event_generator():
        # Send initial status
        yield f"data: {json.dumps({'type': 'status', 'message': 'Starting generation...'})}\n\n"
        await asyncio.sleep(0.1)

        # Subscribe to LLM provider status updates
        status_queue = asyncio.Queue()
        async def on_status(msg):
            await status_queue.put(msg)
        llm_router.subscribe_status(on_status)

        try:
            buffer = ""
            async for chunk in agent.stream_run(
                filter_result=filter_result,
                framework=framework,
                language=language,
                test_flows=test_flows or session["request"].get("test_flows", ""),
                base_url=base_url,
            ):
                buffer += chunk
                # Send status updates if any are queued (non-blocking)
                while not status_queue.empty():
                    status_msg = status_queue.get_nowait()
                    yield f"data: {json.dumps({'type': 'provider_status', 'message': status_msg})}\n\n"

                yield f"data: {json.dumps({'type': 'chunk', 'content': chunk})}\n\n"

            # Cache the streamed output so /api/generate can reuse it instead of
            # running the whole (expensive) generation a second time.
            store.update(job_id, streamed_raw=buffer)

            yield f"data: {json.dumps({'type': 'done', 'message': 'Generation complete'})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            llm_router.unsubscribe_status(on_status)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",       # Important for Nginx
        },
    )


# ─────────────────────────────────────────────
# API call logs
# ─────────────────────────────────────────────

@app.get("/api/logs")
async def get_logs(limit: int = 50):
    return {"logs": llm_router.get_call_log(limit=limit)}
