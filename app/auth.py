"""Cookie sessions: anonymous visitors, optional verified email, and admin login."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import uuid

from fastapi import Depends, HTTPException, Request

from .config import Settings, get_settings

PBKDF2_ROUNDS = 310_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return "pbkdf2$%d$%s$%s" % (PBKDF2_ROUNDS, base64.b64encode(salt).decode(),
                                base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        _, rounds, salt_b64, digest_b64 = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(rounds))
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except Exception:
        return False


def check_admin_credentials(username: str, password: str, s: Settings) -> bool:
    if not hmac.compare_digest(username, s.admin_username):
        return False
    if s.admin_password_hash:
        return verify_password(password, s.admin_password_hash)
    if s.admin_password:  # dev only
        return hmac.compare_digest(password, s.admin_password)
    return False


def owner_key(request: Request) -> str:
    """Who owns saved replies, uploads and chat sessions for this browser."""
    sess = request.session
    if "visitor" not in sess:
        sess["visitor"] = uuid.uuid4().hex
    return f"email:{sess['email']}" if sess.get("email") else f"visitor:{sess['visitor']}"


def current_user(request: Request) -> dict | None:
    return request.session.get("user")


def require_admin(request: Request, s: Settings = Depends(get_settings)) -> dict:
    user = current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=401, detail="Admin login required")
    return user
