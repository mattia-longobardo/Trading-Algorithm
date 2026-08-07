"""Contratto delle risposte API che portano denaro.

Esistono per una ragione sola: `frontend/lib/types.ts` è scritto a mano e
`api.get<T>()` è solo un cast, quindi finora backend e frontend potevano
divergere in silenzio. Con `response_model` la forma è dichiarata, finisce in
OpenAPI e un campo che sparisce si vede subito.

I modelli rispecchiano ESATTAMENTE i payload già serviti: nessun campo tolto,
nessuno rinominato. I timestamp restano stringhe ISO come li serializzano le
route (`.isoformat()`), non `datetime`, per non cambiare il formato sul filo.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

# --- executions -------------------------------------------------------------


class ExecutionRow(BaseModel):
    id: str
    run_id: str
    symbol: str
    side: str
    amount_usd: float
    status: str
    detail: str | None
    execution_price: float | None
    etoro_position_id: int | None
    created_at: str


class ExecutionsResponse(BaseModel):
    executions: list[ExecutionRow]


# --- portfolio --------------------------------------------------------------


class PortfolioPosition(BaseModel):
    etoro_position_id: int
    symbol: str
    instrument_id: int
    amount_usd: float
    direction: str
    entry_price: float
    current_price: float | None
    unrealized_pnl_usd: float | None
    unrealized_pnl_pct: float | None
    sector: str | None
    opened_at: str


class PortfolioAnomaly(BaseModel):
    """Oggi la lista è sempre vuota, ma il tipo è quello che il frontend
    dichiara: se un giorno la si popola, la forma è già concordata."""

    symbol: str
    detail: str
    detected_at: str


class PortfolioResponse(BaseModel):
    positions: list[PortfolioPosition]
    cash_usd: float
    equity_usd: float
    exposure_usd: float
    max_trade_amount_usd: float
    capital_source: str
    anomalies: list[PortfolioAnomaly]


# --- giornale operazioni ----------------------------------------------------


class TradeRow(BaseModel):
    id: str
    position_id: int | None
    execution_id: str | None
    symbol: str
    side: str
    status: str
    amount_usd: float
    entry_price: float | None
    current_price: float | None
    pnl_usd: float | None
    created_at: str
    detail: str | None
    can_close: bool
    can_cancel: bool


class TradesResponse(BaseModel):
    trades: list[TradeRow]


class TradeHistoryItem(BaseModel):
    id: str
    symbol: str
    side: str
    status: str
    amount_usd: float
    price: float | None
    pnl_usd: float | None
    opened_at: str
    closed_at: str | None
    detail: str | None


class TradeHistoryResponse(BaseModel):
    items: list[TradeHistoryItem]


# --- backtest ---------------------------------------------------------------


class BacktestMetrics(BaseModel):
    total_return_pct: float | None
    cagr_pct: float | None
    volatility_pct: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown_pct: float | None
    calmar: float | None
    alpha: float | None
    beta: float | None
    information_ratio: float | None
    win_rate_pct: float | None
    profit_factor: float | None
    recovery_factor: float | None
    expectancy_usd: float | None
    exposure_pct: float | None
    max_win_usd: float | None
    max_loss_usd: float | None
    std_win_usd: float | None
    std_loss_usd: float | None


class BacktestSummaryResponse(BaseModel):
    metrics: BacktestMetrics
    n_closed_trades: int
    n_days: int
    insufficient_sample: bool
    annualization_available: bool
    risk_free_rate_pct: float | None


class EquityCurvePoint(BaseModel):
    date: str
    equity_usd: float
    spy_lump_sum_usd: float | None
    spy_cash_flow_matched_usd: float | None


class EquityCurveResponse(BaseModel):
    points: list[EquityCurvePoint]
    note_dividends: str


class BacktestTrade(BaseModel):
    etoro_position_id: int
    symbol: str
    amount_usd: float
    entry_price: float
    close_price: float | None
    opened_at: str
    closed_at: str | None
    realized_pnl_usd: float | None
    close_reason: str | None
    sector: str | None


class BacktestTradesResponse(BaseModel):
    trades: list[BacktestTrade]


class MonthlyReturnsRow(BaseModel):
    year: int
    months: list[float | None]


class MonthlyReturnsResponse(BaseModel):
    rows: list[MonthlyReturnsRow]


# --- arena ------------------------------------------------------------------


class ArenaOpenPosition(BaseModel):
    id: str
    symbol: str
    amount_usd: float
    entry_price: float
    opened_at: str
    open_reason: str | None
    direction: str


class ArenaAgent(BaseModel):
    id: str
    name: str
    generation: int
    status: str
    is_champion: bool
    parent_id: str | None
    born_at: str | None
    died_at: str | None
    death_reason: str | None
    month: str
    dna: dict[str, Any]
    starting_capital_usd: float
    cash_usd: float
    invested_usd: float
    equity_usd: float
    pnl_month_usd: float
    open_positions: list[ArenaOpenPosition]
    # assente nella lineage (`with_memory=False`): le route la servono con
    # `response_model_exclude_unset` così la chiave non compare, come prima.
    memory: str | None = None


class ArenaSession(BaseModel):
    name: str
    open_utc: str
    close_utc: str
    open_now: bool


class ArenaOverviewResponse(BaseModel):
    state: dict[str, Any]
    market_open: bool
    sessions: list[ArenaSession]
    next_cycle_at: str | None
    days_to_evaluation: int
    generation: int
    agents: list[ArenaAgent]
    champion: ArenaAgent | None
    lineage: list[ArenaAgent]


class SimEquityPoint(BaseModel):
    ts: str
    equity_usd: float


class SimTrade(BaseModel):
    id: str
    symbol: str
    amount_usd: float
    entry_price: float
    close_price: float
    pnl_usd: float
    opened_at: str
    closed_at: str
    open_reason: str | None
    close_reason: str | None
    direction: str
    holding_hours: float
    return_pct: float | None


class ArenaAgentDetailResponse(BaseModel):
    agent: ArenaAgent
    equity: list[SimEquityPoint]
    trades: list[SimTrade]
    # dict di metriche calcolate (arena/metrics.py): tutte float | None, chiavi
    # in evoluzione — tiparle una a una qui le congelerebbe senza guadagno.
    metrics: dict[str, Any]


class ArenaEvent(BaseModel):
    id: str
    ts: str
    event: str
    payload: dict[str, Any]


class ArenaEventsResponse(BaseModel):
    events: list[ArenaEvent]
