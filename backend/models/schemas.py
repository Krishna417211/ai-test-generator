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

class SiteLogin(BaseModel):
    """Credentials for the app under test, so the crawl can get past its sign-in.

    Most apps worth testing put their content behind a login: an anonymous crawl
    of one bounces off the wall and grounds the whole suite on a single form. The
    selectors are optional — the common field shapes are auto-detected — and are
    the escape hatch for a form that isn't one of them.

    These are the user's credentials for their own site. They are used for the
    one crawl and never stored: `main.analyze_crawl` strips them before the job
    is persisted, and `LoginSpec.__repr__` keeps the password out of logs.
    """
    url: str = ""                            # sign-in page; relative to the site root is fine
    username: str = ""
    password: str = ""
    username_selector: str = ""
    password_selector: str = ""
    submit_selector: str = ""

    @property
    def is_set(self) -> bool:
        return bool(self.username and self.password)


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
    # Optional sign-in for the crawl. Without it, an auth-guarded app exposes
    # only its login screen to the crawler.
    site_login: Optional[SiteLogin] = None

    @field_validator("repo_url")
    @classmethod
    def validate_github_url(cls, v):
        if v and "github.com" not in v:
            raise ValueError("Only GitHub URLs are supported.")
        return v


class StatusRequest(BaseModel):
    pass


class CrawlPreviewRequest(BaseModel):
    """A single deployed URL to render-and-crawl on its own, so the user can see
    whether the live crawler works before spending a generation credit."""
    url: str
    site_login: Optional[SiteLogin] = None


# ── Responses (live-crawl preview) ────────────

class CrawledRoute(BaseModel):
    path: str                                # the route's path, e.g. "/login"
    n_anchors: int                           # anchors this page contributed


class CrawlAnchor(BaseModel):
    kind: str                                # id | testid | role | text | ...
    value: str


class CrawlPreviewResponse(BaseModel):
    """The observable result of a standalone live crawl.

    `status` tells the UI what actually happened, honestly:
      ok          — crawl rendered enough to ground a suite against.
      thin        — it rendered but found too few anchors; a real run would fall
                    back to source-only grounding (so we say so, not "success").
      unavailable — no headless browser on the server (CrawlUnavailable).
      blocked     — the URL is private/out-of-scope (the SSRF/scope guard).
      login_failed— credentials were given but the app rejected them, so the
                    crawl would only have seen the sign-in page.
      error       — the crawl couldn't complete (unreachable host, etc.).
    """
    status: str
    message: str = ""
    seed: Optional[str] = None
    n_pages: int = 0
    n_anchors: int = 0
    # Whether this index would actually be used for grounding (status == "ok").
    useful: bool = False
    pages: list[CrawledRoute] = []
    # Routes discovered but not visited because the page cap was hit.
    unvisited: list[str] = []
    sample_anchors: list[CrawlAnchor] = []
    elapsed_ms: int = 0


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
    # Bring-your-own-key. None leaves the stored key alone (so saving other
    # settings never clears it by omission); "" clears it deliberately.
    gemini_api_key: Optional[str] = None

    @field_validator("gemini_api_key")
    @classmethod
    def validate_gemini_key(cls, v):
        if v is None:
            return v
        v = v.strip()
        if not v:
            return ""          # explicit clear
        # Shape check only — whether it WORKS is settled by a probe call in the
        # endpoint, because a well-formed key can still be revoked. Catching the
        # obvious paste error here gives a better message than Google's 400.
        if not v.startswith("AIza"):
            raise ValueError(
                "That doesn't look like a Google AI Studio key — they begin with "
                "'AIza'. Check you haven't pasted a key for a different provider."
            )
        if len(v) < 30:
            raise ValueError("That key looks too short to be a Gemini API key.")
        return v

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
    """Always fully populated — the store resolves unset fields to defaults.

    Note what is absent: the user's Gemini key. This model is what /api/settings
    returns, and a write-only credential must not have a field on the shape that
    comes back — otherwise one day something assigns to it. Only the mask and the
    boolean travel outward.
    """
    framework: str
    language: str
    base_url: str
    # "AIza…9f2k" — enough to recognise which key is saved, never enough to use.
    gemini_key_hint: str = ""
    has_gemini_key: bool = False


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


# ── CLI device-code login (RFC 8628) ─────────
# The CLI cannot own a browser redirect: it may be running over SSH on a box with
# no browser at all. So it asks for a short code, the user approves that code in
# a browser anywhere (phone included), and the CLI polls until it is approved.
# The user's password never passes through the CLI.

class CliAuthStartResponse(BaseModel):
    """What the CLI needs to show the user, and to poll with."""
    # The secret the CLI holds. Long and random: possession of this is what
    # entitles a caller to collect the session token, so it is never displayed.
    device_code: str
    # The short code the human reads out and types. Deliberately not a secret —
    # it is designed to be shown on a screen and typed into another device.
    user_code: str
    verification_uri: str
    # Same page with the code pre-filled, so the common case is one click.
    verification_uri_complete: str
    expires_in: int
    # Seconds the client must wait between polls. Honoured by our CLI; enforced
    # by the rate limiter for clients that don't.
    interval: int


class CliAuthApproveRequest(BaseModel):
    user_code: str


class CliAuthTokenRequest(BaseModel):
    device_code: str


class CliAuthTokenResponse(BaseModel):
    """Poll result.

    `status`:
      pending   — not approved yet; keep polling after `interval`
      approved  — `token` and `user` are set. Returned exactly once.
      denied    — the user rejected it; stop polling
      expired   — the code timed out (or was already collected); start over
    """
    status: str
    token: Optional[str] = None
    user: Optional[UserPublic] = None


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


class ScoreComponent(BaseModel):
    key: str
    label: str
    passed: int
    total: int
    rate: float
    weight: float
    detail: str = ""


class SuccessRate(BaseModel):
    """The share of our automated checks a generated suite passed.

    Distinct from `Grounding`, which reports individual measurements: this
    aggregates them into the single number the UI leads with. It is a
    *generation* success rate — planned files delivered, code parsed, selectors
    traced, tests produced — and explicitly not a prediction that the tests pass
    against the running app, which we never execute. `measures` and
    `not_measured` carry that distinction in the payload so the caveat travels
    with the number. See services/success_rate.py.
    """
    score: Optional[int] = None              # 0–100; None when nothing was measurable
    grade: str = "—"
    measured: bool = True
    components: list[ScoreComponent] = []
    measures: str = ""
    not_measured: str = ""


class ExcludedSecret(BaseModel):
    path: str
    reason: str
    kind: str                                # "path" | "content"


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
    # The single headline number, aggregated from validation + grounding +
    # completeness. See services/success_rate.py for what it does and does not
    # claim.
    success_rate: Optional[SuccessRate] = None
    # Credential files held back before anything was sent to the model.
    excluded_secrets: list[ExcludedSecret] = []
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
    success_rate: Optional[SuccessRate] = None
    # Credential files that were withheld from the push. Named, not just
    # counted: a user whose .env silently vanished will debug a missing-config
    # error we caused.
    excluded_secrets: list[ExcludedSecret] = []
    # True when the pushed tree was read back from GitHub and every expected
    # file was found with the exact content we uploaded. False means the check
    # itself couldn't complete — `verification_note` says why. A genuinely
    # missing file fails the publish outright rather than arriving here.
    files_verified: bool = False
    verification_note: str = ""
    # Files that needed a second commit before they appeared in the repo.
    repaired_files: list[str] = []
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
    # Keys that are out of credit or have burned a daily quota. Distinct from a
    # 429 cooldown: waiting does not fix these, so a status page that lumps them
    # in with "available" tells the user to keep waiting for a provider that is
    # never coming back on its own.
    hard_blocked_keys: int = 0
    # No successful call since this process started. Key state is in memory, so
    # after a restart every provider reads healthy until something actually
    # tries it — this says "unproven" rather than letting the UI imply "good".
    never_used: bool = True


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
