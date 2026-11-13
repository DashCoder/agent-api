"""OpenRouter traces in the agent logs (OpenRouter → Settings → Observability → Broadcast → Webhook).

OpenRouter sends one OpenTelemetry (OTLP JSON) trace per model call: model, provider, tokens, cost, timing, finish
reason and errors. Each becomes an `openrouter.trace` entry in Agent logs, under the agent that made the call (agents
send `user: "inagentic:<agent>"` with each request; calls without it, e.g. Hermes' own, go under the assistant).
Calls OpenRouter or the provider blocked (moderation, a content filter) are marked as blocked.

Two ways in, because OpenRouter needs a public URL and the mini isn't public:

  relay (recommended): a tiny AWS Lambda (deploy/openrouter-relay) receives the webhook and queues it; agent-api pulls
         the queue every OPENROUTER_RELAY_INTERVAL_S while OPENROUTER_RELAY_URL and OPENROUTER_RELAY_TOKEN are set.
         Nothing on the mini is exposed. Remove the two settings to stop.
  push:  POST /api/hooks/openrouter with "Authorization: Bearer <OPENROUTER_TRACES_TOKEN>", if you publish agent-api
         yourself (e.g. Tailscale Funnel for that one path).

Prompts and answers are never stored here, only the numbers and statuses (switch on Privacy Mode in OpenRouter too).
"""
from __future__ import annotations

import asyncio
import gzip
import hmac
import json
import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request

from .auth import require_admin
from .db import DB

log = logging.getLogger("agent_api.openrouter_traces")
router = APIRouter()
_state: dict[str, Any] = {"received": 0, "lastAt": None, "lastPull": None, "lastError": None}
BLOCK_WORDS = ("moderation", "flagged", "content_filter", "content filter", "safety", "blocked", "policy")


# ------------------------------------------------------------------ OTLP JSON
def _value(v: dict[str, Any]) -> Any:
    if not isinstance(v, dict):
        return v
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return v["intValue"]
    if "doubleValue" in v:
        return v["doubleValue"]
    if "boolValue" in v:
        return v["boolValue"]
    if "arrayValue" in v:
        return [_value(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return {x["key"]: _value(x.get("value", {})) for x in v["kvlistValue"].get("values", [])}
    return None


def spans(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every span in an OTLP JSON body, with its attributes as a flat dict."""
    out = []
    for rs in payload.get("resourceSpans") or []:
        res = {a["key"]: _value(a.get("value", {})) for a in (rs.get("resource") or {}).get("attributes", [])}
        for ss in rs.get("scopeSpans") or []:
            for sp in ss.get("spans") or []:
                attrs = {a["key"]: _value(a.get("value", {})) for a in sp.get("attributes", [])}
                out.append({"traceId": sp.get("traceId"), "spanId": sp.get("spanId"), "name": sp.get("name"),
                            "start": int(sp.get("startTimeUnixNano") or 0), "end": int(sp.get("endTimeUnixNano") or 0),
                            "status": sp.get("status") or {}, "attrs": attrs, "resource": res,
                            "events": [e.get("name") for e in sp.get("events") or []]})
    return out


def _first(attrs: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if attrs.get(k) not in (None, ""):
            return attrs[k]
    return None


def _find(attrs: dict[str, Any], *parts: str) -> Any:
    """The first attribute whose name contains all the parts (OpenRouter adds fields over time)."""
    for k, v in attrs.items():
        if all(p in k.lower() for p in parts) and v not in (None, ""):
            return v
    return None


def summary(sp: dict[str, Any]) -> dict[str, Any]:
    a = sp["attrs"]
    finish = _first(a, "gen_ai.response.finish_reasons", "gen_ai.response.finish_reason") or _find(a, "finish_reason")
    if isinstance(finish, list):
        finish = ",".join(str(x) for x in finish)
    status_code = _find(a, "status_code") or _find(a, "statuscode")
    err_code = _find(a, "error", "code")
    err_msg = _find(a, "error", "message") or (sp["status"].get("message") if sp["status"].get("code") in (2, "STATUS_CODE_ERROR") else None)
    user = _first(a, "user.id", "gen_ai.user.id")
    agent = str(user)[len("inagentic:"):] if isinstance(user, str) and user.startswith("inagentic:") else None
    tin = _first(a, "gen_ai.usage.prompt_tokens", "gen_ai.usage.input_tokens")
    tout = _first(a, "gen_ai.usage.completion_tokens", "gen_ai.usage.output_tokens")
    cost = _first(a, "gen_ai.usage.cost", "gen_ai.usage.total_cost") or _find(a, "total", "cost") or _find(a, "cost")
    ms = round((sp["end"] - sp["start"]) / 1e6) if sp["end"] and sp["start"] else None
    out: dict[str, Any] = {
        "plugin": agent, "model": _first(a, "gen_ai.response.model", "gen_ai.request.model") or _find(a, "model"),
        "provider": _first(a, "gen_ai.provider.name", "gen_ai.system") or _find(a, "provider", "name"),
        "tokensIn": tin, "tokensOut": tout, "cost": float(cost) if isinstance(cost, (int, float, str)) and _num(cost) else None,
        "ms": ms, "ttftMs": _find(a, "time_to_first_token"), "finish": finish, "statusCode": status_code,
        "errorCode": err_code, "error": str(err_msg)[:300] if err_msg else None, "apiKey": _find(a, "api_key_name") or _find(a, "apikeyname"),
        "session": _first(a, "session.id"), "user": user, "generationId": _first(a, "gen_ai.response.id") or _find(a, "generation_id"),
        "spanId": sp["spanId"], "truncated": "openrouter.trace_truncated" in sp["events"]}
    out["blocked"] = blocked_reason(out)
    return {k: v for k, v in out.items() if v not in (None, "", False)}


def _num(x: Any) -> bool:
    try:
        float(x)
        return True
    except (TypeError, ValueError):
        return False


def blocked_reason(d: dict[str, Any]) -> str | None:
    """Why a call was blocked by moderation or a content filter, in words; None when it wasn't."""
    finish = str(d.get("finish") or "").lower()
    if "content_filter" in finish:
        return "the provider's content filter stopped the answer"
    text = f"{d.get('errorCode') or ''} {d.get('error') or ''}".lower()
    if "403" in (str(d.get("statusCode")), str(d.get("errorCode"))) or any(w in text for w in BLOCK_WORDS):
        return "OpenRouter's moderation refused the request" + (f" ({d['error'][:120]})" if d.get("error") else "")
    return None


# ------------------------------------------------------------------ storing
def ingest(db: DB, payload: dict[str, Any]) -> int:
    """Add each new span to the agent logs; spans seen before (a relay may deliver twice) are skipped."""
    n = 0
    for sp in spans(payload):
        key = sp["spanId"] or f"{sp['traceId']}:{sp['start']}"
        if db.one("SELECT 1 AS x FROM otel_seen WHERE span_id = ?", (key,)):
            continue
        db.execute("INSERT INTO otel_seen(span_id, ts) VALUES(?, ?)", (key, time.time()))
        d = summary(sp)
        db.audit("openrouter", "openrouter.trace", str(d.get("model") or sp.get("name") or "model call"), **d)
        n += 1
    if n:
        _state["received"] += n
        _state["lastAt"] = time.time()
        db.execute("DELETE FROM otel_seen WHERE ts < ?", (time.time() - 14 * 86400,))
    return n


def decode(body: bytes, encoding: str | None) -> dict[str, Any] | None:
    if encoding and "gzip" in encoding.lower():
        body = gzip.decompress(body)
    if not body.strip():
        return None
    data = json.loads(body)
    return data if isinstance(data, dict) else None


# ------------------------------------------------------------------ push
@router.api_route("/api/hooks/openrouter", methods=["POST", "PUT"], include_in_schema=False)
async def webhook(request: Request):
    s = request.app.state.settings
    if not s.openrouter_traces_token:
        raise HTTPException(status_code=404, detail="Not found")
    auth = request.headers.get("authorization", "")
    if not hmac.compare_digest(auth, f"Bearer {s.openrouter_traces_token}"):
        raise HTTPException(status_code=401, detail="Wrong token")
    if request.headers.get("x-test-connection") == "true":
        return {"ok": True, "test": True}
    try:
        payload = decode(await request.body(), request.headers.get("content-encoding"))
    except (ValueError, OSError) as e:
        raise HTTPException(status_code=400, detail=f"Not OTLP JSON: {e}") from e
    return {"ok": True, "added": ingest(request.app.state.db, payload) if payload else 0}


# ------------------------------------------------------------------ pull from the relay
async def pull_once(app: FastAPI, transport=None) -> int:
    s = app.state.settings
    headers = {"Authorization": f"Bearer {s.openrouter_relay_token}"}
    base = s.openrouter_relay_url.rstrip("/")
    added = 0
    async with httpx.AsyncClient(timeout=30, transport=transport) as c:
        for _ in range(20):                     # up to 200 messages per round
            r = await c.get(f"{base}/pull", headers=headers)
            r.raise_for_status()
            items = r.json().get("items") or []
            if not items:
                break
            for it in items:
                try:
                    payload = json.loads(it["body"])
                    added += ingest(app.state.db, payload) if isinstance(payload, dict) else 0
                except ValueError:
                    log.warning("Skipped a relay message that isn't JSON")
            (await c.post(f"{base}/ack", headers=headers, json={"ids": [it["id"] for it in items]})).raise_for_status()
    _state["lastPull"], _state["lastError"] = time.time(), None
    return added


async def _poll(app: FastAPI) -> None:
    s = app.state.settings
    while True:
        try:
            await pull_once(app, getattr(app.state, "relay_transport", None))
        except Exception as e:  # noqa: BLE001 - keep polling; the status shows the last error
            _state["lastError"] = f"{e.__class__.__name__}: {e}"[:300]
            log.warning("OpenRouter relay: %s", _state["lastError"])
        await asyncio.sleep(max(10, s.openrouter_relay_interval_s))


def install(app: FastAPI) -> None:
    app.include_router(router)
    s = app.state.settings
    tasks: list[asyncio.Task] = []
    if s.openrouter_relay_url and s.openrouter_relay_token:
        async def start() -> None:
            tasks.append(asyncio.get_running_loop().create_task(_poll(app)))

        async def stop() -> None:
            for t in tasks:
                t.cancel()
        app.router.on_startup.append(start)
        app.router.on_shutdown.append(stop)


@router.get("/api/admin/openrouter-traces")
def status(request: Request, _=Depends(require_admin)):
    """For the Agent logs page: whether traces come in, and from where."""
    s = request.app.state.settings
    mode = "relay" if s.openrouter_relay_url and s.openrouter_relay_token else "push" if s.openrouter_traces_token else None
    return {"mode": mode, "received": _state["received"], "lastAt": _state["lastAt"], "lastPull": _state["lastPull"],
            "lastError": _state["lastError"], "intervalS": s.openrouter_relay_interval_s if mode == "relay" else None}
