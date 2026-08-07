"""Metriche di performance del conto simulato di un agente dell'arena.

Modulo puro: riceve trade chiusi, serie equity e saldi, restituisce un dict
pronto per l'API. Nessun accesso a DB o rete.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any, Sequence

from etoro_bot.arena.engine import position_direction

TRADING_DAYS = 252
MIN_TRADES_SAMPLE = 5
MIN_DAILY_POINTS = 3


def trade_direction(trade: Any) -> str:
    """Direzione di un trade/posizione dalla colonna persistita."""
    return position_direction(trade)


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _equity_series(equity_points: Sequence[Any]) -> list[float]:
    """Serie equity completa (anche intraday) ordinata per timestamp."""
    return [p.equity_usd for p in sorted(equity_points, key=lambda p: p.ts)]


def _daily_closes(equity_points: Sequence[Any]) -> list[float]:
    """Ultimo valore equity per ogni giorno di calendario (UTC), in ordine."""
    by_day: dict[Any, float] = {}
    for p in sorted(equity_points, key=lambda p: p.ts):
        by_day[p.ts.date()] = p.equity_usd
    return [by_day[d] for d in sorted(by_day)]


def _max_drawdown_pct(series: Sequence[float]) -> float | None:
    """Peggior scostamento % dal massimo corrente; None sotto i 2 punti."""
    if len(series) < 2:
        return None
    peak = series[0]
    worst = 0.0
    for value in series:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, (value - peak) / peak * 100.0)
    return worst


def _split_stats(trades: Sequence[Any]) -> dict[str, Any]:
    n = len(trades)
    wins = [t for t in trades if t.pnl_usd > 0]
    return {
        "n": n,
        "pnl_usd": round(sum(t.pnl_usd for t in trades), 2),
        "win_rate_pct": _round(100.0 * len(wins) / n) if n else None,
    }


def compute_agent_metrics(
    *,
    starting_capital_usd: float,
    cash_usd: float,
    invested_usd: float,
    born_at: datetime | None,
    trades: Sequence[Any],
    equity_points: Sequence[Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Riassume in un dict le metriche di un agente (money 2 dec., ore 1 dec.)."""
    now = now or datetime.now(timezone.utc)
    equity = cash_usd + invested_usd
    pnl = equity - starting_capital_usd

    wins = [t.pnl_usd for t in trades if t.pnl_usd > 0]
    losses = [t.pnl_usd for t in trades if t.pnl_usd < 0]
    pnls = [t.pnl_usd for t in trades]
    n = len(trades)
    gross_win = sum(wins)
    gross_loss = -sum(losses)

    holding_hours = [
        (t.closed_at - t.opened_at).total_seconds() / 3600.0 for t in trades
    ]
    age_days = max(1, (now - born_at).days) if born_at else 1

    # --- drawdown sulla serie completa (i minimi intraday contano) ----------
    max_dd = _max_drawdown_pct(_equity_series(equity_points))

    # --- serie giornaliera per volatilità/sharpe ----------------------------
    closes = _daily_closes(equity_points)
    returns = [
        closes[i] / closes[i - 1] - 1.0
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    ]
    volatility = sharpe = None
    if len(closes) >= MIN_DAILY_POINTS and len(returns) >= 2:
        std = statistics.stdev(returns)
        if std > 0:
            volatility = std * (TRADING_DAYS ** 0.5) * 100.0
            sharpe = statistics.mean(returns) / std * (TRADING_DAYS ** 0.5)

    longs = [t for t in trades if trade_direction(t) != "short"]
    shorts = [t for t in trades if trade_direction(t) == "short"]

    return {
        "pnl_usd": round(pnl, 2),
        "return_pct": _round(100.0 * pnl / starting_capital_usd)
        if starting_capital_usd > 0
        else None,
        "equity_usd": round(equity, 2),
        "exposure_pct": _round(100.0 * invested_usd / equity) if equity > 0 else 0.0,
        "cash_pct": _round(100.0 * cash_usd / equity) if equity > 0 else 0.0,
        "n_trades": n,
        "n_wins": len(wins),
        "n_losses": len(losses),
        "win_rate_pct": _round(100.0 * len(wins) / n) if n else None,
        "profit_factor": _round(gross_win / gross_loss) if gross_loss > 0 else None,
        "expectancy_usd": _round(statistics.mean(pnls)) if pnls else None,
        "avg_win_usd": _round(statistics.mean(wins)) if wins else None,
        "avg_loss_usd": _round(statistics.mean(losses)) if losses else None,
        "best_trade_usd": _round(max(pnls)) if pnls else None,
        "worst_trade_usd": _round(min(pnls)) if pnls else None,
        "max_drawdown_pct": _round(max_dd),
        "volatility_pct": _round(volatility),
        "sharpe": _round(sharpe),
        "avg_holding_hours": _round(statistics.mean(holding_hours), 1)
        if holding_hours
        else None,
        "trades_per_day": _round(n / age_days),
        "long": _split_stats(longs),
        "short": _split_stats(shorts),
        "insufficient_sample": n < MIN_TRADES_SAMPLE or len(closes) < MIN_DAILY_POINTS,
    }
