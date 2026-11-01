"""Settings, read from environment variables or a .env file."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Hermes Agent ------------------------------------------------------
    # "mock" runs a built-in fake agent so the UI works without Hermes.
    hermes_mode: Literal["mock", "hermes"] = "mock"
    hermes_api_url: str = "http://127.0.0.1:8642"
    hermes_api_key: str = ""
    hermes_model: str = "hermes-agent"
    hermes_timeout_s: float = 300.0
    # Where admins open the full Hermes dashboard (e.g. a Tailscale Serve URL).
    hermes_dashboard_url: str = ""

    # --- App -------------------------------------------------------------
    app_name: str = "Agent API"
    secret_key: str = "change-me-in-production"
    cookie_secure: bool = False  # set true behind HTTPS
    cors_origins: str = "http://localhost:5173"
    data_dir: Path = Path("./data")
    max_upload_mb: int = 10
    # Folders whose files the chat may show and download (comma-separated). Agent output only — never ~ itself.
    file_dirs: str = "~/.hermes/cache/generated,~/agent-workspace"

    # --- Admin login -------------------------------------------------------
    admin_username: str = "admin"
    # Prefer ADMIN_PASSWORD_HASH (python -m app.cli hash-password). ADMIN_PASSWORD is for local dev only.
    admin_password_hash: str = ""
    admin_password: str = ""

    # --- Live avatar (Simli, optional) ------------------------------------------
    # The key stays on the server; browsers get short-lived session tokens from /api/simli/token.
    simli_api_key: str = ""
    simli_api_url: str = "https://api.simli.ai"
    simli_max_session_s: int = 600
    simli_daily_sessions: int = 40  # stops runaway costs; resets at midnight UTC

    # --- Dev helpers ---------------------------------------------------------
    # Return the "Save my work" verification code in the API response instead of emailing it.
    dev_show_codes: bool = True
    mock_delay_s: float = 0.02

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
