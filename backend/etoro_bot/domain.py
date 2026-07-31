"""Modelli di dominio condivisi da arena, live engine, journal e API."""

from __future__ import annotations

import enum
import uuid

from pydantic import BaseModel


class Side(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


class ExecutionStatus(str, enum.Enum):
    FILLED = "filled"
    FAILED = "failed"
    SKIPPED = "skipped"
    REJECTED = "rejected"


class DecisionStage(str, enum.Enum):
    TRADER = "trader"


class ExecutionResult(BaseModel):
    symbol: str
    side: Side
    amount_usd: float
    status: ExecutionStatus
    detail: str = ""
    execution_price: float | None = None
    etoro_position_id: int | None = None


def order_request_id(run_id: str, symbol: str, side: Side) -> str:
    """UUID5 deterministico: un ciclo ritentato dopo un crash non duplica ordini."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"etoro-bot:{run_id}:{symbol}:{side.value}"))
