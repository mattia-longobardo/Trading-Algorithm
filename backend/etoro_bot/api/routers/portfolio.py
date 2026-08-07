"""Portafoglio: solo le posizioni aperte dal bot (§7)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from etoro_bot.api import dependencies as deps
from etoro_bot.api import schemas
from etoro_bot.api.dependencies import UserIdentity, current_user
from etoro_bot.services.portfolio import portfolio_snapshot

log = logging.getLogger("etoro_bot.api")

router = APIRouter()


@router.get("/portfolio", response_model=schemas.PortfolioResponse)
def portfolio(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    try:
        return portfolio_snapshot(deps.get_repo(), deps.make_client(identity))
    except HTTPException:
        raise
    except Exception as exc:
        # broker irraggiungibile o senza capitale pubblicato: è un guasto a
        # monte, non un errore del backend.
        log.warning("portafoglio eToro non disponibile", exc_info=True)
        raise HTTPException(502, f"Capitale eToro non disponibile: {exc}") from exc
