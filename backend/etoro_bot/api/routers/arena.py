"""Arena evolutiva: agenti, campione, lineage, eventi e controlli."""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from etoro_bot.api import dependencies as deps
from etoro_bot.api import schemas
from etoro_bot.api.dependencies import UserIdentity, current_user, require_owner
from etoro_bot.db.repo import Repository

log = logging.getLogger("etoro_bot.api")

router = APIRouter()


# Quanti trade chiusi finiscono nella risposta del dettaglio: le metriche però
# li leggono tutti, altrimenti descriverebbero solo la finestra più recente.
ARENA_TRADES_SHOWN = 200


def _agent_payload(
    repo: Repository, agent, with_memory: bool = True,
    equity_usd: float | None = None, positions=None,
) -> dict[str, Any]:
    """`equity_usd` è l'ultimo punto della serie sim (mark-to-market, scritto dal
    ciclo di training): senza prezzi correnti in questa route è l'unica
    definizione coerente con `agent_equity`. Senza serie si ripiega sul costo.

    `positions` già lette dal chiamante evitano una query per agente (la pagina
    arena ne mostra tre liste: agenti vivi, campione, lineage)."""
    from etoro_bot.arena.dna import position_direction

    positions = repo.sim_positions(agent.id) if positions is None else positions
    invested = sum(p.amount_usd for p in positions)
    if equity_usd is None:
        equity_usd = agent.cash_usd + invested
    pnl_month = equity_usd - agent.starting_capital_usd
    payload = {
        "id": str(agent.id),
        "name": agent.name,
        "generation": agent.generation,
        "status": agent.status,
        "is_champion": agent.is_champion,
        "parent_id": str(agent.parent_id) if agent.parent_id else None,
        "born_at": agent.born_at.isoformat() if agent.born_at else None,
        "died_at": agent.died_at.isoformat() if agent.died_at else None,
        "death_reason": agent.death_reason,
        "month": agent.month,
        "dna": agent.dna,
        "starting_capital_usd": agent.starting_capital_usd,
        "cash_usd": agent.cash_usd,
        "invested_usd": round(invested, 2),
        "equity_usd": round(equity_usd, 2),
        "pnl_month_usd": round(pnl_month, 2),
        "open_positions": [
            {
                "id": str(p.id),
                "symbol": p.symbol,
                "amount_usd": p.amount_usd,
                "entry_price": p.entry_price,
                "opened_at": p.opened_at.isoformat(),
                "open_reason": p.open_reason,
                "direction": position_direction(p),
            }
            for p in positions
        ],
    }
    if with_memory:
        payload["memory"] = agent.memory
    return payload



@router.get("/arena", response_model=schemas.ArenaOverviewResponse,
         response_model_exclude_unset=True)
def arena_overview() -> dict[str, Any]:
    from etoro_bot.services.app_settings import arena_state
    from etoro_bot.services.scheduler import (
        market_is_open,
        market_sessions,
        next_cycle_at,
        open_sessions,
    )

    repo = deps.get_repo()
    settings = deps.full_settings()
    agents = repo.all_agents()
    alive = [a for a in agents if a.status == "alive"]
    champ = repo.champion()
    # due query aggregate al posto di due per agente: la pagina è in polling
    equities = repo.last_sim_equity([a.id for a in agents])
    by_agent = repo.sim_positions_by_agent([a.id for a in agents])
    now = datetime.now(UTC)
    from calendar import monthrange

    days_left = monthrange(now.year, now.month)[1] - now.day
    now_open = set(open_sessions(settings, now))
    return {
        "state": arena_state(repo),
        "market_open": market_is_open(settings, now),
        "sessions": [
            {"name": name, "open_utc": window[0], "close_utc": window[1],
             "open_now": name in now_open}
            for name, window in sorted(market_sessions(settings).items())
        ],
        "next_cycle_at": next_cycle_at(settings),
        "days_to_evaluation": days_left,
        "generation": max((a.generation for a in alive), default=0),
        "agents": [
            _agent_payload(repo, a, equity_usd=equities.get(a.id),
                           positions=by_agent.get(a.id, []))
            for a in alive
        ],
        "champion": (
            _agent_payload(repo, champ, equity_usd=equities.get(champ.id),
                           positions=by_agent.get(champ.id, []))
            if champ else None
        ),
        "lineage": [
            _agent_payload(repo, a, with_memory=False, equity_usd=equities.get(a.id),
                           positions=by_agent.get(a.id, []))
            for a in agents
        ],
    }



@router.get("/arena/agents/{agent_id}", response_model=schemas.ArenaAgentDetailResponse,
         response_model_exclude_unset=True)
def arena_agent_detail(agent_id: uuid.UUID) -> dict[str, Any]:
    from etoro_bot.arena.metrics import compute_agent_metrics, trade_direction

    repo = deps.get_repo()
    agent = repo.get_agent(agent_id)
    if agent is None:
        raise HTTPException(404, "agente non trovato")
    all_trades = repo.sim_trades(agent_id, limit=None)  # metriche su tutto lo storico
    trades = all_trades[:ARENA_TRADES_SHOWN]
    equity_points = repo.sim_equity_series(agent_id)
    payload = _agent_payload(
        repo, agent, equity_usd=equity_points[-1].equity_usd if equity_points else None
    )
    return {
        "agent": payload,
        "equity": [
            {"ts": p.ts.isoformat(), "equity_usd": p.equity_usd}
            for p in equity_points
        ],
        "trades": [
            {
                "id": str(t.id),
                "symbol": t.symbol,
                "amount_usd": t.amount_usd,
                "entry_price": t.entry_price,
                "close_price": t.close_price,
                "pnl_usd": t.pnl_usd,
                "opened_at": t.opened_at.isoformat(),
                "closed_at": t.closed_at.isoformat(),
                "open_reason": t.open_reason,
                "close_reason": t.close_reason,
                "direction": trade_direction(t),
                "holding_hours": round(
                    (t.closed_at - t.opened_at).total_seconds() / 3600.0, 1
                ),
                "return_pct": round(t.pnl_usd / t.amount_usd * 100.0, 2)
                if t.amount_usd
                else None,
            }
            for t in trades
        ],
        "metrics": compute_agent_metrics(
            starting_capital_usd=agent.starting_capital_usd,
            cash_usd=agent.cash_usd,
            # già sommato dal payload dell'agente: evita una query in più
            invested_usd=payload["invested_usd"],
            born_at=agent.born_at,
            trades=all_trades,
            equity_points=equity_points,
        ),
    }



@router.get("/arena/events", response_model=schemas.ArenaEventsResponse)
def arena_events(limit: int = Query(100, le=500)) -> dict[str, Any]:
    rows = deps.get_repo().arena_events(limit=limit)
    return {
        "events": [
            {"id": str(e.id), "ts": e.ts.isoformat(), "event": e.event,
             "payload": e.payload}
            for e in rows
        ]
    }



@router.post("/arena/pause")
def arena_pause(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    from etoro_bot.services.app_settings import set_arena_state

    require_owner(identity, "mettere in pausa l'arena")
    state = set_arena_state(deps.get_repo(), paused=True)
    deps.get_repo().add_arena_event("pause", {})
    return {"state": state}



@router.post("/arena/resume")
def arena_resume(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    from etoro_bot.services.app_settings import set_arena_state

    require_owner(identity, "riavviare l'arena")
    state = set_arena_state(deps.get_repo(), paused=False)
    deps.get_repo().add_arena_event("resume", {})
    return {"state": state}



@router.post("/arena/cycle", status_code=202)
def arena_trigger_cycle(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    """Forza un ciclo di allenamento adesso (per test/monitoraggio manuale)."""
    require_owner(identity, "forzare un ciclo")

    def _job() -> None:
        try:
            from etoro_bot.arena.engine import run_training_cycle

            cycle_deps = deps.arena_deps()
            log.info("arena: ciclo manuale %s", run_training_cycle(cycle_deps))
        except Exception:
            log.exception("ciclo manuale fallito")

    threading.Thread(target=_job, daemon=True).start()
    return {"status": "accepted"}
