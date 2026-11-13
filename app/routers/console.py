"""Admin console data: conversations (Agent logs), activity per agent and usage.

An "agent" is the core chat assistant (id "assistant") or one plugin (id = its plugin name). Activity is read from
the audit trail: a plugin's own actions start with "<plugin>." (or one of its `public_info["auditActions"]`
prefixes), and every MCP tool call is logged as "mcp.call" with the plugin that served it.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import require_admin
from ..db import DB
from ..deps import get_db

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])
ASSISTANT = "assistant"


# ------------------------------------------------------------------ agents and their audit events
def agent_ids(request: Request) -> list[str]:
    return [ASSISTANT, *request.app.state.plugins.keys()]


def _prefixes(request: Request, agent: str) -> list[str]:
    p = request.app.state.plugins.get(agent)
    if not p:
        return []
    return [f"{agent}.", *p.public_info.get("auditActions", [])]


def _check_agent(request: Request, agent: str) -> str:
    if agent in ("", "all"):
        return "all"
    if agent not in agent_ids(request):
        raise HTTPException(404, f"No agent called {agent}")
    return agent


def belongs(event: dict[str, Any], agent: str, prefixes: list[str], all_prefixes: list[str]) -> bool:
    action = event["action"]
    detail = event["detail"] if isinstance(event["detail"], dict) else json.loads(event["detail"] or "{}")
    if agent == "all":
        return True
    if action == "openrouter.trace" and not detail.get("plugin"):   # e.g. Hermes' own calls: the assistant's
        return agent == ASSISTANT
    if action in ("mcp.call", "llm.call", "models.set", "openrouter.trace"):   # tagged with the agent they belong to
        return detail.get("plugin") == agent
    if agent == ASSISTANT:  # everything no plugin claims: logins, settings, the chat assistant
        return not any(action.startswith(x) for x in all_prefixes)
    return any(action.startswith(x) for x in prefixes)


def agent_events(request: Request, db: DB, agent: str, limit: int, since: float = 0) -> list[dict[str, Any]]:
    agent = _check_agent(request, agent)
    prefixes = _prefixes(request, agent)
    all_prefixes = [x for a in request.app.state.plugins for x in _prefixes(request, a)]
    out, offset, page = [], 0, max(limit * 4, 400)
    while len(out) < limit:  # filter in Python: the trail is small and plugins name their actions freely
        rows = db.all("SELECT * FROM audit_events WHERE ts >= ? ORDER BY ts DESC LIMIT ? OFFSET ?", (since, page, offset))
        if not rows:
            break
        for r in rows:
            r = {**r, "detail": json.loads(r["detail"] or "{}")}
            if belongs(r, agent, prefixes, all_prefixes):
                out.append(r)
                if len(out) >= limit:
                    break
        offset += page
    return out


@router.get("/activity")
def activity(request: Request, agent: str = "all", limit: int = 200, db: DB = Depends(get_db)):
    """The audit trail, optionally one agent's part of it (Agent logs → Actions)."""
    return {"items": agent_events(request, db, agent, min(max(limit, 1), 1000))}


# ------------------------------------------------------------------ conversations (chat assistant)
def _summary(text: str | None, n: int = 160) -> str | None:
    if not text:
        return None
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


@router.get("/conversations")
def conversations(offset: int = 0, limit: int = 25, since: float = 0, until: float = 0, db: DB = Depends(get_db)):
    """Chat conversations, newest first: when, who, the first question, the last reply and whether it was answered."""
    limit = min(max(limit, 1), 100)
    until = until or time.time() + 60
    rows = db.all(
        "SELECT s.id, s.owner, s.created_at, MAX(m.created_at) AS last_at, COUNT(m.id) AS messages, "
        "SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END) AS questions "
        "FROM sessions s JOIN messages m ON m.session_id = s.id "
        "GROUP BY s.id HAVING questions > 0 AND last_at >= ? AND last_at < ? "
        "ORDER BY last_at DESC LIMIT ? OFFSET ?", (since, until, limit + 1, max(offset, 0)))
    items = []
    for r in rows[:limit]:
        first = db.one("SELECT text FROM messages WHERE session_id = ? AND role = 'user' ORDER BY created_at LIMIT 1",
                       (r["id"],))
        last = db.one("SELECT role, text FROM messages WHERE session_id = ? ORDER BY created_at DESC LIMIT 1", (r["id"],))
        reply = db.one("SELECT text FROM messages WHERE session_id = ? AND role = 'assistant' ORDER BY created_at DESC "
                       "LIMIT 1", (r["id"],))
        items.append({"id": r["id"], "channel": "chat", "contact": f"Visitor {r['owner'][:6]}",
                      "startedAt": r["created_at"], "lastAt": r["last_at"], "messages": r["messages"],
                      "questions": r["questions"], "summary": _summary(first["text"] if first else None),
                      "reply": _summary(reply["text"] if reply else None),
                      "status": "answered" if last and last["role"] == "assistant" else "no_reply"})
    return {"items": items, "hasMore": len(rows) > limit}


@router.get("/conversations/{session_id}")
def conversation(session_id: str, db: DB = Depends(get_db)):
    msgs = db.all("SELECT id, role, text, created_at FROM messages WHERE session_id = ? ORDER BY created_at",
                  (session_id,))
    if not msgs:
        raise HTTPException(404, "No such conversation")
    return {"messages": msgs}


# ------------------------------------------------------------------ usage per agent
def _month_start(t: float) -> float:
    d = dt.datetime.fromtimestamp(t)
    return dt.datetime(d.year, d.month, 1).timestamp()


def _counts(request: Request, db: DB, agent: str, since: float, until: float) -> dict[str, int]:
    out: dict[str, Any] = {"messages": 0, "conversations": 0, "actions": 0, "toolCalls": 0, "modelCalls": 0, "cost": 0.0}
    if agent == ASSISTANT:
        out["messages"] = db.one("SELECT COUNT(*) AS n FROM messages WHERE role = 'user' AND created_at >= ? AND "
                                 "created_at < ?", (since, until))["n"]
        out["conversations"] = db.one(
            "SELECT COUNT(DISTINCT session_id) AS n FROM messages WHERE role = 'user' AND created_at >= ? AND "
            "created_at < ?", (since, until))["n"]
    events = [e for e in agent_events(request, db, agent, 100000, since) if e["ts"] < until]
    out["toolCalls"] = sum(1 for e in events if e["action"] == "mcp.call")
    calls = [e for e in events if e["action"] == "llm.call"]
    out["modelCalls"] = len(calls)
    out["cost"] = round(sum(float((e["detail"] or {}).get("cost") or 0) for e in calls
                            if isinstance(e["detail"], dict)), 4)
    out["actions"] = len(events) - out["toolCalls"] - out["modelCalls"]
    return out


@router.get("/usage")
def usage(request: Request, agent: str = "all", months: int = 6, db: DB = Depends(get_db)):
    """Activity per agent today and this month, plus monthly history. Model cost = what providers reported for the
    calls made through Model & tools (OpenRouter reports it; local models cost nothing; Bedrock reports tokens only)."""
    agent = _check_agent(request, agent)
    ids = agent_ids(request) if agent == "all" else [agent]
    now = time.time()
    today = dt.datetime.combine(dt.date.today(), dt.time()).timestamp()
    month = _month_start(now)
    agents = [{"id": a, "today": _counts(request, db, a, today, now + 60),
               "month": _counts(request, db, a, month, now + 60)} for a in ids]
    history, start = [], month
    for _ in range(max(1, min(months, 24))):
        end = _month_start(start + 32 * 86400)  # first day of the next month
        row = {"month": dt.datetime.fromtimestamp(start).strftime("%Y-%m"), "messages": 0, "conversations": 0,
               "actions": 0, "toolCalls": 0, "modelCalls": 0, "cost": 0.0}
        for a in ids:
            for k, v in _counts(request, db, a, start, min(end, now + 60)).items():
                row[k] += v
        history.append(row)
        prev = dt.datetime.fromtimestamp(start) - dt.timedelta(days=1)
        start = dt.datetime(prev.year, prev.month, 1).timestamp()
    for row in history:
        row["cost"] = round(row["cost"], 4)
    tracked = any(r["modelCalls"] for r in history)
    return {"agents": agents, "history": history, "currentMonth": history[0]["month"], "costTracked": tracked}
