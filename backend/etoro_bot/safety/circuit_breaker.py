"""Circuit breaker di SOPRAVVIVENZA, persistente su file JSON (volume ./state).

Non è un moderatore di aggressività: esiste per impedire l'azzeramento del
conto. Scatta solo su drawdown giornaliero estremo (soglia di bancarotta, non
di prudenza) e blocca le APERTURE — mai le chiusure — per cooloff_hours.
Sopravvive al riavvio e, come il kill switch, non dipende da Postgres.

Il freno "N perdite consecutive" è disattivato di default
(max_consecutive_losses = 0): una serie di stop loss è normale in un'operatività
ad alta frequenza e non minaccia la sopravvivenza. Resta configurabile per chi
lo vuole riaccendere.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from etoro_bot.config import CircuitBreakerRules

logger = logging.getLogger(__name__)

STATE_FILENAME = "circuit_breaker.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class BreakerState:
    tripped: bool = False
    reason: str | None = None
    tripped_at: str | None = None       # ISO 8601 UTC
    cooloff_until: str | None = None    # ISO 8601 UTC
    consecutive_losses: int = 0
    day: str | None = None              # giorno UTC del conteggio perdita giornaliera
    daily_pnl_usd: float = 0.0


def state_path(state_dir: str | Path | None = None) -> Path:
    base = Path(state_dir or os.environ.get("KILL_SWITCH_DIR", "."))
    return base / STATE_FILENAME


class CircuitBreaker:
    """Un'istanza per file di stato: usare `get_breaker()`, non il costruttore.

    Ogni sequenza leggi-modifica-scrivi è protetta da un lock: cicli live,
    chiusure di sessione ed EOD girano in thread diversi sullo stesso stato e
    due `record_closed_trade` sovrapposti perderebbero un trade.
    """

    def __init__(self, rules: CircuitBreakerRules, state_dir: str | Path | None = None):
        self.rules = rules
        self.path = state_path(state_dir)
        self._lock = threading.RLock()
        self.state = self._load()

    def _load(self) -> BreakerState:
        if self.path.exists():
            try:
                return BreakerState(**json.loads(self.path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, TypeError):
                # File corrotto: fail-safe, breaker scattato finché non si interviene
                return BreakerState(
                    tripped=True,
                    reason="stato breaker illeggibile: intervento manuale richiesto",
                    tripped_at=_now().isoformat(),
                    cooloff_until=None,
                )
        return BreakerState()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.state), indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def blocks_openings(self) -> bool:
        """True se le aperture sono bloccate. Le chiusure passano SEMPRE."""
        with self._lock:
            if not self.state.tripped:
                return False
            if self.state.cooloff_until is None:
                return True  # trip permanente (es. stato corrotto) finché non resettato
            if _now() >= datetime.fromisoformat(self.state.cooloff_until):
                self.reset()
                return False
            return True

    def record_closed_trade(
        self, realized_pnl_usd: float, equity_usd: float, count_streak: bool = True
    ) -> None:
        """Aggiorna i contatori a ogni trade chiuso e fa scattare il breaker se serve.

        `count_streak=False` serve alle rettifiche: quando il PnL reale del
        broker arriva dopo la stima, la differenza corregge il drawdown
        giornaliero senza contare come un secondo trade nella serie di perdite.
        """
        with self._lock:
            today = _now().date().isoformat()
            if self.state.day != today:
                self.state.day = today
                self.state.daily_pnl_usd = 0.0
            self.state.daily_pnl_usd += realized_pnl_usd

            if count_streak:
                if realized_pnl_usd < 0:
                    self.state.consecutive_losses += 1
                elif realized_pnl_usd > 0:
                    self.state.consecutive_losses = 0

            streak_limit = int(self.rules.max_consecutive_losses)
            daily_limit = float(self.rules.max_daily_loss_pct)
            if daily_limit > 0 and equity_usd <= 0:
                # niente equity = niente denominatore: il limite giornaliero
                # resta scoperto, e deve vedersi nei log invece di sparire.
                logger.warning(
                    "circuit breaker: equity %.2f USD non utilizzabile, drawdown "
                    "giornaliero NON verificato su questo trade", equity_usd,
                )
            if streak_limit > 0 and self.state.consecutive_losses >= streak_limit:
                self._trip(f"{self.state.consecutive_losses} perdite consecutive")
            elif (
                daily_limit > 0
                and equity_usd > 0
                and -self.state.daily_pnl_usd / equity_usd * 100 >= daily_limit
            ):
                self._trip(
                    f"drawdown giornaliero {self.state.daily_pnl_usd:.2f} USD oltre "
                    f"{daily_limit}% dell'equity: soglia di sopravvivenza"
                )
            else:
                self._save()

    def _trip(self, reason: str) -> None:
        with self._lock:
            self.state.tripped = True
            self.state.reason = reason
            self.state.tripped_at = _now().isoformat()
            self.state.cooloff_until = (
                _now() + timedelta(hours=self.rules.cooloff_hours)
            ).isoformat()
            self._save()

    def reset(self) -> None:
        with self._lock:
            self.state = BreakerState(
                day=self.state.day, daily_pnl_usd=self.state.daily_pnl_usd
            )
            self._save()


# Un solo oggetto CircuitBreaker per file di stato in tutto il processo: API,
# scheduler e cicli live devono condividere lo stesso stato in memoria, o le
# scritture concorrenti si sovrascriverebbero a vicenda.
_instances: dict[Path, CircuitBreaker] = {}
_instances_lock = threading.Lock()


def get_breaker(
    rules: CircuitBreakerRules, state_dir: str | Path | None = None
) -> CircuitBreaker:
    """Istanza condivisa del breaker per quel file di stato."""
    path = state_path(state_dir).resolve()
    with _instances_lock:
        breaker = _instances.get(path)
        if breaker is None:
            breaker = CircuitBreaker(rules, state_dir)
            _instances[path] = breaker
        else:
            breaker.rules = rules  # le soglie possono cambiare da config
        return breaker
