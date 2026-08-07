"""Modelli di dominio condivisi da arena, live engine, journal e API."""

from __future__ import annotations

import enum
import uuid

from pydantic import BaseModel


class Side(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


class ExecutionStatus(str, enum.Enum):
    # write-ahead: l'intento è a giornale PRIMA che l'ordine parta, così un
    # fill seguito da crash lascia comunque una traccia con il reference id.
    PENDING = "pending"
    FILLED = "filled"
    FAILED = "failed"
    SKIPPED = "skipped"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


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


# --- riconoscimento dei simboli nel testo -----------------------------------
# Un ticker esplicitamente citato: $AAPL | (AAPL) | NASDAQ: AAPL. Il bound è 6
# caratteri (alcuni ETF e classi di azioni ci arrivano): lo usano sia la
# discovery dell'universo sia l'estrazione ticker dalle news, che prima
# avevano due copie già divergenti ({1,5} contro {1,6}).
SYMBOL_PATTERN = r"[A-Z]{1,6}"

EXPLICIT_SYMBOL_PATTERNS: tuple[str, ...] = (
    rf"\$(?P<sym>{SYMBOL_PATTERN})\b",
    rf"\((?P<sym>{SYMBOL_PATTERN})\)",
    (
        rf"\b(?:NASDAQ|NYSE|AMEX|ARCA|BATS|TICKER|SYMBOL)\s*[:\-]\s*"
        rf"(?P<sym>{SYMBOL_PATTERN})\b"
    ),
)
