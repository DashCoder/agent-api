"""The plugin contract: routes, MCP tools, migrations, settings and public info."""
import json

import pytest
from fastapi import APIRouter, Depends, Request
from fastapi.testclient import TestClient
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.auth import require_admin
from app.config import Settings
from app.main import create_app
from app.plugins import PLUGIN_API, McpTool, Plugin, PluginError, plugin_settings, resolve_plugins


class DemoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEMO_", extra="ignore")
    greeting: str = "hello"


router = APIRouter(prefix="/api/admin/demo")


@router.get("/notes")
def notes(request: Request, _=Depends(require_admin)):
    return {"notes": request.app.state.db.all("SELECT text FROM demo_notes"),
            "greeting": plugin_settings(request, "demo").greeting}


async def add_note(ctx, args):
    ctx.db.execute("INSERT INTO demo_notes(text) VALUES (?)", (args["text"],))
    ctx.audit("demo.note_added", "demo", text=args["text"])
    return {"ok": True, "greeting": ctx.plugin_settings.greeting}


plugin = Plugin(
    name="demo", version="1.2.3", routers=[router],
    migrations=["CREATE TABLE demo_notes (text TEXT NOT NULL);", "ALTER TABLE demo_notes ADD COLUMN extra TEXT;"],
    mcp_tools=[McpTool("add_note", "Store a note", {"type": "object", "properties": {"text": {"type": "string"}}},
                       add_note)],
    settings=DemoSettings, public_info={"adminPages": ["/admin/demo"]}, mcp_instructions="Demo tools.")


def make(tmp_path, plugins, **kw):
    s = Settings(hermes_mode="mock", data_dir=tmp_path, admin_password="secret", mock_delay_s=0, secret_key="t",
                 mcp_key="k", _env_file=None, **kw)
    return TestClient(create_app(s, plugins=plugins))


def rpc(c, method, params=None):
    return c.post("/api/mcp", headers={"Authorization": "Bearer k"},
                  json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).json()


def test_plugin_adds_routes_tools_tables_and_config(tmp_path):
    c = make(tmp_path, [plugin])
    assert c.get("/api/config").json()["plugins"] == [{"name": "demo", "version": "1.2.3", "adminPages": ["/admin/demo"]}]
    assert [t["name"] for t in rpc(c, "tools/list")["result"]["tools"]] == ["add_note"]
    assert "Demo tools." in rpc(c, "initialize")["result"]["instructions"]
    res = rpc(c, "tools/call", {"name": "add_note", "arguments": {"text": "hi"}})["result"]
    assert not res["isError"] and json.loads(res["content"][0]["text"])["greeting"] == "hello"
    assert c.get("/api/admin/demo/notes").status_code == 401
    c.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    assert c.get("/api/admin/demo/notes").json()["notes"] == [{"text": "hi"}]
    assert "demo.note_added" in [e["action"] for e in c.get("/api/admin/audit").json()["items"]]


def test_migrations_run_once_across_restarts(tmp_path):
    make(tmp_path, [plugin])
    make(tmp_path, [plugin])  # would fail on "ALTER TABLE … ADD COLUMN extra" if re-run


def test_no_plugins_means_empty_mcp_and_config(tmp_path):
    c = make(tmp_path, [])
    assert c.get("/api/config").json()["plugins"] == []
    assert rpc(c, "tools/list")["result"]["tools"] == []


def test_mcp_needs_key(tmp_path):
    c = make(tmp_path, [plugin])
    assert c.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401


def test_incompatible_or_unknown_plugins_fail_at_startup(tmp_path):
    old = Plugin(name="old", api_versions=(PLUGIN_API + 1,))
    with pytest.raises(PluginError):
        make(tmp_path, [old])
    with pytest.raises(PluginError):
        resolve_plugins("not-installed")


def test_dotted_path_loading(tmp_path):
    [p] = resolve_plugins("tests.test_plugins:plugin")
    assert p.name == "demo"
