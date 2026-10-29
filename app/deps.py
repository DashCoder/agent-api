"""Shared app state and helpers."""
from __future__ import annotations

import copy
from typing import Any

from fastapi import Request

from .db import DB
from .defaults import DEFAULT_SITE
from .hermes import Agent


def get_db(request: Request) -> DB:
    return request.app.state.db


def get_agent(request: Request) -> Agent:
    return request.app.state.agent


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def get_site(db: DB) -> dict[str, Any]:
    return deep_merge(DEFAULT_SITE, db.get_setting("site", {}))
