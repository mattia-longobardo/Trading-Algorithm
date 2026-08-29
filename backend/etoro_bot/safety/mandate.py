"""Mandate: policy di rischio pre-trade, congelata e fail-closed.

Pattern preso da Vibe-Trading: i bound del DNA restano "rappresentabilità"
(cosa un agente può esprimere), il mandate è la policy di rischio (cosa il
sistema lascia passare). Vive in config/risk_rules.yaml, viene letto una volta
al boot e l'LLM non ha alcun path di scrittura. File assente o malformato =
HALTED: nessun default permissivo.

Ogni apertura (sim e live) passa da `check_open` DOPO l'enforce contabile e
PRIMA dell'esecuzione; un rifiuto produce un codice motivo a giornale, mai
un'eccezione. Le chiusure non passano mai dal mandate: chiudere è sempre lecito.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache

from etoro_bot import config

logger = logging.getLogger(__name__)

VALID_STATES = {"ACTIVE", "REDUCING", "HALTED"}


@dataclass(frozen=True)
class Mandate:
    # Default fail-closed: senza configurazione esplicita non si apre niente.
    trading_state: str = "HALTED"          # ACTIVE | REDUCING | HALTED
    max_notional_per_order_usd: float = 0.0
    max_total_exposure_pct: float = 0.0    # % dell'equity
    max_symbol_exposure_pct: float = 0.0   # % dell'equity, per simbolo
    max_orders_per_day: int = 0
    min_stop_loss_pct: float = 0.0         # floor per il DNA (0 = nessun floor)
    denylist: tuple[str, ...] = ()

    @classmethod
    def unlimited(cls) -> Mandate:
        """Mandate senza limiti: SOLO per test e replay storici, mai in produzione."""
        return cls(
            trading_state="ACTIVE",
            max_notional_per_order_usd=float("inf"),
            max_total_exposure_pct=float("inf"),
            max_symbol_exposure_pct=float("inf"),
            max_orders_per_day=10**9,
            min_stop_loss_pct=0.0,
        )


@lru_cache(maxsize=1)
def load_mandate() -> Mandate:
    """Mandate da config/risk_rules.yaml, letto una volta per processo."""
    raw = config._load_yaml("risk_rules.yaml").get("mandate")
    if not raw:
        logger.warning(
            "risk_rules.yaml senza sezione mandate: trading HALTED (fail-closed)"
        )
        return Mandate()
    state = str(raw.get("trading_state", "HALTED")).upper()
    if state not in VALID_STATES:
        logger.warning("mandate: trading_state %r sconosciuto: HALTED", state)
        state = "HALTED"
    denylist = tuple(str(s).upper() for s in raw.get("denylist") or ())
    known = {
        k: v
        for k, v in raw.items()
        if k in Mandate.__dataclass_fields__ and k not in ("trading_state", "denylist")
    }
    return Mandate(trading_state=state, denylist=denylist, **known)


def check_open(
    mandate: Mandate,
    *,
    symbol: str,
    amount_usd: float,
    equity_usd: float,
    total_exposure_usd: float,
    symbol_exposure_usd: float,
    opens_today: int,
    stop_loss_pct: float,
) -> str | None:
    """Valida un'apertura contro il mandate. None = ok, altrimenti codice motivo.

    Ordine fisso dei controlli: la prima violazione vince.
    """
    if mandate.trading_state == "HALTED":
        return "halted"
    if mandate.trading_state == "REDUCING":
        return "reducing"
    if symbol.upper() in mandate.denylist:
        return "denylist"
    if amount_usd > mandate.max_notional_per_order_usd:
        return "max_notional"
    if total_exposure_usd + amount_usd > equity_usd * mandate.max_total_exposure_pct / 100.0:
        return "max_total_exposure"
    if symbol_exposure_usd + amount_usd > equity_usd * mandate.max_symbol_exposure_pct / 100.0:
        return "max_symbol_exposure"
    if opens_today >= mandate.max_orders_per_day:
        return "max_orders_per_day"
    if stop_loss_pct < mandate.min_stop_loss_pct:
        return "stop_loss_floor"
    return None
