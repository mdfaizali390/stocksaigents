from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings.

    All API keys are optional at load time — agents enforce their own keys
    via ``require_*`` helpers below when actually called. This lets us run
    parts of the system (e.g. cache, yfinance-only flows) without every
    secret configured.
    """

    anthropic_api_key: str | None = None
    finnhub_api_key: str | None = None
    trading_mode: Literal["dry_run", "live"] = "dry_run"
    log_level: str = "INFO"
    cache_dir: str = "./.cache"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def require_anthropic(self) -> str:
        if not self.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required for this operation")
        return self.anthropic_api_key

    def require_finnhub(self) -> str:
        if not self.finnhub_api_key:
            raise RuntimeError("FINNHUB_API_KEY is required for this operation")
        return self.finnhub_api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
