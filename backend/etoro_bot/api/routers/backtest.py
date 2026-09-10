"""Track record dei trade reali (§11.1)."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from etoro_bot.api import dependencies as deps
from etoro_bot.api import schemas
from etoro_bot.api.dependencies import UserIdentity, current_user, require_owner

log = logging.getLogger("etoro_bot.api")

router = APIRouter()


def _backtest_service(identity: UserIdentity):
    from etoro_bot.services.backtest import BacktestService

    def price_fetcher(symbol: str, start_date):
        from datetime import date as _date

        client = deps.make_client(identity)
        found = client.search_instruments(
            {"internalSymbolFull": symbol},
            fields=["instrumentId", "internalSymbolFull"],
        )
        if not found:
            return {}
        iid = int(found[0]["instrumentId"])
        days = max((datetime.now(UTC).date() - start_date).days + 5, 30)
        candles = client.get_candles(iid, interval="OneDay", count=min(days, 1000))
        out: dict[_date, float] = {}
        for c in candles:
            day = datetime.fromisoformat(c["fromDate"].replace("Z", "+00:00")).date()
            out[day] = float(c["close"])
        return out

    return BacktestService(deps.get_repo(), price_fetcher, settings=deps.full_settings())


def _pct(value: float | None) -> float | None:
    return None if value is None else value * 100.0



@router.get("/backtest/summary", response_model=schemas.BacktestSummaryResponse)
def backtest_summary(
    date_from: date | None = None,
    date_to: date | None = None,
    identity: UserIdentity = Depends(current_user),
) -> dict[str, Any]:
    s = _backtest_service(identity).summary(date_from=date_from, date_to=date_to)
    return {
        "metrics": {
            "total_return_pct": _pct(s["total_return"]),
            "cagr_pct": _pct(s["cagr"]),
            "volatility_pct": _pct(s["annualized_volatility"]),
            "sharpe": s["sharpe"],
            "sortino": s["sortino"],
            "max_drawdown_pct": _pct(s["max_drawdown"]),
            "calmar": s["calmar"],
            "alpha": s["alpha"],
            "beta": s["beta"],
            "information_ratio": s["information_ratio"],
            "win_rate_pct": _pct(s["win_rate"]),
            "profit_factor": s["profit_factor"],
            "recovery_factor": s["recovery_factor"],
            "expectancy_usd": s["expectancy"],
            "exposure_pct": s["exposure_pct"],
            "max_win_usd": s["max_win_usd"],
            "max_loss_usd": s["max_loss_usd"],
            "std_win_usd": s["std_win_usd"],
            "std_loss_usd": s["std_loss_usd"],
        },
        "n_closed_trades": s["n_closed_trades"],
        "n_days": s["n_days"],
        "insufficient_sample": s["insufficient_sample"],
        "annualization_available": s["annualization_available"],
        "risk_free_rate_pct": s["risk_free_rate_pct"],
    }


SPY_DIVIDEND_NOTE = (
    "Il prezzo SPY non include i dividendi (~1.3-1.5%/anno di total return in più): "
    "il confronto sottostima leggermente il benchmark."
)



@router.get("/backtest/equity-curve", response_model=schemas.EquityCurveResponse)
def backtest_equity_curve(
    benchmark: str = "spy",
    date_from: date | None = None,
    date_to: date | None = None,
    identity: UserIdentity = Depends(current_user),
) -> dict[str, Any]:
    return {
        "points": _backtest_service(identity).equity_curve(
            benchmark=benchmark, date_from=date_from, date_to=date_to
        ),
        "note_dividends": SPY_DIVIDEND_NOTE,
    }



@router.get("/backtest/trades", response_model=schemas.BacktestTradesResponse)
def backtest_trades(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    return {"trades": _backtest_service(identity).trades()}



@router.get("/backtest/monthly-returns", response_model=schemas.MonthlyReturnsResponse)
def backtest_monthly_returns(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    return {"rows": _backtest_service(identity).monthly_returns()}


class ReplayRequest(BaseModel):
    dna: dict[str, Any] | None = None   # None = DNA del campione corrente
    symbols: list[str] | None = None    # None = watchlist (troncata a 15)
    days: int = 60
    starting_capital_usd: float = 10_000.0


@router.post("/backtest/replay")
def backtest_replay(
    payload: ReplayRequest | None = None,
    identity: UserIdentity = Depends(current_user),
) -> dict[str, Any]:
    """Replay storico di un DNA con lo stesso engine dell'arena (fase 1.2).

    Sincrono e potenzialmente lento (una chiamata LLM per bar): pensato per
    valutazioni una tantum dalla UI, non per polling.
    """
    from etoro_bot.arena.replay import run_replay
    from etoro_bot.services.deps import build_arena_deps

    require_owner(identity, "lanciare un replay storico")
    payload = payload or ReplayRequest()
    repo = deps.get_repo()
    arena_deps = build_arena_deps(repo)
    dna = payload.dna
    if dna is None:
        champion = repo.champion()
        if champion is None:
            return {"error": "nessun campione e nessun dna fornito"}
        dna = champion.dna
    symbols = payload.symbols or [
        str(s).upper() for s in (arena_deps.settings.get("watchlist") or [])
    ][:15]
    return run_replay(
        arena_deps, dna, symbols,
        days=max(30, min(payload.days, 250)),
        starting_capital_usd=payload.starting_capital_usd,
    )
