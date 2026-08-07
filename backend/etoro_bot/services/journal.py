"""Giornale operativo: le due viste sui trade che l'API serve.

`/trades` (operativo: cosa posso chiudere o annullare adesso) e `/trade-history`
(archivio) leggono le stesse tre sorgenti — posizioni aperte, posizioni chiuse,
esecuzioni — e le fondono. Prima il codice era duplicato quasi verbatim nelle
due route, filtri compresi: qui sta una volta sola.

I payload restituiti sono quelli storici, campo per campo.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


def position_side(position, closing: bool = False) -> str:
    """Lato dell'ordine di una posizione del registry: aprire un long è un buy
    e chiuderlo un sell; sullo short i due lati si invertono."""
    short = (position.direction or "long") == "short"
    return "buy" if short == closing else "sell"


def parse_statuses(statuses: str | None) -> set[str]:
    """Filtro stati dalla query string: "open,failed" → {"open", "failed"}."""
    return {value.strip() for value in (statuses or "").split(",") if value.strip()}


def _selected(rows: list[dict[str, Any]], statuses: set[str], symbol: str | None):
    if statuses:
        rows = [row for row in rows if row["status"] in statuses]
    if symbol:
        needle = symbol.strip().upper()
        rows = [row for row in rows if needle in row["symbol"].upper()]
    return rows


def trade_rows(
    repo, statuses: str | None = None, symbol: str | None = None
) -> list[dict[str, Any]]:
    """Vista operativa: posizioni aperte + esecuzioni non ancora diventate
    posizioni, con i flag delle azioni possibili."""
    rows: list[dict[str, Any]] = []
    for position in repo.open_positions():
        rows.append(
            {
                "id": f"position:{position.etoro_position_id}",
                "position_id": position.etoro_position_id,
                "execution_id": None,
                "symbol": position.symbol,
                "side": position_side(position),
                "status": "open",
                "amount_usd": position.amount_usd,
                "entry_price": position.entry_price,
                "current_price": None,
                "pnl_usd": None,
                "created_at": position.opened_at.isoformat(),
                "detail": "Posizione aperta",
                "can_close": True,
                "can_cancel": False,
            }
        )
    for execution in repo.list_executions(limit=500):
        if execution.status == "filled" and execution.etoro_position_id:
            continue
        rows.append(
            {
                "id": f"execution:{execution.id}",
                "position_id": execution.etoro_position_id,
                "execution_id": str(execution.id),
                "symbol": execution.symbol,
                "side": execution.side,
                "status": execution.status,
                "amount_usd": execution.amount_usd,
                "entry_price": execution.execution_price,
                "current_price": None,
                "pnl_usd": None,
                "created_at": execution.created_at.isoformat(),
                "detail": execution.detail,
                "can_close": False,
                "can_cancel": execution.status == "pending",
            }
        )
    rows = _selected(rows, parse_statuses(statuses), symbol)
    rows.sort(key=lambda row: row["created_at"], reverse=True)
    return rows


def history_items(
    repo,
    statuses: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    """Archivio: aperte, chiuse ed esecuzioni non concluse, in ordine inverso."""
    items: list[dict[str, Any]] = []
    for position in repo.open_positions():
        items.append(
            {
                "id": f"position:{position.etoro_position_id}",
                "symbol": position.symbol,
                "side": position_side(position),
                "status": "open",
                "amount_usd": position.amount_usd,
                "price": position.entry_price,
                "pnl_usd": None,
                "opened_at": position.opened_at.isoformat(),
                "closed_at": None,
                "detail": "Posizione aperta",
            }
        )
    for position in repo.closed_positions():
        items.append(
            {
                "id": f"position:{position.etoro_position_id}",
                "symbol": position.symbol,
                "side": position_side(position, closing=True),
                "status": "closed",
                "amount_usd": position.amount_usd,
                "price": position.close_price,
                "pnl_usd": position.realized_pnl_usd,
                "opened_at": position.opened_at.isoformat(),
                "closed_at": position.closed_at.isoformat() if position.closed_at else None,
                "detail": position.close_reason,
            }
        )
    for execution in repo.list_executions(limit=500):
        if execution.status == "filled":
            continue
        items.append(
            {
                "id": f"execution:{execution.id}",
                "symbol": execution.symbol,
                "side": execution.side,
                "status": execution.status,
                "amount_usd": execution.amount_usd,
                "price": execution.execution_price,
                "pnl_usd": None,
                "opened_at": execution.created_at.isoformat(),
                "closed_at": execution.created_at.isoformat(),
                "detail": execution.detail,
            }
        )
    items = _selected(items, parse_statuses(statuses), symbol)
    if date_from or date_to:
        def in_range(item: dict[str, Any]) -> bool:
            raw = item["closed_at"] or item["opened_at"]
            day = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
            return (date_from is None or day >= date_from) and (
                date_to is None or day <= date_to
            )
        items = [item for item in items if in_range(item)]
    items.sort(key=lambda row: row["closed_at"] or row["opened_at"], reverse=True)
    return items
