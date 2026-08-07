"""Attivazione e disattivazione del trading live."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from etoro_bot.api import dependencies as deps
from etoro_bot.api.dependencies import UserIdentity, current_user, require_owner

log = logging.getLogger("etoro_bot.api")

router = APIRouter()


class LiveBody(BaseModel):
    confirmation: bool | None = None



@router.post("/live/enable")
def live_enable(
    body: LiveBody, identity: UserIdentity = Depends(current_user)
) -> dict[str, Any]:
    """Accende il trading live (denaro REALE) col DNA del campione."""
    require_owner(identity, "attivare il trading live")
    from etoro_bot.services.app_settings import (
        SettingsValidationError,
        check_live_activation,
        set_arena_state,
    )

    if body.confirmation is not True:
        raise HTTPException(422, "l'attivazione del live richiede confirmation: true")
    try:
        check_live_activation(
            deps.get_repo(), etoro_configured=deps.user_keys(identity).etoro_configured
        )
    except SettingsValidationError as exc:
        raise HTTPException(422, exc.message) from exc
    state = set_arena_state(deps.get_repo(), live_enabled=True)
    deps.get_repo().add_arena_event("live_on", {"by": identity.user_id})
    return {"state": state}



@router.post("/live/disable")
def live_disable(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    from etoro_bot.services.app_settings import set_arena_state

    require_owner(identity, "spegnere il trading live")
    state = set_arena_state(deps.get_repo(), live_enabled=False)
    deps.get_repo().add_arena_event("live_off", {"by": identity.user_id})
    return {"state": state}
