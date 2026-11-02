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
import json
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..plugins import ToolContext

router = APIRouter(prefix="/api")


def _text(payload: Any, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}], "isError": error}


async def _call(request: Request, name: str, args: dict[str, Any]) -> dict[str, Any]:
    entry = request.app.state.mcp_tools.get(name)
    if not entry:
        return _text({"error": f"Unknown tool {name}"}, True)
    plugin_name, tool = entry
    ctx = ToolContext(request=request, db=request.app.state.db, settings=request.app.state.settings,
                      plugin_settings=request.app.state.plugin_settings.get(plugin_name))
    try:
        return _text(await tool.handler(ctx, args))
    except Exception as exc:  # report failures to the agent instead of breaking its session
        return _text({"error": getattr(exc, "detail", None) or str(exc)}, True)


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
