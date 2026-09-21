"""Application configuration — loads from environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """CUA system settings. Reads from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Anthropic API (only required for discovery, not replay)
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        description="Anthropic API key for Claude (required only for discovery)",
    )
    anthropic_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Claude model ID for the agent loop",
    )

    # Paths
    capabilities_dir: Path = Field(default=Path("capabilities"))
    evidence_dir: Path = Field(default=Path("evidence"))
    policies_dir: Path = Field(default=Path("policies"))

    # Agent loop defaults
    max_agent_steps: int = Field(default=30, description="Max steps in a discovery run")
    agent_timeout_seconds: int = Field(default=180, description="Max wall time for discovery")

    # Replay defaults
    replay_timeout_seconds: int = Field(default=60, description="Max wall time for replay")
    replay_step_timeout_ms: int = Field(default=10_000, description="Max time per replay step")

    # Fingerprint settings
    fingerprint_match_threshold: float = Field(
        default=0.80,
        description="Minimum similarity score for fingerprint-based locator match",
    )
    fingerprint_assisted_threshold: float = Field(
        default=0.65,
        description="Below match threshold but above this: trigger assisted LLM fallback",
    )

    # Browser
    headless: bool = Field(default=True, description="Run browser in headless mode")
    browser_viewport_width: int = 1_280
    browser_viewport_height: int = 720

    # Observability
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="json", description="json or console")


def get_settings() -> Settings:
    """Load settings from environment."""
    return Settings()
