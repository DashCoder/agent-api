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
from .hermes import make_agent
from .routers import admin, chat, public, user


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()
    logging.basicConfig(level=logging.INFO)
    if s.secret_key == "change-me-in-production" and s.hermes_mode == "hermes":
        logging.getLogger("agent_api").warning("SECRET_KEY is the default — set it before going live.")

    app = FastAPI(title=s.app_name, version="0.1.0")
    app.dependency_overrides[get_settings] = lambda: s
    app.state.settings = s
    app.state.db = DB(Path(s.data_dir) / "app.db")
    app.state.agent = make_agent(s)

    app.add_middleware(SessionMiddleware, secret_key=s.secret_key, session_cookie="agent_session",
                       same_site="lax", https_only=s.cookie_secure, max_age=60 * 60 * 24 * 180)
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origin_list, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    for r in (public.router, chat.router, user.router, admin.router):
        app.include_router(r)
    return app


app = create_app()
