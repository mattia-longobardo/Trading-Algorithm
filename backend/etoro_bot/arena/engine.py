"""Motore dell'arena: cicli di trading simulato dei due agenti rivali.

Ogni ciclo: chiusure automatiche previste dal DNA (se l'agente le ha attivate),
poi una decisione LLM per agente applicata al conto simulato. Long e short sono
entrambi ammessi; l'orizzonte (intraday o swing su più sedute) è un gene, non
una regola di sistema. L'unico freno di sistema è il pavimento di bancarotta:
sotto quella soglia l'agente muore all'istante.

Il libro mastro simulato (repo) conosce solo posizioni "lunghe": la direzione
sta nella colonna `direction` di sim_positions/sim_trades e lo short si
rappresenta specchiando il prezzo in valutazione e chiusura (uno short entrato
a E e chiuso a P vale come un long chiuso a 2E-P, stesso PnL units*(E-P)).
"""

from __future__ import annotations

import logging
import random
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from etoro_bot.arena.dna import (
    DEFAULT_DNA,
    DEFAULT_SURVIVAL_FLOOR_PCT,
    clamp_dna,
    mutate,
    position_direction,
    risk_close_reason,
    survival_creed,
)
from etoro_bot.arena.trader import SHORT, build_prompt, decide, enforce
from etoro_bot.db.repo import Repository
from etoro_bot.domain import SimCosts

logger = logging.getLogger(__name__)

MEMORY_MAX_CHARS = 8000  # la memoria non cresce senza limite


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


def sim_costs(settings: dict[str, Any]) -> SimCosts:
    """Costi di transazione del conto simulato da settings (arena.costs)."""
    return SimCosts.from_settings(arena_settings(settings))


def survival_floor_pct(settings: dict[str, Any]) -> float:
    """% del capitale iniziale sotto cui l'agente è in bancarotta (0 = disattivo)."""
    try:
        return float(
            arena_settings(settings).get("survival_floor_pct", DEFAULT_SURVIVAL_FLOOR_PCT)
        )
    except (TypeError, ValueError):
        return DEFAULT_SURVIVAL_FLOOR_PCT


# ------------------------------------------------------------------ direzione
# `position_direction` e `SHORT_TAG` vivono in arena/dna.py (modulo senza
# dipendenze di dominio) e sono importati qui sopra: così le metriche possono
# usarli senza tirarsi dietro il motore.


def effective_price(pos, price: float) -> float:
    """Prezzo da passare al libro mastro long-only per un PnL corretto.

    Long: il prezzo stesso. Short: il prezzo specchiato sull'ingresso
    (2*entry - price), azzerato se il titolo raddoppia (collaterale bruciato).
    """
    if position_direction(pos) != SHORT:
        return float(price)
    return max(2.0 * float(pos.entry_price) - float(price), 0.0)


def position_value(pos, price: float | None) -> float:
    """Valore mark-to-market della posizione; senza prezzo vale il suo costo."""
    if not price:
        return float(pos.amount_usd)
    return float(pos.units) * effective_price(pos, float(price))


def position_change_pct(pos, price: float) -> float:
    """Variazione % a favore dell'agente (positiva = in profitto), per direzione."""
    entry = float(pos.entry_price or 0.0)
    if not entry:
        return 0.0
    change = (float(price) / entry - 1.0) * 100.0
    return -change if position_direction(pos) == SHORT else change


def starting_capital_usd(deps: ArenaDeps) -> float:
    """Budget iniziale di ogni agente, in USD (default 10.000 $).

    Il capitale è USD-nativo come tutto il journal: la valuta scelta nelle
    Impostazioni resta di sola presentazione. Configurarlo in EUR e convertirlo
    una volta sola alla nascita dell'agente lo congelava al cambio di quel
    giorno, mentre la UI riconverte al cambio corrente: bastava un EUR più
    forte perché un agente in profitto apparisse sotto il capitale iniziale.
    """
    try:
        return round(
            float(arena_settings(deps.settings).get("starting_capital_usd", 10_000)), 2
        )
    except (TypeError, ValueError):
        return 10_000.0


def agent_equity(agent, positions, prices: dict[str, float]) -> float:
    """Cash + mark-to-market (long e short); senza prezzo la posizione vale il costo."""
    equity = float(agent.cash_usd)
    for pos in positions:
        equity += position_value(pos, prices.get(pos.symbol))
    return round(equity, 2)


def auto_risk_closes(dna: dict, positions, prices: dict[str, float]) -> list[tuple]:
    """(posizione, motivo) per ogni stop loss / take profit scattato.

    Stop e take profit sono geni: a 0 sono DISATTIVATI e l'agente resta l'unico
    a decidere quando uscire.
    """
    closes: list[tuple] = []
    for pos in positions:
        price = prices.get(pos.symbol)
        if not price or not pos.entry_price:
            continue
        reason = risk_close_reason(dna, position_change_pct(pos, price))
        if reason is not None:
            closes.append((pos, reason))
    return closes


def bootstrap_if_needed(deps: ArenaDeps, now: datetime | None = None) -> bool:
    """Crea una nuova generazione quando non c'è nessun agente vivo. True se creata.

    Primo avvio: generazione 1 dal DNA di default. Se invece i predecessori sono
    morti in corso di mese (bancarotta), la vita riparte dalla generazione
    successiva col DNA del campione — la selezione non si azzera.
    """
    if deps.repo.alive_agents():
        return False
    from etoro_bot.arena.evolution import current_month

    now = now or _utcnow()
    month = current_month(now)
    capital = starting_capital_usd(deps)
    creed = survival_creed(survival_floor_pct(deps.settings))
    previous = deps.repo.all_agents()
    champion = deps.repo.champion()
    generation = (max(a.generation for a in previous) + 1) if previous else 1
    source = champion.dna if champion is not None else (previous[-1].dna if previous else None)
    base = clamp_dna(source or DEFAULT_DNA)
    parent_id = champion.id if champion is not None else None
    rng = random.Random()
    a_id = deps.repo.create_agent(
        f"G{generation}-Alfa", generation, base, creed, month, capital, parent_id=parent_id
    )
    b_id = deps.repo.create_agent(
        f"G{generation}-Beta", generation,
        mutate(base, rng, llm=deps.llm, model=deps.model, max_tokens=deps.max_tokens),
        creed, month, capital, parent_id=parent_id,
    )
    arena = deps.repo.get_setting("arena") or {}
    deps.repo.set_setting(
        "arena",
        {"paused": False, "live_enabled": False, **arena, "month": month},
        source="arena",
    )
    deps.repo.add_arena_event(
        "birth",
        {"generation": generation, "agents": [str(a_id), str(b_id)],
         "starting_capital_usd": capital},
    )
    logger.info(
        "arena: creata generazione %d (capitale %.2f USD a testa)", generation, capital
    )
    return True


def _positions_view(
    positions, prices: dict[str, float], max_days: int, now: datetime
) -> list[str]:
    lines = []
    for pos in positions:
        price = prices.get(pos.symbol)
        pnl = position_value(pos, price) - float(pos.amount_usd) if price else 0.0
        held = trading_days_held(pos.opened_at, now)
        direction = position_direction(pos).upper()
        lines.append(
            f"- {pos.symbol} {direction}: {pos.amount_usd:.2f} USD @ "
            f"{pos.entry_price:.2f} (PnL {pnl:+.2f} USD, giorno {held}/{max_days})"
        )
    return lines


def _survival_context(agent, equity: float, now: datetime, floor_pct: float) -> str:
    from calendar import monthrange

    days_left = monthrange(now.year, now.month)[1] - now.day
    pnl = equity - agent.starting_capital_usd
    floor_usd = float(agent.starting_capital_usd) * floor_pct / 100.0
    lines = [
        f"Mese in corso: PnL {pnl:+.2f} USD su {agent.starting_capital_usd:.2f} "
        f"iniziali. Giorni alla valutazione: {days_left}.",
        (
            "SEI IN ZONA MORTE: a questo ritmo a fine mese verrai eliminato. "
            "Cambia passo: più operazioni, entrambe le direzioni, idee nuove."
            if pnl <= 0
            else "Sei in profitto: difendilo e accrescilo, non rallentare."
        ),
    ]
    if floor_pct > 0:
        lines.append(
            f"Pavimento di bancarotta: {floor_usd:.2f} USD di equity "
            f"({floor_pct:.0f}% dell'iniziale). Se lo sfondi muori all'istante: "
            "è l'unico limite che non puoi negoziare, tutto il resto è tuo."
        )
    return " ".join(lines)


# Un solo ciclo di allenamento alla volta (scheduler + trigger manuale). Lo
# condivide anche l'evoluzione mensile: liquidare e valutare i conti mentre un
# ciclo opera sugli stessi agenti falserebbe l'equity che elegge il campione.
cycle_lock = threading.Lock()


def run_training_cycle(
    deps: ArenaDeps,
    market: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Un ciclo di trading simulato per ogni agente vivo."""
    arena = deps.repo.get_setting("arena") or {}
    if arena.get("paused"):
        return {"skipped": "paused"}
    if not cycle_lock.acquire(blocking=False):
        return {"skipped": "cycle_in_progress"}
    try:
        return _run_training_cycle_locked(deps, market, now)
    finally:
        cycle_lock.release()


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
    costs = sim_costs(deps.settings)
    positions = deps.repo.sim_positions(agent.id)
    for pos, reason in auto_risk_closes(dna, positions, prices):
        deps.repo.close_sim_position(
            pos.id, effective_price(pos, prices[pos.symbol]), reason,
            costs=costs, now=now,
        )

    if deps.llm is not None:
        agent = deps.repo.get_agent(agent.id)  # cash aggiornato dopo SL/TP
        positions = deps.repo.sim_positions(agent.id)
        equity = agent_equity(agent, positions, prices)
        prompt = build_prompt(
            name=agent.name,
            dna=dna,
            memory=agent.memory,
            survival=_survival_context(
                agent, equity, now, survival_floor_pct(deps.settings)
            ),
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
            held_count=len(positions),
        )
        for close in closes:
            price = prices.get(close["symbol"])
            if not price:
                continue
            wanted = close.get("direction")
            for pos in positions:
                if pos.symbol != close["symbol"]:
                    continue
                if wanted and position_direction(pos) != wanted:
                    continue
                deps.repo.close_sim_position(
                    pos.id, effective_price(pos, price), close["reason"] or "chiusura",
                    costs=costs, now=now,
                )
        for order in opens:
            price = prices.get(order["symbol"])
            if price:
                deps.repo.open_sim_position(
                    agent.id, order["symbol"], order["instrument_id"],
                    order["amount_usd"], price, order["reason"],
                    opened_at=now,
                    direction=order["direction"],
                    costs=costs,
                )

    agent = deps.repo.get_agent(agent.id)
    equity = agent_equity(agent, deps.repo.sim_positions(agent.id), prices)
    deps.repo.record_sim_equity(agent.id, now, equity)
    _enforce_survival_floor(deps, agent, equity, prices)


def _enforce_survival_floor(deps: ArenaDeps, agent, equity: float, prices) -> None:
    """Bancarotta: sotto il pavimento l'agente muore subito, senza aspettare il mese.

    È l'unico veto di sistema rimasto sul conto simulato: non modera le
    decisioni, decide solo quando la partita è finita.
    """
    floor_pct = survival_floor_pct(deps.settings)
    if floor_pct <= 0:
        return
    floor = float(agent.starting_capital_usd) * floor_pct / 100.0
    if equity > floor:
        return
    costs = sim_costs(deps.settings)
    for pos in deps.repo.sim_positions(agent.id):
        price = prices.get(pos.symbol)
        deps.repo.close_sim_position(
            pos.id,
            effective_price(pos, price) if price else float(pos.entry_price),
            "liquidazione per bancarotta",
            costs=costs,
        )
    reason = (
        f"bancarotta: equity {equity:.2f} USD sotto il pavimento di "
        f"sopravvivenza ({floor:.2f} USD)"
    )
    deps.repo.kill_agent(agent.id, reason[:128])
    deps.repo.add_arena_event("death", {"agent": agent.name, "detail": reason})
    logger.warning("arena: %s eliminato per bancarotta (%.2f USD)", agent.name, equity)


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
    raggiunto il holding massimo del DNA (max_holding_days=1 = intraday puro,
    fino a 60 = swing lungo). Le altre passano la notte: lo swing è
    autorizzato, non è un'anomalia da correggere."""
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
            price = prices.get(pos.symbol)
            deps.repo.close_sim_position(
                pos.id,
                effective_price(pos, price) if price else float(pos.entry_price),
                f"fine sessione {market_name}: holding massimo raggiunto "
                f"({held}/{max_days} giorni)",
                costs=sim_costs(deps.settings), now=now,
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
    creed = survival_creed(survival_floor_pct(deps.settings))
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
            "aumentano il profitto di domani. Sei libero di rinnegare qualunque "
            "regola che ti sei dato: puoi cambiare direzione preferita (long o "
            "short), orizzonte (intraday o swing), frequenza e size, e riscrivere "
            "la tua strategia da capo. Se hai operato poco, annota che l'inerzia "
            "ti sta uccidendo. Rispondi solo col diario."
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
