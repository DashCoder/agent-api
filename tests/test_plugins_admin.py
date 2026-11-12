"""The Plugins page: list, check a git repo, queue install / on-off / remove jobs."""
import json
import subprocess

from tests.test_console import login
from tests.test_plugins import make, plugin


def git_repo(path, manifest=None, pyproject=True):
    path.mkdir(parents=True)
    (path / "README.md").write_text("plugin")
    if manifest is not None:
        (path / "inagentic-plugin.json").write_text(json.dumps(manifest))
    if pyproject:
        (path / "api/my_ext").mkdir(parents=True)
        (path / "api/my_ext/pyproject.toml").write_text(
            '[project]\nname="my-ext"\nversion="0.2.0"\n[project.entry-points."agent_api.plugins"]\nmyext = "x:plugin"\n')
    run = lambda *a: subprocess.run(["git", *a], cwd=path, check=True, capture_output=True)
    run("init", "-q", "-b", "main"); run("add", "."); run("-c", "user.email=a@b", "-c", "user.name=Ann", "commit", "-qm", "first")
    return f"file://{path}"


def setup(tmp_path, **kw):
    apps = tmp_path / "apps"
    apps.mkdir()
    (apps / "inagentic.conf").write_text('AGENTS="content kpi"\n')
    (apps / "ina-kpi-agent").mkdir()
    (apps / "agent-api").mkdir()
    c = make(tmp_path / "data", [plugin], apps_dir=apps, plugin_sources=f"file://{tmp_path}/src", **kw)
    return c, apps


def test_list_shows_installed_agents(tmp_path):
    c, apps = setup(tmp_path)
    assert c.get("/api/admin/plugins").status_code == 401
    login(c)
    r = c.get("/api/admin/plugins").json()
    by = {p["key"]: p for p in r["plugins"]}
    assert by["kpi"]["installed"] and by["kpi"]["enabled"] and not by["kpi"]["running"]
    assert "coding" not in by and by["demo"]["running"] and by["demo"]["version"] == "1.2.3"
    assert r["installerReady"] is False and r["jobs"] == []


def test_check_and_install(tmp_path):
    c, apps = setup(tmp_path)
    login(c)
    url = git_repo(tmp_path / "src/my-ext", {"name": "myext", "title": "My extension", "python": "api/my_ext",
                                             "settings": [{"name": "MYEXT_TOKEN", "label": "Token", "secret": True}]})
    bad = c.post("/api/admin/plugins/check", json={"url": "https://github.com/evil/x"})
    assert bad.status_code == 403 and "only come from" in bad.json()["detail"]
    info = c.post("/api/admin/plugins/check", json={"url": url}).json()
    assert info["name"] == "myext" and info["title"] == "My extension" and len(info["commit"]) == 40
    assert info["author"] == "Ann" and info["message"] == "first" and info["warnings"] == []
    assert c.post("/api/admin/plugins/install", json={"checkId": info["id"], "settings": {"OTHER": "x"}}).status_code == 400
    job = c.post("/api/admin/plugins/install", json={"checkId": info["id"], "settings": {"MYEXT_TOKEN": "s3cret"}}).json()
    assert job["action"] == "add" and job["commit"] == info["commit"] and job["settingsWritten"] == ["MYEXT_TOKEN"]
    assert "MYEXT_TOKEN=s3cret" in (apps / "agent-api/.env").read_text()
    assert "s3cret" not in json.dumps(job)
    assert json.loads((apps / "jobs" / f"{job['id']}.json").read_text())["status"] == "queued"
    assert c.post("/api/admin/plugins/kpi/toggle", json={"enabled": False}).status_code == 409   # one at a time
    (apps / "jobs" / f"{job['id']}.log").write_text("installing…")
    assert c.get(f"/api/admin/plugins/jobs/{job['id']}").json()["log"] == "installing…"
    assert "plugins.add" in [e["action"] for e in c.get("/api/admin/audit").json()["items"]]


def test_check_without_manifest_reads_pyproject(tmp_path):
    c, _ = setup(tmp_path)
    login(c)
    info = c.post("/api/admin/plugins/check", json={"url": git_repo(tmp_path / "src/bare")}).json()
    assert info["name"] == "myext" and info["version"] == "0.2.0" and info["warnings"]
    none = git_repo(tmp_path / "src/empty", None, pyproject=False)
    r = c.post("/api/admin/plugins/check", json={"url": none})
    assert r.status_code == 400 and "isn't an agent-api plugin" in r.json()["detail"]
    builtin = c.post("/api/admin/plugins/check", json={"url": git_repo(tmp_path / "src/k", {"name": "kpi"})})
    assert builtin.status_code == 400


def test_toggle_and_remove(tmp_path):
    c, apps = setup(tmp_path)
    login(c)
    (apps / "plugins.conf").write_text("myext|my-ext|myext|Mine|file:///x|main|abc\n")
    assert c.post("/api/admin/plugins/nope/toggle", json={"enabled": True}).status_code == 404
    assert c.post("/api/admin/plugins/kpi/remove").status_code == 400
    j = c.post("/api/admin/plugins/myext/toggle", json={"enabled": False}).json()
    assert j["action"] == "disable" and j["name"] == "myext"
    (apps / "jobs" / f"{j['id']}.json").write_text(json.dumps({**j, "status": "done"}))
    assert c.post("/api/admin/plugins/myext/remove").json()["action"] == "remove"
    assert c.get("/api/admin/plugins/jobs/../x").status_code == 404
