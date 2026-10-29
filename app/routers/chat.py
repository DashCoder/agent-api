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


@router.post("/sessions")
def create_session(request: Request, db: DB = Depends(get_db)):
    sid = new_id()
    db.execute("INSERT INTO sessions(id, owner, created_at) VALUES(?,?,?)", (sid, owner_key(request), time.time()))
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


def _sse(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


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
    context = {"business": get_site(db).get("business", {})}

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
