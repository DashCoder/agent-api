"""Shared safety checks, mirroring the 17 Nov 2026 hardening of the OxComply sites.

- who is asking (caller): admin, a trusted or verified email, or an anonymous visitor; tools look at it
- secret redaction: known secret values (this server's own keys) and well-known token formats never leave in a chat
  reply or a tool result
- rate limits kept in the database (so they hold across restarts)
- probe detection: attempts to extract prompts, tools, config or keys get a fixed reply with no model call
- email "inbox" normalisation, so plus-addresses (axel+b@…) share one inbox's limits
"""
from __future__ import annotations

import re
import time
from typing import Any

from fastapi import Request

from .db import DB

# ------------------------------------------------------------------ who is asking
# anonymous: no sign-in · verified: proved an email address (anyone with an inbox can) · trusted: a verified email
# listed in TRUSTED_EMAILS · admin: signed in to the admin · system: no web chat is running (Slack, scheduled jobs,
# set up by the admin on the server)
ANONYMOUS, VERIFIED, TRUSTED, ADMIN, SYSTEM = "anonymous", "verified", "trusted", "admin", "system"
RANK = {ANONYMOUS: 0, VERIFIED: 1, TRUSTED: 2, ADMIN: 3, SYSTEM: 4}


def email_list(value: str) -> set[str]:
    return {e.strip().lower() for e in (value or "").replace(";", ",").split(",") if "@" in e}


def caller_of(request: Request) -> dict[str, Any]:
    """The person behind this browser, from the signed session cookie only (never from what they type)."""
    user = request.session.get("user") or {}
    email = (request.session.get("email") or "").lower() or None
    if user.get("role") == "admin":
        return {"role": ADMIN, "email": email, "username": user.get("username")}
    if email and email in email_list(getattr(request.app.state.settings, "trusted_emails", "")):
        return {"role": TRUSTED, "email": email}
    if email:
        return {"role": VERIFIED, "email": email}
    return {"role": ANONYMOUS, "email": None}


def at_least(caller: dict[str, Any], role: str) -> bool:
    return RANK.get(caller.get("role", ANONYMOUS), 0) >= RANK[role]


def least(callers: list[dict[str, Any]]) -> dict[str, Any]:
    """With several chats running at once, a tool call can't tell whose it is: assume the least trusted one, and
    no email at all when they differ."""
    if not callers:
        return {"role": SYSTEM, "email": None}
    low = min(callers, key=lambda c: RANK.get(c.get("role", ANONYMOUS), 0))
    emails = {c.get("email") for c in callers}
    return {**low, "email": low.get("email") if len(emails) == 1 else None}


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ------------------------------------------------------------------ secrets
TOKEN_PATTERNS = [
    re.compile(r"\b(?:sk-(?:ant-|or-|proj-)?[A-Za-z0-9_\-]{16,})"),             # Anthropic, OpenRouter, OpenAI
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),                 # GitHub classic tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),                             # GitHub fine-grained
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"),                            # Slack
    re.compile(r"\bxapp-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),                                # AWS access key ids
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),                                  # Google API keys
    re.compile(r"\bATATT[A-Za-z0-9_\-=]{20,}\b"),                                # Atlassian API tokens
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWTs
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/]{20,}=*"),
    re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s'\"<>]+"),  # connection strings
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:aws_secret_access_key|secret_key|api_key|apikey|password|passwd|token)\s*[=:]\s*['\"]?[^\s'\"]{12,}"),
]
REDACTED = "[redacted]"
MIN_SECRET = 12            # shorter values (e.g. "true", a port) are never treated as secrets
SECRET_NAME = re.compile(r"(key|token|secret|password|passwd|hash|oauth)", re.I)


def known_secrets(app: Any) -> list[str]:
    """This server's own secret values: core settings and every plugin's settings whose names say key/token/secret,
    plus every secret saved on Settings & keys. Longest first, so overlapping values are fully removed."""
    vals: set[str] = set()
    for obj in [app.state.settings, *getattr(app.state, "plugin_settings", {}).values()]:
        if obj is None:
            continue
        for name in type(obj).model_fields:
            if SECRET_NAME.search(name):
                v = getattr(obj, name, "")
                if isinstance(v, str) and len(v.strip()) >= MIN_SECRET:
                    vals.add(v.strip())
    for v in getattr(app.state, "extra_secrets", ()):
        if isinstance(v, str) and len(v) >= MIN_SECRET:
            vals.add(v)
    return sorted(vals, key=len, reverse=True)


def redact(text: str, secrets: list[str] | None = None) -> str:
    if not text:
        return text
    for s in secrets or ():
        if s in text:
            text = text.replace(s, REDACTED)
    for p in TOKEN_PATTERNS:
        text = p.sub(REDACTED, text)
    return text


class StreamRedactor:
    """Redacts a reply that arrives in pieces: holds back a short tail so a key split across two pieces is still
    caught, and releases it at the end."""

    def __init__(self, secrets: list[str], hold: int = 120):
        self.secrets, self.buf = secrets, ""
        self.hold = max(hold, max((len(s) for s in secrets), default=0) + 8)

    def feed(self, piece: str) -> str:
        self.buf += piece
        if len(self.buf) <= self.hold:
            return ""
        clean = redact(self.buf, self.secrets)
        if clean != self.buf:             # something was redacted: keep the tail in case it continues
            out, self.buf = clean[:-self.hold], clean[-self.hold:]
            return out
        cut = len(self.buf) - self.hold
        cut = max(self.buf.rfind(" ", 0, cut + 1), 0) or cut   # don't split in the middle of a word if possible
        out, self.buf = self.buf[:cut], self.buf[cut:]
        return out

    def flush(self) -> str:
        out, self.buf = redact(self.buf, self.secrets), ""
        return out


# ------------------------------------------------------------------ rate limits (in the database)
def hit(db: DB, key: str, window_s: int, limit: int, now: float | None = None) -> bool:
    """Count one event for key; False when the key already had `limit` events in the window."""
    now = now or time.time()
    row = db.one("SELECT COUNT(*) AS n FROM rate_events WHERE key = ? AND ts > ?", (key, now - window_s))
    if row and row["n"] >= limit:
        return False
    db.execute("INSERT INTO rate_events(key, ts) VALUES(?, ?)", (key, now))
    return True


def count(db: DB, key: str, window_s: int) -> int:
    row = db.one("SELECT COUNT(*) AS n FROM rate_events WHERE key = ? AND ts > ?", (key, time.time() - window_s))
    return int(row["n"]) if row else 0


def cleanup(db: DB) -> None:
    db.execute("DELETE FROM rate_events WHERE ts < ?", (time.time() - 3 * 86400,))


def inbox(email: str) -> str:
    """axel+b@inagentic.ai and Axel@InAgentic.ai are the same inbox."""
    local, _, domain = email.strip().lower().partition("@")
    return f"{local.split('+', 1)[0]}@{domain}"


# ------------------------------------------------------------------ probes
PROBES = [re.compile(p, re.I) for p in (
    r"\b(system|developer|hidden|initial|original)\s+(prompt|instructions|message)",
    r"\b(ignore|disregard|forget)\b.{0,40}\b(previous|prior|above|earlier|all)\b.{0,20}\b(instructions|rules|prompt)",
    r"\b(reveal|show|print|dump|list|output|repeat|tell me)\b.{0,40}\b(prompt|instructions|config(uration)?|"
    r"api[ _-]?keys?|secrets?|tokens?|credentials|env(ironment)?( variables)?|\.env|passwords?)\b",
    r"\b(env(ironment)? variables?|os\.environ|printenv|process\.env|/etc/passwd|\.ssh/|id_rsa)\b",
    r"\bdebug[ _-]?(mode|dump)\b|\bdeveloper mode\b|\bjailbreak\b|\bDAN\b",
    r"\breply (exactly|only) with\b|\brespond with exactly\b",
    r"\b(call|run|use|invoke) (the )?tool\b.{0,30}\b(with|args?|arguments|parameters)\b",
    r"\bwhat (tools|functions) (do|can) you (have|call|use)\b",
)]
PROBE_REPLY = ("I can't help with that. I'm happy to answer questions about our services, or you can sign in to the "
               "admin to work with the agents.")


def is_probe(text: str) -> bool:
    return any(p.search(text) for p in PROBES)
