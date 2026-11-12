"""Change the model Hermes uses (chat, Slack, tool calls) from the admin.

Hermes keeps it in ~/.hermes/config.yaml:

    model:
      default: anthropic/claude-sonnet-4.5     # the model id
      provider: openrouter                      # openrouter | anthropic | bedrock | custom (any OpenAI-compatible server)
      base_url: http://127.0.0.1:11434/v1       # custom only, e.g. Ollama on this machine
      context_length: 64000                     # custom only; Hermes needs at least 64k

Saving rewrites only that block (everything else in the file is kept), backs the file up first, restarts Hermes and
asks it a test question. If Hermes doesn't answer, the backup is put back and Hermes restarted again.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException

KINDS = {
    "openrouter": "OpenRouter",
    "ollama": "Local model (Ollama)",
    "anthropic": "Anthropic (Claude API)",
    "bedrock": "Claude on AWS Bedrock",
}
OLLAMA_CONTEXT = 65536   # Hermes needs >= 64,000; the model itself must support it (qwen3:14b stops at 40,960)
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@-]{1,160}$")


def config_file(home: Path) -> Path:
    return Path(os.path.expanduser(str(home))) / "config.yaml"


def read(home: Path) -> dict[str, str] | None:
    """{"model", "provider", "baseUrl", "kind"} from config.yaml, or None. Never reads keys."""
    try:
        lines = config_file(home).read_text().splitlines()
    except OSError:
        return None
    out: dict[str, str] = {}
    for i, line in enumerate(lines):
        m = re.match(r"^model:\s*(\S.*)?$", line)
        if not m:
            continue
        if m.group(1) and not m.group(1).startswith("#"):
            out["model"] = m.group(1).strip().strip("'\"")
        for sub in lines[i + 1:]:
            if sub and not sub[0].isspace():
                break
            k = re.match(r"^\s+(default|model|provider|base_url)\s*:\s*(\S[^#]*)", sub)
            if k:
                key = {"default": "model", "model": "model", "provider": "provider", "base_url": "baseUrl"}[k.group(1)]
                out.setdefault(key, k.group(2).strip().strip("'\""))
        break
    if not out:
        return None
    p, base = out.get("provider", ""), out.get("baseUrl", "")
    out["kind"] = ("ollama" if p == "custom" and ("11434" in base or "ollama" in base) else
                   p if p in KINDS else "other")
    return out


def render_block(kind: str, model: str, ollama_url: str) -> list[str]:
    if kind == "ollama":
        return ["model:", f"  default: {model}", "  provider: custom",
                f"  base_url: {ollama_url.rstrip('/')}/v1", f"  context_length: {OLLAMA_CONTEXT}",
                f"  ollama_num_ctx: {OLLAMA_CONTEXT}"]
    return ["model:", f"  default: {model}", f"  provider: {kind}"]


def write(home: Path, kind: str, model: str, ollama_url: str) -> Path | None:
    """Replace the model: block. Returns the backup's path (None if there was no file)."""
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"Choose one of: {', '.join(KINDS.values())}")
    if not MODEL_RE.match(model.strip()):
        raise HTTPException(status_code=400, detail=f"Invalid model name {model!r}")
    f = config_file(home)
    backup = None
    lines: list[str] = []
    if f.exists():
        backup = f.with_name(f"config.yaml.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(f, backup)
        lines = f.read_text().splitlines()
    out, i, done = [], 0, False
    while i < len(lines):
        if re.match(r"^model:", lines[i]) and not done:
            i += 1
            while i < len(lines) and (not lines[i] or lines[i][0].isspace()):
                if not lines[i].strip() and i + 1 < len(lines) and lines[i + 1] and not lines[i + 1][0].isspace():
                    break
                i += 1
            out += render_block(kind, model.strip(), ollama_url)
            done = True
            continue
        out.append(lines[i])
        i += 1
    if not done:
        out = render_block(kind, model.strip(), ollama_url) + ([""] if out else []) + out
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(".config.yaml.tmp")
    tmp.write_text("\n".join(out) + "\n")
    os.replace(tmp, f)
    return backup


async def restart(cmd: str, timeout: float = 60) -> tuple[bool, str]:
    parts = [os.path.expanduser(p) for p in cmd.split()]
    try:
        proc = await asyncio.create_subprocess_exec(*parts, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (OSError, asyncio.TimeoutError) as e:
        return False, f"{e.__class__.__name__}: {e}"
    return proc.returncode == 0, out.decode(errors="replace")[-500:]


async def probe(agent: Any, wait_s: float = 90) -> tuple[bool, str]:
    """Hermes answers a tiny question after the restart (it may take a few seconds to come back)."""
    deadline = time.monotonic() + wait_s
    last = ""
    while time.monotonic() < deadline:
        text, err = [], None
        try:
            async for ev in agent.stream(f"model-check-{int(time.time())}", "Reply with the single word OK.", learn=False,
                                         context={"assistant": {"name": "Hermes"}}):
                if ev["type"] == "delta":
                    text.append(ev["text"])
                elif ev["type"] == "error":
                    err = ev["message"]
        except Exception as e:  # noqa: BLE001
            err = str(e)
        if text and not err:
            return True, "".join(text).strip()[:100]
        last = err or "no answer"
        await asyncio.sleep(3)
    return False, last
