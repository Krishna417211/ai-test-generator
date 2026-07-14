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

    @property
    def github_oauth_enabled(self) -> bool:
        return bool(self.github_client_id and self.github_client_secret)

    @property
    def billing_enabled(self) -> bool:
        """True once at least one hosted checkout link is configured."""
        return bool(self.billing_checkout_url_monthly or self.billing_checkout_url_yearly)

    @property
    def oauth_callback_url(self) -> str:
        return f"{self.backend_url.rstrip('/')}/api/auth/github/callback"


settings = Settings()
