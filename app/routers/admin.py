"""Admin API: dashboard, site settings, knowledge sources and Hermes status."""
from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..auth import require_admin
from ..config import Settings, get_settings
from ..db import DB, new_id
from ..deps import deep_merge, get_agent, get_db, get_site

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])


@router.get("/dashboard")
async def dashboard(request: Request, db: DB = Depends(get_db)):
    site = get_site(db)
    knowledge = db.one("SELECT COUNT(*) AS n FROM knowledge")["n"]
    day_ago = time.time() - 86400
    stats = {
        "conversations24h": db.one("SELECT COUNT(*) AS n FROM sessions WHERE created_at > ?", (day_ago,))["n"],
        "messages24h": db.one("SELECT COUNT(*) AS n FROM messages WHERE created_at > ? AND role = 'user'",
                              (day_ago,))["n"],
        "savedReplies": db.one("SELECT COUNT(*) AS n FROM saved_replies")["n"],
        "knowledgeSources": knowledge,
    }
    phone_ready = bool(site["phone"].get("number"))
    checklist = [
        {"id": "phone", "icon": "📞", "title": "Phone assistant", "href": "/admin/phone", "done": phone_ready,
         "description": "Your AI phone assistant is ready. Add your phone number and update the call script "
                        "in the Phone tab." if not phone_ready else
                        f"Answering calls on {site['phone']['number']}."},
        {"id": "knowledge", "icon": "📚", "title": "Knowledge base", "href": "/admin/knowledge",
         "done": knowledge > 0,
         "description": "Connect documents or website URLs so your agents can give accurate, detailed answers "
                        "about your business — services, pricing, FAQs."},
        {"id": "calendar", "icon": "📅", "title": "Booking & calendar", "href": "/admin/calendar",
         "done": bool(site["calendar"].get("connected")),
         "description": "Connect Google Calendar or use the built-in scheduler so customers can book "
                        "appointments directly through the phone or chat."},
        {"id": "widget", "icon": "💬", "title": "Chat widget", "href": "/admin/builder", "done": True,
         "description": "Your AI also appears as a chat assistant with a face and voice on your website — "
                        "ideal for visitors who prefer chat over calling."},
    ]
    return {"checklist": checklist, "stats": stats, "agent": await get_agent(request).status()}


@router.get("/settings")
def get_settings_doc(db: DB = Depends(get_db)):
    return get_site(db)


@router.put("/settings")
def update_settings(patch: dict[str, Any], db: DB = Depends(get_db)):
    """Deep-merges the patch into the stored site settings and returns the result."""
    stored = deep_merge(db.get_setting("site", {}), patch)
    db.set_setting("site", stored)
    return get_site(db)


class KnowledgeBody(BaseModel):
    kind: Literal["url", "text"] = "url"
    value: str = Field(min_length=1, max_length=20000)


@router.get("/knowledge")
def list_knowledge(db: DB = Depends(get_db)):
    return {"items": db.all("SELECT * FROM knowledge ORDER BY created_at DESC")}


@router.post("/knowledge")
def add_knowledge(body: KnowledgeBody, db: DB = Depends(get_db)):
    item = {"id": new_id(), "kind": body.kind, "value": body.value.strip(), "created_at": time.time()}
    db.execute("INSERT INTO knowledge(id, kind, value, created_at) VALUES(?,?,?,?)",
               (item["id"], item["kind"], item["value"], item["created_at"]))
    return item


@router.delete("/knowledge/{item_id}")
def delete_knowledge(item_id: str, db: DB = Depends(get_db)):
    db.execute("DELETE FROM knowledge WHERE id = ?", (item_id,))
    return {"ok": True}


@router.get("/hermes")
async def hermes_status(request: Request, s: Settings = Depends(get_settings)):
    agent = get_agent(request)
    return {"status": await agent.status(), "skills": await agent.skills(),
            "dashboardUrl": s.hermes_dashboard_url or None, "mode": s.hermes_mode}
