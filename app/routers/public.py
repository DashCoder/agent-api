"""Health, public site config and admin login."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import check_admin_credentials, current_user, owner_key
from ..config import Settings, get_settings
from ..db import DB
from ..deps import get_agent, get_db, get_site

router = APIRouter(prefix="/api")


@router.get("/health")
async def health(request: Request):
    agent = get_agent(request)
    return {"status": "ok", "agent": await agent.status()}


@router.get("/config")
def public_config(request: Request, db: DB = Depends(get_db)):
    """Everything the chat page needs. Never include secrets here."""
    owner_key(request)  # make sure the visitor cookie exists
    site = get_site(db)
    return {k: site[k] for k in ("brand", "assistant", "avatar", "quickReplies")}


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
def login(body: LoginBody, request: Request, s: Settings = Depends(get_settings)):
    if not check_admin_credentials(body.username, body.password, s):
        raise HTTPException(status_code=401, detail="Wrong username or password")
    request.session["user"] = {"username": body.username, "role": "admin"}
    return {"user": request.session["user"]}


@router.post("/auth/logout")
def logout(request: Request):
    request.session.pop("user", None)
    return {"ok": True}


@router.get("/auth/me")
def me(request: Request):
    return {"user": current_user(request), "email": request.session.get("email")}
