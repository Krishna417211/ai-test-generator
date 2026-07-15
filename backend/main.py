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

import os
import json
import time
import uuid
import secrets
import asyncio
import logging
import zipfile
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, RedirectResponse

from config import settings
from logging_config import configure_logging, request_id_var
from ratelimit import rate_limit, analyze_limiter, generate_limiter
from agents.filter_agent import FilterAgent, NoTestableUIError
from agents.writer_agent import WriterAgent
from models.schemas import (
    GenerateRequest, GenerateResponse, GeneratedFile,
    ProjectAnalysis, FilePreview, StatusResponse, ProviderStatus,
    PublishResponse, ScanRequest, ScanResponse,
    SignupRequest, LoginRequest, AuthResponse,
)
from services.file_extractor import extract_zip, filter_for_push
from services.github_service import GitHubService, parse_github_url, RepoNotFoundError, RepoAccessError
from services.git_publisher import GitPublisher, GitPublishError
from services.security_scanner import SecurityScanner, ScanError
from services import github_oauth
from services import auth as auth_svc
from services import billing
from services.auth import require_user
from services.quota import require_quota, get_quota
from services.llm_router import router as llm_router, AllProvidersExhausted
from services.store import store

configure_logging(settings.log_level)
logger = logging.getLogger(__name__)

_START_TIME = time.time()

# Shown when every LLM key is dry. Deliberately not an upgrade prompt: this is
# our capacity failing, it affects Pro accounts exactly the same, and paying
# would not fix it. See services/quota.py.
PROVIDER_OUTAGE_MESSAGE = (
    "Test generation is temporarily unavailable — we're out of AI capacity "
    "right now. This isn't something upgrading would fix; please try again "
    "shortly. Your quota was not charged."
)

# How often expired jobs/sessions are swept. Their TTLs would otherwise only be
# applied at startup, so a long-running server never reclaims the space.
SWEEP_INTERVAL_SECONDS = int(os.getenv("TESTGEN_SWEEP_INTERVAL", str(60 * 60)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    async def sweeper():
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            try:
                # store is blocking SQLite — keep it off the event loop.
                freed = await asyncio.to_thread(store.sweep)
                if freed["jobs"] or freed["sessions"]:
                    logger.info(
                        f"Swept {freed['jobs']} expired job(s), "
                        f"{freed['sessions']} expired session(s)"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Store sweep failed: {e}")

    task = asyncio.create_task(sweeper())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(
    lifespan=lifespan,
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
    try:
        result = await agent.run(raw_files, user_description=payload.test_flows)
    except NoTestableUIError as e:
        raise HTTPException(422, str(e))

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

MAX_UPLOAD_BYTES = settings.max_repo_size_mb * 1024 * 1024

_TOO_LARGE_HINT = (
    "Exclude dependency folders (venv/, node_modules/) from the archive — "
    "they are stripped on push anyway."
)


async def read_zip_upload(file: UploadFile) -> bytes:
    """Validate a .zip upload and return its bytes.

    The multipart parser records the size on UploadFile, so an oversized
    archive is rejected before read() pulls it into memory.
    """
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(400, "Only .zip files are supported")

    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413,
            f"ZIP file too large ({file.size / 1024 / 1024:.0f} MB) — "
            f"max {settings.max_repo_size_mb} MB. {_TOO_LARGE_HINT}",
        )

    content = await file.read()
    # Fallback for parsers that leave .size unset.
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413,
            f"ZIP file too large ({len(content) / 1024 / 1024:.0f} MB) — "
            f"max {settings.max_repo_size_mb} MB. {_TOO_LARGE_HINT}",
        )
    return content


@app.post("/api/upload-zip", dependencies=[Depends(rate_limit(analyze_limiter))])
async def upload_zip(
    file: UploadFile = File(...),
    framework: str = Form("playwright"),
    language: str = Form("typescript"),
    test_flows: str = Form(""),
    base_url: str = Form("http://localhost:3000"),
    ctx: dict = Depends(require_user),
):
    content = await read_zip_upload(file)

    try:
        raw_files = extract_zip(content)
    except zipfile.BadZipFile:
        raise HTTPException(400, "Invalid ZIP file")

    logger.info(f"ZIP upload: {len(raw_files)} files extracted")

    agent = FilterAgent()
    try:
        result = await agent.run(raw_files, user_description=test_flows)
    except NoTestableUIError as e:
        raise HTTPException(422, str(e))

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
    """Tells the frontend which social logins are available, plus the upload
    cap so it can reject oversized archives without sending them."""
    return {
        "github_oauth_enabled": settings.github_oauth_enabled,
        "max_upload_mb": settings.max_repo_size_mb,
    }


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
        # Send failures to /auth/callback (not /) — that's the only page that
        # reads ?login_error= and shows it to the user.
        return RedirectResponse(f"{frontend}/auth/callback?login_error={reason}", status_code=307)

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


@app.get("/api/profile")
async def profile(ctx: dict = Depends(require_user)):
    """Everything the profile page renders, in one call: identity, plan and
    quota, lifetime totals, and recent work.

    Every read is keyed by the session's own user_id — never a client-supplied
    id — so one account can't enumerate another's history.
    """
    user_id = ctx["user_id"]
    row = store.get_user_by_id(user_id) or {}
    quota = get_quota(user_id).as_dict()

    # get_quota() already lapses an expired plan to free; only report a renewal
    # date when the plan it belongs to is actually still live.
    expires_at = row.get("plan_expires_at") if quota["plan"] != "free" else None

    return {
        "user": ctx["user"],
        "member_since": row.get("created_at"),
        "plan": {
            **quota,
            "expires_at": expires_at,
            # False here is why the upgrade CTA must render an honest
            # "not available yet" instead of a button that 503s on click.
            "checkout_available": settings.billing_enabled,
            "catalogue": billing.plans(),
        },
        "totals": store.activity_totals(user_id),
        "generations": store.list_generations(user_id, limit=10),
        "scans": store.list_scans(user_id, limit=10),
        "repos": store.list_published_repos(user_id),
    }


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = request.headers.get("Authorization", "")
    token = token[7:].strip() if token.lower().startswith("bearer ") else request.query_params.get("token", "")
    if token:
        store.delete_session(token)
    return {"ok": True}


# ─────────────────────────────────────────────
# Billing: plan catalogue, usage, checkout, entitlement webhook
# ─────────────────────────────────────────────

@app.get("/api/billing/me")
async def billing_me(ctx: dict = Depends(require_user)):
    """This user's plan, usage, and the plan catalogue — everything the
    upgrade modal and the usage meter need, in one call."""
    return {
        "quota": get_quota(ctx["user_id"]).as_dict(),
        "plans": billing.plans(),
        "checkout_available": settings.billing_enabled,
        "contact_email": settings.billing_contact_email or None,
    }


@app.post("/api/billing/checkout")
async def billing_checkout(plan: str = Form(...), ctx: dict = Depends(require_user)):
    """Hand back the hosted checkout URL for a plan.

    Returns 503 (not a broken link) while no payment provider is configured —
    the client renders a "not available yet" state from this.
    """
    try:
        url = billing.checkout_url(plan, ctx["user_id"])
    except billing.BillingNotConfigured as e:
        raise HTTPException(503, str(e))
    return {"checkout_url": url}


@app.post("/api/billing/webhook")
async def billing_webhook(request: Request):
    """Grant Pro after a provider confirms payment.

    This is the ONLY path that may upgrade an account: it's the only one that
    can prove money moved. Signature is checked over the raw bytes before the
    body is trusted at all.
    """
    raw = await request.body()
    signature = (
        request.headers.get("X-Razorpay-Signature")
        or request.headers.get("Stripe-Signature")
        or ""
    )
    if not billing.verify_webhook(raw, signature):
        logger.warning("Rejected a billing webhook with a bad/missing signature")
        raise HTTPException(400, "Invalid signature")

    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "Malformed webhook body")

    # A good signature only proves Razorpay sent this — payment.failed and
    # subscription.cancelled are signed too. The event name decides.
    if not billing.is_granting_event(event):
        logger.info(f"Ignoring non-granting billing event: {event.get('event')}")
        return {"ok": True, "granted": False}

    user_id, plan_id = billing.extract_grant(event)
    if not user_id or not plan_id:
        raise HTTPException(400, "Webhook is missing client_reference_id or plan_id")
    if not store.get_user_by_id(user_id):
        logger.warning(f"Billing webhook for unknown user {user_id}")
        raise HTTPException(404, "Unknown user")

    store.set_plan(user_id, "pro", time.time() + billing.grant_seconds(plan_id))
    logger.info(f"Granted Pro ({plan_id}) to {user_id} via billing webhook")
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
    if not repo_name.strip():
        raise HTTPException(400, "A repository name is required")

    # GitHub token: the logged-in user's linked GitHub token, else a PAT fallback.
    token = ctx["session"].get("github_token") or github_token.strip()
    if not token:
        raise HTTPException(
            403, "Connect your GitHub account to publish (use 'Continue with GitHub')."
        )

    content = await read_zip_upload(file)

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

    # None means "don't attempt a suite" — either the user didn't ask for CI, or
    # there's no UI to drive. Pushing the project itself never depends on this.
    filter_result = None
    if add_cicd:
        # Generate a validated E2E test suite + CI workflow and fold it into the
        # push. WriterAgent's self-heal loop is the "validate locally until green"
        # step — it re-prompts the LLM to fix any file that fails static validation.
        filter_agent = FilterAgent()
        try:
            filter_result = await filter_agent.run(raw_files, user_description=test_flows)
        except NoTestableUIError as e:
            # Same reasoning as the provider-outage branch below: land the code
            # and say what's missing. The CI workflow is dropped with it — it
            # runs `npx playwright test`, which goes red on a repo with no specs.
            logger.info(f"Skipping CI generation — nothing to test: {e}")
            warnings.append(
                f"{e} Your project was pushed without an E2E suite or CI/CD pipeline."
            )

    if filter_result is not None:
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
            # Writing tests needs an LLM; pushing the project doesn't. A provider
            # outage shouldn't cost the user their push — land the code and say
            # what's missing. The CI workflow is deliberately dropped too: it runs
            # `npx playwright test`, which fails a repo that has no specs, and a
            # red pipeline is worse than none.
            logger.warning(f"CI generation unavailable — pushing without it: {e}")
            warnings.append(
                f"Test generation is unavailable right now ({str(e)[:160]}). Your "
                "project was pushed without tests or the CI/CD pipeline — "
                "re-publish to add them once capacity is back."
            )
        else:
            test_count = result.test_count
            validation = result.validation
            all_valid = all(v.get("ok", False) for v in validation)
            if result.failed_files:
                # Partial suite: some files never generated (quota ran out mid-run).
                # The writer already withheld the CI workflow, so don't claim CI
                # was added — say what's missing instead of shipping a red pipeline.
                warnings.append(
                    f"{len(result.failed_files)} of "
                    f"{len(result.files) + len(result.failed_files)} test file(s) "
                    f"couldn't be generated ({', '.join(result.failed_files[:3])}"
                    f"{'...' if len(result.failed_files) > 3 else ''}) — the CI/CD "
                    "pipeline was left out because an incomplete suite can't pass. "
                    "Re-publish once capacity is back for the full suite."
                )
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
            # Only true when the suite is whole and the CI workflow actually shipped.
            cicd_added = not result.failed_files

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

    # Remember what we created — /api/repo deletion is limited to these.
    store.record_published_repo(pub.full_name, ctx["user_id"], pub.repo_url)

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


@app.get("/api/repos")
async def list_published_repos(ctx: dict = Depends(require_user)):
    """Repos this user created through Testra — the only ones we offer to delete."""
    return {"repos": store.list_published_repos(ctx["user_id"])}


@app.delete("/api/repo/{owner}/{repo}", dependencies=[Depends(rate_limit(analyze_limiter))])
async def delete_published_repo(
    owner: str,
    repo: str,
    confirm: str = "",
    ctx: dict = Depends(require_user),
):
    """Delete a repo Testra created for this user. Irreversible.

    Guards, in order: the repo must be in this user's published_repos (so a
    stolen token can't reach unrelated repos), and `confirm` must echo the
    full name back.
    """
    full_name = f"{owner}/{repo}"

    if not store.was_published_by(full_name, ctx["user_id"]):
        # 404 rather than 403: don't confirm the repo exists to a non-owner.
        raise HTTPException(
            404, f"{full_name} was not published through Testra by this account."
        )
    if confirm != full_name:
        raise HTTPException(
            400, f"Confirmation mismatch — pass confirm={full_name} to delete it."
        )

    token = ctx["session"].get("github_token")
    if not token:
        raise HTTPException(403, "Connect your GitHub account to delete a repository.")

    try:
        await GitPublisher(token=token).delete_repo(full_name)
    except GitPublishError as e:
        raise HTTPException(400, str(e))

    store.forget_published_repo(full_name, ctx["user_id"])
    logger.info(f"Deleted repo {full_name} on behalf of user {ctx['user_id']}")
    return {"success": True, "deleted": full_name}


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
        raise HTTPException(500, "Scan failed unexpectedly. Please try again.")

    summary = ""
    if payload.ai_summary and result.findings:
        summary = await _ai_scan_summary(result)
    if not summary:
        summary = _fallback_summary(result)

    logger.info(f"Scanned {result.final_url}: grade {result.grade}, "
                f"{len(result.findings)} findings")

    # Scans were previously not recorded anywhere, so a user's audit history
    # vanished the moment they navigated away. As with generations, a failure
    # to log must not discard the result the user is waiting on.
    try:
        store.record_scan(
            user_id=ctx["user_id"],
            url=result.final_url or result.url,
            grade=result.grade,
            score=result.score,
            findings=len(result.findings),
        )
    except Exception as e:
        logger.warning(f"Could not record scan history: {e}")

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
                         ctx: dict = Depends(require_quota)):
    """
    Phase 2: Given a job_id from Phase 1, generate the test scripts.

    Costs one generation credit (see services/quota.py). The credit is reserved
    by the require_quota dependency and refunded below if the run fails.
    """
    lease = ctx["quota_lease"]

    session = store.get(job_id)
    if not session:
        lease.refund()
        raise HTTPException(404, "Job not found. Please re-analyze the repo first.")
    if session.get("user_id") != ctx["user_id"]:
        lease.refund()
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
    except AllProvidersExhausted as e:
        # Our shared API keys are dry — this is an outage on our side and hits
        # Pro users identically, so it must not be dressed up as an upsell.
        lease.refund()
        logger.error(f"Test generation unavailable: {e}")
        raise HTTPException(503, PROVIDER_OUTAGE_MESSAGE)
    except Exception as e:
        lease.refund()
        logger.error(f"Test generation failed: {e}")
        raise HTTPException(500, "Test generation failed. Please try again.")

    lease.commit()

    # History for the user's profile. `jobs` holds the detail but is pruned at
    # 24h, and `usage` is only a per-month counter — neither can answer "what
    # have I generated?". Never let bookkeeping sink a successful generation:
    # the user's tests are already made and paid for.
    try:
        req = session.get("request", {})
        store.record_generation(
            user_id=ctx["user_id"],
            source=req.get("repo_url") or req.get("zip_name") or "ZIP upload",
            framework=result.framework,
            language=payload.language or req.get("language", ""),
            test_count=result.test_count,
            file_count=len(result.files),
        )
    except Exception as e:
        logger.warning(f"Could not record generation history: {e}")

    response = GenerateResponse(
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

    # Generation is the last step that needs the job — drop the uploaded source
    # now rather than holding it for the full TTL.
    store.delete(job_id)

    return response


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
async def get_logs(limit: int = 50, ctx: dict = Depends(require_user)):
    return {"logs": llm_router.get_call_log(limit=limit)}
