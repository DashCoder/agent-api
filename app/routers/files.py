"""Serve files the agent created (generated images, documents) so the chat can show and download them.

Only files inside FILE_DIRS are served; anything else is refused, including paths that escape through
symlinks or "..". Replies mention files by their full path, and agent-ui turns those into links here.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from ..auth import owner_key
from ..config import Settings

router = APIRouter(prefix="/api")


def allowed_roots(s: Settings) -> list[Path]:
    return [Path(d.strip()).expanduser().resolve() for d in s.file_dirs.split(",") if d.strip()]


def resolve_allowed(raw: str, s: Settings) -> Path:
    try:
        p = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=404, detail="File not found") from None
    if not p.is_file() or not any(p.is_relative_to(root) for root in allowed_roots(s)):
        raise HTTPException(status_code=404, detail="File not found")
    return p


@router.get("/files")
def get_file(request: Request, path: str = Query(..., max_length=1024), download: bool = False):
    owner_key(request)
    s: Settings = request.app.state.settings
    p = resolve_allowed(path, s)
    media_type = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return FileResponse(p, media_type=media_type, filename=p.name if download else None,
                        content_disposition_type="attachment" if download else "inline",
                        headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})
