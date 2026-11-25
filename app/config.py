"""Settings, read from environment variables or a .env file."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Hermes Agent ------------------------------------------------------
    # "mock" runs a built-in fake agent so the UI works without Hermes. "direct" chats without Hermes: agent-api asks
    # the model chosen for "Chat" on Model & tools and runs the agents' tools itself (faster on CPU; see app/direct.py).
    hermes_mode: Literal["mock", "hermes", "direct"] = "mock"
    direct_model: str = "qwen3.6:35b-a3b"   # Chat's model until one is chosen on Model & tools
    direct_history: int = 12                # earlier messages of the chat sent with each question
    direct_tool_rounds: int = 6             # rounds of tool calls per reply
    direct_max_tokens: int = 4000
    # Let a local model think before it answers (Ollama). Off: on a CPU server thinking first delays the reply by a
    # minute or more; the tools still give it the facts.
    direct_think: bool = False
    direct_cloud_model: str = "google/gemini-2.5-flash"   # "OpenRouter" in the chat box (changeable on Model & tools)
    hermes_api_url: str = "http://127.0.0.1:8642"
    hermes_api_key: str = ""
    hermes_model: str = "hermes-agent"
    hermes_home: Path = Path("~/.hermes")   # read for the model Hermes is set to use (shown on Model & tools)
    hermes_timeout_s: float = 300.0
    # Where admins open the full Hermes dashboard (e.g. a Tailscale Serve URL).
    hermes_dashboard_url: str = ""
    # How agent-api restarts Hermes after the admin changes its model on Model & tools.
    hermes_restart_cmd: str = "~/.local/bin/hermes gateway restart"
    # Who may chat with the agents (Hermes with its tools and terminal): the admin, and verified emails listed in
    # TRUSTED_EMAILS (comma-separated). With CHAT_ACCESS=public everyone else may chat too, but only with a separate
    # Hermes that has no tools (HERMES_PUBLIC_API_URL/KEY); without one, they're asked to sign in.
    trusted_emails: str = ""
    chat_access: Literal["trusted", "public"] = "trusted"
    hermes_public_api_url: str = ""
    hermes_public_api_key: str = ""
    # Chat limits for anyone who isn't the admin (per IP address, and for the whole site)
    chat_per_minute: int = 8
    chat_per_day: int = 150
    chat_site_per_day: int = 3000

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
    # This site's own address, used in links in emails (e.g. https://agents.example.com). Empty = the address in
    # use, but only if it's this machine or listed in CORS_ORIGINS.
    public_url: str = ""
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
    # Email (AWS SES): sign-in links and "Save my work" codes. Set on Settings & keys.
    ses_access_key_id: str = ""
    ses_secret_access_key: str = ""
    ses_region: str = "eu-west-2"
    mail_from: str = "InAgentic <noreply@inagentic.ai>"
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
    # Saving on "Settings & keys" restarts agent-api so every agent picks the values up (Docker or launchd starts
    # it again). Off in tests.
    options_restart: bool = True
    mock_delay_s: float = 0.02

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
