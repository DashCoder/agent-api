"""Saved replies and "Save my work" (link a browser's work to a verified email)."""
from __future__ import annotations

import logging
import re
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth import owner_key
from ..config import Settings, get_settings
from ..db import DB, new_id
from ..deps import get_db

router = APIRouter(prefix="/api")
log = logging.getLogger("agent_api")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.get("/saved-replies")
def list_saved(request: Request, db: DB = Depends(get_db)):
    return {"items": db.all("SELECT id, text, created_at FROM saved_replies WHERE owner = ? ORDER BY created_at DESC",
                            (owner_key(request),))}


class SaveBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/saved-replies")
def add_saved(body: SaveBody, request: Request, db: DB = Depends(get_db)):
    item = {"id": new_id(), "text": body.text.strip(), "created_at": time.time()}
    db.execute("INSERT INTO saved_replies(id, owner, text, created_at) VALUES(?,?,?,?)",
               (item["id"], owner_key(request), item["text"], item["created_at"]))
    return item


@router.delete("/saved-replies/{item_id}")
def delete_saved(item_id: str, request: Request, db: DB = Depends(get_db)):
    db.execute("DELETE FROM saved_replies WHERE id = ? AND owner = ?", (item_id, owner_key(request)))
    return {"ok": True}


class EmailBody(BaseModel):
    email: str


class VerifyBody(BaseModel):
    email: str
    code: str


def send_code(email: str, code: str) -> None:
    """Replace with your mail provider (Postmark, SES, Resend…)."""
    log.warning("Save-my-work code for %s: %s (wire up a mailer in app/routers/user.py)", email, code)


@router.post("/save-work/start")
def save_work_start(body: EmailBody, request: Request, db: DB = Depends(get_db),
                    s: Settings = Depends(get_settings)):
    email = body.email.strip().lower()
    if not EMAIL.match(email):
        raise HTTPException(status_code=422, detail="Please enter a valid email address")
    owner_key(request)
    visitor = request.session["visitor"]
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.execute("INSERT INTO save_work(email, visitor, code, verified, created_at) VALUES(?,?,?,0,?) "
               "ON CONFLICT(email, visitor) DO UPDATE SET code = excluded.code, created_at = excluded.created_at",
               (email, visitor, code, time.time()))
    send_code(email, code)
    return {"sent": True, **({"devCode": code} if s.dev_show_codes else {})}


@router.post("/save-work/verify")
def save_work_verify(body: VerifyBody, request: Request, db: DB = Depends(get_db)):
    email = body.email.strip().lower()
    owner_key(request)
    visitor = request.session["visitor"]
    row = db.one("SELECT * FROM save_work WHERE email = ? AND visitor = ?", (email, visitor))
    if not row or row["code"] != body.code.strip() or time.time() - row["created_at"] > 15 * 60:
        raise HTTPException(status_code=400, detail="That code is wrong or has expired")
    db.execute("UPDATE save_work SET verified = 1 WHERE email = ? AND visitor = ?", (email, visitor))
    old, new = f"visitor:{visitor}", f"email:{email}"
    for table in ("saved_replies", "sessions", "uploads"):
        db.execute(f"UPDATE {table} SET owner = ? WHERE owner = ?", (new, old))
    request.session["email"] = email
    return {"email": email}
