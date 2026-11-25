"""FastAPI application factory.

Run locally:  uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .config import Settings, get_settings
from .db import DB
from .hermes import make_agent, make_public_agent
from . import direct, openrouter_traces, options
from .plugins import Plugin, install_plugins, resolve_plugins
from .routers import (admin, avatar_clips, chat, console, email_login, files, mcp, models, options_admin, plugins_admin, public,
                      simli, tts, user)


def create_app(settings: Settings | None = None, plugins: list[Plugin] | None = None) -> FastAPI:
    """`plugins` overrides PLUGINS from the settings (handy in tests)."""
    s = settings or get_settings()
    logging.basicConfig(level=logging.INFO)
    s = options.apply_saved(s)        # values set on "Settings & keys" win over .env (plugins read them too)
    if s.secret_key == "change-me-in-production" and s.hermes_mode == "hermes":
        logging.getLogger("agent_api").warning("SECRET_KEY is the default — set it before going live.")

    app = FastAPI(title=s.app_name, version="0.2.0")
    app.dependency_overrides[get_settings] = lambda: s
    app.state.settings = s
    app.state.db = DB(Path(s.data_dir) / "app.db")
    app.state.agent = make_agent(s, app)
    app.state.public_agent = make_public_agent(s, app)
    if s.hermes_mode == "direct":
        direct.register(app, s)       # its model is chosen on Model & tools, like the agents' models
    app.state.active_turns = {}       # web chats running now → who they're for (tools check it; app/security.py)
    app.state.extra_secrets = list(options.SAVED.values())   # saved keys are redacted from replies too

    app.add_middleware(SessionMiddleware, secret_key=s.secret_key, session_cookie="agent_session",
                       same_site="lax", https_only=s.cookie_secure, max_age=60 * 60 * 24 * 180)
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origin_list, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    for r in (public.router, chat.router, user.router, admin.router, console.router, tts.router, simli.router,
              files.router, mcp.router, plugins_admin.router, models.router, options_admin.router, email_login.router,
              avatar_clips.router):
        app.include_router(r)
    openrouter_traces.install(app)
    install_plugins(app, plugins if plugins is not None else resolve_plugins(s.plugins))
    return app


app = create_app()
