"""Giornale operativo: esecuzioni, trade aperti e storico."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from etoro_bot.api import dependencies as deps
from etoro_bot.api import schemas
from etoro_bot.api.dependencies import UserIdentity, current_user, require_owner
from etoro_bot.services import journal

log = logging.getLogger("etoro_bot.api")

router = APIRouter()



@router.get("/executions", response_model=schemas.ExecutionsResponse)
def list_executions(limit: int = Query(50, le=500)) -> dict[str, Any]:
    rows = deps.get_repo().list_executions(limit=limit)
    return {
        "executions": [
            {
                "id": str(e.id),
                "run_id": e.run_id,
                "symbol": e.symbol,
                "side": e.side,
                "amount_usd": e.amount_usd,
                "status": e.status,
                "detail": e.detail,
                "execution_price": e.execution_price,
                "etoro_position_id": e.etoro_position_id,
                "created_at": e.created_at.isoformat(),
            }
            for e in rows
        ]
    }






class CloseTradeBody(BaseModel):
    confirmation: str



@router.post("/trades/{position_id}/close")
def close_trade(
    position_id: int,
    body: CloseTradeBody,
    identity: UserIdentity = Depends(current_user),
) -> dict[str, Any]:
    if body.confirmation != "CHIUDI":
        raise HTTPException(422, "Conferma non valida: digita CHIUDI")
    require_owner(identity, "chiudere una posizione reale")
    repo = deps.get_repo()
    position = repo.get_open_position(position_id)
    if position is None:
        raise HTTPException(404, "Posizione aperta non trovata")
    from etoro_bot.arena.live import record_close

    client = deps.make_client(identity)
    try:
        client.close_position(position_id, position.instrument_id)
    except Exception as exc:
        # broker irraggiungibile o ordine rifiutato: è un guasto a monte, non
        # un errore del backend (stesso trattamento di /portfolio).
        log.warning("chiusura eToro della posizione %s fallita", position_id, exc_info=True)
        raise HTTPException(502, f"Chiusura eToro non riuscita: {exc}") from exc
    close_price = None
    pnl = None
    try:
        # finestra corta: la chiusura è di adesso, e senza minDate l'API
        # pagina l'intero storico del conto
        recent = datetime.now(UTC) - timedelta(days=3)
        for item in client.get_trade_history(min_date=recent):
            if int(item.get("positionId") or -1) == position_id:
                close_price = item.get("closeRate")
                pnl = item.get("netProfit")
                break
    except Exception:
        log.warning("chiusura %s eseguita, dettaglio PnL non ancora disponibile", position_id)
    written = record_close(
        repo,
        position_id,
        close_price=float(close_price) if close_price is not None else None,
        realized_pnl_usd=float(pnl) if pnl is not None else None,
        close_reason="manual_close",
        # PnL non ancora pubblicato dal broker: la posizione resta in attesa e
        # la liquidazione del prossimo ciclo live la completerà
        pnl_settled=pnl is not None,
    )
    # La posizione al broker è chiusa: un 500 qui inviterebbe l'utente a
    # ripetere l'ordine. Il reconcile del prossimo ciclo la chiude a registro.
    return {
        "status": "closed",
        "position_id": position_id,
        "registry": "closed" if written else "pending_reconcile",
    }



@router.post("/executions/{execution_id}/cancel")
def cancel_execution(
    execution_id: uuid.UUID, identity: UserIdentity = Depends(current_user)
) -> dict[str, Any]:
    require_owner(identity, "annullare un ordine")
    if not deps.get_repo().cancel_pending_execution(execution_id):
        raise HTTPException(409, "L'ordine non è annullabile: è già terminale o inesistente")
    return {"status": "cancelled", "execution_id": str(execution_id)}


@router.get("/trades", response_model=schemas.TradesResponse)
def trades(
    statuses: str | None = None,
    symbol: str | None = None,
    identity: UserIdentity = Depends(current_user),
) -> dict[str, Any]:
    return {"trades": journal.trade_rows(deps.get_repo(), statuses, symbol)}


@router.get("/trade-history", response_model=schemas.TradeHistoryResponse)
def trade_history(
    statuses: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    symbol: str | None = None,
    identity: UserIdentity = Depends(current_user),
) -> dict[str, Any]:
    return {
        "items": journal.history_items(
            deps.get_repo(), statuses, date_from, date_to, symbol
        )
    }
