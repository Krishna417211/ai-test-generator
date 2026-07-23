"""
schemas.py — Pydantic models for all API request/response shapes
"""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, field_validator


class TestFramework(str, Enum):
    PLAYWRIGHT = "playwright"
    CYPRESS = "cypress"
    SELENIUM = "selenium"


class Language(str, Enum):
    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"
    PYTHON = "python"
    JAVA = "java"


# ── Requests ─────────────────────────────────

class GenerateRequest(BaseModel):
    repo_url: Optional[str] = None          # GitHub URL (mutually exclusive with zip)
    github_token: Optional[str] = None      # PAT for private repos
    framework: TestFramework = TestFramework.PLAYWRIGHT
    language: Language = Language.TYPESCRIPT
    test_flows: str = ""                     # user's description of flows to test
    base_url: str = "http://localhost:3000"
    include_ci: bool = True
    self_heal: bool = False                  # re-prompt LLM to fix invalid generated files
    # A deployed URL the user owns. When present, selectors are also verified
    # against the live DOM (services/grounding.py) and healed if they miss.
    live_url: Optional[str] = None

    @field_validator("repo_url")
    @classmethod
    def validate_github_url(cls, v):
        if v and "github.com" not in v:
            raise ValueError("Only GitHub URLs are supported.")
        return v


class StatusRequest(BaseModel):
    pass


# ── Auth ─────────────────────────────────────

class SignupRequest(BaseModel):
    email: str
    password: str
    name: Optional[str] = None


class LoginRequest(BaseModel):
    email: str
    password: str


class EmailRequest(BaseModel):
    """Shared by resend-verification and forgot-password: an address is all
    either one needs, and both answer identically whether or not it exists."""
    email: str


class TokenRequest(BaseModel):
    """A one-time link token from an email (verify-email)."""
    token: str


class ResetPasswordRequest(BaseModel):
    token: str
    password: str


class VerifyOtpRequest(BaseModel):
    challenge_id: str
    code: str


class ChangePasswordRequest(BaseModel):
    """The current password is required even though the caller already holds a
    session: a session proves the tab is logged in, not that the person at the
    keyboard is the owner. Without it, an unattended screen is a password
    change."""
    current_password: str
    new_password: str


class DeleteAccountRequest(BaseModel):
    """`confirm` must echo the account's own email back — the same shape as the
    repo-deletion guard, and the reason is the same: make an irreversible,
    unprompted click impossible."""
    confirm: str


class UserSettings(BaseModel):
    """Form defaults for Generate/Publish. Every field optional: PUT /api/settings
    patches, so omitting one leaves it alone rather than resetting it."""
    framework: Optional[TestFramework] = None
    language: Optional[Language] = None
    base_url: Optional[str] = None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, v):
        if v is None:
            return v
        v = v.strip()
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("Base URL must start with http:// or https://")
        return v


class UserSettingsResponse(BaseModel):
    """Always fully populated — the store resolves unset fields to defaults."""
    framework: str
    language: str
    base_url: str


class UserPublic(BaseModel):
    id: str
    email: Optional[str] = None
    name: Optional[str] = None
    github_login: Optional[str] = None
    avatar_url: Optional[str] = None
    has_github: bool = False
    # Whether *this session* holds a usable GitHub access token (i.e. publishing
    # will work right now). Set by /api/auth/me only — a login response can't
    # know it, and the account-level has_github above doesn't imply it.
    github_connected: bool = False
    email_verified: bool = True
    plan: str = "free"
    # Whether to render the Admin nav item. Not a permission — see
    # services/admin.py; the routes check the allowlist themselves.
    is_admin: bool = False


class AuthResponse(BaseModel):
    token: str
    user: UserPublic


class LoginResponse(BaseModel):
    """Login is no longer a single step, so the client has to be told which of
    three places it landed in rather than just getting a token or an error.

    `status`:
      ok                    — authenticated; `token` and `user` are set.
      otp_required          — password was right, a code is in their inbox;
                              continue at /api/auth/login/verify-otp with
                              `challenge_id`.
      verification_required — the address was never confirmed; a fresh link has
                              been sent. No token is issued.

    `token`/`user` are populated only for "ok", so a client that ignores
    `status` and reads `token` gets nothing usable rather than a half-session.
    """
    status: str
    token: Optional[str] = None
    user: Optional[UserPublic] = None
    challenge_id: Optional[str] = None
    email_hint: Optional[str] = None          # masked: a**@example.com
    expires_in: Optional[int] = None          # seconds the OTP stays valid
    message: Optional[str] = None


class SimpleResponse(BaseModel):
    ok: bool = True
    message: str = ""


# ── Responses ────────────────────────────────

class FilePreview(BaseModel):
    path: str
    size: int
    importance: int
    preview: str                            # first 200 chars


class ProjectAnalysis(BaseModel):
    project_summary: str
    framework: str
    key_pages: list[dict]
    key_components: list[dict]
    routes: list[str]
    testing_challenges: list[str]
    file_count: int
    total_tokens: int
    file_previews: list[FilePreview]


class GeneratedFile(BaseModel):
    filename: str
    content: str
    description: str
    size: int


# ── How an output was produced, and what we verified about it ──
#
# These two shapes are shared by all three flows so "how much can I trust this,
# and what wrote it?" is answered the same way everywhere.

class ModelUsage(BaseModel):
    provider: str
    model: str
    calls: int
    total_tokens: Optional[int] = None       # None = provider didn't report usage
    share: float


class Provenance(BaseModel):
    """Which model(s) actually produced this output.

    Reports the model, never the key. Which numbered key served a call is an
    operations detail (admin console → get_key_health); on a user-facing
    response it would leak the key pool's shape for no user benefit.
    """
    calls: int
    primary: dict                            # {"provider": ..., "model": ...}
    mixed: bool                              # rotation split the work across models
    models: list[ModelUsage] = []
    # The model a paid plan would have used, or None when upgrading would change
    # nothing. A fact about the model table — never a reaction to how this run
    # scored, and never set when capacity ran out (that hits paid users too).
    upgrade_model: Optional[str] = None


class Grounding(BaseModel):
    """What we verified about a generated suite. NOT an accuracy score.

    We never execute the generated tests (that needs Docker + a live target —
    see services/validator.py), so we cannot know whether they pass. These are
    the two things we do check, reported under their own names:
    selectors checked back against the user's real source, and code files a
    parser actually read. `*_rate` is None when there was nothing to measure —
    an empty suite has not earned a 100%.
    """
    selectors_total: int = 0
    selectors_verified: int = 0
    selector_rate: Optional[float] = None
    files_checked: int = 0
    files_valid: int = 0
    file_rate: Optional[float] = None
    heal_attempts: int = 0


class GenerateResponse(BaseModel):
    success: bool
    files: list[GeneratedFile]
    test_count: int
    framework: str
    selector_warnings: list[str]
    summary: str
    validation: list[dict] = []              # per-file syntax-validity results
    # Planned files the model never produced — a partial run. The publish path
    # has always warned about these; the download path used to drop them, so a
    # suite missing every spec file downloaded looking exactly like a whole one.
    failed_files: list[str] = []
    grounding: Optional[Grounding] = None
    # Richer grounding with per-selector provenance (file:line) and, when a live
    # URL was given, live-DOM verification. See services/grounding.py.
    selector_grounding: Optional[dict] = None
    # Break-risk report: which selectors are likely to be flaky. services/fragility.py.
    fragility: Optional[dict] = None
    provenance: Optional[Provenance] = None
    error: Optional[str] = None


class PublishResponse(BaseModel):
    success: bool
    repo_url: str
    full_name: str
    branch: str
    commit_sha: str
    files_pushed: int
    cicd_added: bool
    test_count: int = 0
    all_valid: bool = True                   # did every generated test file pass validation
    validation: list[dict] = []              # per-file syntax-validity results
    grounding: Optional[Grounding] = None
    provenance: Optional[Provenance] = None
    warnings: list[str] = []


class ScanRequest(BaseModel):
    url: str
    ai_summary: bool = True
    # Active scanning sends real attack traffic (via OWASP ZAP) and can find
    # exploitable bugs the passive checks never can. It only runs when the server
    # has ZAP configured AND the user confirms they own the target — both are
    # required, and the passive scan is the default.
    active: bool = False
    authorized: bool = False


class ScanResponse(BaseModel):
    url: str
    final_url: str
    score: int
    grade: str
    summary: str = ""
    counts: dict
    checks_run: int
    findings: list[dict] = []
    # Which engine produced this: "passive" (configuration audit) or "active"
    # (ZAP sent payloads). The UI frames the result differently for each.
    mode: str = "passive"
    # Set when active was requested but couldn't run (ZAP not configured/reachable)
    # and the scan fell back to passive — so the UI can say so instead of silently
    # returning a weaker result than was asked for.
    scan_note: str = ""
    # Scan reports its trust differently from generate, because the flows differ
    # in kind. The findings are deterministic — a header is present or it isn't,
    # so there is no rate to quote and inventing one would be noise. The only
    # part that varies is the prioritised plan, hence: did a model write it, or
    # is this the deterministic fallback? A user acting on the plan deserves to
    # know which they're reading.
    summary_source: str = "fallback"         # "ai" | "fallback"
    provenance: Optional[Provenance] = None


class ProviderStatus(BaseModel):
    name: str
    total_keys: int
    available_keys: int
    total_calls: int
    healthy: bool


class StatusResponse(BaseModel):
    providers: list[ProviderStatus]
    call_log: list[dict]


# ── Admin console ────────────────────────────
#
# Every model below is served only behind services.admin.require_admin. They
# carry fields UserPublic deliberately withholds (suspension state, live session
# count, usage) — that's the point of the console, but it also means none of
# these may ever be returned from a non-admin route.

class AdminUser(BaseModel):
    id: str
    email: Optional[str] = None
    name: Optional[str] = None
    github_login: Optional[str] = None
    avatar_url: Optional[str] = None
    created_at: float
    plan: str = "free"                       # effective plan (a lapsed Pro reads free)
    plan_expires_at: Optional[float] = None
    email_verified: bool = False
    suspended: bool = False
    suspended_at: Optional[float] = None
    suspended_reason: Optional[str] = None
    is_admin: bool = False
    generations: int = 0
    scans: int = 0
    usage_this_period: int = 0


class AdminUserList(BaseModel):
    users: list[AdminUser]
    total: int                               # matching the query, not the page
    limit: int
    offset: int


class AdminUserDetail(BaseModel):
    user: AdminUser
    quota: dict
    totals: dict[str, int]
    active_sessions: int
    recent_generations: list[dict]
    recent_scans: list[dict]
    published_repos: list[dict]


class AdminSetPlanRequest(BaseModel):
    plan: str                                # "free" | "pro"
    # Days from now the grant lapses; omit for a plan that never expires.
    # A comped Pro with no expiry is forever, so the UI defaults to setting one.
    expires_in_days: Optional[int] = None
    reason: Optional[str] = None

    @field_validator("plan")
    @classmethod
    def validate_plan(cls, v):
        v = (v or "").strip().lower()
        if v not in {"free", "pro"}:
            raise ValueError("Plan must be 'free' or 'pro'.")
        return v

    @field_validator("expires_in_days")
    @classmethod
    def validate_expiry(cls, v):
        if v is not None and not (1 <= v <= 3650):
            raise ValueError("Expiry must be between 1 and 3650 days.")
        return v


class AdminSuspendRequest(BaseModel):
    suspended: bool
    reason: Optional[str] = None


class AdminSetUsageRequest(BaseModel):
    count: int

    @field_validator("count")
    @classmethod
    def validate_count(cls, v):
        if v < 0 or v > 1_000_000:
            raise ValueError("Usage count must be between 0 and 1,000,000.")
        return v


class AdminOverview(BaseModel):
    totals: dict[str, int]
    series: list[dict]                       # per-day, zero-filled
    frameworks: list[dict]
    languages: list[dict]
    recent_failures: list[dict]


class AdminSystem(BaseModel):
    app_env: str
    uptime_seconds: float
    jobs_stored: int
    providers: list[ProviderStatus]
    keys: list[dict]                         # per-key health, incl. cooldowns
    call_log: list[dict]
    config: dict                             # feature switches — never secrets
