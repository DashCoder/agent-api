"""Short-lived Simli session tokens for the live lip-synced avatar.

The Simli API key never leaves the server. Each browser asks for a token when the visitor switches the live
avatar on; the token is only good for one session of at most SIMLI_MAX_SESSION_S seconds. A per-visitor
cooldown and a daily cap keep a public page from running up the bill.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import owner_key
from ..config import Settings
from ..db import DB
from ..deps import get_db, get_site

router = APIRouter(prefix="/api")

COOLDOWN_S = 20
_last_request: dict[str, float] = {}


def _count_today(db: DB, limit: int) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    usage = db.get_setting("simli_usage", {})
    count = usage.get("count", 0) if usage.get("date") == today else 0
    if count >= limit:
        raise HTTPException(status_code=429, detail="The live avatar has reached today's limit.")
    db.set_setting("simli_usage", {"date": today, "count": count + 1})


async def request_token(s: Settings, face_id: str) -> str:
    body = {"faceId": face_id, "handleSilence": True, "maxSessionLength": s.simli_max_session_s,
            "maxIdleTime": 120}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{s.simli_api_url}/compose/token", json=body,
                         headers={"x-simli-api-key": s.simli_api_key})
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Simli refused the session ({r.status_code})")
    token = r.json().get("session_token")
    if not token:
        raise HTTPException(status_code=502, detail="Simli returned no session token")
    return token


@router.post("/simli/token")
async def simli_token(request: Request, db: DB = Depends(get_db)):
    s: Settings = request.app.state.settings
    avatar = get_site(db)["avatar"]
    if not (s.simli_api_key and avatar.get("simliEnabled") and avatar.get("simliFaceId")):
        raise HTTPException(status_code=404, detail="The live avatar is not switched on.")
    who = owner_key(request)
    now = time.monotonic()
    if now - _last_request.get(who, -1e9) < COOLDOWN_S:
        raise HTTPException(status_code=429, detail="Please wait a few seconds before restarting the live avatar.")
    _last_request[who] = now
    _count_today(db, s.simli_daily_sessions)
    try:
        token = await request_token(s, avatar["simliFaceId"])
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Simli: {type(exc).__name__}") from exc
    return {"sessionToken": token, "maxSessionSeconds": s.simli_max_session_s}
