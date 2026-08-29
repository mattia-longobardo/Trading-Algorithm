"""Replay storico dell'arena: stesso engine, bordi scambiati (pattern nautilus).

`run_training_cycle`/`_agent_cycle` consumano snapshot a forma di
`build_snapshot()` e un `now` esplicito: il replay costruisce gli snapshot da
candele storiche e fa girare LO STESSO codice del ciclo di training, bar per
bar. Nessuna biforcazione backtest/live: ciò che vale nel replay vale nel
ciclo reale.

Anti look-ahead (lezione TradingAgents, issue #203):
- lo snapshot del bar N usa solo chiusure <= N;
- niente memoria news (`_memory_hint`): il contenuto attuale della KB
  parlerebbe del futuro rispetto alla data simulata.

Gli agenti di replay vivono nel mese "replay-<uuid>" e vengono uccisi a fine
corsa: non toccano mai il torneo reale. Il lock di ciclo dell'engine viene
tenuto per l'intera durata così un tick reale non può ciclare l'agente di
replay con prezzi live.
ponytail: lock globale per tutta la corsa — se i replay diventano lunghi o
frequenti servirà un flag di esclusione in alive_agents invece del lock.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from etoro_bot.arena.dna import clamp_dna
from etoro_bot.arena.engine import (
    ArenaDeps,
    _agent_cycle,
    cycle_lock,
    effective_price,
    sim_costs,
)
from etoro_bot.arena.market import MARKET_LABELS, market_of_symbol, metrics_from_closes

logger = logging.getLogger(__name__)

WARMUP_BARS = 21  # servono >= 20 chiusure per la SMA20 del primo snapshot
REPLAY_MONTH = "replay-"[:7]  # marcatore nel campo month (String(7))


def _fmt(value: float | None) -> str:
    return f"{value:+.1f}%" if value is not None else "n/d"


def _parse_bar_date(raw: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_history(
    client: Any, symbols: list[str], days: int, type_ids: tuple[int, ...] = (5, 6)
) -> tuple[dict[str, list[dict]], dict[str, int]]:
    """Candele giornaliere e instrument_id per i simboli richiesti."""
    wanted = {str(s).upper() for s in symbols}
    meta: dict[str, int] = {}
    for type_id in type_ids:
        try:
            for row in client.get_instruments_by_type(type_id):
                symbol = str(row.get("symbolFull") or "").upper()
                if symbol in wanted and symbol not in meta:
                    meta[symbol] = int(row["instrumentID"])
        except Exception as exc:
            logger.warning("replay: catalogo tipo %s non disponibile: %s", type_id, exc)
    candles: dict[str, list[dict]] = {}
    for symbol, instrument_id in meta.items():
        try:
            rows = client.get_candles(instrument_id, "OneDay", count=days + WARMUP_BARS)
        except Exception as exc:
            logger.warning("replay: candele non disponibili per %s: %s", symbol, exc)
            continue
        if rows:
            candles[symbol] = rows
    return candles, meta


def snapshot_from_history(
    candles: dict[str, list[dict]], meta: dict[str, int], bar_index: int
) -> tuple[dict[str, dict[str, Any]], datetime | None]:
    """Snapshot a forma di build_snapshot() usando SOLO dati fino a bar_index."""
    snapshot: dict[str, dict[str, Any]] = {}
    bar_date: datetime | None = None
    for symbol, rows in candles.items():
        if bar_index >= len(rows):
            continue
        bar = rows[bar_index]
        price = bar.get("close")
        if not price:
            continue
        if bar_date is None:
            bar_date = _parse_bar_date(bar.get("fromDate"))
        closes = [
            float(r["close"]) for r in rows[:bar_index] if r.get("close")
        ]
        m = metrics_from_closes(float(price), closes)
        market = market_of_symbol(symbol)
        view = (
            f"[{MARKET_LABELS.get(market, market)}] {symbol} {float(price):.2f} | "
            f"oggi {_fmt(m['day_pct'])} | 5g {_fmt(m['week_pct'])} | "
            f"vs SMA20 {_fmt(m['sma20_dist_pct'])}"
        )
        snapshot[symbol] = {
            "instrument_id": meta.get(symbol, 0),
            "price": float(price),
            "market": market,
            **m,
            "view": view,
        }
    return snapshot, bar_date


def replay_metrics(equity: list[float], pnls: list[float], starting: float) -> dict[str, Any]:
    """Metriche minime di valutazione di una corsa di replay."""
    out: dict[str, Any] = {
        "final_equity": round(equity[-1], 2) if equity else starting,
        "return_pct": round((equity[-1] / starting - 1.0) * 100.0, 2) if equity else 0.0,
        "trades": len(pnls),
        "win_rate_pct": round(
            100.0 * sum(1 for p in pnls if p > 0) / len(pnls), 1
        ) if pnls else None,
        "max_drawdown_pct": 0.0,
    }
    peak = starting
    max_dd = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak * 100.0)
    out["max_drawdown_pct"] = round(max_dd, 2)
    return out


def run_replay(
    deps: ArenaDeps,
    dna: dict[str, Any],
    symbols: list[str],
    days: int = 60,
    starting_capital_usd: float = 10_000.0,
    history: tuple[dict[str, list[dict]], dict[str, int]] | None = None,
    lock_held: bool = False,
) -> dict[str, Any]:
    """Valuta un DNA su candele storiche con lo stesso engine del training.

    Ritorna le metriche della corsa; l'agente di replay viene sempre ucciso a
    fine corsa (anche su errore) e non entra mai nel torneo reale.
    `lock_held=True` è per i chiamanti che tengono già `cycle_lock`
    (l'evoluzione mensile): il lock non è rientrante.
    """
    if history is None:
        if deps.client is None:
            return {"error": "client eToro assente e nessuna history fornita"}
        history = load_history(deps.client, symbols, days)
    candles, meta = history
    if not candles:
        return {"error": "nessuna candela storica disponibile"}
    total_bars = max(len(rows) for rows in candles.values())
    if total_bars <= WARMUP_BARS:
        return {"error": f"storico troppo corto ({total_bars} bar, servono > {WARMUP_BARS})"}

    if not lock_held and not cycle_lock.acquire(blocking=False):
        return {"skipped": "cycle_in_progress"}
    # la colonna month è String(7) ("YYYY-MM"): il marcatore replay ci sta esatto
    run_tag = f"R-{uuid.uuid4().hex[:8]}"
    agent_id = deps.repo.create_agent(
        run_tag, 0, clamp_dna(dna), "", REPLAY_MONTH, starting_capital_usd
    )
    try:
        bars_run = 0
        for bar_index in range(WARMUP_BARS, total_bars):
            agent = deps.repo.get_agent(agent_id)
            if agent is None or agent.status != "alive":
                break  # bancarotta nel replay: la corsa finisce qui
            snapshot, bar_date = snapshot_from_history(candles, meta, bar_index)
            if not snapshot:
                continue
            now = bar_date or datetime.now(timezone.utc)
            prices = {s: float(r["price"]) for s, r in snapshot.items() if r.get("price")}
            _agent_cycle(deps, agent, snapshot, prices, now)
            bars_run += 1
        # liquidazione finale all'ultima chiusura disponibile (con costi)
        last_snapshot, last_date = snapshot_from_history(candles, meta, total_bars - 1)
        last_prices = {s: float(r["price"]) for s, r in last_snapshot.items()}
        costs = sim_costs(deps.settings)
        for pos in deps.repo.sim_positions(agent_id):
            price = last_prices.get(pos.symbol)
            deps.repo.close_sim_position(
                pos.id,
                effective_price(pos, price) if price else float(pos.entry_price),
                "liquidazione fine replay",
                costs=costs,
                now=last_date,
            )
        agent = deps.repo.get_agent(agent_id)
        equity = [float(p.equity_usd) for p in deps.repo.sim_equity_series(agent_id)]
        cash = float(agent.cash_usd) if agent is not None else starting_capital_usd
        equity.append(cash)  # dopo la liquidazione l'equity è tutta cassa
        pnls = [float(t.pnl_usd) for t in deps.repo.sim_trades(agent_id, limit=None)]
        metrics = replay_metrics(equity, pnls, starting_capital_usd)
        metrics.update({"bars": bars_run, "symbols": sorted(candles), "run_tag": run_tag})
        return metrics
    finally:
        deps.repo.kill_agent(agent_id, "replay completato")
        if not lock_held:
            cycle_lock.release()
