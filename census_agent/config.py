"""Application configuration. Every value comes from the environment (or .env locally).

Secrets never have defaults. Non-secret operational knobs do, so a fresh deploy with only the
secrets set behaves sensibly.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = Field(default="", description="Anthropic API key")
    agent_model: str = "claude-opus-5"
    classifier_model: str = "claude-haiku-4-5"
    agent_effort: Literal["low", "medium", "high"] = "low"

    snowflake_account: str = ""
    snowflake_user: str = "CENSUS_APP"
    snowflake_private_key: str = Field(
        default="", description="PEM contents of the RSA private key (deployed environments)"
    )
    snowflake_private_key_path: str = Field(
        default="keys/rsa_key.p8", description="Path to the PEM private key (local dev)"
    )
    snowflake_role: str = "CENSUS_READER"
    snowflake_warehouse: str = "CENSUS_WH"
    snowflake_census_database: str = Field(
        default="", description="Name the Marketplace share mounted as; discovered if empty"
    )
    snowflake_app_database: str = "CENSUS_APP_DB"
    snowflake_app_schema: str = "SEMANTIC"
    snowflake_statement_timeout_s: int = 20

    field_search_backend: Literal["cortex", "local"] = "local"
    classifier_enabled: bool = True
    grounding_enabled: bool = True
    speculative_execution: bool = True
    simulate_failures_enabled: bool = True

    turn_wall_clock_s: float = 45.0
    max_tool_calls: int = 6
    max_result_rows: int = 5000
    max_input_chars: int = 2000
    max_concurrent_turns: int = 8
    session_ttl_s: int = 7 * 24 * 3600

    demo_user: str = "reviewer"
    demo_password: str = Field(default="", description="Empty disables basic auth (local dev)")

    log_level: str = "INFO"
    trace_dump_enabled: bool = False
    git_sha: str = Field(default_factory=lambda: os.environ.get("GIT_SHA", "dev"))

    @property
    def snowflake_configured(self) -> bool:
        return bool(self.snowflake_account and (self.snowflake_private_key or self.snowflake_private_key_path))

    @property
    def anthropic_configured(self) -> bool:
        return bool(self.anthropic_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
