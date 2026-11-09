from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime settings. Read from the environment and `.env`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///data/demo/chinook.sqlite"
    anthropic_api_key: str | None = None

    model: str = "claude-haiku-5-5"
    effort: str = "medium"
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 3

    row_limit: int = Field(500, description="LIMIT the guard puts on the outermost query")
    statement_timeout_ms: int = 5000
    max_attempts: int = Field(3, description="SQL attempts per question, first try included")
    max_link_tables: int = 6
    clarify: bool = True

    spend_cap_usd: float = 5.00
    data_dir: Path = Path("data")
    llm_cache: bool = True
    log_level: str = "INFO"

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "spend.jsonl"

    @property
    def cache_path(self) -> Path:
        return self.data_dir / "llm_cache.sqlite"


@lru_cache
def get_settings() -> Settings:
    return Settings()
