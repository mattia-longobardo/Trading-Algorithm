"""Evoluzione mensile dell'arena: selezione, morte, clonazione e mutazione.

Regole (dalla specifica dell'utente):
- a fine mese sopravvive chi ha il PnL migliore, MA solo se > 0: chiudere a
  zero o in negativo è morte comunque, anche per il "vincitore";
- il sopravvissuto diventa il campione (il suo DNA guida il trading live) e
  viene clonato: la generazione successiva è lui stesso + una sua mutazione;
- se muoiono entrambi, la nuova generazione riparte dal DNA dell'ultimo
  campione storico (o dal DNA di default se non c'è mai stato un campione).
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from typing import Any

from etoro_bot.arena.dna import DEFAULT_DNA, clamp_dna, mutate, survival_creed
from etoro_bot.arena.engine import (
    ArenaDeps,
    agent_equity,
    effective_price,
    starting_capital_usd,
    survival_floor_pct,
)

logger = logging.getLogger(__name__)


def current_month(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def maybe_evolve(deps: ArenaDeps, now: datetime | None = None) -> dict[str, Any] | None:
    """Valuta il mese chiuso e genera la nuova generazione. None se non è ora."""
    arena = deps.repo.get_setting("arena")
    alive = deps.repo.alive_agents()
    if not arena or not alive:
        return None
    month_now = current_month(now)
    if arena.get("month") == month_now:
        return None

    # Liquidazione forzata: la valutazione si fa a conti chiusi, anche per gli
    # swing trader che tengono posizioni overnight (agli ultimi prezzi noti).
    prices: dict[str, float] = {}
    if deps.client is not None:
        try:
            from etoro_bot.arena.market import build_snapshot

            snapshot = build_snapshot(deps.client, deps.settings, only_open=False)
            prices = {s: float(r["price"]) for s, r in snapshot.items() if r.get("price")}
        except Exception as exc:
            logger.warning("evoluzione: prezzi di liquidazione non disponibili: %s", exc)
    for agent in alive:
        for pos in deps.repo.sim_positions(agent.id):
            price = prices.get(pos.symbol)
            deps.repo.close_sim_position(
                pos.id,
                effective_price(pos, price) if price else float(pos.entry_price),
                "liquidazione di fine mese (valutazione)",
            )

    results = []
    for agent in alive:
        agent = deps.repo.get_agent(agent.id)  # cash aggiornato dopo la liquidazione
        equity = agent_equity(agent, deps.repo.sim_positions(agent.id), {})
        results.append({"agent": agent, "pnl": round(equity - agent.starting_capital_usd, 2)})
    results.sort(key=lambda r: r["pnl"], reverse=True)
    best = results[0]
    survivor = best["agent"] if best["pnl"] > 0 else None

    for row in results:
        agent = row["agent"]
        if survivor is not None and agent.id == survivor.id:
            continue
        reason = (
            f"PnL mensile {row['pnl']:+.2f} USD ≤ 0"
            if row["pnl"] <= 0
            else f"sconfitto dal rivale ({row['pnl']:+.2f} vs {best['pnl']:+.2f} USD)"
        )
        deps.repo.kill_agent(agent.id, reason)

    floor_pct = survival_floor_pct(deps.settings)
    next_gen = max(a.generation for a in alive) + 1
    capital = starting_capital_usd(deps)
    rng = random.Random()

    if survivor is not None:
        deps.repo.retire_agent(survivor.id)  # "evolved": vive nella prossima generazione
        deps.repo.set_champion(survivor.id)
        base_dna = clamp_dna(survivor.dna)
        base_memory = survivor.memory or survival_creed(floor_pct)
        parent_id = survivor.id
        clone_dna = base_dna
    else:
        champion = deps.repo.champion()
        base_dna = clamp_dna(champion.dna) if champion else clamp_dna(DEFAULT_DNA)
        base_memory = survival_creed(floor_pct)
        parent_id = champion.id if champion else None
        clone_dna = base_dna

    mutant_dna = mutate(base_dna, rng, llm=deps.llm, model=deps.model,
                        max_tokens=deps.max_tokens)
    a_id = deps.repo.create_agent(
        f"G{next_gen}-Alfa", next_gen, clone_dna, base_memory, month_now, capital,
        parent_id=parent_id,
    )
    b_id = deps.repo.create_agent(
        f"G{next_gen}-Beta", next_gen, mutant_dna, base_memory, month_now, capital,
        parent_id=parent_id,
    )

    deps.repo.set_setting("arena", {**arena, "month": month_now}, source="arena")
    summary = {
        "month_closed": arena.get("month"),
        "results": [
            {"name": r["agent"].name, "pnl_usd": r["pnl"],
             "survived": survivor is not None and r["agent"].id == survivor.id}
            for r in results
        ],
        "survivor": survivor.name if survivor else None,
        "generation": next_gen,
        "children": [str(a_id), str(b_id)],
        "starting_capital_usd": capital,
    }
    deps.repo.add_arena_event("evolution", summary)
    logger.info("arena: evoluzione completata: %s", summary)
    return summary
