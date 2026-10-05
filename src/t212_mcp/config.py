from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
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


class Settings(DatabaseSettings):
    api_key: SecretStr
    # Optional: keys created before Trading 212 introduced secrets authenticate with the key alone.
    api_secret: SecretStr | None = None
    env: Literal["live", "demo"] = "live"
    data_dir: Path = Path.home() / ".t212_mcp"  # logs from the scheduled refresh
    # Required by --http when binding to anything other than localhost.
    mcp_auth_token: SecretStr | None = None

    # Look-through agent, backed by OpenAI. llm_model is an OpenAI model name; the key is read
    # from OPENAI_API_KEY (in .env or the environment).
    llm_model: str = "gpt-6.1-sol"
    reasoning_effort: str | None = "high"  # passed to OpenAI; None leaves the model's default
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    llm_kwargs: dict = {}
    search_provider: Literal["duckduckgo", "tavily", "brave"] = "duckduckgo"
    agent_token_budget: int = 400_000
    agent_recursion_limit: int = 40
    max_repair_attempts: int = 3
    max_agent_runs_per_day: int = 5

    @property
    def base_url(self) -> str:
        return BASE_URLS[self.env]
