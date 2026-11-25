"""Chat without Hermes (HERMES_MODE=direct) against a fake Ollama: streaming, tool calls, access, history."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.direct import ThinkFilter
from app.main import create_app
from app.plugins import McpTool, Plugin

from .test_api import sse_events

CALLS: list = []


async def lookup(ctx, args):
    CALLS.append({"args": args, "role": ctx.caller["role"], "app": ctx.request.app is not None})
    return {"company": "INAGENTIC LTD", "due": "2026-12-21", "key": "sk-or-v1-" + "a" * 40}


plugin = Plugin(name="neil", mcp_tools=[McpTool("neil_next_deadline", "Next filing deadline",
                                                {"type": "object", "properties": {"number": {"type": "string"}}},
                                                lookup)],
                public_info={"agentName": "Neil", "assistantHint": "Tracks filing deadlines."})


def chunks(*deltas, done=True):
    out = "".join(f"data: {json.dumps({'choices': [{'delta': d}]})}\n\n" for d in deltas)
    return out + ("data: [DONE]\n\n" if done else "")


class FakeOllama:
    def __init__(self, *replies):
        self.replies, self.bodies = list(replies), []

    def __call__(self, req: httpx.Request):
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3.6:35b-a3b"}]})
        assert req.url.path == "/v1/chat/completions"
        self.bodies.append(json.loads(req.content))
        return httpx.Response(200, text=self.replies.pop(0), headers={"content-type": "text/event-stream"})


def make(tmp_path, fake, **kw):
    s = Settings(hermes_mode="direct", data_dir=tmp_path, admin_password="secret", secret_key="t", mcp_key="k",
                 ollama_url="http://ollama:11434", _env_file=None, **kw)
    c = TestClient(create_app(s, plugins=[plugin]))
    c.app.state.agent.transport = httpx.MockTransport(fake)
    if c.app.state.public_agent:
        c.app.state.public_agent.transport = httpx.MockTransport(fake)
    return c


def chat(c, text, agent=None):
    sid = c.post("/api/chat/sessions", json={"agent": agent}).json()["id"]
    r = c.post(f"/api/chat/sessions/{sid}/stream", json={"text": text, "agent": agent})
    return sid, sse_events(r.text)


def login(c):
    assert c.post("/api/auth/login", json={"username": "admin", "password": "secret"}).status_code == 200


def test_think_filter_handles_split_tags():
    f = ThinkFilter()
    got = "".join(f.feed(p) for p in ["Hel", "lo <thi", "nk>secret</th", "ink> world <", "b>"]) + f.flush()
    assert got == "Hello  world <b>"


def test_answers_with_a_tool_call_then_words(tmp_path):
    CALLS.clear()
    fake = FakeOllama(
        chunks({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "neil_next_deadline",
                                                                     "arguments": "{\"number\": "}}]},
               {"tool_calls": [{"index": 0, "function": {"arguments": "\"16702679\"}"}}]}),
        chunks({"content": "<think>hmm</think>Your next "}, {"content": "deadline is 21 December."}))
    c = make(tmp_path, fake)
    login(c)
    sid, events = chat(c, "When is my next filing?", agent="neil")
    assert {"type": "progress", "label": "Asking Neil…"} in events
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert text == "Your next deadline is 21 December."
    assert CALLS == [{"args": {"number": "16702679"}, "role": "admin", "app": True}]
    first, second = fake.bodies
    assert [t["function"]["name"] for t in first["tools"]] == ["neil_next_deadline"]
    assert first["model"] == "qwen3.6:35b-a3b" and first["stream"] is True
    assert "You are Neil" in first["messages"][0]["content"] and len(first["messages"][0]["content"]) < 6000
    tool_msg = second["messages"][-1]
    assert tool_msg["role"] == "tool" and "INAGENTIC LTD" in tool_msg["content"]
    assert "sk-or-v1-" not in tool_msg["content"]                    # secrets never reach the model
    saved = c.get(f"/api/chat/sessions/{sid}/messages").json()
    assert saved["messages"][-1]["text"] == "Your next deadline is 21 December."


def test_history_and_model_choice(tmp_path):
    fake = FakeOllama(chunks({"content": "First."}), chunks({"content": "Second."}))
    c = make(tmp_path, fake)
    login(c)
    r = c.put("/api/admin/models/chat", json={"provider": "ollama", "model": "qwen3:32b"})
    assert r.status_code == 200, r.text
    sid = c.post("/api/chat/sessions", json={}).json()["id"]
    c.post(f"/api/chat/sessions/{sid}/stream", json={"text": "one"})
    c.post(f"/api/chat/sessions/{sid}/stream", json={"text": "two"})
    msgs = fake.bodies[1]["messages"]
    assert [m["content"] for m in msgs[1:]] == ["one", "First.", "two"]
    assert fake.bodies[1]["model"] == "qwen3:32b"


def test_untrusted_caller_never_gets_tools(tmp_path):
    fake = FakeOllama(chunks({"content": "Hello!"}))
    c = make(tmp_path, fake, chat_access="public")
    _, events = chat(c, "hi")
    assert [e["text"] for e in events if e["type"] == "delta"] == ["Hello!"]
    assert "tools" not in fake.bodies[0]


def test_model_errors_are_shown(tmp_path):
    def down(req):
        return httpx.Response(404, json={"error": "model 'qwen3.6:35b-a3b' not found"})
    c = make(tmp_path, down)
    login(c)
    _, events = chat(c, "hi")
    err = [e for e in events if e["type"] == "error"][0]["message"]
    assert "not found" in err and "qwen3.6:35b-a3b" in err


def test_health_says_direct(tmp_path):
    c = make(tmp_path, FakeOllama())
    assert c.get("/api/health").json()["agent"]["mode"] == "direct"
