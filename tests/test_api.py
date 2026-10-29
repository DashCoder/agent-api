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
