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

    # Google OAuth ("Continue with Google" → email/profile login only).
    # Create credentials at https://console.cloud.google.com/apis/credentials and
    # set the authorized redirect URI to  <backend>/api/auth/google/callback
    google_client_id: str = ""
    google_client_secret: str = ""

    # Where the browser is sent back to after login (your frontend origin).
    frontend_url: str = "http://localhost:5173"
    # Public base URL of THIS backend, used to build the OAuth callback URL.
    backend_url: str = "http://localhost:8000"
    # How long a login session (stored access token) stays valid.
    oauth_session_ttl_seconds: int = 8 * 60 * 60

    # Limits
    max_repo_size_mb: int = 100
    max_files_per_repo: int = 2000
    token_budget: int = 600_000
    job_ttl_seconds: int = 24 * 60 * 60

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
    def oauth_callback_url(self) -> str:
        return f"{self.backend_url.rstrip('/')}/api/auth/github/callback"

    @property
    def google_oauth_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def google_oauth_callback_url(self) -> str:
        return f"{self.backend_url.rstrip('/')}/api/auth/google/callback"


settings = Settings()
