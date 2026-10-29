"""Agent backends.

Both backends yield the same small set of events, which the API forwards to the UI as SSE:
    {"type": "delta",    "text": "..."}                    streamed reply text
    {"type": "progress", "label": "Looking that up…"}      friendly tool progress
    {"type": "approval", "runId": "...", "prompt": "..."}  agent wants permission
    {"type": "error",    "message": "..."}

HermesAgent talks to the Hermes Agent API server (`hermes gateway` with API_SERVER_ENABLED=true).
MockAgent is a built-in fake so the UI can be developed and tested without Hermes.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any, AsyncIterator, Protocol

import httpx

from .config import Settings

# Tool name -> friendly progress label shown in the UI. Extend freely.
TOOL_LABELS = {
    "web_search": "Searching the web…",
    "web_extract": "Reading a web page…",
    "browser_exec": "Looking that up in the browser…",
    "terminal": "Working on the computer…",
    "process": "Working on the computer…",
    "read_file": "Reading a file…",
    "patch": "Updating a file…",
    "execute_code": "Running some code…",
    "memory": "Checking what I remember…",
    "session_search": "Checking earlier conversations…",
    "cronjob": "Scheduling a task…",
    "delegate_task": "Asking a helper agent…",
    "vision_analyze": "Looking at the image…",
}

NO_LEARN_INSTRUCTIONS = (
    "Answer directly and concisely. For this turn, do not create or update skills "
    "and do not write to long-term memory."
)


def tool_label(name: str | None) -> str:
    if not name:
        return "Working on it…"
    if name.startswith("mcp"):
        return "Using a connected app…"
    return TOOL_LABELS.get(name, "Working on it…")


class Agent(Protocol):
    def stream(self, conversation: str, text: str, *, learn: bool,
               context: dict[str, Any]) -> AsyncIterator[dict[str, Any]]: ...
    async def approve(self, run_id: str, approve: bool) -> dict[str, Any]: ...
    async def status(self) -> dict[str, Any]: ...
    async def skills(self) -> list[dict[str, Any]]: ...


async def iter_sse(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, str]]:
    """Parse a text/event-stream into (event, data) pairs. Comment lines (keepalives) are skipped."""
    event, data = "message", []
    async for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield event, "\n".join(data)


def translate_hermes_event(event: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Map one Hermes/OpenAI-style stream event to our UI event (or None to ignore)."""
    etype = payload.get("type") or event
    # Responses API
    if etype == "response.output_text.delta":
        return {"type": "delta", "text": payload.get("delta", "")}
    if etype in ("response.output_item.added",):
        item = payload.get("item") or {}
        if item.get("type") == "function_call":
            return {"type": "progress", "label": tool_label(item.get("name"))}
        return None
    if etype in ("response.failed", "error"):
        err = payload.get("error") or payload
        return {"type": "error", "message": str(err.get("message", err))}
    # Hermes run/tool progress events
    if etype in ("hermes.tool.progress", "tool.started"):
        return {"type": "progress", "label": tool_label(payload.get("tool") or payload.get("name"))}
    if "approval" in etype:
        return {"type": "approval", "runId": payload.get("run_id") or payload.get("runId", ""),
                "prompt": payload.get("prompt") or payload.get("command") or "The assistant needs your approval."}
    # Chat Completions chunk
    choices = payload.get("choices")
    if choices:
        delta = (choices[0].get("delta") or {}).get("content")
        return {"type": "delta", "text": delta} if delta else None
    return None


class HermesAgent:
    def __init__(self, s: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.s = s
        self.headers = {"Authorization": f"Bearer {s.hermes_api_key}"}
        self.transport = transport  # injectable for tests

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.s.hermes_api_url, headers=self.headers,
                                 timeout=timeout or self.s.hermes_timeout_s, transport=self.transport)

    async def stream(self, conversation: str, text: str, *, learn: bool,
                     context: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        body: dict[str, Any] = {"model": self.s.hermes_model, "input": text,
                                "conversation": conversation, "stream": True}
        if not learn:
            body["instructions"] = NO_LEARN_INSTRUCTIONS
        try:
            async with self._client() as c:
                async with c.stream("POST", "/v1/responses", json=body,
                                    headers={"Idempotency-Key": uuid.uuid4().hex}) as r:
                    if r.status_code >= 400:
                        detail = (await r.aread()).decode(errors="replace")[:300]
                        yield {"type": "error", "message": f"Hermes returned {r.status_code}: {detail}"}
                        return
                    async for event, data in iter_sse(r.aiter_lines()):
                        if data.strip() == "[DONE]":
                            break
                        try:
                            payload = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        out = translate_hermes_event(event, payload)
                        if out:
                            yield out
        except httpx.HTTPError as e:
            yield {"type": "error", "message": f"Can't reach Hermes at {self.s.hermes_api_url} ({e.__class__.__name__})."}

    async def approve(self, run_id: str, approve: bool) -> dict[str, Any]:
        async with self._client(30) as c:
            r = await c.post(f"/v1/runs/{run_id}/approval",
                             json={"approved": approve, "decision": "approve" if approve else "deny"})
            r.raise_for_status()
            return {"status": "approved" if approve else "denied", "message": None}

    async def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"mode": "hermes", "url": self.s.hermes_api_url, "reachable": False}
        try:
            async with self._client(5) as c:
                h = await c.get("/health")
                out["reachable"] = h.status_code == 200
                m = await c.get("/v1/models")
                if m.status_code == 200:
                    out["models"] = [x.get("id") for x in m.json().get("data", [])]
                out["authorized"] = m.status_code != 401
        except httpx.HTTPError as e:
            out["error"] = e.__class__.__name__
        return out

    async def skills(self) -> list[dict[str, Any]]:
        try:
            async with self._client(10) as c:
                r = await c.get("/v1/skills")
                r.raise_for_status()
                data = r.json()
                items = data.get("data", data) if isinstance(data, dict) else data
                return [{"name": i.get("name", str(i)), "description": i.get("description", "")}
                        if isinstance(i, dict) else {"name": str(i), "description": ""} for i in items]
        except (httpx.HTTPError, ValueError):
            return []


class MockAgent:
    """Deterministic fake agent for development, demos and tests."""

    def __init__(self, s: Settings):
        self.delay = s.mock_delay_s
        self.pending: dict[str, str] = {}

    async def _words(self, text: str) -> AsyncIterator[dict[str, Any]]:
        for token in re.findall(r"\S+\s*|\n", text):
            await asyncio.sleep(self.delay)
            yield {"type": "delta", "text": token}

    async def stream(self, conversation: str, text: str, *, learn: bool,
                     context: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        biz = context.get("business", {})
        t = text.lower()
        if any(w in t for w in ("delete", "run ", "install", "remove")):
            run_id = uuid.uuid4().hex[:12]
            self.pending[run_id] = text
            yield {"type": "progress", "label": "Working on the computer…"}
            async for e in self._words("I can do that, but it changes things on the computer, "
                                       "so I need your OK first."):
                yield e
            yield {"type": "approval", "runId": run_id, "prompt": f"Allow the assistant to: {text}"}
            return
        if "service" in t:
            reply = biz.get("services", "")
        elif "book" in t:
            yield {"type": "progress", "label": "Checking the calendar…"}
            reply = biz.get("booking", "")
        elif "rate" in t or "price" in t or "cost" in t:
            reply = biz.get("rates", "")
        elif "area" in t or "cover" in t:
            reply = biz.get("areas", "")
        else:
            yield {"type": "progress", "label": "Thinking…"}
            reply = (f"(Mock agent) You said: \"{text}\". Connect Hermes Agent to get real answers — "
                     "see docs/HERMES_MAC_MINI_SETUP.md.")
        if learn:
            reply += "\n\nI've noted this so I can answer faster next time."
        async for e in self._words(reply or "I'm not sure yet — an admin can add this in Settings."):
            yield e

    async def approve(self, run_id: str, approve: bool) -> dict[str, Any]:
        task = self.pending.pop(run_id, None)
        if task is None:
            return {"status": "unknown", "message": "That request has expired."}
        return {"status": "approved" if approve else "denied",
                "message": f"Done (mock): {task}" if approve else "OK, I won't do that."}

    async def status(self) -> dict[str, Any]:
        return {"mode": "mock", "reachable": True, "models": ["mock-agent"], "authorized": True}

    async def skills(self) -> list[dict[str, Any]]:
        return [{"name": "answer-faq", "description": "Answers questions from the knowledge base"},
                {"name": "book-appointment", "description": "Finds a free slot and books it"}]


def make_agent(s: Settings) -> Agent:
    return HermesAgent(s) if s.hermes_mode == "hermes" else MockAgent(s)
