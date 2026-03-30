"""Application configuration."""

from __future__ import annotations

from functools import lru_cache
from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "WBA Control Plane"
    version: str = "0.1.0"
    database_url: str = "postgresql+asyncpg://wba:wba@localhost:5432/wba_monitor"
    api_prefix: str = "/api/v1"
    agent_api_tokens: list[str] = []
    dashboard_api_tokens: list[str] = []

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    @field_validator("agent_api_tokens", "dashboard_api_tokens", mode="before")
    @classmethod
    def _split_tokens(cls, value: object) -> list[str]:
        if isinstance(value, str):
            return [token.strip() for token in value.split(",") if token.strip()]
        if value is None:
            return []
        return list(value)


@lru_cache()
def get_settings() -> Settings:
    return Settings()


