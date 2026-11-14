"""Settings & keys: optional settings saved encrypted on this server, applied at start-up, never shown again."""
import os
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import options
from app.config import Settings
from app.main import create_app
from app.plugins import Plugin


class JiraSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    jira_base_url: str = ""
    jira_api_token: str = ""
    jira_email: str = ""


@pytest.fixture(autouse=True)
def clean_env():
    """Saved settings go into os.environ at start-up: put it back after each test."""
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)
    options.SAVED.clear()


def jira_plugin():
    return Plugin(name="jirabot", settings=JiraSettings, public_info={"agentName": "Jira bot"}, options=[
        {"name": "JIRA_BASE_URL", "label": "Jira address", "group": "jira", "test": "jira"},
        {"name": "JIRA_EMAIL", "label": "Jira email", "group": "jira", "test": "jira"},
        {"name": "JIRA_API_TOKEN", "label": "Jira API token", "group": "jira", "secret": True, "test": "jira"},
        {"name": "SECRET_KEY", "label": "sneaky"},                       # never allowed
        {"name": "OPENROUTER_API_KEY", "label": "dup", "group": "models"},  # the core already has it
    ])


def make(tmp_path, monkeypatch):
    for n in ("JIRA_BASE_URL", "JIRA_API_TOKEN", "JIRA_EMAIL", "OPENROUTER_API_KEY", "SETTINGS_KEY"):
        monkeypatch.delenv(n, raising=False)
    s = Settings(data_dir=tmp_path, admin_password="secret", mock_delay_s=0, secret_key="k1", hermes_home=tmp_path / "hermes",
                 options_restart=False, _env_file=None)
    app = create_app(s, plugins=[jira_plugin()])
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": "admin", "password": "secret"}).status_code == 200
    return app, c, s


def item(listing, name):
    return next(i for g in listing["groups"] for i in g["items"] if i["name"] == name)


def test_keys_are_saved_encrypted_never_shown_and_used_after_restart(tmp_path, monkeypatch):
    app, c, s = make(tmp_path, monkeypatch)
    first = c.get("/api/admin/options").json()
    names = [i["name"] for g in first["groups"] for i in g["items"]]
    assert "JIRA_API_TOKEN" in names and "OPENROUTER_API_KEY" in names and "SECRET_KEY" not in names
    assert names.count("OPENROUTER_API_KEY") == 1
    assert item(first, "JIRA_API_TOKEN")["set"] is False

    r = c.put("/api/admin/options", json={"values": {"JIRA_BASE_URL": "https://acme.atlassian.net",
                                                    "JIRA_API_TOKEN": "tok-123", "OPENROUTER_API_KEY": "sk-or-1"}})
    assert r.status_code == 200 and r.json()["restarting"] is False
    j = item(r.json(), "JIRA_API_TOKEN")
    assert j["set"] is False or j["source"] == "admin"
    assert "value" not in j and item(r.json(), "JIRA_BASE_URL")["pending"] is True
    # encrypted in the database; the audit log has names, never values
    raw = sqlite3.connect(tmp_path / "app.db").execute("SELECT value FROM option_values WHERE name='JIRA_API_TOKEN'").fetchone()[0]
    assert "tok-123" not in raw
    assert "tok-123" not in str(c.get("/api/admin/audit").json())
    # Hermes gets the keys it needs in its own .env
    assert "OPENROUTER_API_KEY=sk-or-1" in (tmp_path / "hermes" / ".env").read_text()
    assert "JIRA" not in (tmp_path / "hermes" / ".env").read_text()

    # "restart": a new app reads them before any settings
    app2 = create_app(s, plugins=[jira_plugin()])
    assert app2.state.plugin_settings["jirabot"].jira_api_token == "tok-123"
    assert app2.state.settings.openrouter_api_key == "sk-or-1"
    c2 = TestClient(app2)
    c2.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    lst = c2.get("/api/admin/options").json()
    assert item(lst, "JIRA_API_TOKEN")["source"] == "admin" and item(lst, "JIRA_API_TOKEN")["pending"] is False
    assert "value" not in item(lst, "JIRA_API_TOKEN") and item(lst, "JIRA_BASE_URL")["value"] == "https://acme.atlassian.net"

    # remove one: gone from the store and from Hermes' .env
    c2.put("/api/admin/options", json={"remove": ["OPENROUTER_API_KEY"]})
    assert "OPENROUTER_API_KEY" not in (tmp_path / "hermes" / ".env").read_text()
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    app3 = create_app(s, plugins=[jira_plugin()])
    assert app3.state.settings.openrouter_api_key == ""

    # a changed SECRET_KEY makes old values unreadable: they're skipped, not crashed on
    for n in ("JIRA_BASE_URL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(n, raising=False)
    app4 = create_app(s.model_copy(update={"secret_key": "other"}), plugins=[jira_plugin()])
    assert app4.state.plugin_settings["jirabot"].jira_api_token == ""


def test_only_catalogue_settings_and_one_line_values(tmp_path, monkeypatch):
    _, c, _ = make(tmp_path, monkeypatch)
    for bad in ({"SECRET_KEY": "x"}, {"ADMIN_PASSWORD_HASH": "x"}, {"MCP_KEY": "x"}, {"NOT_A_THING": "x"}):
        assert c.put("/api/admin/options", json={"values": bad}).status_code == 400
    assert c.put("/api/admin/options", json={"values": {"JIRA_API_TOKEN": "a\nB=1"}}).status_code == 400
    c.post("/api/auth/logout")
    assert c.get("/api/admin/options").status_code == 401
    assert c.put("/api/admin/options", json={"values": {"JIRA_API_TOKEN": "x"}}).status_code == 401


def test_test_buttons_use_typed_values(tmp_path, monkeypatch):
    app, c, _ = make(tmp_path, monkeypatch)
    seen = []

    def route(req: httpx.Request):
        seen.append(req)
        if req.url.host == "acme.atlassian.net":
            ok = req.headers.get("authorization", "").startswith("Basic ")
            return httpx.Response(200 if ok else 401, json={"displayName": "Axel"})
        if req.url.host == "openrouter.ai":
            good = req.headers["authorization"] == "Bearer sk-good"
            return httpx.Response(200 if good else 401, json={"data": {"limit": 20}})
        return httpx.Response(404)
    app.state.options_transport = httpx.MockTransport(route)
    r = c.post("/api/admin/options/test", json={"test": "jira", "values": {
        "JIRA_BASE_URL": "https://acme.atlassian.net", "JIRA_EMAIL": "a@b.c", "JIRA_API_TOKEN": "t"}}).json()
    assert r == {"ok": True, "message": "Signed in as Axel"}
    assert c.post("/api/admin/options/test", json={"test": "openrouter", "values": {"OPENROUTER_API_KEY": "sk-bad"}}).json()["ok"] is False
    assert "limit $20" in c.post("/api/admin/options/test", json={"test": "openrouter", "values": {"OPENROUTER_API_KEY": "sk-good"}}).json()["message"]
    assert "https://" in c.post("/api/admin/options/test", json={"test": "jira", "values": {"JIRA_BASE_URL": "acme"}}).json()["message"]


def test_change_password(tmp_path, monkeypatch):
    app, c, s = make(tmp_path, monkeypatch)
    assert c.post("/api/admin/password", json={"current": "wrong", "new": "a-long-new-password"}).status_code == 400
    assert c.post("/api/admin/password", json={"current": "secret", "new": "short"}).status_code == 422
    assert c.post("/api/admin/password", json={"current": "secret", "new": "a-long-new-password"}).json() == {"ok": True}
    c.post("/api/auth/logout")
    assert c.post("/api/auth/login", json={"username": "admin", "password": "secret"}).status_code == 401
    assert c.post("/api/auth/login", json={"username": "admin", "password": "a-long-new-password"}).status_code == 200
    # it survives a restart (stored in the database, not the .env)
    c2 = TestClient(create_app(s, plugins=[jira_plugin()]))
    assert c2.post("/api/auth/login", json={"username": "admin", "password": "a-long-new-password"}).status_code == 200


def test_settings_key_is_used_when_set(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    _, c, s = make(tmp_path, monkeypatch)
    monkeypatch.setenv("SETTINGS_KEY", Fernet.generate_key().decode())
    c.put("/api/admin/options", json={"values": {"JIRA_API_TOKEN": "tok-9"}})
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    assert create_app(s, plugins=[jira_plugin()]).state.plugin_settings["jirabot"].jira_api_token == "tok-9"
    monkeypatch.delenv("SETTINGS_KEY")
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    assert create_app(s, plugins=[jira_plugin()]).state.plugin_settings["jirabot"].jira_api_token == ""
