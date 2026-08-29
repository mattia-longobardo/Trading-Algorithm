"""Decay state machine del campione live (pattern Vibe-Trading strategy_store).

Il campione oggi resta in carica fino al torneo mensile anche se il suo edge
è morto. Qui viene monitorato ogni sera sui dati REALI (equity_snapshots):
Sharpe rolling e max drawdown sulla finestra; una violazione delle soglie per
K giorni consecutivi fa scattare `monitoring` (visibilità a giornale), il
doppio dei giorni fa scattare `decayed`: live spento (`live_enabled=False`)
in attesa del torneo — il capitale reale non resta in mano a un edge decaduto.

ponytail: in `monitoring` il sizing NON viene ridotto (il Mandate è congelato
da file); se serve, aggiungere uno stato REDUCING pilotabile dal codice.
"""

from __future__ import annotations

import logging
import statistics
from typing import Any

logger = logging.getLogger(__name__)

TRADING_DAYS = 252
STATE_KEY = "champion_health"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "window_days": 30,
    "min_sharpe": -0.5,       # sotto: violazione
    "max_drawdown_pct": 15.0,  # sopra: violazione
    "breach_days": 3,          # violazioni consecutive per entrare in monitoring
}


def health_config(settings: dict[str, Any]) -> dict[str, Any]:
    raw = ((settings.get("arena") or {}).get("champion_decay") or {})
    return {**DEFAULTS, **{k: raw[k] for k in raw if k in DEFAULTS}}


def rolling_metrics(equity_values: list[float]) -> dict[str, float | None]:
    """Sharpe annualizzato e max drawdown % sulla serie giornaliera data."""
    out: dict[str, float | None] = {"sharpe": None, "max_drawdown_pct": None}
    if len(equity_values) < 5:
        return out
    returns = [
        b / a - 1.0 for a, b in zip(equity_values[:-1], equity_values[1:]) if a > 0
    ]
    if len(returns) >= 2:
        std = statistics.stdev(returns)
        if std > 0:
            out["sharpe"] = round(
                statistics.mean(returns) / std * (TRADING_DAYS ** 0.5), 2
            )
    peak = equity_values[0]
    max_dd = 0.0
    for value in equity_values:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak * 100.0)
    out["max_drawdown_pct"] = round(max_dd, 2)
    return out


def evaluate_champion_health(deps: Any, settings: dict[str, Any]) -> dict[str, Any] | None:
    """Valuta il campione a fine giornata; applica le transizioni di stato."""
    cfg = health_config(settings)
    if not cfg["enabled"] or deps.repo.champion() is None:
        return None
    points = deps.repo.equity_series(limit=int(cfg["window_days"]))
    values = [float(p.equity_usd) for p in points]
    metrics = rolling_metrics(values)
    if metrics["sharpe"] is None and metrics["max_drawdown_pct"] is None:
        return None  # storico insufficiente

    breached = (
        (metrics["sharpe"] is not None and metrics["sharpe"] < float(cfg["min_sharpe"]))
        or (
            metrics["max_drawdown_pct"] is not None
            and metrics["max_drawdown_pct"] > float(cfg["max_drawdown_pct"])
        )
    )
    stored = deps.repo.get_setting(STATE_KEY) or {}
    breaches = int(stored.get("breaches") or 0) + 1 if breached else 0
    previous = str(stored.get("state") or "active")
    if breaches >= 2 * int(cfg["breach_days"]):
        state = "decayed"
    elif breaches >= int(cfg["breach_days"]):
        state = "monitoring"
    else:
        state = "active"

    if state != previous:
        deps.repo.add_arena_event(
            f"champion_{state}", {"previous": previous, "breaches": breaches, **metrics}
        )
        if state == "decayed":
            from etoro_bot.services.app_settings import set_arena_state

            set_arena_state(deps.repo, source="champion_health", live_enabled=False)
            logger.warning(
                "campione DECADUTO (sharpe=%s, dd=%s%%): live disattivato in "
                "attesa del torneo", metrics["sharpe"], metrics["max_drawdown_pct"],
            )
    deps.repo.set_setting(
        STATE_KEY, {"state": state, "breaches": breaches, **metrics},
        source="champion_health",
    )
    return {"state": state, "breaches": breaches, **metrics}
