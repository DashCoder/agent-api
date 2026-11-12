"""One model switch for every agent.

Each agent registers which model it uses by default and which providers it allows; the admin changes it on Model &
tools (Save makes it the default). Agents call complete() and every call is logged ("llm.call": agent, provider,
model, time, tokens, cost, error) so Agent logs show which model actually answered.

Providers:
  ollama      a local model on this machine (OLLAMA_URL), free and works offline
  openrouter  any OpenRouter model (OPENROUTER_API_KEY)
  bedrock     Claude on AWS Bedrock (BEDROCK_ACCESS_KEY_ID / BEDROCK_SECRET_ACCESS_KEY, BEDROCK_REGION)
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException

PROVIDERS = {
    "ollama": "Local model (Ollama)",
    "openrouter": "OpenRouter",
    "bedrock": "Claude on AWS Bedrock",
}
SUGGESTIONS = {
    "ollama": ["qwen3:14b", "qwen3:8b", "qwen2.5-coder:14b", "gemma3:12b", "llama3.1:8b"],
    "openrouter": ["anthropic/claude-sonnet-4.5", "qwen/qwen3-coder", "deepseek/deepseek-chat-v3.1",
                   "moonshotai/kimi-k2", "google/gemini-2.5-flash", "openai/gpt-4.1-mini"],
    "bedrock": ["eu.anthropic.claude-sonnet-4-5-20250929-v1:0", "eu.anthropic.claude-haiku-4-5-20251001-v1:0"],
}
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@-]{1,160}$")
OPENROUTER_URL = "https://openrouter.ai/api/v1"


class LlmError(RuntimeError):
    pass


def _agents(app: FastAPI) -> dict[str, dict[str, Any]]:
    if not hasattr(app.state, "model_agents"):
        app.state.model_agents = {}
    return app.state.model_agents


def register(app: FastAPI, agent: str, *, name: str, uses: str, default: dict[str, str],
             providers: list[str] | None = None, warnings: dict[str, str] | None = None) -> None:
    """An agent says it has a model: its default ({"provider", "model"}), what it uses it for, and which providers
    are allowed (e.g. Kai keeps email text local, so no Bedrock)."""
    allowed = [p for p in (providers or list(PROVIDERS)) if p in PROVIDERS]
    if default.get("provider") not in allowed:
        raise ValueError(f"{agent}: default provider {default.get('provider')} isn't allowed")
    _agents(app)[agent] = {"id": agent, "name": name, "uses": uses, "default": dict(default), "providers": allowed,
                           "warnings": dict(warnings or {})}


def choice(app: FastAPI, agent: str) -> dict[str, str]:
    reg = _agents(app).get(agent)
    if not reg:
        raise LlmError(f"No model registered for {agent}")
    saved = app.state.db.get_setting(f"models.{agent}") or {}
    out = {**reg["default"], **{k: v for k, v in saved.items() if k in ("provider", "model")}}
    if out["provider"] not in reg["providers"]:
        out = dict(reg["default"])
    return out


def set_choice(app: FastAPI, agent: str, provider: str, model: str, admin: str) -> dict[str, str]:
    reg = _agents(app).get(agent)
    if not reg:
        raise HTTPException(status_code=404, detail=f"{agent} has no model to choose")
    if provider not in reg["providers"]:
        raise HTTPException(status_code=400, detail=f"{reg['name']} can use: {', '.join(PROVIDERS[p] for p in reg['providers'])}")
    model = model.strip()
    if not MODEL_RE.match(model):
        raise HTTPException(status_code=400, detail=f"Invalid model name {model!r}")
    before = choice(app, agent)
    app.state.db.set_setting(f"models.{agent}", {"provider": provider, "model": model})
    app.state.db.audit(admin, "models.set", agent, plugin=agent, provider=provider, model=model,
                       before=f"{before['model']} ({before['provider']})")
    return choice(app, agent)


def label(c: dict[str, str]) -> str:
    return f"{c['model']} ({ {'ollama': 'local, Ollama', 'openrouter': 'OpenRouter', 'bedrock': 'AWS Bedrock'}[c['provider']] })"


def record(app: FastAPI, agent: str, provider: str, model: str, ms: int, *, task: str = "", tokens_in: int | None = None,
           tokens_out: int | None = None, cost: float | None = None, error: str | None = None) -> None:
    """Log one model call (agents that call models their own way use this too)."""
    detail: dict[str, Any] = {"plugin": agent, "provider": provider, "model": model, "ms": ms}
    if task:
        detail["task"] = task
    if tokens_in is not None:
        detail["tokensIn"] = tokens_in
    if tokens_out is not None:
        detail["tokensOut"] = tokens_out
    if cost is not None:
        detail["cost"] = round(cost, 5)
    if error:
        detail["error"] = error[:300]
    app.state.db.audit(agent, "llm.call", model, **detail)


def model_fn(app: FastAPI, agent: str, task: str = ""):
    """For agents written around `await llm(system, user, max_tokens) -> str`: the same, with the chosen model."""
    async def call(system: str, user: str, max_tokens: int = 2000) -> str:
        return await complete(app, agent, system, user, max_tokens, task=task)
    return call


# ------------------------------------------------------------------ calling a model
async def complete(app: FastAPI, agent: str, system: str, user: str, max_tokens: int = 2000, *,
                   json_mode: bool = False, task: str = "") -> str:
    """The agent's chosen model answers. Logged either way."""
    c = choice(app, agent)
    s = app.state.settings
    transport = getattr(app.state, "llm_transport", None)
    started = time.monotonic()
    usage: dict[str, Any] = {}
    try:
        if c["provider"] == "bedrock":
            text, usage = await _bedrock(s, c["model"], system, user, max_tokens)
        else:
            text, usage = await _openai_compatible(s, c, system, user, max_tokens, json_mode, transport)
    except Exception as e:
        record(app, agent, c["provider"], c["model"], round((time.monotonic() - started) * 1000), task=task,
               error=f"{e.__class__.__name__}: {e}")
        raise LlmError(f"{label(c)}: {e}") from e
    record(app, agent, c["provider"], c["model"], round((time.monotonic() - started) * 1000), task=task, **usage)
    return text


async def _openai_compatible(s, c: dict[str, str], system: str, user: str, max_tokens: int, json_mode: bool,
                             transport) -> tuple[str, dict[str, Any]]:
    if c["provider"] == "ollama":
        base, headers = f"{s.ollama_url.rstrip('/')}/v1", {}
    else:
        if not s.openrouter_api_key:
            raise LlmError("OPENROUTER_API_KEY isn't set in agent-api's .env")
        base = OPENROUTER_URL
        headers = {"Authorization": f"Bearer {s.openrouter_api_key}", "HTTP-Referer": "https://inagentic.ai",
                   "X-Title": "InAgentic"}
    body: dict[str, Any] = {"model": c["model"], "max_tokens": max_tokens, "temperature": 0.3,
                            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if c["provider"] == "openrouter":
        body["usage"] = {"include": True}
    async with httpx.AsyncClient(timeout=s.llm_timeout_s, transport=transport) as cl:
        r = await cl.post(f"{base}/chat/completions", json=body, headers=headers)
    if r.status_code >= 400:
        raise LlmError(f"{r.status_code}: {r.text[:300]}")
    data = r.json()
    text = re.sub(r"<think>[\s\S]*?</think>", "", data["choices"][0]["message"].get("content") or "").strip()
    u = data.get("usage") or {}
    return text, {"tokens_in": u.get("prompt_tokens"), "tokens_out": u.get("completion_tokens"), "cost": u.get("cost")}


async def _bedrock(s, model: str, system: str, user: str, max_tokens: int) -> tuple[str, dict[str, Any]]:
    import boto3  # only needed when an agent uses Bedrock

    key = s.bedrock_access_key_id or s.lambda_access_key_id
    secret = s.bedrock_secret_access_key or s.lambda_secret_access_key
    kw = {"aws_access_key_id": key, "aws_secret_access_key": secret} if key and secret else {}
    client = boto3.client("bedrock-runtime", region_name=s.bedrock_region, **kw)
    body = json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": max_tokens, "system": system,
                       "messages": [{"role": "user", "content": user}]})

    def call() -> tuple[str, dict[str, Any]]:
        r = client.invoke_model(modelId=model, contentType="application/json", accept="application/json", body=body)
        data = json.loads(r["body"].read())
        u = data.get("usage") or {}
        return ("".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"),
                {"tokens_in": u.get("input_tokens"), "tokens_out": u.get("output_tokens")})
    return await asyncio.to_thread(call)


# ------------------------------------------------------------------ what's available
async def providers_status(app: FastAPI) -> dict[str, Any]:
    s = app.state.settings
    out: dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=5, transport=getattr(app.state, "llm_transport", None)) as c:
            r = await c.get(f"{s.ollama_url.rstrip('/')}/api/tags")
        out["ollama"] = {"ready": True, "models": sorted(m["name"] for m in r.json().get("models", []))}
    except (httpx.HTTPError, ValueError, KeyError) as e:
        out["ollama"] = {"ready": False, "models": [], "error": f"Ollama isn't answering at {s.ollama_url} ({e.__class__.__name__})"}
    out["openrouter"] = {"ready": bool(s.openrouter_api_key),
                         "error": None if s.openrouter_api_key else "OPENROUTER_API_KEY isn't set in agent-api's .env"}
    try:
        import boto3  # noqa: F401
        has_boto = True
    except ImportError:
        has_boto = False
    keys = bool((s.bedrock_access_key_id or s.lambda_access_key_id) and (s.bedrock_secret_access_key or s.lambda_secret_access_key))
    out["bedrock"] = {"ready": has_boto and keys,
                      "error": None if has_boto and keys else
                      "Bedrock keys aren't set in agent-api's .env" if has_boto else "boto3 isn't installed"}
    return out
