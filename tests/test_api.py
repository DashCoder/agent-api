import json

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password, verify_password
from app.config import Settings
from app.hermes import translate_hermes_event
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    s = Settings(hermes_mode="mock", data_dir=tmp_path, admin_password="secret",
                 mock_delay_s=0, secret_key="test", _env_file=None)
    return TestClient(create_app(s))


def sse_events(text: str):
    out = []
    for block in text.strip().split("\n\n"):
        data = [l[5:].strip() for l in block.splitlines() if l.startswith("data:")]
        if data:
            out.append(json.loads("\n".join(data)))
    return out


def test_health_and_config(client):
    assert client.get("/api/health").json()["agent"]["mode"] == "mock"
    cfg = client.get("/api/config").json()
    assert cfg["assistant"]["name"] == "Builder"
    assert len(cfg["quickReplies"]) == 4
    assert "business" not in cfg


def test_chat_stream_and_history(client):
    sess = client.post("/api/chat/sessions").json()
    assert sess["messages"][0]["role"] == "assistant"
    r = client.post(f"/api/chat/sessions/{sess['id']}/stream", json={"text": "What are your rates?"})
    events = sse_events(r.text)
    assert events[0]["type"] == "start"
    reply = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "£49" in reply
    assert events[-1]["type"] == "done"
    msgs = client.get(f"/api/chat/sessions/{sess['id']}/messages").json()["messages"]
    assert [m["role"] for m in msgs] == ["assistant", "user", "assistant"]


def test_sessions_are_private(client, tmp_path):
    sess = client.post("/api/chat/sessions").json()
    other = TestClient(client.app)  # new cookie jar = different visitor
    assert other.get(f"/api/chat/sessions/{sess['id']}/messages").status_code == 404


def test_approval_flow(client):
    sess = client.post("/api/chat/sessions").json()
    events = sse_events(client.post(f"/api/chat/sessions/{sess['id']}/stream",
                                    json={"text": "delete old files"}).text)
    approval = next(e for e in events if e["type"] == "approval")
    res = client.post(f"/api/chat/runs/{approval['runId']}/approval",
                      json={"approve": True, "sessionId": sess["id"]}).json()
    assert res["status"] == "approved" and res["savedMessage"]["text"].startswith("Done")


def test_saved_replies_and_save_work(client):
    item = client.post("/api/saved-replies", json={"text": "Remember this"}).json()
    assert client.get("/api/saved-replies").json()["items"][0]["id"] == item["id"]
    code = client.post("/api/save-work/start", json={"email": "Jo@Example.com"}).json()["devCode"]
    assert client.post("/api/save-work/verify", json={"email": "jo@example.com", "code": "000000x"}).status_code == 400
    assert client.post("/api/save-work/verify", json={"email": "jo@example.com", "code": code}).json()["email"] == "jo@example.com"
    assert len(client.get("/api/saved-replies").json()["items"]) == 1  # moved to the email owner
    client.delete(f"/api/saved-replies/{item['id']}")
    assert client.get("/api/saved-replies").json()["items"] == []


def test_admin_requires_login(client):
    assert client.get("/api/admin/dashboard").status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": "secret"}).status_code == 200
    dash = client.get("/api/admin/dashboard").json()
    assert [c["id"] for c in dash["checklist"]] == ["phone", "knowledge", "calendar", "widget"]


def test_admin_settings_and_knowledge(client):
    client.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    client.put("/api/admin/settings", json={"assistant": {"name": "Ava"}, "phone": {"number": "+44 20 1234 5678"}})
    assert client.get("/api/config").json()["assistant"]["name"] == "Ava"
    assert client.get("/api/config").json()["assistant"]["greeting"].startswith("Hi!")  # deep-merged
    client.post("/api/admin/knowledge", json={"kind": "url", "value": "https://example.com/faq"})
    dash = client.get("/api/admin/dashboard").json()
    done = {c["id"]: c["done"] for c in dash["checklist"]}
    assert done["phone"] and done["knowledge"]
    assert client.get("/api/admin/hermes").json()["mode"] == "mock"


def test_upload(client):
    r = client.post("/api/chat/uploads", files={"file": ("notes ../x.txt", b"hello", "text/plain")})
    assert r.status_code == 200 and "/" not in r.json()["filename"]


def test_password_hash_roundtrip():
    h = hash_password("pw")
    assert verify_password("pw", h) and not verify_password("nope", h)


@pytest.mark.parametrize("event,payload,expected", [
    ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "Hi"}, {"type": "delta", "text": "Hi"}),
    ("message", {"type": "response.output_item.added", "item": {"type": "function_call", "name": "terminal"}},
     {"type": "progress", "label": "Working on the computer…"}),
    ("hermes.tool.progress", {"tool": "web_search"}, {"type": "progress", "label": "Searching the web…"}),
    ("message", {"choices": [{"delta": {"content": "Yo"}}]}, {"type": "delta", "text": "Yo"}),
    ("message", {"type": "response.created"}, None),
])
def test_translate_hermes_event(event, payload, expected):
    assert translate_hermes_event(event, payload) == expected


def test_tts_returns_mp3_and_cleans_text(client, monkeypatch):
    from app.routers import tts as tts_mod

    seen = {}

    async def fake(text, voice):
        seen["text"], seen["voice"] = text, voice
        return b"ID3fake"

    monkeypatch.setattr(tts_mod, "synthesize", fake)
    r = client.post("/api/tts", json={"text": "**Done!** See https://x.y\n```py\nprint(1)\n```"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    assert seen["voice"] == "en-GB-RyanNeural"
    assert "print" not in seen["text"] and "https" not in seen["text"] and "*" not in seen["text"]
    assert client.post("/api/tts", json={"text": "hi", "voice": "bad voice; rm"}).status_code == 400
    assert client.post("/api/tts", json={"text": "```x```"}).status_code in (200, 204)


def test_tts_reports_unavailable(client, monkeypatch):
    from app.routers import tts as tts_mod

    async def broken(text, voice):
        raise OSError("offline")

    monkeypatch.setattr(tts_mod, "synthesize", broken)
    assert client.post("/api/tts", json={"text": "hello"}).status_code == 503


def test_simli_token_is_off_until_configured(client):
    assert client.get("/api/config").json()["avatar"]["simliAvailable"] is False
    assert client.post("/api/simli/token").status_code == 404


def test_simli_token_cooldown_and_daily_cap(tmp_path, monkeypatch):
    from app.routers import simli as simli_mod

    s = Settings(hermes_mode="mock", data_dir=tmp_path, admin_password="secret", mock_delay_s=0,
                 secret_key="test", simli_api_key="sk", simli_daily_sessions=1, _env_file=None)
    app = create_app(s)
    calls = []

    async def fake_token(settings, face_id):
        calls.append(face_id)
        return "tok-123"

    monkeypatch.setattr(simli_mod, "request_token", fake_token)
    simli_mod._last_request.clear()
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    c.put("/api/admin/settings", json={"avatar": {"simliEnabled": True, "simliFaceId": "face-1"}})
    assert c.get("/api/config").json()["avatar"]["simliAvailable"] is True
    r = c.post("/api/simli/token")
    assert r.status_code == 200 and r.json()["sessionToken"] == "tok-123" and calls == ["face-1"]
    assert c.post("/api/simli/token").status_code == 429          # cooldown
    simli_mod._last_request.clear()
    assert c.post("/api/simli/token").status_code == 429          # daily cap of 1
    assert "sk" not in c.get("/api/config").text


def test_files_only_served_from_allowed_folders(tmp_path):
    allowed = tmp_path / "generated"
    allowed.mkdir()
    img = allowed / "robot.png"
    img.write_bytes(b"\x89PNG fake")
    secret = tmp_path / "secret.env"
    secret.write_text("KEY=1")
    (allowed / "link.env").symlink_to(secret)
    s = Settings(hermes_mode="mock", data_dir=tmp_path / "d", mock_delay_s=0, secret_key="t",
                 file_dirs=str(allowed), _env_file=None)
    c = TestClient(create_app(s))
    r = c.get("/api/files", params={"path": str(img)})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    r = c.get("/api/files", params={"path": str(img), "download": "true"})
    assert "attachment" in r.headers["content-disposition"]
    for bad in (str(secret), str(allowed / "link.env"), str(allowed / ".." / "secret.env"), "/etc/passwd",
                str(allowed / "missing.png")):
        assert c.get("/api/files", params={"path": bad}).status_code == 404


def test_remove_a_question_and_its_answer(tmp_path):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    c = TestClient(create_app(Settings(hermes_mode="mock", data_dir=tmp_path, admin_password="x", mock_delay_s=0,
                                       secret_key="t", _env_file=None), plugins=[]))
    sid = c.post("/api/chat/sessions").json()["id"]
    c.post(f"/api/chat/sessions/{sid}/stream", json={"text": "What are your rates?", "learn": False})
    c.post(f"/api/chat/sessions/{sid}/stream", json={"text": "Do you cover Leeds?", "learn": False})
    msgs = c.get(f"/api/chat/sessions/{sid}/messages").json()["messages"]
    q1 = next(m for m in msgs if m["text"] == "What are your rates?")
    assert len(c.delete(f"/api/chat/sessions/{sid}/messages/{q1['id']}").json()["removed"]) == 2
    left = [m["text"] for m in c.get(f"/api/chat/sessions/{sid}/messages").json()["messages"]]
    assert "What are your rates?" not in left and "Do you cover Leeds?" in left
    assert c.delete(f"/api/chat/sessions/{sid}/messages/nope").status_code == 404
    other = TestClient(c.app)   # another visitor (no cookie) can't touch it
    assert other.delete(f"/api/chat/sessions/{sid}/messages/{msgs[0]['id']}").status_code == 404


def test_model_from_hermes_config(tmp_path):
    from app.routers.admin import hermes_model
    (tmp_path / "config.yaml").write_text("toolsets:\n  - web\nmodel:\n  default: anthropic/claude-sonnet-4.5\n"
                                          "  provider: openrouter   # via OpenRouter\n  api_key: sk-secret\nterminal:\n  backend: local\n")
    assert hermes_model(tmp_path) == {"model": "anthropic/claude-sonnet-4.5", "provider": "openrouter"}
    (tmp_path / "config.yaml").write_text("model: qwen3:14b\n")
    assert hermes_model(tmp_path) == {"model": "qwen3:14b"}
    assert hermes_model(tmp_path / "missing") is None


def test_chat_as_one_agent(tmp_path):
    """/?agent=demo: the greeting and Hermes' instructions are that agent's."""
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    from app.hermes import build_instructions
    from tests.test_plugins import plugin
    import dataclasses
    p = dataclasses.replace(plugin, public_info={**plugin.public_info, "agentName": "Dex", "assistantHint": "Keep demo notes."})
    c = TestClient(create_app(Settings(hermes_mode="mock", data_dir=tmp_path, admin_password="x", mock_delay_s=0,
                                       secret_key="t", _env_file=None), plugins=[p]))
    s = c.post("/api/chat/sessions", json={"agent": "demo"}).json()
    assert s["messages"][0]["text"] == "Hi! I'm Dex 👋 I keep demo notes. What would you like to do?"
    plain = c.post("/api/chat/sessions").json()
    assert plain["messages"][0]["text"].startswith("Hi! I'm Builder")
    assert c.post("/api/chat/sessions", json={"agent": "nobody"}).json()["messages"][0]["text"].startswith("Hi! I'm Builder")
    text = build_instructions({"assistant": {"name": "Dex"}, "focus": {"id": "demo", "name": "Dex", "hint": "Keep demo notes."},
                               "agentHints": ["Keep demo notes."]}, True)
    assert text.startswith("You are Dex. The person opened Dex's chat") and "show that table as it is" in text
    t2 = build_instructions({"assistant": {}, "focus": {"id": "demo", "name": "Dex", "hint": "", "tools": ["add_note"]}}, True)
    assert "Dex's tools (in the inagentic MCP server): add_note" in t2 and "never ask the person to connect" in t2


def test_simli_refusals_say_what_to_fix():
    import asyncio
    import httpx
    from app.routers import simli as simli_mod
    seen = {}

    def route(req):
        seen["key"] = req.headers["x-simli-api-key"]
        return httpx.Response(401, json={"detail": "Unauthorized"})
    s = Settings(hermes_mode="mock", data_dir="/tmp/x", admin_password="p", secret_key="t",
                 simli_api_key=' "sk-abc" ', _env_file=None)
    try:
        asyncio.run(simli_mod.request_token(s, "face", httpx.MockTransport(route)))
    except Exception as e:  # noqa: BLE001
        assert "didn't accept the API key" in e.detail and "SIMLI_API_KEY" in e.detail
    assert seen["key"] == "sk-abc"
    assert "another Simli account" in simli_mod.refusal(403, "")
