"""Chat without Hermes (HERMES_MODE=direct): agent-api asks one model directly and runs the agents' own tools.

Why: Hermes sends the model a long prompt (its tools, skills and memory) on every turn. On a server without a GPU
that prompt alone can take a minute to read, so the chat shows "typing…" for a long time, and Hermes needs its own
model in memory. Direct chat sends a short prompt and only the tools of the installed agents (Neil, Axelle, the
coding agent…), so one bigger model fits and the first words come quickly.

What you don't get without Hermes: its long-term memory and skills, its terminal and web browsing, scheduled jobs
and its Slack bot. Switch back with HERMES_MODE=hermes.

The model is the one chosen for "Chat" on Model & tools (a local Ollama model or OpenRouter). Tool calls go through
the same checks as Hermes' MCP calls (routers/mcp.py: who may use which tool, secrets removed from results), and
each reply is limited to a few rounds of tool calls.
"""
from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI

from . import llm
from .config import Settings
from .hermes import build_instructions, iter_sse

AGENT = "chat"
CLOUD = "chat-cloud"        # the model used when someone picks "OpenRouter" in the chat box
TOOLS_NOTE = (
    "You answer in this chat yourself. When a question needs data or an action, use the tools you're given; don't "
    "guess their results. You can't run commands, browse the web or remember past chats beyond this one. If a tool "
    "fails, say what went wrong in plain words."
)
NO_TOOLS_NOTE = "You have no tools here: answer from the information above, and say so if you don't know."


def register(app: FastAPI, s: Settings) -> None:
    llm.register(app, AGENT, name="Chat (without Hermes)",
                 uses="answers in the chat and uses the agents' tools",
                 default={"provider": "ollama", "model": s.direct_model}, providers=["ollama", "openrouter"])
    llm.register(app, CLOUD, name="Chat on OpenRouter",
                 uses="answers in the chat when someone picks OpenRouter in the chat box (faster, sent to the cloud)",
                 default={"provider": "openrouter", "model": s.direct_cloud_model}, providers=["openrouter"])


def engines(app: FastAPI) -> list[str]:
    """What the chat box can offer: local always; OpenRouter when there's a key."""
    s = app.state.settings
    if s.hermes_mode != "direct":
        return []
    return ["local", "openrouter"] if s.openrouter_api_key else ["local"]


def short(text: str, limit: int) -> str:
    """Text cut to about `limit` characters at a sentence end (tool descriptions are read on every first reply)."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("; "))
    return cut[:end + 1] if end > limit // 3 else cut.rstrip() + "…"


def compact(schema: Any) -> Any:
    """A tool's input schema with long property descriptions shortened; types, names and enums kept."""
    if isinstance(schema, dict):
        return {k: (short(v, 120) if k == "description" and isinstance(v, str) else compact(v))
                for k, v in schema.items() if k not in ("examples", "title", "$schema")}
    if isinstance(schema, list):
        return [compact(v) for v in schema]
    return schema


def _obj(args: str) -> dict[str, Any]:
    try:
        v = json.loads(args or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


class ThinkFilter:
    """Hide <think>…</think> from a stream of text pieces, even when a tag is split between pieces."""

    def __init__(self) -> None:
        self.inside, self.buf = False, ""

    def feed(self, text: str) -> str:
        self.buf += text
        out = []
        while self.buf:
            tag = "</think>" if self.inside else "<think>"
            i = self.buf.find(tag)
            if i >= 0:
                if not self.inside:
                    out.append(self.buf[:i])
                self.buf, self.inside = self.buf[i + len(tag):], not self.inside
                continue
            keep = next((n for n in range(len(tag) - 1, 0, -1) if self.buf.endswith(tag[:n])), 0)
            if not self.inside:
                out.append(self.buf[:len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep:]
            break
        return "".join(out)

    def flush(self) -> str:
        tail, self.buf = ("" if self.inside else self.buf), ""
        return tail


class AppRequest:
    """What tool handlers read from ctx.request (they only use .app) when the call isn't an HTTP request."""

    def __init__(self, app: FastAPI):
        self.app = app
        self.client = None
        self.headers: dict[str, str] = {}


class DirectAgent:
    def __init__(self, s: Settings, app: FastAPI, *, tools: bool = True,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.s, self.app, self.use_tools, self.transport = s, app, tools, transport

    # ------------------------------------------------------------------ helpers
    def _endpoint(self, engine: str | None = None) -> tuple[dict[str, str], str, dict[str, str]]:
        """The model for this reply: the one picked in the chat box (local / OpenRouter), else Chat's model."""
        c = llm.choice(self.app, AGENT)
        if engine == "openrouter":
            c = llm.choice(self.app, CLOUD)
        elif engine == "local" and c["provider"] != "ollama":
            c = {"provider": "ollama", "model": self.s.direct_model}
        if c["provider"] == "ollama":
            return c, f"{self.s.ollama_url.rstrip('/')}/v1", {}
        if not self.s.openrouter_api_key:
            raise llm.LlmError("OPENROUTER_API_KEY isn't set")
        return c, llm.OPENROUTER_URL, {"Authorization": f"Bearer {self.s.openrouter_api_key}",
                                       "HTTP-Referer": "https://inagentic.ai", "X-Title": "InAgentic"}

    def _tools(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        """The tools this turn may use: the opened agent's own, else every agent's that the caller is allowed."""
        if not self.use_tools:
            return []
        from .routers.mcp import allowed, current_caller
        caller = current_caller(self.app)
        focus = set((context.get("focus") or {}).get("tools") or [])
        out = []
        for name, (_, tool) in sorted(self.app.state.mcp_tools.items()):
            if focus and name not in focus:
                continue
            if not allowed(caller, tool):
                continue
            out.append({"type": "function", "function": {
                "name": name, "description": short(tool.description or "", 300),
                "parameters": compact(tool.input_schema or {"type": "object", "properties": {}})}})
        return out

    def _history(self, conversation: str, text: str) -> list[dict[str, str]]:
        rows = self.app.state.db.all(
            "SELECT role, text FROM messages WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
            (conversation, self.s.direct_history + 1))
        msgs = [{"role": "assistant" if r["role"] == "assistant" else "user", "content": r["text"]}
                for r in reversed(rows) if r["text"]]
        if not msgs or msgs[-1]["role"] != "user" or msgs[-1]["content"] != text:
            msgs.append({"role": "user", "content": text})
        while msgs and msgs[0]["role"] == "assistant" and len(msgs) > 1:
            msgs.pop(0)                       # start with the person (the greeting adds nothing)
        return msgs[-self.s.direct_history:]

    # ------------------------------------------------------------------ one model call
    async def _round_ollama(self, c, base, messages, tools, out) -> AsyncIterator[dict[str, Any]]:
        """Ollama's own API: it can switch thinking off (DIRECT_THINK=false), which the OpenAI-style one can't.
        Thinking first is what made replies start a minute late on CPU servers."""
        body: dict[str, Any] = {"model": c["model"], "messages": messages, "stream": True, "think": self.s.direct_think,
                                "keep_alive": "30m",
                                "options": {"temperature": 0.3, "num_predict": self.s.direct_max_tokens}}
        if tools:
            body["tools"] = tools
        url = f"{self.s.ollama_url.rstrip('/')}/api/chat"
        async with httpx.AsyncClient(timeout=self.s.llm_timeout_s, transport=self.transport) as cl:
            async with cl.stream("POST", url, json=body) as r:
                if r.status_code >= 400:
                    raise llm.LlmError(f"{r.status_code}: {(await r.aread()).decode(errors='replace')[:300]}")
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise llm.LlmError(str(chunk["error"]))
                    msg = chunk.get("message") or {}
                    piece = out["think"].feed(msg.get("content") or "")
                    if piece:
                        out["said"].append(piece)
                        yield {"type": "delta", "text": piece}
                    for tc in msg.get("tool_calls") or []:
                        fn = tc.get("function") or {}
                        args = fn.get("arguments")
                        out["calls"].append({"id": tc.get("id") or "", "name": fn.get("name") or "",
                                             "args": args if isinstance(args, str) else json.dumps(args or {})})
                    if chunk.get("done"):
                        out["usage"] = {"tokens_in": chunk.get("prompt_eval_count"),
                                        "tokens_out": chunk.get("eval_count")}

    async def _round_openai(self, c, base, headers, messages, tools, out) -> AsyncIterator[dict[str, Any]]:
        body: dict[str, Any] = {"model": c["model"], "messages": messages, "stream": True,
                                "temperature": 0.3, "max_tokens": self.s.direct_max_tokens}
        if tools:
            body["tools"] = tools
        if c["provider"] == "openrouter":
            body["user"] = f"inagentic:{AGENT}"
        calls: dict[int, dict[str, Any]] = {}
        async with httpx.AsyncClient(timeout=self.s.llm_timeout_s, transport=self.transport) as cl:
            async with cl.stream("POST", f"{base}/chat/completions", json=body, headers=headers) as r:
                if r.status_code >= 400:
                    raise llm.LlmError(f"{r.status_code}: {(await r.aread()).decode(errors='replace')[:300]}")
                async for _, data in iter_sse(r.aiter_lines()):
                    if data.strip() == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if chunk.get("error"):
                        raise llm.LlmError(str(chunk["error"].get("message") or chunk["error"]))
                    delta = ((chunk.get("choices") or [{}])[0].get("delta") or {})
                    piece = out["think"].feed(delta.get("content") or "")
                    if piece:
                        out["said"].append(piece)
                        yield {"type": "delta", "text": piece}
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", len(calls)), {"id": "", "name": "", "args": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] += fn.get("name") or ""
                        slot["args"] += fn.get("arguments") or ""
        out["calls"] = [v for _, v in sorted(calls.items())]

    # ------------------------------------------------------------------ the Agent protocol
    async def stream(self, conversation: str, text: str, *, learn: bool,
                     context: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        from .routers.mcp import call_tool
        try:
            c, base, headers = self._endpoint(context.get("engine"))
        except llm.LlmError as e:
            yield {"type": "error", "message": str(e)}
            return
        native = c["provider"] == "ollama"
        tools = self._tools(context)
        system = build_instructions(context, learn=True) + "\n\n" + (TOOLS_NOTE if tools else NO_TOOLS_NOTE)
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}, *self._history(conversation, text)]
        for rnd in range(self.s.direct_tool_rounds + 1):
            offer = tools if rnd < self.s.direct_tool_rounds else []      # the last round must answer in words
            out: dict[str, Any] = {"said": [], "calls": [], "usage": {}, "think": ThinkFilter()}
            started, err = time.monotonic(), None
            try:
                gen = (self._round_ollama(c, base, messages, offer, out) if native
                       else self._round_openai(c, base, headers, messages, offer, out))
                async for ev in gen:
                    yield ev
                tail = out["think"].flush()
                if tail:
                    out["said"].append(tail)
                    yield {"type": "delta", "text": tail}
            except (httpx.HTTPError, llm.LlmError, ValueError) as e:
                err = f"{e.__class__.__name__}: {e}"
            llm.record(self.app, AGENT, c["provider"], c["model"], round((time.monotonic() - started) * 1000),
                       task="chat", error=err, **{k: v for k, v in out["usage"].items() if v is not None})
            if err:
                yield {"type": "error", "message": f"The model didn't answer ({llm.label(c)}): {err}"}
                return
            calls = out["calls"]
            if not calls:
                return
            if native:
                messages.append({"role": "assistant", "content": "".join(out["said"]), "tool_calls": [
                    {"function": {"name": v["name"], "arguments": _obj(v["args"])}} for v in calls]})
            else:
                messages.append({"role": "assistant", "content": "".join(out["said"]), "tool_calls": [
                    {"id": v["id"] or f"call_{i}", "type": "function",
                     "function": {"name": v["name"], "arguments": v["args"] or "{}"}} for i, v in enumerate(calls)]})
            for i, v in enumerate(calls):
                yield {"type": "progress", "label": self._label(v["name"])}
                try:
                    args = json.loads(v["args"] or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be an object")
                    res = await call_tool(self.app, v["name"], args)
                    result = res["content"][0]["text"]
                except ValueError as e:
                    result = json.dumps({"error": f"Bad arguments for {v['name']}: {e}"})
                if native:
                    messages.append({"role": "tool", "tool_name": v["name"], "content": result[:20000]})
                else:
                    messages.append({"role": "tool", "tool_call_id": v["id"] or f"call_{i}", "content": result[:20000]})

    def _label(self, name: str) -> str:
        owner = (self.app.state.mcp_tools.get(name) or (None,))[0]
        p = self.app.state.plugins.get(owner or "")
        who = (p.public_info.get("agentName") if p else None) or (owner or "").title()
        return f"Asking {who}…" if who else "Working on it…"

    async def approve(self, run_id: str, approve: bool) -> dict[str, Any]:
        return {"status": "unknown", "message": "There's nothing waiting for approval."}

    async def status(self) -> dict[str, Any]:
        try:
            c, base, headers = self._endpoint()
        except llm.LlmError as e:
            return {"mode": "direct", "reachable": False, "models": [], "authorized": False, "error": str(e)}
        if c["provider"] != "ollama":
            return {"mode": "direct", "reachable": True, "models": [c["model"]], "authorized": True}
        try:
            async with httpx.AsyncClient(timeout=5, transport=self.transport) as cl:
                r = await cl.get(f"{self.s.ollama_url.rstrip('/')}/api/tags")
            have = [m.get("name") for m in r.json().get("models", [])]
            return {"mode": "direct", "reachable": r.status_code == 200, "models": [c["model"]], "authorized": True,
                    "installed": c["model"] in have}
        except (httpx.HTTPError, ValueError) as e:
            return {"mode": "direct", "reachable": False, "models": [c["model"]], "authorized": True, "error": str(e)}

    async def skills(self) -> list[dict[str, Any]]:
        return []
