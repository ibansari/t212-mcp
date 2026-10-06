import os
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

BASE_URLS = {
    "live": "https://live.trading212.com",
    "demo": "https://demo.trading212.com",
}


DEFAULT_DATABASE_URL = "postgresql+psycopg://t212:t212@localhost:5432/t212"


class DatabaseSettings(BaseSettings):
    """Just the database, so tests can find it without Trading 212 credentials."""

    model_config = SettingsConfigDict(
        env_prefix="T212_", env_file=PROJECT_ROOT / ".env", extra="ignore"
    )

    database_url: str = DEFAULT_DATABASE_URL

    @field_validator("database_url")
    @classmethod
    def _use_psycopg(cls, url: str) -> str:
        """Hosted Postgres (Railway, Neon, ...) hands out postgres:// URLs; SQLAlchemy needs the psycopg driver named."""
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url


class Settings(DatabaseSettings):
    api_key: SecretStr
    # Optional: keys created before Trading 212 introduced secrets authenticate with the key alone.
    api_secret: SecretStr | None = None
    env: Literal["live", "demo"] = "live"
    data_dir: Path = Path.home() / ".t212_mcp"  # logs from the scheduled refresh
    # HTTP auth (one is required when binding to anything other than localhost).
    # GitHub sign-in: a GitHub OAuth App, the accounts allowed in (comma-separated usernames or numeric user IDs), and
    # the server's public URL (defaults to https://$RAILWAY_PUBLIC_DOMAIN on Railway).
    github_client_id: str | None = None
    github_client_secret: SecretStr | None = None
    allowed_github_users: str = ""
    public_url: str | None = None
    # Or a static bearer token (ignored when GitHub sign-in is configured).
    mcp_auth_token: SecretStr | None = None

    # Morning digest email (t212-mcp send-digest), sent through Resend. digest_to is comma-separated.
    resend_api_key: SecretStr | None = None
    digest_to: str | None = None
    digest_from: str = "Portfolio digest <onboarding@resend.dev>"
    # The daily set time: the run at this hour saves the baseline that the next day's changes are measured against.
    digest_hour: int = 7
    digest_tz: str = "Europe/London"

    # Look-through agent, backed by OpenAI. llm_model is an OpenAI model name; the key is read
    # from OPENAI_API_KEY (in .env or the environment).
    llm_model: str = "gpt-6.1-sol"
    reasoning_effort: str | None = "high"  # passed to OpenAI; None leaves the model's default
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    llm_kwargs: dict = {}
    openfigi_api_key: SecretStr | None = None  # optional; raises OpenFIGI's rate limit for entity resolution
    search_provider: Literal["duckduckgo", "tavily", "brave"] = "duckduckgo"
    agent_token_budget: int = 400_000
    agent_recursion_limit: int = 40
    max_repair_attempts: int = 3
    max_agent_runs_per_day: int = 5

    @field_validator("api_secret", "github_client_id", "github_client_secret", "public_url", "mcp_auth_token",
                     "openai_api_key", "resend_api_key", "digest_to", "openfigi_api_key", mode="before")
    @classmethod
    def _blank_is_unset(cls, v):
        """`KEY=` in .env means not set, not an empty secret."""
        return None if isinstance(v, str) and not v.strip() else v

    @property
    def base_url(self) -> str:
        return BASE_URLS[self.env]

    @property
    def allowed_github_user_set(self) -> set[str]:
        return {u.strip().lstrip("@").lower() for u in self.allowed_github_users.split(",") if u.strip()}

    @property
    def public_base_url(self) -> str | None:
        if self.public_url:
            return self.public_url.rstrip("/")
        domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
        return f"https://{domain}" if domain else None
