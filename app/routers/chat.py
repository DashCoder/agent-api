"""Chat sessions, streaming replies (SSE), approvals and attachments."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..auth import owner_key
from ..config import Settings, get_settings
from ..db import DB, new_id
from ..deps import get_agent, get_db, get_site

router = APIRouter(prefix="/api/chat")


def _own_session(db: DB, session_id: str, owner: str) -> dict:
    row = db.one("SELECT * FROM sessions WHERE id = ?", (session_id,))
    if not row or row["owner"] != owner:
        raise HTTPException(status_code=404, detail="Session not found")
    return row


def agent_of(request: Request, agent: str | None) -> dict | None:
    """An installed agent the visitor chat can talk as (?agent=kpi): its name and what it does."""
    p = request.app.state.plugins.get(agent or "")
    if not p:
        return None
    return {"id": p.name, "name": p.public_info.get("agentName") or p.name.title(),
            "hint": p.public_info.get("assistantHint", "")}


class SessionBody(BaseModel):
    agent: str | None = Field(default=None, max_length=40)


@router.post("/sessions")
def create_session(request: Request, body: SessionBody | None = None, db: DB = Depends(get_db)):
    sid = new_id()
    db.execute("INSERT INTO sessions(id, owner, created_at) VALUES(?,?,?)", (sid, owner_key(request), time.time()))
    a = agent_of(request, body.agent if body else None)
    if a:
        greeting = f"Hi! I'm {a['name']} 👋" + (f" I {a['hint'][0].lower()}{a['hint'][1:]}" if a["hint"] else "") + \
                   " What would you like to do?"
    else:
        greeting = get_site(db)["assistant"]["greeting"]
    msg = db.add_message(sid, "assistant", greeting)
    return {"id": sid, "messages": [msg]}


@router.get("/sessions/{session_id}/messages")
def list_messages(session_id: str, request: Request, db: DB = Depends(get_db)):
    _own_session(db, session_id, owner_key(request))
    return {"messages": db.all("SELECT * FROM messages WHERE session_id = ? ORDER BY created_at", (session_id,))}


class SendBody(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    learn: bool = False
    attachments: list[str] = []
    agent: str | None = Field(default=None, max_length=40)   # talking to one agent (?agent=kpi)


def _sse(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


@router.delete("/sessions/{session_id}/messages/{message_id}")
def remove_message(session_id: str, message_id: str, request: Request, db: DB = Depends(get_db)):
    """Remove a question and its answer from the visitor's view (removing an answer removes just that answer).
    The agent may still remember the conversation; "Clear chat" starts a new one."""
    _own_session(db, session_id, owner_key(request))
    rows = db.all("SELECT * FROM messages WHERE session_id = ? ORDER BY created_at", (session_id,))
    i = next((n for n, m in enumerate(rows) if m["id"] == message_id), None)
    if i is None:
        raise HTTPException(status_code=404, detail="Message not found")
    gone = [rows[i]["id"]]
    if rows[i]["role"] == "user" and i + 1 < len(rows) and rows[i + 1]["role"] == "assistant":
        gone.append(rows[i + 1]["id"])
    for mid in gone:
        db.execute("DELETE FROM messages WHERE id = ?", (mid,))
    return {"removed": gone}


@router.post("/sessions/{session_id}/stream")
async def stream_reply(session_id: str, body: SendBody, request: Request, db: DB = Depends(get_db)):
    owner = owner_key(request)
    _own_session(db, session_id, owner)
    agent = get_agent(request)

    text = body.text.strip()
    if body.attachments:
        names = [r["filename"] for r in (db.one("SELECT filename FROM uploads WHERE id = ? AND owner = ?", (a, owner))
                                         for a in body.attachments) if r]
        if names:
            text += "\n\n[Attached files: " + ", ".join(names) + "]"
    user_msg = db.add_message(session_id, "user", text)
    site = get_site(db)
    context = {"assistant": site.get("assistant", {}), "business": site.get("business", {}),
               "agentHints": [p.public_info.get("assistantHint", "") for p in request.app.state.plugins.values()]}
    focus = agent_of(request, body.agent)
    if focus:  # the visitor opened this agent's chat: answer as it, with its tools
        context["assistant"] = {**context["assistant"], "name": focus["name"]}
        context["agentHints"] = [focus["hint"]]
        context["focus"] = focus

    async def gen():
        yield _sse({"type": "start", "userMessage": user_msg})
        reply = []
        async for ev in agent.stream(session_id, text, learn=body.learn, context=context):
            if ev["type"] == "delta":
                reply.append(ev["text"])
            yield _sse(ev)
            if await request.is_disconnected():
                break
        final = "".join(reply).strip()
        saved = db.add_message(session_id, "assistant", final) if final else None
        yield _sse({"type": "done", "message": saved})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class ApprovalBody(BaseModel):
    approve: bool
    sessionId: str | None = None


@router.post("/runs/{run_id}/approval")
async def approval(run_id: str, body: ApprovalBody, request: Request, db: DB = Depends(get_db)):
    owner = owner_key(request)
    result = await get_agent(request).approve(run_id, body.approve)
    if body.sessionId and result.get("message"):
        _own_session(db, body.sessionId, owner)
        result["savedMessage"] = db.add_message(body.sessionId, "assistant", result["message"])
    return result


SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@router.post("/uploads")
async def upload(request: Request, file: UploadFile = File(...), db: DB = Depends(get_db),
                 s: Settings = Depends(get_settings)):
    owner = owner_key(request)
    data = await file.read(s.max_upload_mb * 1024 * 1024 + 1)
    if len(data) > s.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File larger than {s.max_upload_mb} MB")
    uid = new_id()
    name = SAFE_NAME.sub("_", file.filename or "file")[:120]
    folder = Path(s.data_dir) / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uid}_{name}"
    path.write_bytes(data)
    db.execute("INSERT INTO uploads(id, owner, filename, path, size, created_at) VALUES(?,?,?,?,?,?)",
               (uid, owner, name, str(path), len(data), time.time()))
    return {"id": uid, "filename": name, "size": len(data)}
