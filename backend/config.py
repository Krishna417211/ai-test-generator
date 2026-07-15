"""
config.py — Typed application settings.

All configuration is read from the environment (and backend/.env) in one place
instead of scattered os.getenv calls. Import `settings` anywhere you need config.
"""

from functools import cached_property

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_env: str = "development"
    log_level: str = "INFO"

    # CORS — comma-separated list of allowed origins, or "*" for all.
    cors_origins: str = "*"

    # GitHub OAuth ("Login with GitHub" → push on the user's behalf).
    # Create an OAuth App at https://github.com/settings/developers and set the
    # callback URL to  <backend>/api/auth/github/callback
    github_client_id: str = ""
    github_client_secret: str = ""

    # Where the browser is sent back to after login (your frontend origin).
    frontend_url: str = "http://localhost:5173"
    # Public base URL of THIS backend, used to build the OAuth callback URL.
    backend_url: str = "http://localhost:8000"
    # How long a login session (stored access token) stays valid.
    oauth_session_ttl_seconds: int = 8 * 60 * 60

    # Secret used to encrypt sensitive session data (e.g. the linked GitHub
    # access token) at rest. Set a long random value in production; if empty,
    # tokens are stored as-is (fine for local dev). Rotating it invalidates
    # previously-encrypted tokens, so users would need to re-connect GitHub.
    session_secret: str = ""

    # ── Email (verification, password reset, login OTP) ──
    # Any SMTP provider: Gmail with an app password, SendGrid, Mailtrap, SES.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""                 # falls back to smtp_user
    smtp_from_name: str = "Testra"
    # STARTTLS (port 587) is the common case; set smtp_ssl for implicit TLS (465).
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    smtp_timeout: int = 15

    # Email round-trips can be turned off independently. Both default on, but
    # neither can work without SMTP configured — see email_enabled.
    require_email_verification: bool = True
    login_otp_enabled: bool = True

    # How long emailed secrets stay valid. The OTP window is short because a
    # 6-digit code is only ~1M guesses; the link tokens are 256-bit and can
    # afford to live longer.
    email_verify_ttl_seconds: int = 24 * 60 * 60
    password_reset_ttl_seconds: int = 60 * 60
    login_otp_ttl_seconds: int = 10 * 60
    login_otp_max_attempts: int = 5

    # ── Admin ──
    # Comma-separated emails that get the admin console. Privilege lives here,
    # in the environment, rather than in a users.role column on purpose: there is
    # then no row an attacker can write to make themselves an admin, no
    # promote-user endpoint to abuse, and no way for a SQL-injection or a stolen
    # session to escalate. Changing who is an admin is a deploy, which is the
    # right amount of friction for the one privilege that can read every account.
    admin_emails: str = ""

    # Limits
    max_repo_size_mb: int = 100
    max_files_per_repo: int = 2000
    token_budget: int = 600_000
    job_ttl_seconds: int = 24 * 60 * 60

    # ── Plans & metering ──
    # Test generations a free account gets per calendar month. Pro is unmetered.
    # This is a *per-user* cap and is unrelated to the shared LLM provider keys
    # running dry — those exhaust for everyone, paid included, and must never
    # trigger an upgrade prompt.
    free_generations_per_month: int = 5
    pro_price_monthly_usd: int = 20
    pro_price_yearly_usd: int = 100

    # Hosted checkout links from a payment provider (e.g. Razorpay Payment Pages
    # or Stripe Payment Links). Leave empty until one is set up — the upgrade
    # modal then shows a "contact us" path instead of a dead button.
    # A provider is required, not optional: entitlements are granted by a signed
    # webhook hitting /api/billing/webhook. A bare UPI/GPay deep link cannot tell
    # this server who paid, so it can never unlock anything.
    billing_checkout_url_monthly: str = ""
    billing_checkout_url_yearly: str = ""
    billing_webhook_secret: str = ""
    billing_contact_email: str = ""

    @cached_property
    def cors_origin_list(self) -> list[str]:
        raw = (self.cors_origins or "*").strip()
        if raw == "*":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}

    # A plain property, not cached_property like cors_origin_list: splitting a
    # short string costs nothing next to the DB reads an admin request already
    # does, and a cache here would mean tests (and a live settings edit) could
    # not change the allowlist without reaching into __dict__.
    @property
    def admin_email_list(self) -> list[str]:
        return [e.strip().lower() for e in (self.admin_emails or "").split(",") if e.strip()]

    def is_admin_email(self, email: str | None) -> bool:
        """Whether an address is on the admin allowlist.

        Compared lowercased because that's how users.email is stored (see
        auth.validate_email) — an admin who typed their address with capitals in
        .env must still match the row they signed up with.
        """
        if not email:
            return False
        return email.strip().lower() in self.admin_email_list

    @property
    def admin_enabled(self) -> bool:
        return bool(self.admin_email_list)

    @property
    def github_oauth_enabled(self) -> bool:
        return bool(self.github_client_id and self.github_client_secret)

    @property
    def smtp_configured(self) -> bool:
        """True once a real SMTP relay is set up."""
        return bool(self.smtp_host and self.mail_from_address)

    @property
    def mail_from_address(self) -> str:
        return self.smtp_from or self.smtp_user

    @property
    def email_enabled(self) -> bool:
        """Whether this server can get an email to a user *somehow*.

        Outside production a missing relay is not fatal: services/mailer.py logs
        the message instead, so a fresh clone can run the whole signup → verify →
        OTP flow with no SMTP account. In production only a real relay counts —
        the console fallback there would mean codes in the logs and, worse, a
        silent downgrade of the security this module exists to provide.
        """
        return self.smtp_configured or not self.is_production

    @property
    def billing_enabled(self) -> bool:
        """True once at least one hosted checkout link is configured."""
        return bool(self.billing_checkout_url_monthly or self.billing_checkout_url_yearly)

    @property
    def oauth_callback_url(self) -> str:
        return f"{self.backend_url.rstrip('/')}/api/auth/github/callback"


settings = Settings()
