"""Salute del container e stato operativo complessivo."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter

from etoro_bot.api import dependencies as deps
from etoro_bot.safety.kill_switch import kill_switch_active

log = logging.getLogger("etoro_bot.api")

router = APIRouter()



@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}



@router.get("/status")
def status() -> dict[str, Any]:
    from etoro_bot.services.app_settings import arena_state
    from etoro_bot.services.scheduler import (
        market_is_open,
        next_cycle_at,
        open_sessions,
    )

    settings = deps.full_settings()
    breaker = deps.get_breaker()
    repo = deps.get_repo()

    equity_usd = None
    equity_change_day_pct = None
    champion = None
    try:
        series = repo.equity_series(limit=2)  # serve solo la variazione del giorno
        if series:
            equity_usd = series[-1].equity_usd
            if len(series) >= 2 and series[-2].equity_usd:
                equity_change_day_pct = (
                    (series[-1].equity_usd - series[-2].equity_usd)
                    / series[-2].equity_usd * 100
                )
        champ = repo.champion()
        if champ is not None:
            champion = {"id": str(champ.id), "name": champ.name,
                        "generation": champ.generation}
    except Exception:  # DB giù: lo status resta consultabile
        log.warning("stato arena non disponibile", exc_info=True)

    arena = arena_state(repo)
    return {
        "kill_switch_active": kill_switch_active(),
        "circuit_breaker": {
            "tripped": breaker.blocks_openings(),
            "reason": breaker.state.reason,
            "until": breaker.state.cooloff_until,
        },
        "arena": arena,
        "champion": champion,
        "market_open": market_is_open(settings, datetime.now(UTC)),
        "open_sessions": open_sessions(settings, datetime.now(UTC)),
        "next_cycle_at": next_cycle_at(settings),
        # False = in questo processo nessuno eseguirà quel next_cycle_at (lock
        # non acquisito, avvio fallito o scheduler disabilitato).
        "scheduler_active": deps.scheduler_running(),
        "equity_usd": equity_usd,
        "equity_change_day_pct": equity_change_day_pct,
    }
