"""Sign in with an email link (a "magic link"), sent through Amazon SES.

1. POST /api/auth/email-link {email, next}: emails a link that works for 15 minutes, once.
2. The person opens the link on any device (their phone is fine). That browser is signed in, and the request is
   marked approved.
3. The browser that asked keeps polling GET /api/auth/email-link/status; once approved, it is signed in too.

"Signed in" means the browser's session carries a verified email (request.session["email"]), the same thing
"Save my work" sets: agents use it to know who someone is (e.g. who may open pull requests).
"""
from __future__ import annotations

import hashlib
import html
import secrets
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .. import mailer
from ..auth import owner_key
from ..db import DB, new_id
from ..deps import get_db
from .user import EMAIL

router = APIRouter(prefix="/api/auth")
TTL_S = 15 * 60
PER_HOUR = 5


class LinkIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    next: str = Field(default="/admin", max_length=300)


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _safe_next(path: str) -> str:
    """Only a path on this site (never another site)."""
    return path if path.startswith("/") and not path.startswith("//") and "\\" not in path else "/admin"


def _base(request: Request) -> str:
    """The address the person used (127.0.0.1:8080 on the mini, 127.0.0.1:8088 through the tunnel, a domain…)."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{proto}://{host}"


def _sign_in(request: Request, db: DB, email: str) -> None:
    """This browser now has a verified email; its chats and uploads move with it (as with "Save my work")."""
    owner_key(request)
    old, new = f"visitor:{request.session['visitor']}", f"email:{email}"
    for table in ("saved_replies", "sessions", "uploads"):
        db.execute(f"UPDATE {table} SET owner = ? WHERE owner = ?", (new, old))
    request.session["email"] = email


@router.post("/email-link")
async def send_link(body: LinkIn, request: Request, db: DB = Depends(get_db)):
    s = request.app.state.settings
    email = body.email.strip().lower()
    if not EMAIL.match(email):
        raise HTTPException(status_code=422, detail="Please enter a valid email address")
    if not mailer.configured(s):
        raise HTTPException(status_code=503, detail="Sign-in by email isn't set up yet: an admin adds the AWS SES "
                                                    "keys on Settings & keys")
    recent = db.one("SELECT COUNT(*) AS n FROM email_links WHERE email = ? AND created_at > ?", (email, time.time() - 3600))
    if recent and recent["n"] >= PER_HOUR:
        raise HTTPException(status_code=429, detail="Too many sign-in emails for this address. Try again in an hour.")
    owner_key(request)
    token = secrets.token_urlsafe(32)
    rid = new_id()
    nxt = _safe_next(body.next)
    db.execute("INSERT INTO email_links(id, token_hash, email, visitor, next, created_at) VALUES(?,?,?,?,?,?)",
               (rid, _h(token), email, request.session["visitor"], nxt, time.time()))
    link = f"{_base(request)}/api/auth/email-link/verify?t={quote(token)}"
    brand = (s.app_name or "InAgentic") if s.app_name != "Agent API" else "InAgentic"
    text = (f"Sign in to {brand}\n\nOpen this link to sign in as {email}:\n{link}\n\n"
            "It works once, for 15 minutes. You can open it on any device: the browser where you asked for it "
            "signs in by itself.\nIf you didn't ask for this, ignore this email.")
    page = (f'<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:480px;margin:auto;padding:24px">'
            f'<h2 style="margin:0 0 12px">Sign in to {html.escape(brand)}</h2>'
            f'<p>Press the button to sign in as <b>{html.escape(email)}</b>.</p>'
            f'<p><a href="{html.escape(link)}" style="display:inline-block;background:#0F6CBD;color:#fff;padding:10px 18px;'
            f'border-radius:6px;text-decoration:none;font-weight:600">Sign in</a></p>'
            f'<p style="color:#424B57;font-size:13px">It works once, for 15 minutes, on any device. If you didn\'t ask for '
            f'this, ignore this email.</p></div>')
    try:
        await mailer.send(s, email, f"Sign in to {brand}", text, page)
    except RuntimeError as e:
        db.execute("DELETE FROM email_links WHERE id = ?", (rid,))
        raise HTTPException(status_code=502, detail=str(e)) from e
    request.session["email_link"] = rid
    db.audit(email, "auth.email_link_sent", email)
    return {"sent": True, "email": email}


@router.get("/email-link/verify", include_in_schema=False)
def verify(t: str, request: Request, db: DB = Depends(get_db)):
    row = db.one("SELECT * FROM email_links WHERE token_hash = ?", (_h(t or ""),))
    if not row or row["used_at"] or time.time() - row["created_at"] > TTL_S:
        return _page("This sign-in link has expired", "<p>Sign-in links work once, for 15 minutes. Ask for a new one "
                     'where you started (e.g. <a href="/admin">the admin</a>).</p>', 400)
    now = time.time()
    db.execute("UPDATE email_links SET approved_at = ?, used_at = ? WHERE id = ?", (now, now, row["id"]))
    _sign_in(request, db, row["email"])
    db.audit(row["email"], "auth.email_signed_in", row["email"])
    nxt = html.escape(_safe_next(row["next"]))
    return _page("You're signed in", f"<p>Signed in as <b>{html.escape(row['email'])}</b>. If you asked for this link "
                 "on another device, that browser signs in by itself within a few seconds: you can close this tab.</p>"
                 f'<p><a href="{nxt}">Continue here</a></p>', 200)


@router.get("/email-link/status")
def status(request: Request, db: DB = Depends(get_db)):
    """Polled by the browser that asked for the link."""
    if request.session.get("email"):
        return {"email": request.session["email"]}
    rid = request.session.get("email_link")
    row = db.one("SELECT * FROM email_links WHERE id = ?", (rid,)) if rid else None
    if not row:
        return {"pending": False}
    if row["approved_at"] and row["visitor"] == request.session.get("visitor"):
        _sign_in(request, db, row["email"])
        request.session.pop("email_link", None)
        return {"email": row["email"]}
    if time.time() - row["created_at"] > TTL_S:
        request.session.pop("email_link", None)
        return {"pending": False, "expired": True}
    return {"pending": True, "email": None, "to": row["email"]}


@router.post("/email-signout")
def sign_out(request: Request):
    request.session.pop("email", None)
    request.session.pop("email_link", None)
    return {"email": None}


def _page(title: str, body: str, status_code: int) -> HTMLResponse:
    return HTMLResponse(f'<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
                        f'<title>{html.escape(title)}</title><body style="font-family:-apple-system,Segoe UI,Arial,'
                        f'sans-serif;max-width:520px;margin:10vh auto;padding:0 20px;color:#1F2A37">'
                        f'<h1 style="font-size:22px">{html.escape(title)}</h1>{body}</body>', status_code=status_code)
