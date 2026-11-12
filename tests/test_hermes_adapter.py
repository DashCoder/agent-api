"""HermesAgent against a fake Hermes API server (httpx.MockTransport)."""
import asyncio
import json

import httpx

from app.config import Settings
from app.hermes import HermesAgent

SSE = (
    ": keepalive\n\n"
    "event: response.created\ndata: {\"type\": \"response.created\"}\n\n"
    "event: response.output_item.added\ndata: {\"type\": \"response.output_item.added\", \"item\": {\"type\": \"function_call\", \"name\": \"web_search\"}}\n\n"
    "event: response.output_text.delta\ndata: {\"type\": \"response.output_text.delta\", \"delta\": \"Hello \"}\n\n"
    "event: response.output_text.delta\ndata: {\"type\": \"response.output_text.delta\", \"delta\": \"there\"}\n\n"
    "event: response.completed\ndata: {\"type\": \"response.completed\"}\n\n"
)


def make(handler):
    s = Settings(hermes_mode="hermes", hermes_api_key="k", _env_file=None)
    return HermesAgent(s, transport=httpx.MockTransport(handler))


def collect(agent, **kw):
    async def run():
        return [e async for e in agent.stream("conv1", "hi", learn=kw.get("learn", False), context={})]
    return asyncio.run(run())


def test_stream_translates_events_and_sends_conversation():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, text=SSE, headers={"content-type": "text/event-stream"})

    events = collect(make(handler))
    assert events == [
        {"type": "progress", "label": "Searching the web…"},
        {"type": "delta", "text": "Hello "},
        {"type": "delta", "text": "there"},
    ]
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["conversation"] == "conv1" and seen["body"]["stream"] is True
    assert "instructions" in seen["body"]  # learn=False adds the no-learning instruction


def test_instructions_carry_the_agent_page_answers_and_style():
    from app.hermes import build_instructions
    ctx = {"assistant": {"name": "Builder"}, "business": {"services": "List last 5 Jira tickets", "rates": " "},
           "agentHints": ["Turn Jira tickets into pull requests"]}
    learn = build_instructions(ctx, True)
    assert "You are Builder" in learn and "List last 5 Jira tickets" in learn and "Rates" not in learn
    assert "Turn Jira tickets into pull requests" in learn and "mcp__inagentic__" in learn  # told not to show these
    assert "do not write to long-term memory" not in learn
    assert "do not write to long-term memory" in build_instructions(ctx, False)
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, text=SSE)

    async def run():
        return [e async for e in make(handler).stream("c", "hi", learn=True, context=ctx)]
    asyncio.run(run())
    assert seen["body"]["instructions"] == learn


def test_http_error_becomes_error_event():
    events = collect(make(lambda req: httpx.Response(401, text="bad key")))
    assert events[0]["type"] == "error" and "401" in events[0]["message"]


def test_unreachable_becomes_error_event():
    def handler(req):
        raise httpx.ConnectError("refused")
    events = collect(make(handler))
    assert events[0]["type"] == "error" and "Can't reach Hermes" in events[0]["message"]


def test_status_and_skills():
    def handler(req):
        if req.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "hermes-agent"}]})
        if req.url.path == "/v1/skills":
            return httpx.Response(200, json={"data": [{"name": "book", "description": "Books"}]})
        return httpx.Response(404)
    agent = make(handler)
    status = asyncio.run(agent.status())
    assert status["reachable"] and status["models"] == ["hermes-agent"]
    assert asyncio.run(agent.skills()) == [{"name": "book", "description": "Books"}]


def test_failed_response_shows_the_readable_reason():
    from app.hermes import translate_hermes_event
    ev = translate_hermes_event("response.failed", {"type": "response.failed", "response": {
        "status": "failed", "error": {"code": "agent_error", "message": "Ollama loaded qwen3:14b with only 40,960 tokens"}}})
    assert ev == {"type": "error", "message": "Ollama loaded qwen3:14b with only 40,960 tokens"}
