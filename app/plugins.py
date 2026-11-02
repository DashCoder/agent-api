"""Plugin system: extend agent-api without changing it.

A plugin is a Python package that exposes a `Plugin` object through the entry-point group `agent_api.plugins`:

    # the plugin's pyproject.toml
    [project.entry-points."agent_api.plugins"]
    builder = "my_package:plugin"

and is switched on by name in agent-api's .env:  PLUGINS=builder  (comma-separated). For development you can
also give a dotted path instead of a name:  PLUGINS=my_package:plugin

What a plugin can contribute, all through this module (it should not reach into other agent-api internals;
require_admin, get_db, DB and new_id are re-exported here for that reason):
  routers      FastAPI routers (use app.auth.require_admin to protect admin routes)
  mcp_tools    tools served to Hermes from /api/mcp (see McpTool)
  migrations   SQL run once each, in order, tracked per plugin in plugin_migrations
  settings     a pydantic-settings class, read from the environment/.env at start-up
  public_info  extra keys returned in /api/config under plugins[]  (never secrets)
  on_startup   a function called with the app once everything is set up

Breaking changes to this contract bump PLUGIN_API. A plugin declares which versions it supports.
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, FastAPI, Request
from pydantic_settings import BaseSettings

from .auth import require_admin
from .config import Settings
from .db import DB, new_id
from .deps import get_db

# The stable surface for plugins: import these from app.plugins, not from other agent-api modules.
__all__ = ["PLUGIN_API", "Plugin", "McpTool", "ToolContext", "PluginError", "plugin_settings",
           "require_admin", "get_db", "DB", "new_id", "Settings"]

PLUGIN_API = 1
ENTRY_POINT_GROUP = "agent_api.plugins"
log = logging.getLogger("agent_api.plugins")


@dataclass
class ToolContext:
    """What an MCP tool handler gets: the request, shared services and its plugin's settings."""
    request: Request
    db: DB
    settings: Settings
    plugin_settings: Any

    def audit(self, action: str, target: str, actor: str = "agent", **detail: Any) -> None:
        self.db.audit(actor, action, target, **detail)


ToolHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[Any]]


@dataclass
class McpTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler


@dataclass
class Plugin:
    name: str
    version: str = "0.0.0"
    api_versions: tuple[int, ...] = (PLUGIN_API,)
    routers: list[APIRouter] = field(default_factory=list)
    mcp_tools: list[McpTool] = field(default_factory=list)
    migrations: list[str] = field(default_factory=list)
    settings: type[BaseSettings] | None = None
    public_info: dict[str, Any] = field(default_factory=dict)
    mcp_instructions: str = ""
    on_startup: Callable[[FastAPI], None] | None = None


class PluginError(RuntimeError):
    pass


def _resolve(spec: str) -> Plugin:
    if ":" in spec:  # dotted path, for development and tests
        module, attr = spec.split(":", 1)
        obj = getattr(importlib.import_module(module), attr)
    else:
        found = [ep for ep in entry_points(group=ENTRY_POINT_GROUP) if ep.name == spec]
        if not found:
            raise PluginError(f"Plugin '{spec}' is not installed (no '{ENTRY_POINT_GROUP}' entry point)")
        obj = found[0].load()
    if callable(obj) and not isinstance(obj, Plugin):
        obj = obj()
    if not isinstance(obj, Plugin):
        raise PluginError(f"'{spec}' did not give a Plugin object")
    return obj


def resolve_plugins(names: str) -> list[Plugin]:
    return [_resolve(n.strip()) for n in names.split(",") if n.strip()]


def install_plugins(app: FastAPI, plugins: list[Plugin]) -> None:
    """Check, migrate and mount every plugin. Fails loudly at start-up rather than half-working later."""
    db: DB = app.state.db
    app.state.plugins = {}
    app.state.plugin_settings = {}
    app.state.mcp_tools = {}
    app.state.mcp_instructions = []
    for p in plugins:
        if PLUGIN_API not in p.api_versions:
            raise PluginError(f"Plugin '{p.name}' supports plugin API {p.api_versions}, this agent-api has {PLUGIN_API}")
        if p.name in app.state.plugins:
            raise PluginError(f"Plugin '{p.name}' is enabled twice")
        db.migrate(p.name, p.migrations)
        app.state.plugin_settings[p.name] = p.settings() if p.settings else None
        for t in p.mcp_tools:
            if t.name in app.state.mcp_tools:
                raise PluginError(f"MCP tool '{t.name}' from '{p.name}' clashes with another plugin")
            app.state.mcp_tools[t.name] = (p.name, t)
        if p.mcp_instructions:
            app.state.mcp_instructions.append(p.mcp_instructions)
        for r in p.routers:
            app.include_router(r)
        app.state.plugins[p.name] = p
        if p.on_startup:
            p.on_startup(app)
        log.info("Plugin %s %s loaded (%d routes, %d MCP tools)", p.name, p.version, len(p.routers), len(p.mcp_tools))


def plugin_settings(request: Request, name: str) -> Any:
    """For plugin routes: their own settings object."""
    return request.app.state.plugin_settings.get(name)
