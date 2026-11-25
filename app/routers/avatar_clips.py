"""Avatar clips made from an uploaded video (admin → Avatar → Upload a clip).

The admin uploads a video (made with Gemini/Veo or recorded on a phone) and says whether it's an idle (listening)
or a talking clip. The server turns it into a seamless loop for the avatar player, like prepare_clips.py: it skips
the first moments, cuts it to the chosen shape (square by default), 25 fps, no sound, and crossfades the end into the
start so the loop has no jump. The clip is added to the avatar's list and served from /api/avatar/clips/<name>.

ffmpeg comes with agent-api (the imageio-ffmpeg package), so nothing needs installing; a system ffmpeg is used
when there is one.
"""
from __future__ import annotations

import asyncio
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from ..auth import require_admin
from ..db import DB
from ..defaults import DEFAULT_SITE
from ..deps import get_db, get_site

router = APIRouter(prefix="/api")

SHAPES = {"square": (720, 720), "wide": (1280, 720), "tall": (720, 1280)}
MAX_MB = 300
MAX_SECONDS = 20.0         # longer recordings are cut to this (a loop doesn't need more)
FADE = 0.6
FPS = 25
DEFAULT_POSTER = DEFAULT_SITE["avatar"]["posterUrl"]
NAME = re.compile(r"^(idle|talk)_[a-f0-9]{8}\.mp4$|^poster_[a-f0-9]{8}\.jpg$")


def ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # pragma: no cover - depends on the machine
        raise HTTPException(status_code=503, detail="Video processing isn't available on this server "
                                                    f"(ffmpeg not found: {e}).") from e


def folder(request: Request) -> Path:
    d = Path(request.app.state.settings.data_dir) / "avatar"
    d.mkdir(parents=True, exist_ok=True)
    return d


def duration(exe: str, path: Path) -> float:
    out = subprocess.run([exe, "-hide_banner", "-i", str(path)], capture_output=True, text=True, timeout=60)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out.stderr)
    if not m:
        raise HTTPException(status_code=400, detail="That file doesn't look like a video.")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def make_loop(exe: str, src: Path, dst: Path, *, skip: float, shape: str) -> float:
    """The same steps as prepare_clips.py. Returns the loop's length in seconds."""
    total = duration(exe, src)
    start = min(max(skip, 0.0), max(total - 2.0, 0.0))
    end = min(total, start + MAX_SECONDS)
    d = end - start
    if d <= 2 * FADE + 0.5:
        raise HTTPException(status_code=400, detail=f"The video is too short ({total:.1f} s). Use at least 3 seconds.")
    w, h = SHAPES[shape]
    graph = (
        f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS,fps={FPS},"
        f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,format=yuv420p,"
        f"split[a][b];"
        f"[a]trim=start={FADE},setpts=PTS-STARTPTS[main];"
        f"[b]trim=end={FADE},setpts=PTS-STARTPTS[head];"
        f"[main][head]xfade=transition=fade:duration={FADE}:offset={d - 2 * FADE:.3f}[out]"
    )
    r = subprocess.run([exe, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-filter_complex", graph,
                        "-map", "[out]", "-an", "-c:v", "libx264", "-crf", "20", "-preset", "medium",
                        "-g", str(FPS), "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dst)],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not dst.exists():
        raise HTTPException(status_code=400, detail="Couldn't process that video: " + (r.stderr.strip()[-300:] or "ffmpeg failed"))
    return round(d - FADE, 1)


def poster(exe: str, clip: Path, dst: Path) -> bool:
    r = subprocess.run([exe, "-y", "-hide_banner", "-loglevel", "error", "-i", str(clip), "-frames:v", "1",
                        "-q:v", "3", str(dst)], capture_output=True, timeout=60)
    return r.returncode == 0 and dst.exists()


@router.post("/admin/avatar/clips", dependencies=[Depends(require_admin)])
async def upload_clip(request: Request, file: UploadFile = File(...),
                      kind: Literal["idle", "talk"] = Form(...),
                      shape: Literal["square", "wide", "tall"] = Form("square"),
                      skip: float = Form(0.5), db: DB = Depends(get_db)) -> dict[str, Any]:
    exe = ffmpeg()
    out_dir = folder(request)
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "upload"
        size = 0
        with raw.open("wb") as f:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_MB * 1024 * 1024:
                    raise HTTPException(status_code=413, detail=f"The video is larger than {MAX_MB} MB.")
                f.write(chunk)
        name = f"{kind}_{secrets.token_hex(4)}.mp4"
        seconds = await asyncio.to_thread(make_loop, exe, raw, out_dir / name, skip=skip, shape=shape)
    url = f"/api/avatar/clips/{name}"
    site = db.get_setting("site", {})
    avatar = dict(get_site(db)["avatar"])
    key = "idleVideos" if kind == "idle" else "talkVideos"
    avatar[key] = [*avatar.get(key, []), url]
    poster_url = None
    if avatar.get("posterUrl") in ("", None, DEFAULT_POSTER):   # a frame of the clip shows while the video loads
        pname = f"poster_{secrets.token_hex(4)}.jpg"
        if await asyncio.to_thread(poster, exe, out_dir / name, out_dir / pname):
            poster_url = avatar["posterUrl"] = f"/api/avatar/clips/{pname}"
    site["avatar"] = avatar
    db.set_setting("site", site)
    db.audit("admin", "avatar.clip", name, kind=kind, shape=shape, seconds=seconds, filename=(file.filename or "")[:120])
    return {"url": url, "kind": kind, "seconds": seconds, "posterUrl": poster_url}


@router.get("/avatar/clips/{name}")
def get_clip(name: str, request: Request):
    if not NAME.match(name):
        raise HTTPException(status_code=404, detail="Not found")
    path = folder(request) / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, media_type="video/mp4" if name.endswith(".mp4") else "image/jpeg",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})
