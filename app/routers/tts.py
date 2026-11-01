"""Text to speech with Microsoft Edge neural voices (free, no API key, needs internet).

The chat page posts each finished reply here and plays the MP3. If this fails, the UI falls back to the
browser's own voices.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..auth import owner_key
from ..db import DB
from ..deps import get_db, get_site

router = APIRouter(prefix="/api")

DEFAULT_VOICE = "en-GB-RyanNeural"
MAX_CHARS = 1500
VOICE_RE = re.compile(r"^[a-z]{2,3}-[A-Z]{2}-[A-Za-z]+Neural$")


def speakable(text: str) -> str:
    """Drop what sounds bad read aloud: code, links, markdown symbols, emoji."""
    t = re.sub(r"```.*?```", " I've put the code in the chat. ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "the link", t)
    t = re.sub(r"[*_#>|~]+", "", t)
    t = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > MAX_CHARS:
        cut = t[:MAX_CHARS]
        t = cut[: max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! ")) + 1] or cut
    return t


async def synthesize(text: str, voice: str) -> bytes:
    import edge_tts  # imported lazily so the app still starts without it

    audio = bytearray()
    async for chunk in edge_tts.Communicate(text, voice).stream():
        if chunk.get("type") == "audio":
            audio.extend(chunk["data"])
    return bytes(audio)


class TTSBody(BaseModel):
    text: str = Field(max_length=20000)
    voice: str | None = None  # admin preview of another voice


@router.post("/tts")
async def tts(body: TTSBody, request: Request, db: DB = Depends(get_db)):
    owner_key(request)
    voice = body.voice or get_site(db)["avatar"].get("voice") or DEFAULT_VOICE
    if not VOICE_RE.match(voice):
        raise HTTPException(status_code=400, detail="Unknown voice")
    text = speakable(body.text)
    if not text:
        return Response(status_code=204)
    try:
        audio = await synthesize(text, voice)
    except Exception as exc:  # network down, voice removed, package missing…
        raise HTTPException(status_code=503, detail=f"Voice service unavailable: {type(exc).__name__}") from exc
    if not audio:
        raise HTTPException(status_code=503, detail="Voice service returned no audio")
    return Response(audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})
