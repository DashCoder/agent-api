"""Settings & keys: optional settings the admin sets in the web admin instead of the server's .env.

API keys and connections (OpenRouter, Jira, GitHub, Slack, …) are typed into this server's own admin, stored
encrypted in its database and never shown again. They're never sent anywhere else: not to InAgentic's website and
not to any other server.

How a saved value takes effect: at start-up, saved values are put in the process environment before any settings are
read, so they win over the .env file and every plugin sees them like any other setting. Saving restarts agent-api
(its supervisor, Docker or launchd, starts it again in a few seconds). Values Hermes needs too (e.g. an OpenRouter key
when Hermes uses an OpenRouter model) are also written to Hermes' own .env, and Hermes is restarted.

Only settings in the catalogue can be saved: the core's (below) plus each plugin's `options`. Machine settings
(secrets, admin login, internal addresses) can never be changed here.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import signal
import time
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken

from .db import DB

log = logging.getLogger("agent_api.options")

# group id → title; plugins may add their own groups
GROUPS = {
    "models": "AI models",
    "github": "GitHub",
    "jira": "Jira",
    "slack": "Slack",
    "content": "Max (content)",
    "notes": "Nora (notes)",
    "kpi": "Kai (KPIs)",
    "avatar": "Talking avatar",
}

CORE_OPTIONS: list[dict[str, Any]] = [
    {"name": "OPENROUTER_API_KEY", "label": "OpenRouter API key", "group": "models", "secret": True, "hermes": True,
     "test": "openrouter", "placeholder": "sk-or-v1-…",
     "help": "Lets Hermes and the agents use bigger or faster models from OpenRouter (openrouter.ai → Keys). "
             "Then choose the models on Model & tools. Set a monthly limit on the key."},
    {"name": "ANTHROPIC_API_KEY", "label": "Anthropic API key", "group": "models", "secret": True, "hermes": True,
     "test": "anthropic", "placeholder": "sk-ant-…",
     "help": "For Claude models directly from Anthropic (console.anthropic.com). Optional."},
    {"name": "SIMLI_API_KEY", "label": "Simli API key", "group": "avatar", "secret": True,
     "help": "A talking video avatar in the chat (simli.com). Optional."},
]

# never settable here, whatever a plugin declares
PROTECTED = re.compile(
    r"^(SECRET_KEY|SETTINGS_KEY|MCP_KEY|PLUGINS|DATA_DIR|COOKIE_SECURE|CORS_ORIGINS|ADMIN_.*|HERMES_.*|"
    r"PLUGIN_.*|APPS_DIR|DEV_SHOW_CODES|.*_MCP_KEY|.*_URL_INTERNAL)$")
NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
MAX_VALUE = 4000
SAVED: dict[str, str] = {}          # what was put in the environment at start-up (for "where is it from")


# ------------------------------------------------------------------ encryption
def _fernet(secret_key: str) -> Fernet:
    """SETTINGS_KEY if set (a Fernet key), else one derived from SECRET_KEY."""
    k = os.environ.get("SETTINGS_KEY", "").strip()
    if k:
        return Fernet(k.encode())
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(f"agent-api options:{secret_key}".encode()).digest()))


def load(db: DB, secret_key: str) -> dict[str, str]:
    out = {}
    f = _fernet(secret_key)
    for r in db.all("SELECT name, value FROM option_values"):
        try:
            out[r["name"]] = f.decrypt(r["value"].encode()).decode()
        except InvalidToken:
            log.warning("Can't read the saved %s (SECRET_KEY or SETTINGS_KEY changed?): ignored", r["name"])
    return out


def store(db: DB, secret_key: str, name: str, value: str | None, admin: str) -> None:
    if value is None:
        db.execute("DELETE FROM option_values WHERE name = ?", (name,))
        return
    enc = _fernet(secret_key).encrypt(value.encode()).decode()
    db.execute("INSERT INTO option_values(name, value, updated_at, updated_by) VALUES(?, ?, ?, ?) "
               "ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, "
               "updated_by = excluded.updated_by", (name, enc, time.time(), admin))


# ------------------------------------------------------------------ start-up
def apply_saved(s: Any) -> Any:
    """Put saved values in the environment (plugins read them from there) and return the core settings with them."""
    db = DB(Path(s.data_dir) / "app.db")
    values = {k: v for k, v in load(db, s.secret_key).items() if NAME.match(k) and not PROTECTED.match(k)}
    SAVED.clear()
    SAVED.update(values)
    for k, v in values.items():
        os.environ[k] = v
    fields = type(s).model_fields
    update = {k.lower(): v for k, v in values.items() if k.lower() in fields}
    if values:
        log.info("Settings & keys: using %d saved setting(s): %s", len(values), ", ".join(sorted(values)))
    return s.model_copy(update=update) if update else s


# ------------------------------------------------------------------ catalogue
def catalogue(app: Any) -> list[dict[str, Any]]:
    """Every setting the admin may change: core first, then each plugin's (with the agent's name)."""
    items = [{**o, "owner": "core"} for o in CORE_OPTIONS]
    seen = {o["name"] for o in items}
    for p in app.state.plugins.values():
        for o in getattr(p, "options", []) or []:
            if not isinstance(o, dict) or not NAME.match(str(o.get("name", ""))) or PROTECTED.match(o["name"]):
                continue
            if o["name"] in seen:          # shared by several agents (e.g. one OpenRouter key)
                continue
            seen.add(o["name"])
            items.append({**o, "owner": p.name})
    return items


def effective(app: Any, name: str) -> str:
    """The value in use: the core's or any plugin's settings object that has this field."""
    attr = name.lower()
    for obj in [app.state.settings, *app.state.plugin_settings.values()]:
        if obj is not None and attr in type(obj).model_fields:
            v = getattr(obj, attr)
            if v not in (None, ""):
                return str(v)
    return os.environ.get(name, "")


def listing(app: Any, db: DB) -> dict[str, Any]:
    saved = set(r["name"] for r in db.all("SELECT name FROM option_values"))
    groups: dict[str, dict[str, Any]] = {}
    for o in catalogue(app):
        g = o.get("group") or o["owner"]
        val = effective(app, o["name"])
        item = {"name": o["name"], "label": o.get("label") or o["name"], "help": o.get("help", ""),
                "secret": bool(o.get("secret")), "placeholder": o.get("placeholder", ""), "test": o.get("test"),
                "set": bool(val), "source": "admin" if o["name"] in saved else ("server" if val else None),
                "pending": o["name"] in saved and o["name"] not in SAVED}
        if not item["secret"]:
            item["value"] = val
        groups.setdefault(g, {"id": g, "title": GROUPS.get(g, g.title()), "items": []})["items"].append(item)
    return {"groups": list(groups.values())}


# ------------------------------------------------------------------ saving
def check_value(name: str, value: str) -> str:
    v = value.strip()
    if len(v) > MAX_VALUE:
        raise ValueError(f"{name} is too long")
    if "\n" in v or "\r" in v or "\x00" in v:
        raise ValueError(f"{name} must be one line")
    return v


def write_hermes_env(hermes_home: Path, changes: dict[str, str | None]) -> None:
    """Set or remove lines in Hermes' own .env (other lines are kept)."""
    f = Path(os.path.expanduser(str(hermes_home))) / ".env"
    lines = f.read_text().splitlines() if f.exists() else []
    for name, value in changes.items():
        lines = [ln for ln in lines if not re.match(rf"^\s*(export\s+)?{name}\s*=", ln)]
        if value is not None:
            lines.append(f"{name}={value}")
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(".env.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, f)


def schedule_restart(delay: float = 1.5) -> None:
    """Exit after the reply is sent; Docker (restart: unless-stopped) or launchd (KeepAlive) starts agent-api again."""
    async def later() -> None:
        await asyncio.sleep(delay)
        log.info("Settings & keys changed: restarting agent-api")
        os.kill(os.getpid(), signal.SIGTERM)
    asyncio.get_running_loop().create_task(later())


# ------------------------------------------------------------------ tests ("Test" buttons)
async def run_test(kind: str, v: dict[str, str], transport=None) -> tuple[bool, str]:
    async with httpx.AsyncClient(timeout=15, transport=transport) as c:
        if kind == "openrouter":
            r = await c.get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {v.get('OPENROUTER_API_KEY', '')}"})
            if r.status_code == 200:
                d = r.json().get("data", {})
                limit = d.get("limit")
                return True, "Key works" + (f" · limit ${limit}" if limit else " · no spending limit set (add one)")
            return False, f"OpenRouter said {r.status_code}"
        if kind == "anthropic":
            r = await c.get("https://api.anthropic.com/v1/models", headers={
                "x-api-key": v.get("ANTHROPIC_API_KEY", ""), "anthropic-version": "2023-06-01"})
            return (True, "Key works") if r.status_code == 200 else (False, f"Anthropic said {r.status_code}")
        if kind == "github":
            base = (v.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
            r = await c.get(f"{base}/user", headers={"Authorization": f"Bearer {v.get('GITHUB_TOKEN', '')}",
                                                     "Accept": "application/vnd.github+json"})
            return (True, f"Signed in as {r.json().get('login')}") if r.status_code == 200 else (False, f"GitHub said {r.status_code}")
        if kind == "jira":
            base = v.get("JIRA_BASE_URL", "").rstrip("/")
            if not base.startswith("https://"):
                return False, "Jira URL should start with https:// (e.g. https://yourcompany.atlassian.net)"
            r = await c.get(f"{base}/rest/api/3/myself", auth=(v.get("JIRA_EMAIL", ""), v.get("JIRA_API_TOKEN", "")))
            return (True, f"Signed in as {r.json().get('displayName')}") if r.status_code == 200 else (False, f"Jira said {r.status_code}")
        if kind == "gemini":
            r = await c.get("https://generativelanguage.googleapis.com/v1beta/models",
                            params={"key": v.get("GOOGLE_AI_API_KEY", ""), "pageSize": 1})
            return (True, "Key works") if r.status_code == 200 else (False, f"Google said {r.status_code}")
        if kind.startswith("slack:"):
            token = v.get(kind.split(":", 1)[1], "")
            r = await c.post("https://slack.com/api/auth.test", headers={"Authorization": f"Bearer {token}"})
            d = r.json() if r.status_code == 200 else {}
            return (True, f"Bot {d.get('user')} in {d.get('team')}") if d.get("ok") else (False, f"Slack said {d.get('error') or r.status_code}")
    return False, f"No test for {kind}"
