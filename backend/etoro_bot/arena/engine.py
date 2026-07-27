"""Motore dell'arena: cicli di day trading simulato dei due agenti rivali.

Ogni ciclo: stop loss / take profit automatici (codice), poi una decisione LLM
per agente, applicata al conto simulato con i vincoli del DNA. A fine giornata
tutte le posizioni vengono chiuse (day trading) e l'agente riflette: la sua
memoria evolve, ancorata al credo di sopravvivenza.
"""

from __future__ import annotations

import logging
import random
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from etoro_bot.arena.dna import DEFAULT_DNA, clamp_dna, mutate, survival_creed
from etoro_bot.arena.trader import build_prompt, decide, enforce
from etoro_bot.db.repo import Repository

logger = logging.getLogger(__name__)

MEMORY_MAX_CHARS = 4000  # la memoria non cresce senza limite


@dataclass
class ArenaDeps:
    """Dipendenze dell'arena; llm None = i trader saltano il turno (solo SL/TP)."""

    repo: Repository
    client: Any
    settings: dict[str, Any]
    llm: Callable[..., str] | None
    model: str
    max_tokens: int


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def arena_settings(settings: dict[str, Any]) -> dict[str, Any]:
    return settings.get("arena") or {}


def starting_capital_usd(deps: ArenaDeps) -> float:
    """Budget iniziale in USD dal budget in EUR configurato (default 10.000 €)."""
    eur = float(arena_settings(deps.settings).get("starting_capital_eur", 10_000))
    try:
        from etoro_bot.services.fx import rate_for

        rate = float(rate_for("EUR")) or 1.0  # moltiplicatore USD→EUR
        return round(eur / rate, 2)
    except Exception as exc:
        logger.warning("fx non disponibile, capitale 1:1 in USD: %s", exc)
        return eur


def agent_equity(agent, positions, prices: dict[str, float]) -> float:
    """Cash + mark-to-market; senza prezzo la posizione vale il suo costo."""
    equity = float(agent.cash_usd)
    for pos in positions:
        price = prices.get(pos.symbol)
        equity += pos.units * price if price else pos.amount_usd
    return round(equity, 2)


def auto_risk_closes(dna: dict, positions, prices: dict[str, float]) -> list[tuple]:
    """(posizione, motivo) per ogni stop loss / take profit scattato."""
    closes: list[tuple] = []
    sl = float(dna["stop_loss_pct"])
    tp = float(dna["take_profit_pct"])
    for pos in positions:
        price = prices.get(pos.symbol)
        if not price or not pos.entry_price:
            continue
        change_pct = (price / pos.entry_price - 1.0) * 100.0
        if change_pct <= -sl:
            closes.append((pos, f"stop loss automatico ({change_pct:+.2f}%)"))
        elif change_pct >= tp:
            closes.append((pos, f"take profit automatico ({change_pct:+.2f}%)"))
    return closes


def bootstrap_if_needed(deps: ArenaDeps, now: datetime | None = None) -> bool:
    """Prima generazione: due agenti dallo stesso DNA, uno mutato. True se creata."""
    if deps.repo.alive_agents():
        return False
    from etoro_bot.arena.evolution import current_month

    now = now or _utcnow()
    month = current_month(now)
    capital = starting_capital_usd(deps)
    creed = survival_creed()
    base = clamp_dna(DEFAULT_DNA)
    rng = random.Random()
    a_id = deps.repo.create_agent("G1-Alfa", 1, base, creed, month, capital)
    b_id = deps.repo.create_agent(
        "G1-Beta", 1, mutate(base, rng, llm=deps.llm, model=deps.model), creed, month, capital
    )
    arena = deps.repo.get_setting("arena") or {}
    deps.repo.set_setting(
        "arena",
        {"paused": False, "live_enabled": False, **arena, "month": month},
        source="arena",
    )
    deps.repo.add_arena_event(
        "birth",
        {"generation": 1, "agents": [str(a_id), str(b_id)],
         "starting_capital_usd": capital},
    )
    logger.info("arena: creata generazione 1 (capitale %.2f USD a testa)", capital)
    return True


def _positions_view(
    positions, prices: dict[str, float], max_days: int, now: datetime
) -> list[str]:
    lines = []
    for pos in positions:
        price = prices.get(pos.symbol)
        pnl = (pos.units * price - pos.amount_usd) if price else 0.0
        held = trading_days_held(pos.opened_at, now)
        lines.append(
            f"- {pos.symbol}: {pos.amount_usd:.2f} USD @ {pos.entry_price:.2f} "
            f"(PnL {pnl:+.2f} USD, giorno {held}/{max_days})"
        )
    return lines


def _survival_context(agent, equity: float, now: datetime) -> str:
    from calendar import monthrange

    days_left = monthrange(now.year, now.month)[1] - now.day
    pnl = equity - agent.starting_capital_usd
    return (
        f"Mese in corso: PnL {pnl:+.2f} USD su {agent.starting_capital_usd:.2f} "
        f"iniziali. Giorni alla valutazione: {days_left}. "
        f"{'SEI IN ZONA MORTE: a questo ritmo a fine mese verrai eliminato.' if pnl <= 0 else 'Sei in profitto: difendilo e accrescilo.'}"
    )


# Un solo ciclo di allenamento alla volta (scheduler + trigger manuale).
_cycle_lock = threading.Lock()


def run_training_cycle(
    deps: ArenaDeps,
    market: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Un ciclo di trading simulato per ogni agente vivo."""
    arena = deps.repo.get_setting("arena") or {}
    if arena.get("paused"):
        return {"skipped": "paused"}
    if not _cycle_lock.acquire(blocking=False):
        return {"skipped": "cycle_in_progress"}
    try:
        return _run_training_cycle_locked(deps, market, now)
    finally:
        _cycle_lock.release()


def _run_training_cycle_locked(
    deps: ArenaDeps,
    market: dict[str, dict[str, Any]] | None,
    now: datetime | None,
) -> dict[str, Any]:
    bootstrap_if_needed(deps, now=now)
    now = now or _utcnow()
    if market is None:
        from etoro_bot.arena.market import build_snapshot

        market = build_snapshot(deps.client, deps.settings)
    prices = {s: float(r["price"]) for s, r in market.items() if r.get("price")}
    cycled = 0
    for agent in deps.repo.alive_agents():
        try:
            _agent_cycle(deps, agent, market, prices, now)
            cycled += 1
        except Exception as exc:
            logger.exception("arena: ciclo fallito per %s", agent.name)
            deps.repo.add_arena_event(
                "error", {"agent": agent.name, "detail": f"ciclo: {exc}"}
            )
    return {"cycled": cycled, "market_symbols": len(market)}


def _agent_cycle(deps: ArenaDeps, agent, market, prices, now: datetime) -> None:
    dna = clamp_dna(agent.dna)
    positions = deps.repo.sim_positions(agent.id)
    for pos, reason in auto_risk_closes(dna, positions, prices):
        deps.repo.close_sim_position(pos.id, prices[pos.symbol], reason)

    if deps.llm is not None:
        agent = deps.repo.get_agent(agent.id)  # cash aggiornato dopo SL/TP
        positions = deps.repo.sim_positions(agent.id)
        equity = agent_equity(agent, positions, prices)
        prompt = build_prompt(
            name=agent.name,
            dna=dna,
            memory=agent.memory,
            survival=_survival_context(agent, equity, now),
            cash=agent.cash_usd,
            equity=equity,
            positions_view=_positions_view(
                positions, prices, int(dna["max_holding_days"]), now
            ),
            market_view=[str(r.get("view", s)) for s, r in market.items()],
        )
        actions = decide(deps.llm, model=deps.model, max_tokens=deps.max_tokens,
                         prompt=prompt)
        opens, closes = enforce(
            actions,
            dna=dna,
            cash=agent.cash_usd,
            equity=equity,
            held_symbols={p.symbol for p in positions},
            market=market,
        )
        by_symbol = {p.symbol: p for p in positions}
        for close in closes:
            pos = by_symbol.get(close["symbol"])
            price = prices.get(close["symbol"])
            if pos is not None and price:
                deps.repo.close_sim_position(pos.id, price, close["reason"] or "chiusura")
        for order in opens:
            price = prices.get(order["symbol"])
            if price:
                deps.repo.open_sim_position(
                    agent.id, order["symbol"], order["instrument_id"],
                    order["amount_usd"], price, order["reason"], opened_at=now,
                )

    agent = deps.repo.get_agent(agent.id)
    equity = agent_equity(agent, deps.repo.sim_positions(agent.id), prices)
    deps.repo.record_sim_equity(agent.id, now, equity)


def trading_days_held(opened_at: datetime, now: datetime) -> int:
    """Giorni di borsa (lun-ven) tra apertura e adesso, estremi inclusi."""
    from datetime import timedelta

    days = 0
    day = opened_at.date()
    while day <= now.date():
        if day.weekday() < 5:
            days += 1
        day += timedelta(days=1)
    return max(days, 1)


def close_market_positions(
    deps: ArenaDeps,
    market_name: str,
    market: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fine sessione: chiude le posizioni simulate di QUEL mercato che hanno
    raggiunto il holding massimo del DNA (max_holding_days=1 = day trading;
    fino a 5 = swing). Le altre passano la notte, protette da SL/TP alla
    riapertura."""
    from etoro_bot.arena.market import build_snapshot, market_of_symbol

    now = now or _utcnow()
    if market is None:
        # la borsa è appena chiusa: servono comunque gli ultimi prezzi
        market = build_snapshot(deps.client, deps.settings, now=now, only_open=False)
    prices = {s: float(r["price"]) for s, r in market.items() if r.get("price")}
    closed = 0
    for agent in deps.repo.alive_agents():
        max_days = int(clamp_dna(agent.dna)["max_holding_days"])
        for pos in deps.repo.sim_positions(agent.id):
            if market_of_symbol(pos.symbol) != market_name:
                continue
            held = trading_days_held(pos.opened_at, now)
            if held < max_days:
                continue
            price = prices.get(pos.symbol) or pos.entry_price
            deps.repo.close_sim_position(
                pos.id, price,
                f"fine sessione {market_name}: holding massimo raggiunto "
                f"({held}/{max_days} giorni)",
            )
            closed += 1
    return {"closed": closed, "market": market_name}


def run_eod(
    deps: ArenaDeps,
    market: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Ultima campanella del giorno: snapshot equity + riflessione serale.

    Le chiusure per scadenza holding avvengono ai singoli fine-sessione
    (close_market_positions); qui restano il mark-to-market delle posizioni
    che passano la notte e l'aggiornamento della memoria degli agenti.
    """
    now = now or _utcnow()
    if market is None:
        from etoro_bot.arena.market import build_snapshot

        market = build_snapshot(deps.client, deps.settings, now=now, only_open=False)
    prices = {s: float(r["price"]) for s, r in market.items() if r.get("price")}
    reflected = 0
    for agent in deps.repo.alive_agents():
        day_pnl = sum(
            t.pnl_usd
            for t in deps.repo.sim_trades(agent.id, limit=200)
            if t.closed_at.date() == now.date()
        )
        equity = agent_equity(agent, deps.repo.sim_positions(agent.id), prices)
        deps.repo.record_sim_equity(agent.id, now, equity)
        _reflect(deps, agent, day_pnl, now)
        reflected += 1
    return {"reflected": reflected}


def _reflect(deps: ArenaDeps, agent, day_pnl: float, now: datetime) -> None:
    """La memoria dell'agente evolve: credo di sopravvivenza + diario compresso."""
    creed = survival_creed()
    diary = agent.memory
    if diary.startswith(creed):
        diary = diary[len(creed):].strip()
    lesson = ""
    if deps.llm is not None:
        trades = deps.repo.sim_trades(agent.id, limit=20)
        trades_text = "\n".join(
            f"- {t.symbol}: {t.pnl_usd:+.2f} USD ({t.close_reason})" for t in trades
        ) or "(nessun trade oggi)"
        pnl_month = agent.cash_usd - agent.starting_capital_usd
        prompt = (
            f"Sei {agent.name}. Giornata chiusa con PnL {day_pnl:+.2f} USD; "
            f"PnL del mese {pnl_month:+.2f} USD. La tua vita dipende dal profitto: "
            f"a fine mese, se non sei in positivo e davanti al rivale, muori.\n"
            f"Trade recenti:\n{trades_text}\n\n"
            f"Diario attuale:\n{diary or '(vuoto)'}\n\n"
            "Aggiorna il diario: massimo 10 punti, conserva solo le lezioni che "
            "aumentano il profitto di domani. Rispondi solo col diario."
        )
        try:
            lesson = deps.llm(
                system_blocks=[], user_prompt=prompt,
                model=deps.model, max_tokens=deps.max_tokens,
            ).strip()
        except Exception as exc:
            logger.warning("arena: riflessione fallita per %s: %s", agent.name, exc)
    new_diary = lesson or diary
    memory = f"{creed}\n\nDIARIO:\n{new_diary}".strip()
    deps.repo.update_agent_memory(agent.id, memory[:MEMORY_MAX_CHARS])
