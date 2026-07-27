"""Trading live: il DNA del campione (vincitore dell'ultimo mese) muove denaro
REALE sul conto eToro, con la stessa logica di ciclo dell'arena.

Unici freni operativi, non "di analisi": kill switch (blocca tutto) e circuit
breaker (blocca solo le aperture dopo perdite anomale, mai le chiusure).
Idempotenza aperture: request_id = UUID5 di (run_id, symbol, buy, ciclo).
Day trading anche qui: a fine giornata ogni posizione viene chiusa.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from etoro_bot.arena.dna import clamp_dna
from etoro_bot.arena.engine import ArenaDeps
from etoro_bot.arena.trader import build_prompt, decide, enforce
from etoro_bot.domain import (
    ExecutionResult,
    ExecutionStatus,
    Side,
    order_request_id,
)
from etoro_bot.safety.kill_switch import kill_switch_active

logger = logging.getLogger(__name__)

LIVE_ENV = "live"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_run(deps: ArenaDeps, now: datetime) -> str:
    run_id = f"live-{now:%Y%m%d}"
    if deps.repo.get_run(run_id) is None:
        deps.repo.create_run(run_id, environment=LIVE_ENV)
    return run_id


def _live_cash(deps: ArenaDeps) -> float:
    portfolio = deps.client.get_portfolio()
    return float(portfolio.get("credit") or 0.0)


def _close_real_position(
    deps: ArenaDeps, breaker, run_id: str, pos, price: float | None, reason: str,
    equity: float,
) -> None:
    deps.client.close_position(pos.etoro_position_id, pos.instrument_id)
    pnl = close_price = None
    try:
        recent = _utcnow() - timedelta(days=3)
        for trade in deps.client.get_trade_history(min_date=recent):
            if trade.get("positionId") == pos.etoro_position_id:
                pnl = trade.get("netProfit")
                close_price = trade.get("closeRate")
                break
    except Exception as exc:
        logger.warning("live: PnL non recuperabile per %s: %s", pos.etoro_position_id, exc)
    if close_price is None:
        close_price = price
    deps.repo.close_position(
        pos.etoro_position_id, close_price=close_price,
        realized_pnl_usd=pnl, close_reason=reason,
    )
    if pnl is not None and breaker is not None:
        breaker.record_closed_trade(float(pnl), equity)
    deps.repo.add_execution(
        run_id,
        ExecutionResult(
            symbol=pos.symbol, side=Side.SELL, amount_usd=pos.amount_usd,
            status=ExecutionStatus.FILLED, detail=reason,
            execution_price=close_price, etoro_position_id=pos.etoro_position_id,
        ),
    )


def _open_real_position(
    deps: ArenaDeps, run_id: str, order: dict[str, Any], slot: str, now: datetime
) -> None:
    request_id = order_request_id(run_id, f"{order['symbol']}#{slot}", Side.BUY)
    fill = deps.client.open_position(
        order["instrument_id"], order["amount_usd"], request_id
    )
    position_id = fill.get("position_id")
    if position_id is not None:
        deps.repo.register_open_position(
            etoro_position_id=int(position_id),
            run_id=run_id,
            symbol=order["symbol"],
            instrument_id=order["instrument_id"],
            amount_usd=order["amount_usd"],
            entry_price=float(fill.get("execution_price") or 0.0),
            opened_at=now,
        )
    deps.repo.add_execution(
        run_id,
        ExecutionResult(
            symbol=order["symbol"], side=Side.BUY, amount_usd=order["amount_usd"],
            status=ExecutionStatus.FILLED, detail=order["reason"] or "apertura live",
            execution_price=fill.get("execution_price"),
            etoro_position_id=int(position_id) if position_id is not None else None,
        ),
    )


# Mai due cicli live sovrapposti: ordini reali, niente doppioni.
_live_lock = threading.Lock()


def run_live_cycle(
    deps: ArenaDeps,
    breaker=None,
    market: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Un ciclo live guidato dal campione. Ritorna un summary sintetico."""
    if not _live_lock.acquire(blocking=False):
        return {"skipped": "cycle_in_progress"}
    try:
        return _run_live_cycle_locked(deps, breaker, market, now)
    finally:
        _live_lock.release()


def _run_live_cycle_locked(
    deps: ArenaDeps,
    breaker,
    market: dict[str, dict[str, Any]] | None,
    now: datetime | None,
) -> dict[str, Any]:
    now = now or _utcnow()
    if kill_switch_active():
        return {"skipped": "kill_switch"}
    champion = deps.repo.champion()
    if champion is None:
        return {"skipped": "no_champion"}
    if deps.llm is None:
        return {"skipped": "no_llm"}
    if market is None:
        from etoro_bot.arena.market import build_snapshot

        market = build_snapshot(deps.client, deps.settings)
    prices = {s: float(r["price"]) for s, r in market.items() if r.get("price")}
    dna = clamp_dna(champion.dna)
    run_id = _ensure_run(deps, now)

    positions = deps.repo.open_positions()
    cash = _live_cash(deps)
    equity = cash + sum(p.amount_usd for p in positions)

    # stop loss / take profit automatici sul reale
    for pos in list(positions):
        price = prices.get(pos.symbol)
        if not price or not pos.entry_price:
            continue
        change_pct = (price / pos.entry_price - 1.0) * 100.0
        if change_pct <= -float(dna["stop_loss_pct"]):
            _close_real_position(deps, breaker, run_id, pos, price,
                                 f"stop loss automatico ({change_pct:+.2f}%)", equity)
        elif change_pct >= float(dna["take_profit_pct"]):
            _close_real_position(deps, breaker, run_id, pos, price,
                                 f"take profit automatico ({change_pct:+.2f}%)", equity)

    positions = deps.repo.open_positions()
    cash = _live_cash(deps)
    equity = cash + sum(p.amount_usd for p in positions)
    prompt = build_prompt(
        name=f"{champion.name} (CAMPIONE LIVE)",
        dna=dna,
        memory=champion.memory,
        survival=(
            "Operi con denaro REALE: sei la versione vincente dell'ultimo mese. "
            "Proteggi il capitale come proteggeresti la tua vita."
        ),
        cash=cash,
        equity=equity,
        positions_view=[
            f"- {p.symbol}: {p.amount_usd:.2f} USD @ {p.entry_price:.2f}" for p in positions
        ],
        market_view=[str(r.get("view", s)) for s, r in market.items()],
    )
    actions = decide(deps.llm, model=deps.model, max_tokens=deps.max_tokens, prompt=prompt)
    opens, closes = enforce(
        actions, dna=dna, cash=cash, equity=equity,
        held_symbols={p.symbol for p in positions}, market=market,
    )

    executed = {"opened": 0, "closed": 0, "blocked": 0}
    by_symbol = {p.symbol: p for p in positions}
    for close in closes:
        pos = by_symbol.get(close["symbol"])
        if pos is None:
            continue
        try:
            _close_real_position(deps, breaker, run_id, pos, prices.get(pos.symbol),
                                 close["reason"] or "chiusura live", equity)
            executed["closed"] += 1
        except Exception as exc:
            logger.warning("live: chiusura %s fallita: %s", close["symbol"], exc)

    blocks_openings = breaker is not None and breaker.blocks_openings()
    slot = f"{now:%Y%m%d%H%M}"
    for order in opens:
        if kill_switch_active() or blocks_openings:
            executed["blocked"] += 1
            continue
        try:
            _open_real_position(deps, run_id, order, slot, now)
            executed["opened"] += 1
        except Exception as exc:
            logger.warning("live: apertura %s fallita: %s", order["symbol"], exc)
            deps.repo.add_execution(
                run_id,
                ExecutionResult(
                    symbol=order["symbol"], side=Side.BUY,
                    amount_usd=order["amount_usd"],
                    status=ExecutionStatus.FAILED, detail=str(exc),
                ),
            )
    for item in closes + [dict(o, action="open") for o in opens]:
        deps.repo.add_decision(run_id, item.get("symbol", "?"), "trader",
                               {k: v for k, v in item.items() if k != "instrument_id"})
    deps.repo.finish_run(run_id, {"cycle": slot, **executed})
    return executed


def close_live_market_positions(
    deps: ArenaDeps,
    market_name: str,
    breaker=None,
    market: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fine sessione live: chiude le posizioni reali di QUEL mercato che hanno
    raggiunto il holding massimo del DNA del campione (1 = day trading)."""
    from etoro_bot.arena.dna import clamp_dna
    from etoro_bot.arena.engine import trading_days_held
    from etoro_bot.arena.market import build_snapshot, market_of_symbol

    now = now or _utcnow()
    if kill_switch_active():
        return {"skipped": "kill_switch"}
    champion = deps.repo.champion()
    max_days = int(clamp_dna(champion.dna if champion else None)["max_holding_days"])
    expired = [
        p
        for p in deps.repo.open_positions()
        if market_of_symbol(p.symbol) == market_name
        and trading_days_held(p.opened_at, now) >= max_days
    ]
    if not expired:
        return {"closed": 0, "market": market_name}
    if market is None:
        market = build_snapshot(deps.client, deps.settings, now=now, only_open=False)
    prices = {s: float(r["price"]) for s, r in market.items() if r.get("price")}
    run_id = _ensure_run(deps, now)
    try:
        cash = _live_cash(deps)
    except Exception:
        cash = 0.0
    equity = cash + sum(p.amount_usd for p in deps.repo.open_positions())
    closed = 0
    for pos in expired:
        try:
            held = trading_days_held(pos.opened_at, now)
            _close_real_position(
                deps, breaker, run_id, pos, prices.get(pos.symbol),
                f"fine sessione {market_name}: holding massimo raggiunto "
                f"({held}/{max_days} giorni)", equity,
            )
            closed += 1
        except Exception as exc:
            logger.warning("live: chiusura sessione %s fallita: %s", pos.symbol, exc)
    return {"closed": closed, "market": market_name}


def run_live_eod(deps: ArenaDeps, breaker=None, now: datetime | None = None) -> dict[str, Any]:
    """Ultima campanella live: fotografa l'equity (cash + posizioni overnight).

    Le chiusure per scadenza holding avvengono ai singoli fine-sessione.
    """
    now = now or _utcnow()
    positions = deps.repo.open_positions()
    if not positions and deps.repo.champion() is None:
        return {"snapshot": False}
    try:
        cash = _live_cash(deps)
        exposure = sum(p.amount_usd for p in positions)
        deps.repo.record_equity_snapshot(now.date(), cash + exposure, cash, exposure)
        return {"snapshot": True, "equity_usd": round(cash + exposure, 2)}
    except Exception as exc:
        logger.warning("live: snapshot equity fallito: %s", exc)
        return {"snapshot": False}
