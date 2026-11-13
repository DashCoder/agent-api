"""OpenRouter traces (Broadcast → Webhook) in Agent logs: push, pull from the relay, blocked calls."""
import asyncio
import json

import httpx
from fastapi.testclient import TestClient

from app import openrouter_traces as ot
from app.config import Settings
from app.main import create_app
from tests.test_models import make, writer_plugin


def otlp(span_id, attrs, status=None, ms=1500):
    def val(v):
        if isinstance(v, bool):
            return {"boolValue": v}
        if isinstance(v, int):
            return {"intValue": str(v)}
        if isinstance(v, float):
            return {"doubleValue": v}
        if isinstance(v, list):
            return {"arrayValue": {"values": [val(x) for x in v]}}
        return {"stringValue": v}
    span = {"traceId": "t1", "spanId": span_id, "name": "chat", "startTimeUnixNano": "1705312800000000000",
            "endTimeUnixNano": str(1705312800000000000 + ms * 1_000_000),
            "attributes": [{"key": k, "value": val(v)} for k, v in attrs.items()]}
    if status:
        span["status"] = status
    return {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "openrouter"}}]},
                               "scopeSpans": [{"spans": [span]}]}]}


OK = otlp("s1", {"gen_ai.request.model": "qwen/qwen3-coder", "gen_ai.usage.prompt_tokens": 100,
                 "gen_ai.usage.completion_tokens": 50, "gen_ai.usage.cost": 0.0123, "user.id": "inagentic:writer",
                 "gen_ai.response.finish_reasons": ["stop"], "gen_ai.provider.name": "Alibaba"})
FILTERED = otlp("s2", {"gen_ai.request.model": "openai/gpt-4o", "user.id": "inagentic:writer",
                       "gen_ai.response.finish_reasons": ["content_filter"]})
MODERATED = otlp("s3", {"gen_ai.request.model": "openai/gpt-4o", "http.status_code": 403,
                        "openrouter_error.code": 403, "openrouter_error.message": "Input was flagged: harassment"},
                 status={"code": 2, "message": "flagged"})


def test_push_with_token_test_connection_and_dedupe(tmp_path):
    c, _ = make(tmp_path, openrouter_traces_token="hook-secret")
    assert c.post("/api/hooks/openrouter", json=OK).status_code == 401
    auth = {"Authorization": "Bearer hook-secret"}
    assert c.post("/api/hooks/openrouter", headers={**auth, "X-Test-Connection": "true"}, content=b"").json()["test"]
    assert c.post("/api/hooks/openrouter", headers=auth, json=OK).json() == {"ok": True, "added": 1}
    assert c.post("/api/hooks/openrouter", headers=auth, json=OK).json()["added"] == 0          # delivered twice
    assert c.put("/api/hooks/openrouter", headers=auth, json=FILTERED).json()["added"] == 1
    c.post("/api/hooks/openrouter", headers=auth, json=MODERATED)
    writer = [e for e in c.get("/api/admin/activity?agent=writer").json()["items"] if e["action"] == "openrouter.trace"]
    ok, filtered = writer[1]["detail"], writer[0]["detail"]
    assert ok["model"] == "qwen/qwen3-coder" and ok["tokensIn"] == 100 and ok["tokensOut"] == 50 and ok["cost"] == 0.0123
    assert ok["ms"] == 1500 and ok["finish"] == "stop" and ok["provider"] == "Alibaba" and "blocked" not in ok
    assert filtered["blocked"] == "the provider's content filter stopped the answer"
    # no agent tag (e.g. Hermes' own calls): under the assistant
    hermes = [e for e in c.get("/api/admin/activity?agent=assistant").json()["items"] if e["action"] == "openrouter.trace"]
    assert len(hermes) == 1 and hermes[0]["detail"]["blocked"].startswith("OpenRouter's moderation refused the request")
    assert "harassment" in hermes[0]["detail"]["blocked"]
    st = c.get("/api/admin/openrouter-traces").json()
    assert st["mode"] == "push" and st["received"] >= 3


def test_push_is_off_without_a_token(tmp_path):
    c, _ = make(tmp_path)
    assert c.post("/api/hooks/openrouter", json=OK).status_code == 404
    assert c.get("/api/admin/openrouter-traces").json()["mode"] is None


def test_pull_from_the_relay_then_ack(tmp_path):
    s = Settings(hermes_mode="mock", data_dir=tmp_path, admin_password="p", secret_key="t", _env_file=None,
                 openrouter_relay_url="https://relay.example/", openrouter_relay_token="pull-secret")
    app = create_app(s, plugins=[writer_plugin()])
    queue = [{"id": "r1", "body": json.dumps(OK)}, {"id": "r2", "body": json.dumps(FILTERED)}, {"id": "r3", "body": "not json"}]
    acked, auths = [], []

    def route(req):
        auths.append(req.headers.get("authorization"))
        if req.url.path == "/pull":
            items, queue[:] = queue[:], []
            return httpx.Response(200, json={"items": items})
        acked.extend(json.loads(req.content)["ids"])
        return httpx.Response(200, json={"ok": True})
    assert asyncio.run(ot.pull_once(app, httpx.MockTransport(route))) == 2
    assert acked == ["r1", "r2", "r3"] and set(auths) == {"Bearer pull-secret"}
    with TestClient(app) as c:
        c.post("/api/auth/login", json={"username": "admin", "password": "p"})
        assert c.get("/api/admin/openrouter-traces").json()["mode"] == "relay"


def test_agents_tag_their_openrouter_calls_and_blocked_calls_say_so(tmp_path):
    c, calls = make(tmp_path)
    c.put("/api/admin/models/writer", json={"provider": "openrouter", "model": "openai/gpt-4o"})
    c.post("/api/admin/models/writer/test")
    assert calls[-1][1]["user"] == "inagentic:writer"

    def blocked(req):
        return httpx.Response(403, json={"error": {"code": 403, "message": "Input was flagged",
                                                   "metadata": {"reasons": ["harassment"], "provider_name": "OpenAI"}}})
    c.app.state.llm_transport = httpx.MockTransport(blocked)
    assert not c.post("/api/admin/models/writer/test").json()["ok"]

    def filtered(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "content_filter"}]})
    c.app.state.llm_transport = httpx.MockTransport(filtered)
    c.post("/api/admin/models/writer/test")
    ev = [e["detail"] for e in c.get("/api/admin/activity?agent=writer").json()["items"] if e["action"] == "llm.call"]
    assert ev[0]["blocked"].startswith("the provider's content filter")
    assert ev[1]["blocked"] == "OpenRouter's moderation refused the request: harassment"
