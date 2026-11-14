"""Admin: Settings & keys (optional settings and API keys, stored encrypted on this server) and the admin password."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import hermes_config, options
from ..auth import ADMIN_HASH_KEY, check_admin_credentials, hash_password, require_admin

router = APIRouter(prefix="/api/admin")


class OptionsIn(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)     # name → new value
    remove: list[str] = Field(default_factory=list)           # names to clear (back to the server's value, if any)


class TestIn(BaseModel):
    test: str = Field(min_length=2, max_length=40)
    values: dict[str, str] = Field(default_factory=dict)     # typed but not saved yet


class PasswordIn(BaseModel):
    current: str = Field(min_length=1, max_length=200)
    new: str = Field(min_length=12, max_length=200)


@router.get("/options")
def options_list(request: Request, _=Depends(require_admin)):
    return options.listing(request.app, request.app.state.db)


@router.put("/options")
async def options_save(body: OptionsIn, request: Request, user: dict = Depends(require_admin)):
    app, db, s = request.app, request.app.state.db, request.app.state.settings
    known = {o["name"]: o for o in options.catalogue(app)}
    unknown = [n for n in [*body.values, *body.remove] if n not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Not a setting you can change here: {', '.join(unknown)}")
    try:
        clean = {n: options.check_value(n, v) for n, v in body.values.items()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    changes: dict[str, str | None] = {n: (v or None) for n, v in clean.items()}   # empty = clear
    changes.update({n: None for n in body.remove})
    if not changes:
        return {"restarting": False, **options.listing(app, db)}
    for n, v in changes.items():
        options.store(db, s.secret_key, n, v, user["username"])
    hermes = {n: v for n, v in changes.items() if known[n].get("hermes")}
    if hermes:
        options.write_hermes_env(s.hermes_home, hermes)
    # names only: values never go in the logs
    db.audit(user["username"], "settings.changed", ", ".join(sorted(changes)),
             set=sorted(n for n, v in changes.items() if v), cleared=sorted(n for n, v in changes.items() if not v))
    restart = s.options_restart
    if restart:
        if hermes and s.hermes_mode == "hermes":
            asyncio.get_running_loop().create_task(hermes_config.restart(s.hermes_restart_cmd))
        options.schedule_restart()
    return {"restarting": restart, **options.listing(app, db)}


@router.post("/options/test")
async def options_test(body: TestIn, request: Request, _=Depends(require_admin)):
    app = request.app
    names = [o["name"] for o in options.catalogue(app)]
    # what's typed in the form, else what's in use; plus the non-catalogue URL the GitHub test needs
    values = {n: (body.values.get(n) or options.effective(app, n)) for n in names}
    values.setdefault("GITHUB_API_URL", options.effective(app, "GITHUB_API_URL"))
    ok, message = await options.run_test(body.test, values, getattr(app.state, "options_transport", None))
    return {"ok": ok, "message": message}


@router.post("/password")
def change_password(body: PasswordIn, request: Request, user: dict = Depends(require_admin)):
    s, db = request.app.state.settings, request.app.state.db
    if not check_admin_credentials(user["username"], body.current, s, db):
        raise HTTPException(status_code=400, detail="The current password isn't right")
    if body.new == body.current:
        raise HTTPException(status_code=400, detail="Choose a new password")
    db.set_setting(ADMIN_HASH_KEY, hash_password(body.new))
    db.audit(user["username"], "admin.password_changed", "admin")
    return {"ok": True}
