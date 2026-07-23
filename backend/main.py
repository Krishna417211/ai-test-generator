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
  GET  /api/admin/*     — Admin console (allowlisted emails only; services/admin.py)
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
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, RedirectResponse

from config import settings
from logging_config import configure_logging, request_id_var
from ratelimit import rate_limit, analyze_limiter, generate_limiter, email_limiter, otp_limiter
from agents.filter_agent import FilterAgent, NoTestableUIError
from agents.writer_agent import WriterAgent
from models.schemas import (
    GenerateRequest, GenerateResponse, GeneratedFile,
    ProjectAnalysis, FilePreview, StatusResponse, ProviderStatus,
    PublishResponse, ScanRequest, ScanResponse,
    SignupRequest, LoginRequest, AuthResponse, LoginResponse,
    EmailRequest, TokenRequest, ResetPasswordRequest, VerifyOtpRequest,
    SimpleResponse, ChangePasswordRequest, DeleteAccountRequest,
    UserSettings, UserSettingsResponse,
    AdminUser, AdminUserList, AdminUserDetail, AdminSetPlanRequest,
    AdminSuspendRequest, AdminSetUsageRequest, AdminOverview, AdminSystem,
)
from services.file_extractor import extract_zip, filter_for_push
from services.github_service import GitHubService, parse_github_url, RepoNotFoundError, RepoAccessError
from services.git_publisher import GitPublisher, GitPublishError
from services.security_scanner import SecurityScanner, ScanError, precheck_target
from services.zap_scanner import ZapScanner, ZapAuthorizationError, ZapUnavailableError
from services import github_oauth, google_oauth
from services import auth as auth_svc
from services import billing
from services import verification
from services.auth import require_user
from services.admin import require_admin
from services.mailer import EmailNotConfigured, EmailDeliveryError
from services.quota import (
    require_quota, get_quota, current_period, tier_for_user,
    has_quota_remaining, quota_exceeded_detail,
)
from services.llm_router import (
    router as llm_router, AllProvidersExhausted, start_provenance, provenance_report, Tier,
)
from services.progress import Progress, ndjson
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


# The interactive docs and the OpenAPI schema enumerate every route — including
# the admin and billing surface — so they are served only outside production.
# In prod these URLs simply don't exist (404); locally and on staging they stay
# on for convenience.
_docs_enabled = not settings.is_production
app = FastAPI(
    lifespan=lifespan,
    title="Testra",
    description="Paste a GitHub URL, get production-ready E2E tests in seconds.",
    version="1.0.0",
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
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

def _analysis_body(result, user_id: str, request: dict, tier: Tier = Tier.FREE) -> dict:
    """Persist the job and build the analyze response. Shared by URL and ZIP.

    Carries provenance because this analysis is itself a user-visible AI output
    — it's what the preview screen asks you to confirm before a credit is spent,
    so "which model read my repo?" is a fair question at exactly this point.
    """
    previews = [
        FilePreview(
            path=path,
            size=len(content),
            importance=5,   # simplified; extractor has the real score
            preview=content[:200],
        )
        for path, content in list(result.files.items())[:50]  # max 50 previews
    ]

    import uuid
    job_id = str(uuid.uuid4())
    store.create(job_id, {
        "user_id": user_id,
        "files": result.files,
        "filter_result": result,
        "request": request,
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
        "provenance": provenance_report(tier),
    }


@app.post("/api/analyze", dependencies=[Depends(rate_limit(analyze_limiter))])
async def analyze_repo(payload: GenerateRequest, ctx: dict = Depends(require_user)):
    """
    Phase 1: Fetch the repo, extract UI files, run Filter Agent.
    Streams step progress as NDJSON, ending with the project analysis + file
    tree preview. The client shows this to the user before generating tests.

    The URL is parsed before the stream opens so a malformed one is still a
    plain 400 — see services/progress.py for why that split matters.
    """
    if not payload.repo_url:
        raise HTTPException(400, "repo_url is required for this endpoint")

    try:
        repo_info = parse_github_url(payload.repo_url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    async def work(progress: Progress) -> dict:
        start_provenance()
        tier = tier_for_user(ctx["user_id"])
        # A user who signed in with GitHub has already granted repo access, so
        # reading their private repo needs nothing further from them. The pasted
        # token stays as an override for the case the session has none.
        gh = GitHubService(token=payload.github_token or ctx["session"].get("github_token", ""))

        await progress.start("fetch")
        try:
            tree = await gh.get_file_tree(repo_info)
        except RepoNotFoundError as e:
            raise HTTPException(404, str(e))
        except RepoAccessError as e:
            raise HTTPException(403, str(e))

        paths = [item["path"] for item in tree]
        logger.info(f"Fetching {len(paths)} files from {payload.repo_url}")
        raw_files_list = await gh.fetch_files(repo_info, paths, concurrency=15)
        raw_files = {f.path: f.content for f in raw_files_list}
        await progress.done("fetch", f"{len(raw_files)} files read")

        agent = FilterAgent()
        try:
            result = await agent.run(
                raw_files,
                user_description=payload.test_flows,
                progress=progress,
                tier=tier,
            )
        except NoTestableUIError as e:
            raise HTTPException(422, str(e))

        return _analysis_body(result, ctx["user_id"], payload.model_dump(), tier)

    return ndjson(work)


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
    # Read and unzip before the stream opens: an oversized or corrupt archive is
    # decidable now, so it stays a plain 413/400 rather than an in-band error.
    content = await read_zip_upload(file)

    try:
        raw_files = extract_zip(content)
    except zipfile.BadZipFile:
        raise HTTPException(400, "Invalid ZIP file")

    logger.info(f"ZIP upload: {len(raw_files)} files extracted")

    async def work(progress: Progress) -> dict:
        start_provenance()
        tier = tier_for_user(ctx["user_id"])
        # The upload itself was the "fetch" — it is already on disk by the time
        # the stream opens, so report it done rather than pretending to wait.
        await progress.start("fetch")
        await progress.done("fetch", f"{len(raw_files)} files read")

        agent = FilterAgent()
        try:
            result = await agent.run(
                raw_files,
                user_description=test_flows,
                progress=progress,
                tier=tier,
            )
        except NoTestableUIError as e:
            raise HTTPException(422, str(e))

        return _analysis_body(result, ctx["user_id"], {
            "framework": framework,
            "language": language,
            "test_flows": test_flows,
            "base_url": base_url,
        }, tier)

    return ndjson(work)


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
        "google_oauth_enabled": settings.google_oauth_enabled,
        "max_upload_mb": settings.max_repo_size_mb,
        # The client can't infer these from a response shape alone, and needs
        # them to know whether to offer "resend link" / a code box at all.
        "email_verification_enabled": settings.require_email_verification and settings.email_enabled,
        "login_otp_enabled": settings.login_otp_enabled and settings.email_enabled,
        "password_reset_enabled": settings.email_enabled,
    }


async def _deliver(coro, *, what: str):
    """Await an email-sending coroutine where failing to send must fail the
    request, and hand back whatever it returned.

    A login code that never arrives is a login that cannot complete, so these
    must surface rather than be swallowed — the caller is left in a dead end
    otherwise, staring at a code box no code is coming for.

    The enumeration-safe endpoints (resend, forgot-password) deliberately do NOT
    use this: there, a failure that's visible only for real accounts is itself
    the leak, so they log and answer the same either way.
    """
    try:
        return await coro
    except EmailNotConfigured as e:
        logger.error(f"{what} not sent — SMTP is not configured: {e}")
        raise HTTPException(
            503, "Email isn't set up on this server, so this step can't be "
                 "completed. Please contact support."
        )
    except EmailDeliveryError as e:
        logger.error(f"{what} not sent: {e}")
        raise HTTPException(502, f"We couldn't send your {what.lower()}. Please try again.")


async def _issue_login(user: dict) -> LoginResponse:
    """Turn a *proven* identity into either a session or the next challenge.

    Only ever called once a password has been checked or GitHub has vouched for
    the account. The three exits are documented on LoginResponse.
    """
    # A suspended account is refused here as well as in require_user. Not for
    # safety — require_user already rejects every request the token could make —
    # but because issuing one anyway means a "successful" login followed by a
    # 403 on the next page, which reads as the app being broken rather than as
    # the deliberate lock it is. Say so at the door instead.
    if user.get("suspended"):
        raise HTTPException(
            403,
            "This account has been suspended. Contact support if you think "
            "this is a mistake.",
        )

    email = user.get("email")

    # Unverified: no session, and re-send the link rather than stranding them.
    if settings.require_email_verification and email and not user.get("email_verified"):
        await _deliver(
            verification.send_verification_email(user["id"], email, user.get("name") or ""),
            what="Verification email",
        )
        return LoginResponse(
            status="verification_required",
            email_hint=verification.mask_email(email),
            message=("Please confirm your email address first — we've sent a fresh "
                     "link to your inbox."),
        )

    # Second factor. Skipped when there's no address to send to, which in
    # practice means a GitHub account with no public email: OAuth already
    # proved inbox control, so there is nothing here for a code to add.
    if settings.login_otp_enabled and email:
        challenge = await _deliver(
            verification.start_login_challenge(user["id"], email, user.get("name") or ""),
            what="Login code",
        )
        return LoginResponse(
            status="otp_required",
            challenge_id=challenge.challenge_id,
            email_hint=verification.mask_email(email),
            expires_in=challenge.expires_in,
            message=f"We sent a 6-digit code to {verification.mask_email(email)}.",
        )

    return LoginResponse(
        status="ok",
        token=auth_svc.create_login_session(user["id"]),
        user=auth_svc.public_user(user),
    )


@app.post("/api/auth/signup", response_model=LoginResponse,
          dependencies=[Depends(rate_limit(analyze_limiter)),
                        Depends(rate_limit(email_limiter))])
async def signup(payload: SignupRequest):
    """Register an email/password account and send a verification link.

    Note what this does NOT return: a session token. Handing one out here would
    make the verification step decorative — you'd be logged in without ever
    proving the address is yours.
    """
    email = auth_svc.validate_email(payload.email)
    auth_svc.validate_password(payload.password)
    name = (payload.name or "").strip() or email.split("@")[0]

    existing = store.get_user_by_email(email)
    if existing and existing.get("email_verified"):
        raise HTTPException(409, "An account with this email already exists. Try logging in.")

    if existing:
        # An unverified row is not a real account — nobody has ever proved they
        # own this address, so nothing here is anyone's to protect. Rejecting
        # would be worse than useless: it would let anyone squat an address they
        # don't own and permanently block its real owner from registering. So
        # the latest signup wins, and only the inbox decides who gets in.
        store.update_user(
            existing["id"],
            password_hash=auth_svc.hash_password(payload.password),
            name=name,
        )
        user_id = existing["id"]
        logger.info(f"Re-signup on an unverified account: {email}")
    else:
        user_id = auth_svc.new_user_id()
        store.create_user({
            "id": user_id,
            "email": email,
            "password_hash": auth_svc.hash_password(payload.password),
            "name": name,
            "created_at": time.time(),
            "email_verified": False,
        })
        logger.info(f"New signup: {email}")

    # If this send fails the account stays unverified and unusable, which is the
    # safe direction — and signing up again resends rather than 409ing, so the
    # user is never stuck.
    await _deliver(
        verification.send_verification_email(user_id, email, name),
        what="Verification email",
    )

    return LoginResponse(
        status="verification_required",
        email_hint=verification.mask_email(email),
        message=f"Almost there — confirm your address using the link we sent to {email}.",
    )


@app.post("/api/auth/login", response_model=LoginResponse,
          dependencies=[Depends(rate_limit(analyze_limiter))])
async def login(payload: LoginRequest):
    """Check email + password, then hand off to verification or the OTP step."""
    email = (payload.email or "").strip().lower()
    user = store.get_user_by_email(email)
    if not user or not auth_svc.verify_password(payload.password, user.get("password_hash")):
        # Same message for both cases — don't reveal whether the email exists.
        raise HTTPException(401, "Incorrect email or password.")
    return await _issue_login(user)


@app.post("/api/auth/login/verify-otp", response_model=AuthResponse,
          dependencies=[Depends(rate_limit(otp_limiter))])
async def verify_login_otp(payload: VerifyOtpRequest):
    """Second step of login: exchange a correct emailed code for a session.

    Attempts are capped per-challenge inside verify_login_challenge; the limiter
    above stops that cap being sidestepped by spreading guesses across many
    challenges.
    """
    try:
        user_id = verification.verify_login_challenge(payload.challenge_id, payload.code)
    except verification.InvalidCode as e:
        raise HTTPException(401, str(e))

    user = store.get_user_by_id(user_id)
    if not user:
        raise HTTPException(401, "Account not found. Please log in again.")

    logger.info(f"Login completed with OTP: {user_id}")
    return AuthResponse(
        token=auth_svc.create_login_session(user_id),
        user=auth_svc.public_user(user),
    )


@app.post("/api/auth/verify-email", response_model=AuthResponse,
          dependencies=[Depends(rate_limit(analyze_limiter))])
async def verify_email(payload: TokenRequest):
    """Redeem a verification link, and log them straight in.

    Skipping the OTP here is not a hole: clicking a link from the inbox proves
    exactly what a code emailed to that inbox would prove. Making them then ask
    for a code, to the same address they just demonstrated control of, would add
    a step without adding a check.
    """
    try:
        user_id = verification.consume_verification_token(payload.token)
    except verification.InvalidCode as e:
        raise HTTPException(400, str(e))

    user = store.get_user_by_id(user_id)
    if not user:
        raise HTTPException(400, "This verification link is invalid or has expired.")

    store.update_user(user_id, email_verified=True)
    user = store.get_user_by_id(user_id)          # re-read so the response says verified
    logger.info(f"Email verified: {user_id}")
    return AuthResponse(
        token=auth_svc.create_login_session(user_id),
        user=auth_svc.public_user(user),
    )


@app.post("/api/auth/resend-verification", response_model=SimpleResponse,
          dependencies=[Depends(rate_limit(email_limiter))])
async def resend_verification(payload: EmailRequest):
    """Send another verification link.

    Answers the same way whether or not the address has an account waiting, so
    it can't be used to test which addresses are registered. Delivery problems
    are logged rather than returned for the same reason — a 502 that only ever
    happened for real accounts would be the leak this is avoiding.
    """
    email = (payload.email or "").strip().lower()
    user = store.get_user_by_email(email)
    if user and user.get("email") and not user.get("email_verified"):
        try:
            await verification.send_verification_email(
                user["id"], user["email"], user.get("name") or ""
            )
        except (EmailNotConfigured, EmailDeliveryError) as e:
            logger.error(f"Could not resend verification to {email}: {e}")
    return SimpleResponse(
        message="If that address is waiting to be verified, a new link is on its way."
    )


@app.post("/api/auth/forgot-password", response_model=SimpleResponse,
          dependencies=[Depends(rate_limit(email_limiter))])
async def forgot_password(payload: EmailRequest):
    """Start a password reset. Enumeration-safe, exactly as above."""
    email = (payload.email or "").strip().lower()
    user = store.get_user_by_email(email)
    # A GitHub-only account has no password to reset; sending a link that sets
    # one would quietly add a second way into an account whose owner chose SSO.
    if user and user.get("email") and user.get("password_hash"):
        try:
            await verification.send_password_reset_email(
                user["id"], user["email"], user.get("name") or ""
            )
        except (EmailNotConfigured, EmailDeliveryError) as e:
            logger.error(f"Could not send reset email to {email}: {e}")
    return SimpleResponse(
        message="If an account exists for that address, a reset link is on its way."
    )


@app.post("/api/auth/reset-password", response_model=AuthResponse,
          dependencies=[Depends(rate_limit(analyze_limiter))])
async def reset_password(payload: ResetPasswordRequest):
    """Set a new password from a reset link, and sign every other session out."""
    auth_svc.validate_password(payload.password)
    try:
        user_id = verification.consume_reset_token(payload.token)
    except verification.InvalidCode as e:
        raise HTTPException(400, str(e))

    user = store.get_user_by_id(user_id)
    if not user:
        raise HTTPException(400, "This reset link is invalid or has expired.")

    store.update_user(
        user_id,
        password_hash=auth_svc.hash_password(payload.password),
        # Redeeming the link proved inbox control, which is all verification
        # asks — so a reset also clears a pending verification.
        email_verified=True,
    )

    # Before minting the new session, not after: whoever prompted this reset may
    # be holding a live session on the account, and leaving it valid would make
    # the reset cosmetic. Ordering matters — do this after and we'd revoke the
    # session we just issued to the real owner.
    revoked = store.delete_sessions_for_user(user_id)
    logger.info(f"Password reset for {user_id}; revoked {revoked} existing session(s)")

    user = store.get_user_by_id(user_id)
    return AuthResponse(
        token=auth_svc.create_login_session(user_id),
        user=auth_svc.public_user(user),
    )


def _safe_next(path: str) -> str:
    """A same-site path to return the browser to after login, or "".

    Only a relative path is ever accepted. Echoing an arbitrary `next` into a
    redirect is an open-redirect hole, and this one is reachable by anyone who
    can craft a link — so anything that could leave the site (absolute URL,
    scheme-relative //host, a backslash Windows browsers normalise to /) is
    dropped rather than sanitised.
    """
    p = (path or "").strip()
    if not p.startswith("/") or p.startswith("//") or "\\" in p:
        return ""
    return p[:200]


@app.get("/api/auth/github/login")
async def github_login(next: str = "", link: str = ""):
    """Start the OAuth flow — redirect the browser to GitHub's consent screen.

    `next`  — where to send the browser once the round-trip completes, so
              "Connect GitHub" from the publish page comes back to the publish
              page with the ZIP form still the thing on screen.
    `link`  — the caller's current session token, when they are already signed
              in. The callback then attaches GitHub to *that* account instead of
              resolving an account from scratch, which is what stops a second
              account being created for someone whose GitHub email is private.
    """
    if not settings.github_oauth_enabled:
        raise HTTPException(503, "GitHub login is not configured on this server.")
    state = secrets.token_urlsafe(24)
    store.create_session(
        _STATE_PREFIX + state,
        {"kind": "oauth_state", "next": _safe_next(next), "link_session": link.strip()},
        _OAUTH_STATE_TTL,
    )
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
    st = store.get_session(_STATE_PREFIX + state) if state else None
    if st is None:
        return _fail("invalid_state")
    store.delete_session(_STATE_PREFIX + state)
    next_path = _safe_next(st.get("next") or "")
    link_session_id = (st.get("link_session") or "").strip()

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
    # Already signed in and connecting GitHub from inside the app (the publish
    # page): attach it to the account they are *holding a session for*. Without
    # this, a GitHub profile with a private email matches nothing, a second
    # account is created, and the user is silently switched into it — their
    # history and plan appearing to vanish at the moment they wanted to publish.
    # A github_id already bound elsewhere still wins: an identity is not moved
    # between accounts by clicking a button.
    if not account and link_session_id:
        live = store.get_session(link_session_id)
        if live and live.get("user_id"):
            account = store.get_user_by_id(live["user_id"])
    if not account and gh.get("email"):
        account = store.get_user_by_email(gh["email"])
    # GitHub only exposes a verified address as the account's email, so arriving
    # here *is* proof of inbox control — the same proof our own verification link
    # asks for. Marking these accounts verified is therefore not a shortcut; the
    # alternative would be emailing a link to an address GitHub already checked.
    # It matters that this is explicit: create_user defaults to unverified, so
    # leaving it off would lock every GitHub user out of their own account.
    if account:
        store.update_user(
            account["id"], github_id=github_id, github_login=gh["login"],
            avatar_url=gh.get("avatar_url") or account.get("avatar_url"),
            email_verified=True,
        )
        user_id = account["id"]
    else:
        user_id = auth_svc.new_user_id()
        store.create_user({
            "id": user_id, "email": gh.get("email"),
            "name": gh.get("name") or gh["login"],
            "github_id": github_id, "github_login": gh["login"],
            "avatar_url": gh.get("avatar_url"), "created_at": time.time(),
            "email_verified": True,
        })
        logger.info(f"New GitHub signup: {gh['login']}")

    # This path builds a session itself rather than going through _issue_login
    # (OAuth has already proved the identity, so there's no password or OTP step
    # to run), which means the suspension check there doesn't cover it. Repeat it
    # rather than hand a suspended account a token every route would then refuse.
    if store.get_user_by_id(user_id).get("suspended"):
        return _fail("suspended")

    # Session carries the GitHub access token so Publish can push on their behalf.
    #
    # When this was a "connect GitHub" from an existing session, write the token
    # into that same session rather than minting a new one. The user keeps the
    # login they already had (same token, same expiry) and simply gains the
    # ability to push — reconnecting must not read as being logged out and back
    # in, and any other tab holding the old token would be exactly that.
    session_id = ""
    if link_session_id:
        live = store.get_session(link_session_id)
        if live and live.get("user_id") == user_id:
            live["github_token"] = auth_svc.encrypt_secret(token)
            if store.update_session(link_session_id, live):
                session_id = link_session_id
    if not session_id:
        session_id = auth_svc.create_login_session(user_id, github_token=token)

    dest = f"{frontend}/auth/callback?token={session_id}"
    if next_path:
        dest += f"&next={quote(next_path, safe='/?=&')}"
    return RedirectResponse(dest, status_code=307)


@app.get("/api/auth/google/login")
async def google_login():
    """Start the Google OAuth flow — redirect to Google's consent screen."""
    if not settings.google_oauth_enabled:
        raise HTTPException(503, "Google login is not configured on this server.")
    state = secrets.token_urlsafe(24)
    store.create_session(_STATE_PREFIX + state, {"kind": "oauth_state"}, _OAUTH_STATE_TTL)
    url = google_oauth.authorize_url(
        client_id=settings.google_client_id,
        redirect_uri=settings.google_callback_url,
        state=state,
    )
    return RedirectResponse(url, status_code=307)


@app.get("/api/auth/google/callback")
async def google_callback(code: str = "", state: str = "", error: str = ""):
    """Google redirects here after the user approves (or denies).

    Structurally the twin of github_callback. The one real difference: Google is
    an identity provider only, so the session carries no provider token, and we
    key accounts on the OpenID `sub`.
    """
    frontend = settings.frontend_url.rstrip("/")

    def _fail(reason: str):
        return RedirectResponse(f"{frontend}/auth/callback?login_error={reason}", status_code=307)

    if error or not code:
        return _fail(error or "access_denied")
    # One-time state check (CSRF protection).
    if not state or store.get_session(_STATE_PREFIX + state) is None:
        return _fail("invalid_state")
    store.delete_session(_STATE_PREFIX + state)

    try:
        token = await google_oauth.exchange_code(
            code=code,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            redirect_uri=settings.google_callback_url,
        )
        gu = await google_oauth.get_user(token)
    except google_oauth.OAuthError as e:
        logger.warning(f"Google OAuth callback failed: {e}")
        return _fail("exchange_failed")

    # Google must hand back a verified address. Without one we have no reliable
    # identity to key on, and marking an unverified address verified would be the
    # very shortcut our own signup refuses to take.
    google_id = str(gu.get("id") or "")
    email = gu.get("email")
    if not google_id or not email or not gu.get("email_verified"):
        return _fail("email_unverified")

    # Find or create the account: by google_id first, then by email so a user who
    # first signed up with email/password and now clicks "Continue with Google"
    # is linked to their existing account rather than getting a duplicate.
    account = store.get_user_by_google(google_id)
    if not account:
        account = store.get_user_by_email(email)
    if account:
        store.update_user(
            account["id"], google_id=google_id,
            avatar_url=gu.get("avatar_url") or account.get("avatar_url"),
            # Arriving here proves control of a Google-verified inbox — the same
            # proof our verification link asks for — so an account that linked
            # this way is verified even if it never clicked our email.
            email_verified=True,
        )
        user_id = account["id"]
    else:
        user_id = auth_svc.new_user_id()
        store.create_user({
            "id": user_id, "email": email,
            "name": gu.get("name") or email.split("@")[0],
            "google_id": google_id, "avatar_url": gu.get("avatar_url"),
            "created_at": time.time(), "email_verified": True,
        })
        logger.info(f"New Google signup: {email}")

    # Like github_callback, this builds a session directly (OAuth already proved
    # the identity — no password or OTP step), so the suspension check in
    # _issue_login doesn't cover it and is repeated here.
    if store.get_user_by_id(user_id).get("suspended"):
        return _fail("suspended")

    session_id = auth_svc.create_login_session(user_id)
    return RedirectResponse(f"{frontend}/auth/callback?token={session_id}", status_code=307)


@app.get("/api/auth/me")
async def auth_me(ctx: dict = Depends(require_user)):
    """Return the currently logged-in user (401 if the token is missing/invalid).

    `github_connected` is deliberately not the same thing as `has_github`.
    has_github says the *account* has a GitHub identity attached — it drives the
    profile page. Pushing needs a live access token, which lives on the
    *session*, so an account that once logged in with GitHub and is now on a
    password session has has_github=True and nothing to push with. Publish keys
    off this field so the UI offers "Connect GitHub" exactly when a push would
    otherwise 403, instead of claiming a connection the request can't use.
    """
    user = dict(ctx["user"])
    user["github_connected"] = bool(ctx["session"].get("github_token"))
    return {"authenticated": True, "user": user}


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


# ─────────────────────────────────────────────
# Dashboard: the post-login landing page
# ─────────────────────────────────────────────

# How much history the overview renders. The feed is a scan-and-recognise list,
# not an archive — /api/profile holds the full history. Kept short deliberately:
# at 20 the page ran twice the height of everything beside it, which is a lot of
# scrolling for rows the user can already see in more detail elsewhere.
DASHBOARD_FEED_LIMIT = 8
DASHBOARD_SERIES_DAYS = 30


@app.get("/api/dashboard")
async def dashboard(ctx: dict = Depends(require_user)):
    """Everything the dashboard renders, in one call.

    One endpoint rather than four (feed/totals/series/quota) because the page has
    no state in which it wants a subset — four calls would just be four round
    trips and four spinners resolving out of order.

    Scoped to the session's own user_id throughout; no id is accepted from the
    client, so one account can never read another's activity.
    """
    user_id = ctx["user_id"]
    quota = get_quota(user_id).as_dict()
    row = store.get_user_by_id(user_id) or {}

    return {
        "user": ctx["user"],
        "member_since": row.get("created_at"),
        # Mirrors /api/profile: only report a renewal date for a plan that's
        # actually still live (get_quota has already lapsed an expired one).
        "plan": {
            **quota,
            "expires_at": row.get("plan_expires_at") if quota["plan"] != "free" else None,
            "checkout_available": settings.billing_enabled,
            "catalogue": billing.plans(),
        },
        "totals": store.activity_totals(user_id),
        "activity": store.activity_feed(user_id, limit=DASHBOARD_FEED_LIMIT),
        "series": store.activity_series(user_id, days=DASHBOARD_SERIES_DAYS),
        "recent": {
            "generations": store.list_generations(user_id, limit=5),
            "scans": store.list_scans(user_id, limit=5),
            "repos": store.list_published_repos(user_id, limit=5),
        },
    }


# ─────────────────────────────────────────────
# Settings: form defaults, credentials, account
# ─────────────────────────────────────────────

@app.get("/api/settings", response_model=UserSettingsResponse)
async def get_user_settings(ctx: dict = Depends(require_user)):
    return store.get_settings(ctx["user_id"])


@app.put("/api/settings", response_model=UserSettingsResponse)
async def put_user_settings(payload: UserSettings, ctx: dict = Depends(require_user)):
    """Patch this user's form defaults. Omitted fields keep their current value."""
    # mode="json" unwraps the enums to their values, so SQLite stores
    # "playwright" rather than "TestFramework.PLAYWRIGHT" — which would come back
    # out as a string the UI's <select> never matches.
    patch = payload.model_dump(exclude_none=True, mode="json")
    return store.save_settings(ctx["user_id"], **patch)


@app.post("/api/auth/change-password", response_model=SimpleResponse,
          dependencies=[Depends(rate_limit(analyze_limiter))])
async def change_password(payload: ChangePasswordRequest, ctx: dict = Depends(require_user)):
    """Change the password of the logged-in account, then sign other sessions out.

    A GitHub-only account has no password to check against, so there's nothing
    here that could authorise the change — it's refused rather than allowing a
    session alone to set a first password (that's what the reset link is for).
    """
    user = store.get_user_by_id(ctx["user_id"])
    if not user or not user.get("password_hash"):
        raise HTTPException(
            400, "This account signs in with GitHub, so it has no password to change."
        )
    if not auth_svc.verify_password(payload.current_password, user["password_hash"]):
        raise HTTPException(401, "That's not your current password.")
    auth_svc.validate_password(payload.new_password)

    store.update_user(ctx["user_id"], password_hash=auth_svc.hash_password(payload.new_password))
    # Same reasoning as the reset flow: a password change must evict anyone else
    # holding a session. `keep` spares the caller's own — they just proved
    # ownership, so logging them out here would be a bug, not a safeguard.
    revoked = store.delete_sessions_for_user(ctx["user_id"], keep=ctx["session_id"])
    logger.info(f"Password changed for {ctx['user_id']}; revoked {revoked} other session(s)")
    return SimpleResponse(
        message=(f"Password updated. {revoked} other session(s) were signed out."
                 if revoked else "Password updated.")
    )


@app.post("/api/auth/logout-all", response_model=SimpleResponse)
async def logout_everywhere(ctx: dict = Depends(require_user)):
    """Revoke every session for this account, including the caller's own.

    Unlike change-password, this one deliberately does NOT keep the current
    session: "log out everywhere" that left you logged in wouldn't be it.
    """
    revoked = store.delete_sessions_for_user(ctx["user_id"])
    logger.info(f"Logged {ctx['user_id']} out of {revoked} session(s)")
    return SimpleResponse(message=f"Signed out of {revoked} session(s).")


@app.delete("/api/account", response_model=SimpleResponse,
            dependencies=[Depends(rate_limit(analyze_limiter))])
async def delete_account(payload: DeleteAccountRequest, ctx: dict = Depends(require_user)):
    """Erase this account and all of its activity. Irreversible.

    Deliberately does NOT touch repos published to GitHub: those live in the
    user's own account, we only ever recorded that we made them, and silently
    deleting someone's code because they closed an account here would be
    indefensible. The warning in the UI says so; /api/repo/... deletes them
    one at a time, on purpose.
    """
    user = store.get_user_by_id(ctx["user_id"])
    if not user:
        raise HTTPException(404, "Account not found.")

    # Echo back the account's own identifier — email, or the GitHub login for
    # SSO accounts that have no address on file.
    expected = user.get("email") or user.get("github_login") or ""
    if payload.confirm.strip().lower() != expected.lower():
        raise HTTPException(400, f"Type {expected} exactly to confirm deletion.")

    freed = store.delete_account(ctx["user_id"])
    logger.info(f"Account deleted: {ctx['user_id']} ({freed})")
    return SimpleResponse(message="Your account and all of its history have been deleted.")


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = request.headers.get("Authorization", "")
    token = token[7:].strip() if token.lower().startswith("bearer ") else request.query_params.get("token", "")
    if token:
        store.delete_session(token)
    return {"ok": True}


# ─────────────────────────────────────────────
# Admin console
# ─────────────────────────────────────────────
#
# Every route here carries Depends(require_admin), which chains off require_user
# — so each one re-derives the role from the ADMIN_EMAILS allowlist on every
# request. The client's `is_admin` flag decides whether a nav link renders and
# nothing else. See services/admin.py for why the grant lives in config.
#
# These are the only routes in this file that read across users, so they're the
# only ones where a missing dependency is an account-enumeration bug rather than
# a bad request. Adding a route below without require_admin is that bug.

ADMIN_PAGE_SIZE = 25
ADMIN_SERIES_DAYS = 30


def _admin_user_view(row: dict) -> AdminUser:
    """A user row plus the counts the console lists them by."""
    user_id = row["id"]
    quota = get_quota(user_id)
    totals = store.activity_totals(user_id)
    return AdminUser(
        id=user_id,
        email=row.get("email"),
        name=row.get("name") or (row.get("email") or "").split("@")[0],
        github_login=row.get("github_login"),
        avatar_url=row.get("avatar_url"),
        created_at=row.get("created_at") or 0.0,
        # quota.plan, not row["plan"] — the effective plan, with a lapsed
        # subscription already read down to free.
        plan=quota.plan,
        plan_expires_at=row.get("plan_expires_at"),
        email_verified=bool(row.get("email_verified")),
        suspended=bool(row.get("suspended")),
        suspended_at=row.get("suspended_at"),
        suspended_reason=row.get("suspended_reason"),
        is_admin=auth_svc.is_admin(row),
        # activity_totals keys these "generations"/"scans" — not the
        # "generate"/"scan" that ACTIVITY_TABLES and activity_series use.
        generations=totals.get("generations", 0),
        scans=totals.get("scans", 0),
        usage_this_period=quota.used,
    )


def _admin_target(user_id: str, ctx: dict, *, action: str) -> dict:
    """Load the user an admin is acting on, refusing self-targeted actions.

    Suspending or deleting yourself is never what an admin meant to do, and both
    are one misclick away in a list of similar-looking rows. Self-suspension is
    also unrecoverable from the UI it would lock you out of: require_user rejects
    the suspended account, so the console that could undo it is now shut to you,
    and the fix is a hand-written UPDATE against the database.
    """
    if user_id == ctx["user_id"]:
        raise HTTPException(400, f"You cannot {action} your own account from the admin console.")
    row = store.get_user_by_id(user_id)
    if not row:
        raise HTTPException(404, "No such user.")
    return row


@app.get("/api/admin/users", response_model=AdminUserList)
async def admin_list_users(
    q: str = "",
    limit: int = ADMIN_PAGE_SIZE,
    offset: int = 0,
    sort: str = "created_at",
    direction: str = "desc",
    ctx: dict = Depends(require_admin),
):
    """Search/browse accounts. `total` counts everything matching `q`, not the
    page, so the client can paginate without a second call.

    `sort`/`direction` are passed through rather than validated here: store
    .list_users owns the allowlist, and duplicating it would give two places to
    disagree about what's sortable.
    """
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    rows = store.list_users(query=q, limit=limit, offset=offset,
                            sort=sort, direction=direction)
    return AdminUserList(
        users=[_admin_user_view(r) for r in rows],
        total=store.count_users(query=q),
        limit=limit,
        offset=offset,
    )


@app.get("/api/admin/users/{user_id}", response_model=AdminUserDetail)
async def admin_user_detail(user_id: str, ctx: dict = Depends(require_admin)):
    """One account in full: plan, quota, history, and where they're signed in."""
    row = store.get_user_by_id(user_id)
    if not row:
        raise HTTPException(404, "No such user.")
    return AdminUserDetail(
        user=_admin_user_view(row),
        quota=get_quota(user_id).as_dict(),
        totals=store.activity_totals(user_id),
        active_sessions=store.count_sessions_for_user(user_id),
        recent_generations=store.list_generations(user_id, limit=10),
        recent_scans=store.list_scans(user_id, limit=10),
        published_repos=store.list_published_repos(user_id, limit=10),
    )


@app.post("/api/admin/users/{user_id}/suspend", response_model=AdminUser)
async def admin_suspend_user(
    user_id: str,
    payload: AdminSuspendRequest,
    ctx: dict = Depends(require_admin),
):
    """Lock or unlock an account.

    Suspending also revokes their live sessions. Without that the flag would only
    be read on the *next* request the token happens to make, and require_user
    would keep serving anyone already holding one — a suspension that doesn't
    take effect until logout is not a suspension.
    """
    row = _admin_target(user_id, ctx, action="suspend")

    # An admin can't be suspended: the allowlist, not the users table, decides
    # who is one, so the flag wouldn't remove their access — it would only lock
    # them out of the app while leaving the console reachable. Two admins fighting
    # over that row is a worse outcome than refusing here.
    if payload.suspended and auth_svc.is_admin(row):
        raise HTTPException(
            400,
            "That account is an admin. Remove the address from ADMIN_EMAILS "
            "and restart before suspending it.",
        )

    store.set_suspended(user_id, payload.suspended, reason=payload.reason or "")
    revoked = store.delete_sessions_for_user(user_id) if payload.suspended else 0
    logger.info(
        f"Admin {ctx['user_id']} {'suspended' if payload.suspended else 'unsuspended'} "
        f"{user_id} (sessions_revoked={revoked}, reason={payload.reason!r})"
    )
    return _admin_user_view(store.get_user_by_id(user_id))


@app.post("/api/admin/users/{user_id}/plan", response_model=AdminUser)
async def admin_set_plan(
    user_id: str,
    payload: AdminSetPlanRequest,
    ctx: dict = Depends(require_admin),
):
    """Grant or revoke a plan by hand — comps, refunds, and support fixes.

    The other caller of set_plan is the signed billing webhook. This one is an
    admin's judgement instead of a payment, so it's logged with who did it: a
    plan that changed without a corresponding webhook should be traceable to a
    person.
    """
    row = store.get_user_by_id(user_id)
    if not row:
        raise HTTPException(404, "No such user.")

    expires_at = (
        time.time() + payload.expires_in_days * 86_400
        if payload.expires_in_days is not None else None
    )
    # Free is the absence of an entitlement, so it must not carry an expiry —
    # a row with plan='free' and a future plan_expires_at reads as "free until
    # then, something else after", which get_plan does not mean and would show
    # a renewal date on a free account.
    if payload.plan == "free":
        expires_at = None

    store.set_plan(user_id, payload.plan, expires_at)
    logger.info(
        f"Admin {ctx['user_id']} set plan of {user_id} to {payload.plan} "
        f"(expires_at={expires_at}, reason={payload.reason!r})"
    )
    return _admin_user_view(store.get_user_by_id(user_id))


@app.post("/api/admin/users/{user_id}/usage", response_model=AdminUser)
async def admin_set_usage(
    user_id: str,
    payload: AdminSetUsageRequest,
    ctx: dict = Depends(require_admin),
):
    """Overwrite this month's usage counter — hand back credits for a run that
    failed in a way the automatic refund (services/quota.QuotaLease) missed."""
    row = store.get_user_by_id(user_id)
    if not row:
        raise HTTPException(404, "No such user.")
    period = current_period()
    store.set_usage(user_id, period, payload.count)
    logger.info(
        f"Admin {ctx['user_id']} set usage of {user_id} to {payload.count} for {period}"
    )
    return _admin_user_view(store.get_user_by_id(user_id))


@app.post("/api/admin/users/{user_id}/logout-all", response_model=SimpleResponse)
async def admin_logout_user(user_id: str, ctx: dict = Depends(require_admin)):
    """Revoke every session a user holds, without suspending them — the polite
    version, for a lost laptop or a shared login."""
    row = store.get_user_by_id(user_id)
    if not row:
        raise HTTPException(404, "No such user.")
    revoked = store.delete_sessions_for_user(user_id)
    logger.info(f"Admin {ctx['user_id']} revoked {revoked} session(s) for {user_id}")
    return SimpleResponse(message=f"Signed out of {revoked} session(s).")


@app.delete("/api/admin/users/{user_id}", response_model=SimpleResponse)
async def admin_delete_user(
    user_id: str,
    confirm: str = "",
    ctx: dict = Depends(require_admin),
):
    """Erase an account and all of its history. Irreversible.

    `confirm` must be the target's own email, mirroring the self-serve delete at
    /api/account: the destructive step should feel the same whether you're doing
    it to yourself or to someone else, and it makes deleting the wrong row take
    more than one wrong click.
    """
    row = _admin_target(user_id, ctx, action="delete")

    if auth_svc.is_admin(row):
        raise HTTPException(
            400,
            "That account is an admin. Remove the address from ADMIN_EMAILS "
            "and restart before deleting it.",
        )

    expected = row.get("email") or row.get("github_login") or user_id
    if confirm.strip().lower() != expected.lower():
        raise HTTPException(400, f"Type {expected} exactly to confirm deletion.")

    freed = store.delete_account(user_id)
    logger.warning(f"Admin {ctx['user_id']} deleted account {user_id} ({freed})")
    return SimpleResponse(message=f"Deleted {expected} and all of its history.")


@app.get("/api/admin/overview", response_model=AdminOverview)
async def admin_overview(ctx: dict = Depends(require_admin)):
    """Platform analytics: totals, a 30-day series, and what's failing."""
    return AdminOverview(
        totals=store.platform_totals(),
        series=store.platform_series(days=ADMIN_SERIES_DAYS),
        frameworks=store.platform_breakdown("framework"),
        languages=store.platform_breakdown("language"),
        recent_failures=store.recent_failures(limit=10),
    )


@app.get("/api/admin/system", response_model=AdminSystem)
async def admin_system(ctx: dict = Depends(require_admin)):
    """Provider health, per-key cooldowns, and the feature switches in force.

    `config` carries booleans derived from settings, never the settings
    themselves — an admin needs to know whether SMTP is configured, not what the
    password is. Nothing secret is reachable from this response; see
    llm_router.get_key_health for the same rule applied to API keys.
    """
    status = llm_router.get_status()
    return AdminSystem(
        app_env=settings.app_env,
        uptime_seconds=round(time.time() - _START_TIME, 1),
        jobs_stored=store.count(),
        providers=[
            ProviderStatus(
                name=name,
                total_keys=info["total_keys"],
                available_keys=info["available_keys"],
                total_calls=info["total_calls"],
                healthy=info["healthy"],
            )
            for name, info in status.items()
        ],
        keys=llm_router.get_key_health(),
        call_log=llm_router.get_call_log(limit=30),
        config={
            "github_oauth_enabled": settings.github_oauth_enabled,
            "google_oauth_enabled": settings.google_oauth_enabled,
            "smtp_configured": settings.smtp_configured,
            "email_enabled": settings.email_enabled,
            "require_email_verification": settings.require_email_verification,
            "login_otp_enabled": settings.login_otp_enabled,
            "billing_enabled": settings.billing_enabled,
            "free_generations_per_month": settings.free_generations_per_month,
            "admin_count": len(settings.admin_email_list),
        },
    )


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
    if not billing.verify_webhook(raw, request.headers.get("Stripe-Signature") or ""):
        logger.warning("Rejected a billing webhook with a bad/missing signature")
        raise HTTPException(400, "Invalid signature")

    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "Malformed webhook body")

    # A good signature only proves Stripe sent this — invoice.payment_failed and
    # customer.subscription.deleted are signed too. The event type decides.
    if not billing.is_granting_event(event):
        logger.info(f"Ignoring non-granting billing event: {event.get('type')}")
        return {"ok": True, "granted": False}

    user_id, plan_id = billing.extract_grant(event)
    customer_id = billing.extract_customer(event)

    # A renewal invoice names no user — only the customer that first checked out.
    if not user_id and customer_id:
        payer = store.get_user_by_stripe_customer(customer_id)
        user_id = payer["id"] if payer else None

    # Everything below answers 200. Stripe retries any non-2xx for days, and none
    # of these resolve on a retry: an untagged payment (someone used the bare
    # Payment Link) and a departed user are both permanent. They're logged with
    # the event id instead, which is what reconciling the payment by hand needs.
    if not user_id or not plan_id:
        logger.warning(
            f"Billing webhook {event.get('id')} ({event.get('type')}) could not be "
            f"attributed: user_id={user_id} plan_id={plan_id} customer={customer_id}. "
            "No grant was made; reconcile this payment in the Stripe dashboard."
        )
        return {"ok": True, "granted": False}

    if not store.get_user_by_id(user_id):
        logger.warning(
            f"Billing webhook {event.get('id')} names unknown user {user_id}; "
            "no grant was made."
        )
        return {"ok": True, "granted": False}

    # Record the payer before granting: this mapping is the only thing that lets
    # the *next* invoice.paid find this user, so a subscription that renews must
    # not depend on the tag being present a second time.
    if customer_id:
        store.link_stripe_customer(user_id, customer_id)

    store.set_plan(user_id, "pro", time.time() + billing.grant_seconds(plan_id))
    logger.info(f"Granted Pro ({plan_id}) to {user_id} via billing webhook")
    return {"ok": True, "granted": True}


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


@app.post("/api/publish-zip", dependencies=[Depends(rate_limit(analyze_limiter))])
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

    Streams step progress as NDJSON, ending with a PublishResponse. Everything
    that can be rejected without doing work — no repo name, no token, a corrupt
    or empty archive — is raised before the stream opens and stays a plain HTTP
    error.
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

    async def work(progress: Progress) -> dict:
        return await _publish_work(
            progress, raw_files=raw_files, token=token, repo_name=repo_name,
            add_cicd=add_cicd, private=private, framework=framework,
            language=language, test_flows=test_flows, base_url=base_url,
            repo_description=repo_description, user_id=ctx["user_id"],
        )

    return ndjson(work)


async def _publish_work(
    progress: Progress, *, raw_files: dict, token: str, repo_name: str,
    add_cicd: bool, private: bool, framework: str, language: str,
    test_flows: str, base_url: str, repo_description: str, user_id: str,
) -> dict:
    """The publish pipeline. Split out of the endpoint so the streaming wrapper
    stays a thin shell over the same logic that used to be inline."""
    start_provenance()
    tier = tier_for_user(user_id)

    await progress.start("read")
    await progress.done("read", f"{len(raw_files)} files in the archive")

    # Keep only what belongs in a repo (drop node_modules, binaries, huge files).
    await progress.start("filter")
    push_files, warnings = filter_for_push(raw_files)
    if not push_files:
        raise HTTPException(400, "Nothing to push after filtering build/dependency files")
    dropped = len(raw_files) - len(push_files)
    await progress.done(
        "filter",
        f"{len(push_files)} to push"
        + (f" · {dropped} dropped" if dropped > 0 else ""),
    )

    cicd_added = False
    test_count = 0
    validation: list[dict] = []
    all_valid = True
    # None until a suite is actually generated: on a push with no CI requested,
    # or one where the AI was down, there is nothing measured and the response
    # must say nothing rather than report a grounding of zero.
    grounding: dict | None = None

    # None means "don't attempt a suite" — either the user didn't ask for CI, or
    # there's no UI to drive. Pushing the project itself never depends on this.
    filter_result = None
    if not add_cicd:
        await progress.skip("suite", "CI/CD not requested")
    if add_cicd:
        # Generate a validated E2E test suite + CI workflow and fold it into the
        # push. WriterAgent's self-heal loop is the "validate locally until green"
        # step — it re-prompts the LLM to fix any file that fails static validation.
        await progress.start("suite")
        filter_agent = FilterAgent()
        try:
            filter_result = await filter_agent.run(
                raw_files, user_description=test_flows, tier=tier,
            )
        except NoTestableUIError as e:
            # Same reasoning as the provider-outage branch below: land the code
            # and say what's missing. The CI workflow is dropped with it — it
            # runs `npx playwright test`, which goes red on a repo with no specs.
            logger.info(f"Skipping CI generation — nothing to test: {e}")
            warnings.append(
                f"{e} Your project was pushed without an E2E suite or CI/CD pipeline."
            )
            await progress.skip("suite", "No testable UI in this project")

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
                tier=tier,
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
            await progress.skip("suite", "AI unavailable — pushing your code anyway")
        else:
            test_count = result.test_count
            validation = result.validation
            all_valid = all(v.get("ok", False) for v in validation)
            grounding = result.grounding.as_dict()
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
                # No collision guard needed: the suite lives in its own directory
                # (agents/scaffold.SUITE_DIR), so its README and package.json are
                # e2e/README.md and e2e/package.json and cannot land on the
                # project's own. The CI workflow is the one file written to the
                # repo root, and only a previous Testra push would own that path.
                push_files[gf.filename] = gf.content
            # Only true when the suite is whole and the CI workflow actually shipped.
            cicd_added = not result.failed_files
            g = result.grounding
            await progress.done(
                "suite",
                f"{test_count} tests · {g.files_valid}/{g.files_checked} code files parsed"
                + (
                    f" · {g.selectors_verified}/{g.selectors_total} selectors verified"
                    if g.selectors_total else ""
                ),
            )

    # Ensure a .gitignore exists so the pushed repo stays clean.
    if ".gitignore" not in push_files:
        push_files[".gitignore"] = _DEFAULT_GITIGNORE

    await progress.start("push")
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
    await progress.done("push", f"{pub.files_pushed} files → {pub.full_name}")

    logger.info(f"Published {pub.files_pushed} files to {pub.full_name} (cicd={cicd_added})")

    # Remember what we created — /api/repo deletion is limited to these, and the
    # dashboard reads the same row back as publish activity.
    await progress.start("record")
    store.record_published_repo(
        pub.full_name, user_id, pub.repo_url,
        test_count=test_count,
        files_pushed=pub.files_pushed,
        cicd_added=cicd_added,
        private=private,
    )
    await progress.done("record", "You can undo this from your dashboard")

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
        grounding=grounding,
        provenance=provenance_report(tier),
        warnings=warnings + pub.warnings,
    ).model_dump()


# ─────────────────────────────────────────────
# Security scan: audit a deployed URL for production vulnerabilities
# ─────────────────────────────────────────────

async def _ai_scan_summary(result, tier: Tier = Tier.FREE) -> str:
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
            tier=tier,
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


@app.get("/api/scan/capabilities")
async def scan_capabilities(ctx: dict = Depends(require_user)):
    """Whether an active (ZAP) scan can run right now, and if not, why.

    The scan endpoint already degrades gracefully and says what happened, but
    only *after* the user has waited through a scan they asked to be active.
    This lets the UI say it up front, next to the checkbox — and it's the one
    call to make when active scanning "isn't working": the reason it returns is
    the actual server-side fact, not a guess.
    """
    if not settings.zap_address:
        return {"active_available": False, "reason": "no ZAP daemon is configured on the server",
                "code": "not_configured"}
    if not settings.zap_allow_active:
        return {"active_available": False, "reason": "active scanning is switched off on this server",
                "code": "disabled"}
    code, reason = await asyncio.to_thread(ZapScanner().diagnose)
    return {"active_available": code == "ok", "reason": reason, "code": code}


@app.post("/api/scan", dependencies=[Depends(rate_limit(analyze_limiter))])
async def scan_url(payload: ScanRequest, ctx: dict = Depends(require_user)):
    """
    Passively audit a deployed URL for common production security issues and
    return prioritized findings with concrete fixes. Non-intrusive: it inspects
    headers/TLS/cookies and checks for accidentally-exposed files — no attacks.

    Streams step progress as NDJSON, ending with a ScanResponse. The response
    model is asserted by _scan_body's return type rather than the decorator —
    a StreamingResponse can't be validated against one.

    Targeting is settled before the stream opens, per progress.py's contract: a
    bad scheme or a third-party site is decidable from the URL alone, so it stays
    a real 400 instead of an in-band error arriving after a 200 and a half-drawn
    pipeline. Only the network-free checks run here — DNS and the SSRF guard stay
    inside the scanner, resolved once, next to the request they protect.
    """
    try:
        precheck_target(payload.url)
    except ScanError as e:
        raise HTTPException(400, str(e))

    async def work(progress: Progress) -> dict:
        start_provenance()
        tier = tier_for_user(ctx["user_id"])
        started = time.monotonic()

        # Active scanning (ZAP) sends real payloads and can find exploitable bugs
        # the passive checks can't. It runs only when the user asked for it, the
        # server allows it, a daemon is reachable, AND the user confirmed they own
        # the target. Anything short of that falls back to the passive audit — with
        # a note when active was asked for but unavailable, so the weaker result is
        # never returned silently.
        mode = "passive"
        scan_note = ""
        result = None
        if payload.active:
            if not payload.authorized:
                raise HTTPException(
                    400, "Active scanning sends attack traffic — confirm you own or "
                    "are authorized to test this site first.")
            zap = ZapScanner()
            if not settings.zap_allow_active:
                why = "active scanning is switched off on this server"
            else:
                # Wait out a booting daemon rather than degrading to passive on
                # the spot. ZAP takes about a minute to answer after a restart,
                # and "the scan I asked for quietly became a weaker one because
                # I clicked during a deploy" is the single most confusing way
                # this feature fails. Anything that isn't a boot delay comes back
                # immediately — see wait_until_ready.
                code, why = await zap.wait_until_ready(progress=progress)
                why = None if code == "ok" else why
            if why is None:
                try:
                    result = await zap.scan(
                        payload.url, active=True, authorized=True, progress=progress)
                    mode = "active"
                except ZapAuthorizationError as e:
                    raise HTTPException(403, str(e))
                except ScanError as e:
                    raise HTTPException(400, str(e))
                except ZapUnavailableError as e:
                    # Daemon dropped after the availability check — degrade to the
                    # passive audit rather than failing the whole request.
                    logger.warning(f"ZAP became unavailable mid-scan: {e}")
                    scan_note = (
                        "Active scanning stopped being available, so a passive "
                        "configuration audit was run instead.")
            else:
                # Name the reason. "Isn't available" on its own is unfalsifiable
                # from the outside and hides which of several different fixes is
                # the right one — see ZapScanner.availability.
                logger.warning(f"Active scan requested but unavailable: {why}")
                scan_note = (
                    f"Active scanning isn't available right now because {why}. "
                    "A passive configuration audit was run instead.")

        if result is None:
            scanner = SecurityScanner()
            try:
                result = await scanner.scan(payload.url, progress=progress)
            except ScanError as e:
                raise HTTPException(400, str(e))
        elapsed_ms = int((time.monotonic() - started) * 1000)

        summary = ""
        summary_source = "fallback"
        if payload.ai_summary and result.findings:
            await progress.start("plan")
            summary = await _ai_scan_summary(result, tier)
            if summary:
                summary_source = "ai"
        if not summary:
            summary = _fallback_summary(result)
            # Either the model was never asked (a clean site) or it failed and
            # _ai_scan_summary swallowed it. Both land here, and neither should
            # leave a "plan" step spinning — nothing else will close it.
            await progress.start("plan")
        await progress.done("plan", "Action plan ready")

        logger.info(f"Scanned {result.final_url}: grade {result.grade}, "
                    f"{len(result.findings)} findings")

        # Scans were previously not recorded anywhere, so a user's audit history
        # vanished the moment they navigated away. As with generations, a failure
        # to log must not discard the result the user is waiting on.
        await progress.start("save")
        try:
            store.record_scan(
                user_id=ctx["user_id"],
                url=result.final_url or result.url,
                grade=result.grade,
                score=result.score,
                findings=len(result.findings),
                duration_ms=elapsed_ms,
                counts=result.counts,
            )
            await progress.done("save", "Saved to your history")
        except Exception as e:
            logger.warning(f"Could not record scan history: {e}")
            await progress.done("save", "Not saved — your result is unaffected")

        return ScanResponse(
            url=result.url,
            final_url=result.final_url,
            score=result.score,
            grade=result.grade,
            summary=summary,
            counts=result.counts,
            checks_run=result.checks_run,
            findings=result.findings,
            summary_source=summary_source,
            provenance=provenance_report(tier),
            mode=mode,
            scan_note=scan_note,
        ).model_dump()

    return ndjson(work)


# ─────────────────────────────────────────────
# Phase 2: Generate tests (from session)
# ─────────────────────────────────────────────

def _record_generation(session: dict, user_id: str, *, framework: str, language: str,
                       test_count: int = 0, file_count: int = 0,
                       status: str = "success", duration_ms: int | None = None,
                       error: str = "") -> None:
    """Log a generation attempt to the user's history. Never raises.

    History for the dashboard. `jobs` holds the detail but is pruned at 24h, and
    `usage` is only a per-month counter — neither can answer "what have I
    generated?". Bookkeeping must never sink the request either way: on success
    the user's tests are already made and paid for, and on failure they're
    already getting an error that says more than this would.
    """
    try:
        req = session.get("request", {})
        store.record_generation(
            user_id=user_id,
            source=req.get("repo_url") or req.get("zip_name") or "ZIP upload",
            framework=framework,
            language=language,
            test_count=test_count,
            file_count=file_count,
            status=status,
            duration_ms=duration_ms,
            error=error[:500],
        )
    except Exception as e:
        logger.warning(f"Could not record generation history: {e}")


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
    started = time.monotonic()
    start_provenance()
    tier = tier_for_user(ctx["user_id"])

    def elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

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
            tier=tier,
            live_url=payload.live_url,
        )
    except AllProvidersExhausted as e:
        # Our shared API keys are dry — this is an outage on our side and hits
        # Pro users identically, so it must not be dressed up as an upsell.
        lease.refund()
        logger.error(f"Test generation unavailable: {e}")
        # Recorded as failed, not dropped: from the dashboard's side a run that
        # vanished and a run that never happened look identical, and the user
        # who watched this spin for a minute deserves to see it happened.
        _record_generation(
            session, ctx["user_id"], framework=payload.framework.value,
            language=payload.language.value, status="failed",
            duration_ms=elapsed_ms(), error=PROVIDER_OUTAGE_MESSAGE,
        )
        raise HTTPException(503, PROVIDER_OUTAGE_MESSAGE)
    except Exception as e:
        lease.refund()
        logger.error(f"Test generation failed: {e}")
        _record_generation(
            session, ctx["user_id"], framework=payload.framework.value,
            language=payload.language.value, status="failed",
            duration_ms=elapsed_ms(), error=str(e),
        )
        raise HTTPException(500, "Test generation failed. Please try again.")

    lease.commit()

    _record_generation(
        session, ctx["user_id"],
        framework=result.framework,
        language=payload.language.value,
        test_count=result.test_count,
        file_count=len(result.files),
        duration_ms=elapsed_ms(),
    )

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
        # A partial run downloads as a plausible-looking archive that cannot
        # run, so the client has to be able to see it and say so.
        failed_files=result.failed_files,
        grounding=result.grounding.as_dict(),
        selector_grounding=result.selector_grounding,
        fragility=result.fragility,
        # This request's own calls, or — when it reused the SSE stream's output
        # and made none — the provenance recorded when that output was written.
        # Either way it names a model that genuinely produced these files, never
        # one we merely would have used.
        provenance=provenance_report(tier) or session.get("streamed_provenance"),
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

    # Quota is the cap on generations, but the expensive work is the LLM run that
    # happens *here* in the stream — /api/generate only charges the credit
    # afterwards (reusing this stream's cached output). Without this check an
    # exhausted user could stream unlimited real LLM runs and simply never
    # finalise, so the cap has to be enforced before the model is invoked, not
    # only at the charging step. Read-only: the credit is still consumed by
    # /api/generate, so this does not double-charge.
    has_quota, quota_state = has_quota_remaining(ctx["user_id"])

    async def event_generator():
        start_provenance()
        if not has_quota:
            # In-band error (EventSource can't read a 402 status). The client
            # recognises reason="quota_exceeded" and shows the upgrade modal,
            # exactly as it does for the /api/generate 402.
            detail = quota_exceeded_detail(quota_state)
            yield f"data: {json.dumps({'type': 'error', 'message': detail['message'], 'detail': detail})}\n\n"
            return
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
                tier=tier_for_user(ctx["user_id"]),
            ):
                buffer += chunk
                # Send status updates if any are queued (non-blocking)
                while not status_queue.empty():
                    status_msg = status_queue.get_nowait()
                    yield f"data: {json.dumps({'type': 'provider_status', 'message': status_msg})}\n\n"

                yield f"data: {json.dumps({'type': 'chunk', 'content': chunk})}\n\n"

            # Cache the streamed output so /api/generate can reuse it instead of
            # running the whole (expensive) generation a second time.
            #
            # The provenance rides along with it. Without this the model that
            # actually wrote the suite is lost: /api/generate reuses this buffer,
            # makes no call of its own, and so honestly reports "no model ran" —
            # leaving the results screen unable to say what wrote the tests in
            # the one flow the UI actually uses. The cached text and the record
            # of who produced it are the same fact and are stored together.
            store.update(
                job_id,
                streamed_raw=buffer,
                streamed_provenance=provenance_report(tier_for_user(ctx["user_id"])),
            )

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
