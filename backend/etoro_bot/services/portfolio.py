"""Fotografia del portafoglio del bot: posizioni, PnL, equity e size massima.

Il calcolo sta qui e non nella route perché è dominio, non serializzazione: la
route si limita a tradurre un guasto del broker in 502.
"""

from __future__ import annotations

from typing import Any


def _rates_by_instrument(client, positions) -> dict[int, float]:
    raw = client.get_rates([p.instrument_id for p in positions]) if positions else {}
    rates: dict[int, float] = {}
    for instrument_id, row in raw.items():
        price = row.get("lastExecution") or row.get("bid")
        if price:
            rates[int(instrument_id)] = float(price)
    return rates


def portfolio_snapshot(repo, client) -> dict[str, Any]:
    """Payload di `/portfolio`. Solleva se il broker non pubblica il capitale:
    una cassa ignota non è zero, e chi chiama deve dirlo, non inventarla."""
    from etoro_bot.arena.dna import clamp_dna
    from etoro_bot.arena.live import live_equity, live_position_value

    positions = repo.open_positions()
    account_portfolio = client.get_portfolio()
    if account_portfolio.get("credit") is None:
        raise ValueError("portfolio eToro senza campo credit")
    cash_usd = max(float(account_portfolio["credit"]), 0.0)
    rates = _rates_by_instrument(client, positions)

    # stessa definizione di equity del ciclo live e del circuit breaker: a costo
    # storico /portfolio, sizing e drawdown raccontavano tre numeri diversi.
    prices = {
        p.symbol: rates[p.instrument_id] for p in positions if p.instrument_id in rates
    }
    invested = sum(live_position_value(p, prices.get(p.symbol)) for p in positions)
    equity_usd = live_equity(positions, prices, cash_usd)

    out = []
    for p in positions:
        cur = rates.get(p.instrument_id)
        # direzione dal registry, non riderivata dal portafoglio del broker
        direction = p.direction or "long"
        pnl = None
        pnl_pct = None
        if cur is not None and p.entry_price > 0:
            change = (cur - p.entry_price) / p.entry_price
            if direction == "short":
                change = -change
            pnl = p.amount_usd * change
            pnl_pct = change * 100
        out.append(
            {
                "etoro_position_id": p.etoro_position_id,
                "symbol": p.symbol,
                "instrument_id": p.instrument_id,
                "amount_usd": p.amount_usd,
                "direction": direction,
                "entry_price": p.entry_price,
                "current_price": cur,
                "unrealized_pnl_usd": pnl,
                "unrealized_pnl_pct": pnl_pct,
                "sector": p.sector,
                "opened_at": p.opened_at.isoformat(),
            }
        )

    # size massima teorica per ordine live, dal DNA del campione (o default)
    champ = repo.champion()
    dna = clamp_dna(champ.dna if champ else None)
    return {
        "positions": out,
        "cash_usd": cash_usd,
        "equity_usd": equity_usd,
        "exposure_usd": invested,
        "max_trade_amount_usd": equity_usd * dna["max_position_pct"] / 100.0,
        "capital_source": "etoro",
        "anomalies": [],
    }
