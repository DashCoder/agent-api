"""One MCP server for Hermes, serving the tools that plugins contribute (Streamable HTTP, JSON responses).

Hermes config (~/.hermes/config.yaml):

    mcp_servers:
      inagentic:
        url: "http://127.0.0.1:8000/api/mcp"
        headers:
          Authorization: "Bearer ${MCP_KEY}"
"""
from __future__ import annotations

import hmac
import time
import json
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .. import security
from ..plugins import ToolContext

router = APIRouter(prefix="/api")


def _text(payload: Any, error: bool = False, secrets: list[str] | None = None) -> dict[str, Any]:
    text = json.dumps(payload, default=str)
    if secrets is not None:          # this server's own keys and well-known token formats never reach the agent
        text = security.redact(text, secrets)
    return {"content": [{"type": "text", "text": text}], "isError": error}


def current_caller(app: Any) -> dict[str, Any]:
    """Hermes calls tools with the MCP key only, so who the call is for comes from the web chats running now
    (signed session cookies): the least trusted of them. No chat running = the server itself (Slack, schedules)."""
    return security.least(list(getattr(app.state, "active_turns", {}).values()))


def allowed(caller: dict[str, Any], tool: Any) -> bool:
    access = getattr(tool, "access", "trusted")
    need = {"public": security.ANONYMOUS, "verified": security.VERIFIED, "admin": security.ADMIN}.get(access,
                                                                                                    security.TRUSTED)
    return security.at_least(caller, need)


async def _call(request: Request, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return await call_tool(request.app, name, args, request=request)


async def call_tool(app: Any, name: str, args: dict[str, Any], request: Any = None) -> dict[str, Any]:
    """Run one plugin tool for whoever is chatting now: Hermes' MCP calls and direct chat (app/direct.py) both
    come here, so the access rules and redaction are the same."""
    if request is None:
        from ..direct import AppRequest
        request = AppRequest(app)
    entry = app.state.mcp_tools.get(name)
    if not entry:
        return _text({"error": f"Unknown tool {name}"}, True)
    plugin_name, tool = entry
    caller = current_caller(app)
    secrets = security.known_secrets(app)
    if not allowed(caller, tool):
        app.state.db.audit(caller.get("email") or caller["role"], "mcp.refused", name, plugin=plugin_name,
                           role=caller["role"])
        return _text({"error": f"{name} isn't available in this chat: it needs the admin or a trusted, signed-in "
                               "email. Tell the person to sign in; don't try another way."}, True)
    ctx = ToolContext(request=request, db=app.state.db, settings=app.state.settings,
                      plugin_settings=app.state.plugin_settings.get(plugin_name), caller=caller)
    started = time.monotonic()
    err = None
    try:
        out = _text(await tool.handler(ctx, args), secrets=secrets)
    except Exception as exc:  # report failures to the agent instead of breaking its session
        err = security.redact(str(getattr(exc, "detail", None) or exc), secrets)
        out = _text({"error": err}, True)
    # Every tool call is logged with its plugin, how long it took, what it was about (never file contents or
    # secrets) and why it failed, so Agent logs can show what the agent did step by step.
    detail: dict[str, Any] = {"plugin": plugin_name, "error": bool(out["isError"]), "role": caller["role"],
                              "ms": round((time.monotonic() - started) * 1000)}
    about = summarize_args(args)
    if about:
        detail["args"] = about
    if err:
        detail["reason"] = err[:300]
    app.state.db.audit("hermes" if app.state.settings.hermes_mode == "hermes" else "chat", "mcp.call", name, **detail)
    return out


SUMMARY_KEYS = ("ticket_key", "repo", "repos", "path", "path_prefix", "job_id", "status", "jql", "limit", "title", "view")


def summarize_args(args: dict[str, Any]) -> str:
    """A short, safe description of a tool call's arguments for the log: identifiers only, no contents."""
    parts = []
    for k in SUMMARY_KEYS:
        v = args.get(k)
        if v in (None, "", []):
            continue
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v[:5]) + (" …" if len(v) > 5 else "")
        parts.append(f"{k}={str(v)[:80]}")
    for k in ("files", "changes"):
        v = args.get(k)
        if isinstance(v, (list, dict)) and v:
            parts.append(f"{k}: {len(v)}")
    return " · ".join(parts)[:300]


async def _handle(request: Request, msg: dict[str, Any]) -> dict[str, Any] | None:
    mid, method, params = msg.get("id"), msg.get("method", ""), msg.get("params") or {}
    if mid is None:  # notification, e.g. notifications/initialized
        return None
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", "2025-03-26"), "capabilities": {"tools": {}},
                  "serverInfo": {"name": "agent-api", "version": request.app.version},
                  "instructions": " ".join(request.app.state.mcp_instructions)}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                            for _, t in request.app.state.mcp_tools.values()]}
    elif method == "tools/call":
        result = await _call(request, params.get("name", ""), params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


@router.post("/mcp")
async def mcp(request: Request):
    key = request.app.state.settings.mcp_key
    if not key:
        return JSONResponse({"error": "MCP is off: set MCP_KEY"}, status_code=503)
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given, key):
        request.app.state.db.audit("unknown", "mcp.denied", "mcp", ip=request.client.host if request.client else "")
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, 400)
    if isinstance(body, list):
        out = [r for r in [await _handle(request, m) for m in body] if r is not None]
        return JSONResponse(out) if out else Response(status_code=202)
    reply = await _handle(request, body)
    return JSONResponse(reply) if reply is not None else Response(status_code=202)


@router.get("/mcp")
async def mcp_stream():
    return Response(status_code=405)  # no server-initiated stream
