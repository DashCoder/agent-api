"""Model & tools: which model each agent uses, and Hermes' own model. Save makes a choice the default."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import hermes_config, llm
from ..auth import require_admin
from ..deps import get_agent

router = APIRouter(prefix="/api/admin/models", dependencies=[Depends(require_admin)])


def _hermes(request: Request) -> dict[str, Any]:
    s = request.app.state.settings
    cur = hermes_config.read(s.hermes_home)
    return {"current": cur, "kinds": hermes_config.KINDS, "mode": s.hermes_mode,
            "file": str(hermes_config.config_file(s.hermes_home)),
            "label": f"{cur['model']} ({hermes_config.KINDS.get(cur['kind'], cur.get('provider') or 'unknown')})" if cur and cur.get("model") else None}


@router.get("")
async def overview(request: Request):
    app = request.app
    agents = []
    for a in llm._agents(app).values():
        c = llm.choice(app, a["id"])
        agents.append({**a, "current": c, "label": llm.label(c)})
    return {"agents": agents, "providers": await llm.providers_status(app),
            "providerNames": llm.PROVIDERS, "suggestions": llm.SUGGESTIONS, "hermes": _hermes(request)}


class ChoiceIn(BaseModel):
    provider: str
    model: str = Field(min_length=1, max_length=160)


@router.put("/{agent}")
async def save(agent: str, body: ChoiceIn, request: Request, user: dict = Depends(require_admin)):
    if agent == "hermes":
        return await save_hermes(body, request, user)
    c = llm.set_choice(request.app, agent, body.provider, body.model, user["username"])
    return {"current": c, "label": llm.label(c)}


@router.post("/{agent}/test")
async def test(agent: str, request: Request):
    """Ask the agent's chosen model a tiny question: does it answer, and how fast?"""
    if agent not in llm._agents(request.app):
        raise HTTPException(status_code=404, detail=f"{agent} has no model to choose")
    import time
    t = time.monotonic()
    try:
        out = await llm.complete(request.app, agent, "You answer in one word.", "Reply with the single word OK.",
                                 max_tokens=20, task="test")
    except llm.LlmError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "reply": out[:100], "ms": round((time.monotonic() - t) * 1000)}


async def save_hermes(body: ChoiceIn, request: Request, user: dict) -> dict[str, Any]:
    s = request.app.state.settings
    db = request.app.state.db
    before = hermes_config.read(s.hermes_home)
    backup = hermes_config.write(s.hermes_home, body.provider, body.model, s.ollama_url)
    steps = [f"Saved {body.model} ({hermes_config.KINDS[body.provider]}) in {hermes_config.config_file(s.hermes_home)}"
             + (f"; backup {backup.name}" if backup else "")]
    if s.hermes_mode != "hermes":
        db.audit(user["username"], "models.hermes_set", body.model, provider=body.provider, restarted=False)
        return {"ok": True, "steps": steps + ["Hermes isn't connected (mock mode): not restarted"], "hermes": _hermes(request)}
    ok, out = await hermes_config.restart(s.hermes_restart_cmd)
    steps.append("Restarted Hermes" if ok else f"Restart didn't work: {out}")
    answered, reply = await hermes_config.probe(get_agent(request)) if ok else (False, out)
    if answered:
        steps.append(f"Hermes answered with {body.model}: “{reply}”")
        db.audit(user["username"], "models.hermes_set", body.model, provider=body.provider,
                 before=(before or {}).get("model", ""), restarted=True)
        return {"ok": True, "steps": steps, "hermes": _hermes(request)}
    # put it back
    if backup:
        backup.replace(hermes_config.config_file(s.hermes_home))
        await hermes_config.restart(s.hermes_restart_cmd)
        prev = f" ({before.get('model')})" if before else ""
        steps.append(f"Hermes didn't answer ({reply}). Put the previous model back{prev} and restarted Hermes.")
    db.audit(user["username"], "models.hermes_failed", body.model, provider=body.provider, error=reply[:300])
    return {"ok": False, "steps": steps, "error": reply, "hermes": _hermes(request)}
