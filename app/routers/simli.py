"""Short-lived Simli session tokens for the live lip-synced avatar.

The Simli API key never leaves the server. Each browser asks for a token when the visitor switches the live
avatar on; the token is only good for one session of at most SIMLI_MAX_SESSION_S seconds. A per-visitor
cooldown and a daily cap keep a public page from running up the bill.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import security
from ..auth import owner_key
from ..config import Settings
from ..db import DB
from ..deps import get_db, get_site

router = APIRouter(prefix="/api")
log = logging.getLogger("agent_api.simli")

COOLDOWN_S = 20
_last_request: dict[str, float] = {}


def _count_today(db: DB, limit: int) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    usage = db.get_setting("simli_usage", {})
    count = usage.get("count", 0) if usage.get("date") == today else 0
    if count >= limit:
        raise HTTPException(status_code=429, detail="The live avatar has reached today's limit.")
    db.set_setting("simli_usage", {"date": today, "count": count + 1})


def clean_key(key: str) -> str:
    """Keys pasted into .env with quotes or a trailing space are the commonest cause of a 401."""
    return key.strip().strip('"').strip("'").strip()


def refusal(status: int, body: str) -> str:
    if status == 401:
        return ("Simli refused the session (401): it didn't accept the API key. Check SIMLI_API_KEY in agent-api's "
                ".env: copy the key again from Simli's dashboard (API keys) and restart agent-api")
    if status == 403:
        return ("Simli refused the session (403): the face belongs to another Simli account. Use a face ID from "
                "the account whose API key is in SIMLI_API_KEY")
    if status == 402:
        return "Simli refused the session (402): the account has no credits left"
    return f"Simli refused the session ({status}){': ' + body[:200] if body else ''}"


FORMATS = {"square": (768, 768), "portrait": (576, 1024), "landscape": (1024, 576)}


def face_for(avatar: dict, agent: str | None) -> str:
    """The agent's own face if it has one, else the site's."""
    own = (avatar.get("simliFaces") or {}).get(agent or "") if agent else None
    return (own or avatar.get("simliFaceId") or "").strip()


def agents_with_faces(avatar: dict) -> list[str]:
    return sorted(a for a, f in (avatar.get("simliFaces") or {}).items() if isinstance(f, str) and f.strip())


async def request_token(s: Settings, face_id: str, transport=None, fmt: str = "square") -> str:
    width, height = FORMATS.get(fmt, FORMATS["square"])
    # Trinity-2 faces use the framing fields; older faces ignore them
    body = {"faceId": face_id.strip(), "apiVersion": "v2", "handleSilence": True,
            "maxSessionLength": s.simli_max_session_s, "maxIdleTime": 120,
            "streaming_width": width, "streaming_height": height}
    async with httpx.AsyncClient(timeout=15, transport=transport) as c:
        r = await c.post(f"{s.simli_api_url}/compose/token", json=body,
                         headers={"x-simli-api-key": clean_key(s.simli_api_key)})
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=refusal(r.status_code, r.text.strip()))
    data = r.json()
    for w in data.get("warnings") or []:
        log.warning("Simli: %s", w)
    token = data.get("session_token")
    if not token or token == "FAIL TOKEN":
        raise HTTPException(status_code=502, detail="Simli returned no session token")
    return token


class TokenBody(BaseModel):
    agent: str | None = Field(default=None, max_length=64)


@router.post("/simli/token")
async def simli_token(request: Request, body: TokenBody | None = None, db: DB = Depends(get_db)):
    s: Settings = request.app.state.settings
    avatar = get_site(db)["avatar"]
    face = face_for(avatar, body.agent if body else None)
    if not (s.simli_api_key and avatar.get("simliEnabled") and face):
        raise HTTPException(status_code=404, detail="The live avatar is not switched on.")
    who = owner_key(request)
    if not security.at_least(security.caller_of(request), security.TRUSTED) and \
            not security.hit(db, f"simli:{security.client_ip(request)}", 86400, 6):
        raise HTTPException(status_code=429, detail="That's the most live-avatar sessions for today.")
    now = time.monotonic()
    if now - _last_request.get(who, -1e9) < COOLDOWN_S:
        raise HTTPException(status_code=429, detail="Please wait a few seconds before restarting the live avatar.")
    _last_request[who] = now
    _count_today(db, s.simli_daily_sessions)
    try:
        token = await request_token(s, face, fmt=avatar.get("simliFormat") or "square")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Simli: {type(exc).__name__}") from exc
    return {"sessionToken": token, "maxSessionSeconds": s.simli_max_session_s}
