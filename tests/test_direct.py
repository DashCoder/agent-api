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


def chunks(*msgs):
    """Ollama's /api/chat stream: one JSON object per line."""
    lines = [json.dumps({"message": {"role": "assistant", **m}, "done": False}) for m in msgs]
    lines.append(json.dumps({"message": {"role": "assistant", "content": ""}, "done": True,
                             "prompt_eval_count": 900, "eval_count": 12}))
    return "\n".join(lines) + "\n"


def sse(*deltas):
    """The OpenAI-style stream (OpenRouter)."""
    return "".join(f"data: {json.dumps({'choices': [{'delta': d}]})}\n\n" for d in deltas) + "data: [DONE]\n\n"


class FakeOllama:
    def __init__(self, *replies):
        self.replies, self.bodies = list(replies), []

    def __call__(self, req: httpx.Request):
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3.6:35b-a3b"}]})
        assert req.url.path in ("/api/chat", "/api/v1/chat/completions"), req.url.path
        self.bodies.append(json.loads(req.content))
        return httpx.Response(200, text=self.replies.pop(0))


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
        chunks({"content": "", "tool_calls": [{"function": {"name": "neil_next_deadline",
                                                            "arguments": {"number": "16702679"}}}]}),
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
    assert first["model"] == "qwen3.6:35b-a3b" and first["stream"] is True and first["think"] is False
    assert "You are Neil" in first["messages"][0]["content"] and len(first["messages"][0]["content"]) < 6000
    assert second["messages"][-2]["tool_calls"][0]["function"]["arguments"] == {"number": "16702679"}
    tool_msg = second["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_name"] == "neil_next_deadline"
    assert "INAGENTIC LTD" in tool_msg["content"]
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


def test_openrouter_streams_the_openai_way(tmp_path):
    CALLS.clear()
    fake = FakeOllama(
        sse({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "neil_next_deadline", "arguments": "{\"number\": "}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": "\"16702679\"}"}}]}),
        sse({"content": "21 December."}))
    c = make(tmp_path, fake, openrouter_api_key="sk-or-test")
    login(c)
    assert c.put("/api/admin/models/chat", json={"provider": "openrouter", "model": "qwen/qwen3-coder"}).status_code == 200
    c.app.state.agent.transport = httpx.MockTransport(lambda req: fake(_as_api(req)))
    _, events = chat(c, "next deadline?")
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "21 December."
    assert CALLS[0]["args"] == {"number": "16702679"}
    assert fake.bodies[1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": fake.bodies[1]["messages"][-1]["content"]}


def test_tool_descriptions_are_kept_short():
    from app.direct import compact, short
    long = "Logs an activity. " + "Very detailed explanation. " * 40
    assert short(long, 300).endswith(".") and len(short(long, 300)) <= 300
    schema = {"type": "object", "title": "X", "properties": {"n": {"type": "string", "description": long,
                                                                   "enum": ["a", "b"]}}}
    small = compact(schema)
    assert "title" not in small and small["properties"]["n"]["enum"] == ["a", "b"]
    assert len(small["properties"]["n"]["description"]) <= 120


def _as_api(req):
    """OpenRouter's URL mapped onto the fake's path check."""
    return httpx.Request(req.method, "http://x/api/v1/chat/completions", content=req.content, headers=req.headers)


def test_chat_box_picks_local_or_openrouter(tmp_path):
    seen = []

    def fake(req):
        seen.append((req.url.host, json.loads(req.content)["model"]))
        if req.url.host == "openrouter.ai":
            return httpx.Response(200, text=sse({"content": "From the cloud."}))
        return httpx.Response(200, text=chunks({"content": "From here."}))
    c = make(tmp_path, FakeOllama(), openrouter_api_key="sk-or-test")
    c.app.state.agent.transport = httpx.MockTransport(fake)
    assert c.get("/api/config").json()["chatModels"] == ["local", "openrouter"]
    login(c)
    sid = c.post("/api/chat/sessions", json={}).json()["id"]
    for model in ("openrouter", "local", None):
        r = c.post(f"/api/chat/sessions/{sid}/stream", json={"text": "hi", "model": model})
        assert r.status_code == 200
    assert seen == [("openrouter.ai", "google/gemini-2.5-flash"), ("ollama", "qwen3.6:35b-a3b"),
                    ("ollama", "qwen3.6:35b-a3b")]
    assert c.put("/api/admin/models/chat-cloud", json={"provider": "openrouter", "model": "anthropic/claude-sonnet-4.5"}).status_code == 200
    c.post(f"/api/chat/sessions/{sid}/stream", json={"text": "hi", "model": "openrouter"})
    assert seen[-1] == ("openrouter.ai", "anthropic/claude-sonnet-4.5")


def test_no_openrouter_choice_without_a_key_or_for_visitors(tmp_path):
    seen = []

    def fake(req):
        seen.append(req.url.host)
        return httpx.Response(200, text=chunks({"content": "Hello."}))
    c = make(tmp_path, FakeOllama())
    assert c.get("/api/config").json()["chatModels"] == ["local"]
    c2 = make(tmp_path / "b", FakeOllama(), openrouter_api_key="sk-or-test", chat_access="public")
    c2.app.state.public_agent.transport = httpx.MockTransport(fake)
    sid = c2.post("/api/chat/sessions", json={}).json()["id"]
    c2.post(f"/api/chat/sessions/{sid}/stream", json={"text": "hi", "model": "openrouter"})
    assert seen == ["ollama"]                         # an anonymous visitor can't run up cloud costs
