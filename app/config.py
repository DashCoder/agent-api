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
    hermes_home: Path = Path("~/.hermes")   # read for the model Hermes is set to use (shown on Model & tools)
    hermes_timeout_s: float = 300.0
    # Where admins open the full Hermes dashboard (e.g. a Tailscale Serve URL).
    hermes_dashboard_url: str = ""
    # How agent-api restarts Hermes after the admin changes its model on Model & tools.
    hermes_restart_cmd: str = "~/.local/bin/hermes gateway restart"

    # --- Models the agents can use (Model & tools; see app/llm.py) ---------------------------
    ollama_url: str = "http://127.0.0.1:11434"
    openrouter_api_key: str = ""
    # OpenRouter traces in Agent logs (see app/openrouter_traces.py): pull from the relay, or accept pushes
    openrouter_relay_url: str = ""
    openrouter_relay_token: str = ""
    openrouter_relay_interval_s: int = 30
    openrouter_traces_token: str = ""
    bedrock_access_key_id: str = ""
    bedrock_secret_access_key: str = ""
    lambda_access_key_id: str = ""        # fallback AWS keys, as in the dashboard
    lambda_secret_access_key: str = ""
    bedrock_region: str = "eu-west-1"
    llm_timeout_s: float = 180.0

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

    # --- Plugins ------------------------------------------------------------------
    plugins: str = ""   # comma-separated plugin names (or module:attr) to switch on; see app/plugins.py
    mcp_key: str = ""   # Hermes sends this as a Bearer token to /api/mcp; empty = MCP endpoint off
    # The Plugins page: where update.sh installs, which git sources plugins may come from (comma-separated
    # "host/owner" or "host" prefixes, e.g. github.com/DashCoder), and the token for private repos.
    apps_dir: Path = Path("~/apps")
    plugin_sources: str = "github.com/DashCoder"
    plugin_token_file: Path = Path("~/.config/inagentic/github-token")

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
