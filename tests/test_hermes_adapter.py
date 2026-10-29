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


def test_learn_mode_sends_no_instructions():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, text=SSE)

    collect(make(handler), learn=True)
    assert "instructions" not in seen["body"]


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
