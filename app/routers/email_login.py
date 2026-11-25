"""Sign in with an email link or code, sent through Amazon SES.

1. POST /api/auth/email-link {email, next}: emails a sign-in link and a 6-digit code (15 minutes, once).
2. Either open the link: it signs in the browser that opens it (only that one), or type the code into the page that
   asked: that signs in the asking browser. Reading the code on your phone and typing it on the computer works.

Security (same rules as the OxComply sites since 17 Nov 2026):
- opening a link never signs in a different browser (so nobody can request a link for your address and get signed
  in when you click it), and it only ever moves the opening browser's own chats to the account
- the link points at this site's own address (PUBLIC_URL, the admin's local addresses or CORS_ORIGINS), never at a
  host taken from the request
- at most 3 links an hour and 6 a day per real inbox (axel+b@ counts as axel@), 10 an hour per IP address
- 5 wrong codes and that sign-in request is void
"""
from __future__ import annotations

import hashlib
import hmac
import html
import secrets
import time
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .. import mailer, security
from ..auth import owner_key
from ..db import DB, new_id
from ..deps import get_db
from .user import EMAIL

router = APIRouter(prefix="/api/auth")
TTL_S = 15 * 60
PER_HOUR, PER_DAY, PER_IP_HOUR, MAX_TRIES = 3, 6, 10, 5
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class LinkIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    next: str = Field(default="/admin", max_length=300)


class CodeIn(BaseModel):
    code: str = Field(min_length=4, max_length=12)


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _safe_next(path: str) -> str:
    """Only a path on this site (never another site)."""
    return path if path.startswith("/") and not path.startswith("//") and "\\" not in path else "/admin"


def site_base(request: Request) -> str | None:
    """This site's own address for links in emails: PUBLIC_URL if set; else the address in use, but only when it's
    this machine (127.0.0.1 / localhost) or listed in CORS_ORIGINS. Never a host someone else put in the request."""
    s = request.app.state.settings
    if getattr(s, "public_url", ""):
        return s.public_url.rstrip("/")
    host = request.headers.get("host") or ""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    candidate = f"{proto}://{host}"
    name = (urlparse(candidate).hostname or "").lower()
    if name in LOCAL_HOSTS or name.endswith(".localhost"):
        return candidate
    allowed = {o.rstrip("/").lower() for o in s.cors_origin_list}
    return candidate if candidate.lower() in allowed else None


def _sign_in(request: Request, db: DB, email: str) -> None:
    """This browser now has a verified email; its own chats and uploads move with it (as with "Save my work")."""
    owner_key(request)
    old, new = f"visitor:{request.session['visitor']}", f"email:{email}"
    for table in ("saved_replies", "sessions", "uploads"):
        db.execute(f"UPDATE {table} SET owner = ? WHERE owner = ?", (new, old))
    request.session["email"] = email
    request.session.pop("email_link", None)


@router.post("/email-link")
async def send_link(body: LinkIn, request: Request, db: DB = Depends(get_db)):
    s = request.app.state.settings
    email = body.email.strip().lower()
    if not EMAIL.match(email):
        raise HTTPException(status_code=422, detail="Please enter a valid email address")
    if not mailer.configured(s):
        raise HTTPException(status_code=503, detail="Sign-in by email isn't set up yet: an admin adds the AWS SES "
                                                    "keys on Settings & keys")
    base = site_base(request)
    if not base:
        raise HTTPException(status_code=400, detail="Sign-in links only work on this site's own address (set "
                                                    "PUBLIC_URL or CORS_ORIGINS)")
    box, ip = security.inbox(email), security.client_ip(request)
    if security.count(db, f"link:{box}", 3600) >= PER_HOUR or security.count(db, f"link:{box}", 86400) >= PER_DAY:
        raise HTTPException(status_code=429, detail="Several sign-in emails were sent to this address already: use "
                                                    "the newest one in your inbox, or try again later.")
    if not security.hit(db, f"link-ip:{ip}", 3600, PER_IP_HOUR):
        raise HTTPException(status_code=429, detail="Too many sign-in requests from here. Try again in an hour.")
    security.hit(db, f"link:{box}", 86400, 10_000)
    owner_key(request)
    token, code, rid = secrets.token_urlsafe(32), f"{secrets.randbelow(1_000_000):06d}", new_id()
    nxt = _safe_next(body.next)
    db.execute("INSERT INTO email_links(id, token_hash, email, visitor, next, created_at, code_hash, tries) "
               "VALUES(?,?,?,?,?,?,?,0)", (rid, _h(token), email, request.session["visitor"], nxt, time.time(), _h(code)))
    link = f"{base}/api/auth/email-link/verify?t={quote(token)}"
    brand = (s.app_name or "InAgentic") if s.app_name != "Agent API" else "InAgentic"
    text = (f"Sign in to {brand}\n\nYour code: {code}\nType it into the page where you asked to sign in.\n\n"
            f"Or open this link on the computer you want to sign in on:\n{link}\n\n"
            "The code and the link work once, for 15 minutes. If you didn't ask for this, ignore this email: "
            "nobody can sign in without the code or the link.")
    page = (f'<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:480px;margin:auto;padding:24px;'
            f'color:#1F2A37"><h2 style="margin:0 0 12px">Sign in to {html.escape(brand)}</h2>'
            f'<p>Your code:</p><p style="font-size:28px;font-weight:700;letter-spacing:4px;margin:4px 0 16px">{code}</p>'
            f'<p>Type it into the page where you asked to sign in. Or press the button on the computer you want to '
            f'sign in on:</p><p><a href="{html.escape(link)}" style="display:inline-block;background:#0F6CBD;color:#fff;'
            f'padding:10px 18px;border-radius:6px;text-decoration:none;font-weight:600">Sign in here</a></p>'
            f'<p style="color:#424B57;font-size:13px">The code and the link work once, for 15 minutes. If you didn\'t '
            f'ask for this, ignore this email.</p></div>')
    try:
        await mailer.send(s, email, f"Your {brand} sign-in code: {code}", text, page)
    except RuntimeError as e:
        db.execute("DELETE FROM email_links WHERE id = ?", (rid,))
        raise HTTPException(status_code=502, detail=str(e)) from e
    request.session["email_link"] = rid
    db.audit(email, "auth.email_link_sent", email, ip=ip)
    return {"sent": True, "email": email}


@router.post("/email-code")
def enter_code(body: CodeIn, request: Request, db: DB = Depends(get_db)):
    """The code from the email, typed into the browser that asked for it: signs that browser in."""
    rid = request.session.get("email_link")
    row = db.one("SELECT * FROM email_links WHERE id = ?", (rid,)) if rid else None
    if not row or row["used_at"] or time.time() - row["created_at"] > TTL_S or row["tries"] >= MAX_TRIES \
            or row["visitor"] != request.session.get("visitor"):
        request.session.pop("email_link", None)
        raise HTTPException(status_code=400, detail="That sign-in request has expired. Ask for a new email.")
    if not hmac.compare_digest(_h(body.code.strip()), row["code_hash"] or ""):
        db.execute("UPDATE email_links SET tries = tries + 1 WHERE id = ?", (row["id"],))
        left = MAX_TRIES - row["tries"] - 1
        raise HTTPException(status_code=400, detail="That code isn't right." + (f" {left} tries left." if left else
                                                                              " Ask for a new email."))
    db.execute("UPDATE email_links SET used_at = ? WHERE id = ?", (time.time(), row["id"]))
    _sign_in(request, db, row["email"])
    db.audit(row["email"], "auth.email_signed_in", row["email"], how="code")
    return {"email": row["email"]}


@router.get("/email-link/verify", include_in_schema=False)
def verify(t: str, request: Request, db: DB = Depends(get_db)):
    """The link from the email: signs in the browser that opens it (and no other)."""
    row = db.one("SELECT * FROM email_links WHERE token_hash = ?", (_h(t or ""),))
    if not row or row["used_at"] or time.time() - row["created_at"] > TTL_S:
        return _page("This sign-in link has expired", "<p>Sign-in links work once, for 15 minutes. Ask for a new one "
                     'where you started (e.g. <a href="/admin">the admin</a>).</p>', 400)
    db.execute("UPDATE email_links SET used_at = ? WHERE id = ?", (time.time(), row["id"]))
    _sign_in(request, db, row["email"])
    db.audit(row["email"], "auth.email_signed_in", row["email"], how="link")
    nxt = html.escape(_safe_next(row["next"]))
    return _page("You're signed in", f"<p>This browser is signed in as <b>{html.escape(row['email'])}</b>.</p>"
                 f'<p><a href="{nxt}">Continue</a></p><p style="color:#424B57;font-size:14px">Signing in on another '
                 "computer? Type the code from the email into that page instead.</p>", 200)


@router.get("/email-link/status")
def status(request: Request):
    """Polled by the page that asked: picks up a sign-in made with the link in another tab of this browser."""
    if request.session.get("email"):
        return {"email": request.session["email"]}
    return {"pending": bool(request.session.get("email_link")), "email": None}


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
