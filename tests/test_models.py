"""Model & tools: each agent's model (ollama / openrouter / bedrock), logged calls, and Hermes' own model."""
import json

import httpx
from fastapi.testclient import TestClient

from app import hermes_config, llm
from app.config import Settings
from app.main import create_app
from app.plugins import Plugin

HERMES_YAML = """# Hermes config
toolsets:
  - web
model:
  default: anthropic/claude-sonnet-4.5
  provider: openrouter

terminal:
  backend: local
"""


def writer_plugin():
    def setup(app):
        llm.register(app, "writer", name="Max", uses="Writes articles", default={"provider": "bedrock", "model": "eu.claude"})
    return Plugin(name="writer", version="1", api_versions=(1,), on_startup=setup)


def make(tmp_path, **kw):
    kw.setdefault("hermes_mode", "mock")
    s = Settings(data_dir=tmp_path, admin_password="secret", mock_delay_s=0, secret_key="t",
                 openrouter_api_key="or-key", hermes_home=tmp_path / "hermes", _env_file=None, **kw)
    app = create_app(s, plugins=[writer_plugin()])
    calls = []

    def route(req):
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:14b"}]})
        body = json.loads(req.content)
        calls.append((req.url.host, body, req.headers.get("authorization")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "<think>x</think>OK"}}],
                                         "usage": {"prompt_tokens": 12, "completion_tokens": 1, "cost": 0.0021}})
    app.state.llm_transport = httpx.MockTransport(route)
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": "admin", "password": "secret"}).status_code == 200
    return c, calls


def test_choose_a_model_per_agent_and_every_call_is_logged(tmp_path):
    c, calls = make(tmp_path)
    o = c.get("/api/admin/models").json()
    w = o["agents"][0]
    assert w["id"] == "writer" and w["current"] == {"provider": "bedrock", "model": "eu.claude"}
    assert w["label"] == "eu.claude (AWS Bedrock)" and o["providers"]["ollama"]["models"] == ["qwen3:14b"]
    assert c.put("/api/admin/models/writer", json={"provider": "gpt", "model": "x"}).status_code == 400
    assert c.put("/api/admin/models/writer", json={"provider": "ollama", "model": "bad model!"}).status_code == 400
    r = c.put("/api/admin/models/writer", json={"provider": "ollama", "model": "qwen3:14b"}).json()
    assert r["label"] == "qwen3:14b (local, Ollama)"
    t = c.post("/api/admin/models/writer/test").json()
    assert t["ok"] and t["reply"] == "OK"
    host, body, auth = calls[-1]
    assert host == "127.0.0.1" and body["model"] == "qwen3:14b" and auth is None
    c.put("/api/admin/models/writer", json={"provider": "openrouter", "model": "qwen/qwen3-coder"})
    c.post("/api/admin/models/writer/test")
    host, body, auth = calls[-1]
    assert host == "openrouter.ai" and auth == "Bearer or-key" and body["usage"] == {"include": True}
    ev = [e for e in c.get("/api/admin/activity?agent=writer").json()["items"]]
    model_calls = [e for e in ev if e["action"] == "llm.call"]
    assert [e["detail"]["model"] for e in model_calls] == ["qwen/qwen3-coder", "qwen3:14b"]
    assert model_calls[0]["detail"]["cost"] == 0.0021 and model_calls[0]["detail"]["tokensIn"] == 12
    assert "models.set" in [e["action"] for e in ev]
    u = c.get("/api/admin/usage?agent=writer").json()
    assert u["agents"][0]["today"]["modelCalls"] == 2 and u["agents"][0]["today"]["cost"] == 0.0042 and u["costTracked"]


def test_failed_calls_are_logged(tmp_path):
    c, _ = make(tmp_path)
    c.put("/api/admin/models/writer", json={"provider": "openrouter", "model": "x/y"})
    c.app.state.settings.openrouter_api_key = ""
    t = c.post("/api/admin/models/writer/test").json()
    assert not t["ok"] and "OPENROUTER_API_KEY" in t["error"]
    e = next(e for e in c.get("/api/admin/activity?agent=writer").json()["items"] if e["action"] == "llm.call")
    assert "OPENROUTER_API_KEY" in e["detail"]["error"]


def test_hermes_config_rewrites_only_the_model_block(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    (home / "config.yaml").write_text(HERMES_YAML)
    assert hermes_config.read(home) == {"model": "anthropic/claude-sonnet-4.5", "provider": "openrouter", "kind": "openrouter"}
    backup = hermes_config.write(home, "ollama", "qwen3:14b", "http://127.0.0.1:11434")
    text = (home / "config.yaml").read_text()
    assert "toolsets:\n  - web\nmodel:\n  default: qwen3:14b\n  provider: custom\n  base_url: http://127.0.0.1:11434/v1\n" in text
    assert "context_length: 65536\n  ollama_num_ctx: 65536" in text and "terminal:\n  backend: local" in text
    assert "claude-sonnet" not in text
    assert backup.read_text() == HERMES_YAML
    assert hermes_config.read(home)["kind"] == "ollama"
    hermes_config.write(home, "anthropic", "claude-sonnet-4-6", "http://x")
    assert hermes_config.read(home) == {"model": "claude-sonnet-4-6", "provider": "anthropic", "kind": "anthropic"}
    assert "base_url" not in (home / "config.yaml").read_text()


def test_save_hermes_model_restarts_tests_and_rolls_back(tmp_path, monkeypatch):
    c, _ = make(tmp_path, hermes_mode="hermes")
    (tmp_path / "hermes").mkdir()
    (tmp_path / "hermes" / "config.yaml").write_text(HERMES_YAML)
    o = c.get("/api/admin/models").json()["hermes"]
    assert o["label"] == "anthropic/claude-sonnet-4.5 (OpenRouter)"
    restarts = []

    async def restart(cmd, timeout=60):
        restarts.append(hermes_config.read(tmp_path / "hermes")["model"])
        return True, ""
    answers = iter([(True, "OK"), (False, "model not found")])

    async def probe(agent, wait_s=90):
        return next(answers)
    monkeypatch.setattr(hermes_config, "restart", restart)
    monkeypatch.setattr(hermes_config, "probe", probe)
    r = c.put("/api/admin/models/hermes", json={"provider": "ollama", "model": "qwen3:14b"}).json()
    assert r["ok"] and r["hermes"]["current"]["kind"] == "ollama" and "Hermes answered" in r["steps"][-1]
    r = c.put("/api/admin/models/hermes", json={"provider": "openrouter", "model": "no/such-model"}).json()
    assert not r["ok"] and "Put the previous model back (qwen3:14b)" in r["steps"][-1]
    assert hermes_config.read(tmp_path / "hermes")["model"] == "qwen3:14b"
    assert restarts == ["qwen3:14b", "no/such-model", "qwen3:14b"]
    acts = [e["action"] for e in c.get("/api/admin/audit").json()["items"]]
    assert "models.hermes_set" in acts and "models.hermes_failed" in acts
