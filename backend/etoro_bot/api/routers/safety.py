"""Kill switch manuale."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends

from etoro_bot.api.dependencies import UserIdentity, current_user, require_owner
from etoro_bot.safety.kill_switch import (
    engage_kill_switch,
    kill_switch_active,
    release_kill_switch,
)

log = logging.getLogger("etoro_bot.api")

router = APIRouter()



@router.post("/kill-switch")
def activate_kill_switch(
    identity: UserIdentity = Depends(current_user),
) -> dict[str, Any]:
    require_owner(identity, "attivare il kill switch")
    engage_kill_switch("api")
    return {"kill_switch_active": True}



@router.delete("/kill-switch")
def deactivate_kill_switch(
    identity: UserIdentity = Depends(current_user),
) -> dict[str, Any]:
    """Rilascia il freno d'emergenza: mutazione critica, solo il proprietario."""
    require_owner(identity, "disattivare il kill switch")
    release_kill_switch()
    return {"kill_switch_active": kill_switch_active()}
