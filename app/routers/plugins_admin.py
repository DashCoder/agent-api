"""The Plugins page: what is installed, check a plugin's git repo, and queue install / update / on-off / remove jobs.

agent-api never installs code itself. It checks a repo into a staging folder and writes a job file to
<apps_dir>/jobs/; the installer job (launchd, `run-jobs.sh`, as the same user) runs update.sh for it, with a backup
first and an automatic rollback if the health check fails. The page follows the job's status and log.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth import require_admin
from ..config import Settings, get_settings
from ..db import DB, new_id
from ..deps import get_db

router = APIRouter(prefix="/api/admin/plugins", dependencies=[Depends(require_admin)])

# The InAgentic agents update.sh knows by name: key | repo | plugin | title
BUILTIN = [
    ("coding", "ina-coding-agent", "builder", "Coding agent: Jira tickets to pull requests"),
    ("content", "ina-content-agent", "content", "Max: videos, articles, newsletters"),
    ("notes", "ina-note-agent", "notes", "Nora: call notes, recall, roleplay"),
    ("kpi", "ina-kpi-agent", "kpi", "Kai: KPIs and targets from Gmail"),
]
KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")
URL_RE = re.compile(r"^(https://[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?|file:///[^\s]+?)(\.git)?/?$")


def apps(s: Settings) -> Path:
    return Path(os.path.expanduser(str(s.apps_dir)))


def _conf(s: Settings) -> dict[str, str]:
    out: dict[str, str] = {}
    f = apps(s) / "inagentic.conf"
    if f.exists():
        for line in f.read_text().splitlines():
            m = re.match(r'^(\w+)="?(.*?)"?$', line.strip())
            if m:
                out[m.group(1)] = m.group(2)
    return out


def custom_entries(s: Settings) -> list[dict[str, str]]:
    f = apps(s) / "plugins.conf"
    out = []
    if f.exists():
        for line in f.read_text().splitlines():
            if line.startswith("#") or "|" not in line:
                continue
            k, repo, plugin, title, url, ref, commit = (line.split("|") + [""] * 7)[:7]
            out.append({"key": k, "repo": repo, "plugin": plugin, "title": title, "url": url, "ref": ref, "commit": commit})
    return out


def _git(*args: str, cwd: Path | None = None, timeout: int = 20, token: str = "", url: str = "") -> str:
    cmd = ["git", "-c", "credential.helper="]
    if token and url.startswith("https://github.com/"):
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        cmd += ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]
    out = subprocess.run([*cmd, *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                         env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[-300:] or "git failed")
    return out.stdout.strip()


def _commit(folder: Path) -> str:
    try:
        return _git("rev-parse", "--short", "HEAD", cwd=folder, timeout=5) if (folder / ".git").exists() else ""
    except Exception:
        return ""


@router.get("")
def installed(request: Request, s: Settings = Depends(get_settings)):
    """Every plugin this machine knows: running, installed but off, and the jobs in progress."""
    agents = set(_conf(s).get("AGENTS", "").split())
    loaded = request.app.state.plugins
    rows = [{"key": k, "repo": r, "plugin": p, "title": t, "url": f"https://github.com/DashCoder/{r}", "ref": "main",
             "commit": "", "builtin": True} for k, r, p, t in BUILTIN]
    rows += [{**e, "builtin": False} for e in custom_entries(s)]
    known = {r["plugin"] for r in rows}
    for name in loaded:  # running but not installed by update.sh (e.g. a dev install)
        if name not in known:
            rows.append({"key": name, "repo": "", "plugin": name, "title": name, "url": "", "ref": "", "commit": "",
                         "builtin": False})
    out = []
    for r in rows:
        folder = apps(s) / r["repo"] if r["repo"] else None
        present = bool(folder and folder.exists())
        p = loaded.get(r["plugin"])
        if not present and not p and r["builtin"]:
            continue  # an InAgentic agent this machine doesn't have
        out.append({**r, "installed": present or bool(p), "enabled": r["key"] in agents or bool(p), "running": bool(p),
                    "version": p.version if p else None, "commit": _commit(folder) if present else r["commit"]})
    return {"plugins": out, "jobs": _jobs(s)[:10], "sources": [x.strip() for x in s.plugin_sources.split(",") if x.strip()],
            "installerReady": (apps(s) / "run-jobs.sh").exists()}


# ------------------------------------------------------------------ check a repo
class CheckIn(BaseModel):
    url: str = Field(min_length=8, max_length=300)
    ref: str = Field(default="main", max_length=100)


def allowed(url: str, sources: str) -> bool:
    bare = re.sub(r"^https://|^file://", "", url.rstrip("/").removesuffix(".git"))
    for src in (x.strip().rstrip("/") for x in sources.split(",") if x.strip()):
        src = re.sub(r"^https://|^file://", "", src)
        if bare == src or bare.startswith(src + "/"):
            return True
    return False


def read_manifest(folder: Path) -> tuple[dict[str, Any], list[str]]:
    """inagentic-plugin.json, or what can be read from the Python package when there is none."""
    warnings: list[str] = []
    mf = folder / "inagentic-plugin.json"
    if mf.exists():
        try:
            m = json.loads(mf.read_text())
        except ValueError:
            raise HTTPException(400, "inagentic-plugin.json isn't valid JSON") from None
    else:
        warnings.append("No inagentic-plugin.json: read what we could from the Python package.")
        m = {}
        for py in sorted(folder.glob("api/*/pyproject.toml")):
            hit = re.search(r'\[project\.entry-points\."agent_api\.plugins"\]\s*\n\s*([A-Za-z0-9_-]+)\s*=', py.read_text())
            if hit:
                m = {"name": hit.group(1), "python": str(py.parent.relative_to(folder))}
                ver = re.search(r'^version\s*=\s*"([^"]+)"', py.read_text(), re.M)
                if ver:
                    m["version"] = ver.group(1)
                break
        if (folder / "ui/plugins/index.ts").exists():
            m["ui"] = "ui/plugins/index.ts"
    if not m.get("name"):
        raise HTTPException(400, "This repo isn't an agent-api plugin: no inagentic-plugin.json and no "
                                 "agent_api.plugins entry point in api/*/pyproject.toml.")
    if not KEY_RE.match(str(m["name"])):
        raise HTTPException(400, f"Plugin name \"{m['name']}\" must be lowercase letters, digits, - or _.")
    if m.get("python") and not (folder / m["python"] / "pyproject.toml").exists():
        raise HTTPException(400, f"The manifest's python folder {m['python']} has no pyproject.toml.")
    api = m.get("pluginApi", [1])
    if 1 not in api:
        warnings.append(f"Made for plugin API {api}; this machine runs API 1.")
    return m, warnings


@router.post("/check")
async def check(body: CheckIn, request: Request, s: Settings = Depends(get_settings), db: DB = Depends(get_db)):
    url, ref = body.url.strip(), body.ref.strip() or "main"
    if not URL_RE.match(url):
        raise HTTPException(400, "Give the repo's address, e.g. https://github.com/DashCoder/my-agent")
    if not allowed(url, s.plugin_sources):
        raise HTTPException(403, f"Plugins can only come from: {s.plugin_sources}. An admin can add more sources "
                                 "with PLUGIN_SOURCES in agent-api's .env.")
    if not re.match(r"^[A-Za-z0-9._/-]+$", ref):
        raise HTTPException(400, "That branch or tag name isn't valid.")
    cid = new_id()[:12]
    stage = apps(s) / "_staging" / cid
    stage.parent.mkdir(parents=True, exist_ok=True)
    tok_file = Path(os.path.expanduser(str(s.plugin_token_file)))
    token = tok_file.read_text().strip() if tok_file.exists() else ""
    try:
        await asyncio.to_thread(_git, "clone", "--quiet", "--depth", "1", "--branch", ref, url, str(stage),
                                timeout=90, token=token, url=url)
        commit = await asyncio.to_thread(_git, "rev-parse", "HEAD", cwd=stage)
        log = await asyncio.to_thread(_git, "log", "-1", "--format=%an%x09%ad%x09%s", "--date=short", cwd=stage)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        shutil.rmtree(stage, ignore_errors=True)
        raise HTTPException(400, f"Couldn't read {url} at {ref}: {e}. Private repo? The GitHub token on this "
                                 "machine needs read access to it.") from None
    try:
        m, warnings = read_manifest(stage)
    except HTTPException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    author, date, subject = (log.split("\t") + ["", "", ""])[:3]
    repo = re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1])
    loaded = request.app.state.plugins
    taken = {e["key"]: e for e in custom_entries(s)}
    update = m["name"] in taken and taken[m["name"]]["url"].rstrip("/") == url.rstrip("/")
    if m["name"] in loaded and not update:
        warnings.append(f"A plugin called {m['name']} is already running; installing would replace it.")
    if any(m["name"] == p for _, _, p, _ in BUILTIN) or any(m["name"] == k for k, *_ in BUILTIN):
        raise HTTPException(400, f"{m['name']} is the name of a built-in InAgentic agent; give the plugin another name.")
    info = {"id": cid, "url": url, "ref": ref, "commit": commit, "repo": repo, "name": m["name"],
            "title": m.get("title") or m["name"], "version": m.get("version"), "description": m.get("description", ""),
            "settings": m.get("settings", []), "ui": bool(m.get("ui")), "python": m.get("python"),
            "author": author, "date": date, "message": subject, "warnings": warnings, "update": update,
            "checkedAt": time.time()}
    db.set_setting(f"plugins.check.{cid}", info)
    db.audit(request.session.get("user", {}).get("username", "admin"), "plugins.check", url, ref=ref, commit=commit[:12])
    return info


# ------------------------------------------------------------------ jobs
def _jobs_dir(s: Settings) -> Path:
    d = apps(s) / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _jobs(s: Settings) -> list[dict[str, Any]]:
    d = apps(s) / "jobs"
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
        try:
            out.append(json.loads(f.read_text()))
        except ValueError:
            continue
    return out


def _queue(s: Settings, request: Request, action: str, **fields: Any) -> dict[str, Any]:
    if any(j.get("status") in ("queued", "running") for j in _jobs(s)):
        raise HTTPException(409, "Another plugin change is still running. Wait for it to finish.")
    job = {"id": time.strftime("%Y%m%d-%H%M%S-") + new_id()[:6], "action": action, "status": "queued",
           "createdAt": time.time(), "by": request.session.get("user", {}).get("username", "admin"), **fields}
    tmp = _jobs_dir(s) / f".{job['id']}.tmp"
    tmp.write_text(json.dumps(job, indent=2))
    tmp.rename(_jobs_dir(s) / f"{job['id']}.json")   # atomic: the installer never sees half a file
    request.app.state.db.audit(job["by"], f"plugins.{action}", fields.get("name", ""), job=job["id"],
                               commit=str(fields.get("commit", ""))[:12])
    return job


ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def write_env(s: Settings, values: dict[str, str]) -> list[str]:
    """Settings the plugin asked for → agent-api's .env (replacing earlier values). Returns the names written."""
    env = apps(s) / "agent-api" / ".env"
    if not env.parent.exists():
        env = Path(".env")
    lines = env.read_text().splitlines() if env.exists() else []
    written = []
    for k, v in values.items():
        if not v:
            continue
        if not ENV_KEY.match(k) or "\n" in v:
            raise HTTPException(400, f"Invalid setting {k}")
        lines = [ln for ln in lines if not ln.startswith(f"{k}=")] + [f"{k}={v}"]
        written.append(k)
    if written:
        env.write_text("\n".join(lines) + "\n")
        os.chmod(env, 0o600)
    return written


class InstallIn(BaseModel):
    checkId: str
    settings: dict[str, str] = Field(default_factory=dict)


@router.post("/install")
def install(body: InstallIn, request: Request, s: Settings = Depends(get_settings), db: DB = Depends(get_db)):
    info = db.get_setting(f"plugins.check.{body.checkId}")
    if not info:
        raise HTTPException(404, "That check has expired. Check the repo again.")
    allowed_keys = {x["name"] for x in info.get("settings", []) if isinstance(x, dict) and x.get("name")}
    unknown = set(body.settings) - allowed_keys
    if unknown:
        raise HTTPException(400, f"Not a setting of this plugin: {', '.join(sorted(unknown))}")
    written = write_env(s, body.settings)
    job = _queue(s, request, "add", name=info["name"], repo=info["repo"], url=info["url"], ref=info["ref"],
                 commit=info["commit"], title=info["title"], settingsWritten=written)
    shutil.rmtree(apps(s) / "_staging" / body.checkId, ignore_errors=True)
    return job


class ToggleIn(BaseModel):
    enabled: bool


@router.post("/{key}/toggle")
def toggle(key: str, body: ToggleIn, request: Request, s: Settings = Depends(get_settings)):
    known = {k for k, *_ in BUILTIN} | {e["key"] for e in custom_entries(s)}
    if key not in known:
        raise HTTPException(404, f"No plugin {key} on this machine")
    return _queue(s, request, "enable" if body.enabled else "disable", name=key)


@router.post("/{key}/remove")
def remove(key: str, request: Request, s: Settings = Depends(get_settings)):
    if key not in {e["key"] for e in custom_entries(s)}:
        raise HTTPException(400, "Built-in agents are turned off, not removed.")
    return _queue(s, request, "remove", name=key)


@router.get("/jobs/{job_id}")
def job(job_id: str, s: Settings = Depends(get_settings)):
    if not re.match(r"^[0-9A-Za-z-]+$", job_id):
        raise HTTPException(404, "No such job")
    f = apps(s) / "jobs" / f"{job_id}.json"
    if not f.exists():
        raise HTTPException(404, "No such job")
    log = apps(s) / "jobs" / f"{job_id}.log"
    return {**json.loads(f.read_text()), "log": log.read_text()[-12000:] if log.exists() else ""}
