"""Impostazioni runtime: precedenza app_settings DB > settings.yaml > default.

Non esiste più l'ambiente demo: il conto eToro è uno solo (reale) e viene
toccato SOLO quando il trading live è attivo. L'attivazione del live ha i suoi
guardrail (check_live_activation), enforced lato backend: la UI non è mai
l'unica difesa.

Due impostazioni sono di sola presentazione e non toccano l'esecuzione:
`timezone` (lo scheduling è sempre in UTC) e `currency` (i conti restano in USD).
"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from etoro_bot.config import load_breaker_rules, load_settings
from etoro_bot.db.repo import Repository
from etoro_bot.safety import CircuitBreaker, kill_switch_active
from etoro_bot.services.fx import SUPPORTED_CURRENCIES

# Default hardcoded: ultimo fallback se anche settings.yaml manca.
_DEFAULTS: dict[str, Any] = {
    "timezone": "Europe/Rome",
    "currency": "USD",
}

# Chiavi modificabili via update(); lo stato dell'arena (pausa, live) ha
# endpoint dedicati e vive nella chiave "arena" di app_settings.
_MUTABLE_KEYS = ("timezone", "currency")


class SettingsValidationError(Exception):
    """Cambio impostazioni respinto dai guardrail: l'API risponde 422."""

    status_code = 422

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def arena_state(repo: Repository) -> dict[str, Any]:
    """Stato dell'arena da app_settings, con default sicuri."""
    stored = repo.get_setting("arena")
    state = stored if isinstance(stored, dict) else {}
    return {
        "month": state.get("month"),
        "paused": bool(state.get("paused", False)),
        "live_enabled": bool(state.get("live_enabled", False)),
    }


def set_arena_state(repo: Repository, source: str = "api", **changes: Any) -> dict[str, Any]:
    stored = repo.get_setting("arena")
    state = dict(stored) if isinstance(stored, dict) else {}
    state.update(changes)
    repo.set_setting("arena", state, source=source)
    return arena_state(repo)


def check_live_activation(repo: Repository, *, etoro_configured: bool | None) -> None:
    """Guardrail per accendere il trading live (denaro REALE)."""
    if not etoro_configured:
        raise SettingsValidationError(
            "chiavi eToro mancanti: configurale in Impostazioni prima di andare live"
        )
    if repo.champion() is None:
        raise SettingsValidationError(
            "nessun campione disponibile: il live richiede il vincitore di almeno "
            "un mese di allenamento"
        )
    if kill_switch_active():
        raise SettingsValidationError("kill switch attivo: impossibile andare live")
    breaker = CircuitBreaker(load_breaker_rules())
    if breaker.blocks_openings():
        raise SettingsValidationError("circuit breaker scattato: impossibile andare live")


class AppSettingsService:
    def __init__(self, repo: Repository):
        self._repo = repo

    # --- lettura ------------------------------------------------------------
    def get_effective(self) -> dict[str, Any]:
        """Impostazioni effettive: app_settings (DB) > settings.yaml > default."""
        yaml_cfg = load_settings()
        effective: dict[str, Any] = {}
        for key, default in _DEFAULTS.items():
            db_value = self._repo.get_setting(key)
            effective[key] = db_value if db_value is not None else yaml_cfg.get(key, default)
        # configurazione arena (sola lettura da yaml) + stato runtime
        effective["arena"] = {**(yaml_cfg.get("arena") or {}), **arena_state(self._repo)}
        return effective

    # --- scrittura con guardrail --------------------------------------------
    def update(
        self,
        changes: dict[str, Any],
        source: str = "api",
        *,
        etoro_configured: bool | None = None,  # firma stabile per l'API
    ) -> dict[str, Any]:
        changes = dict(changes)
        changes.pop("confirmation", None)

        unknown = set(changes) - set(_MUTABLE_KEYS)
        if unknown:
            raise SettingsValidationError(
                f"chiavi non modificabili o sconosciute: {', '.join(sorted(unknown))}"
            )

        current = self.get_effective()
        target = {**current, **changes}
        self._validate_values(target)

        for key, value in changes.items():
            if value != current[key]:
                self._repo.set_setting(key, value, source=source)
        return self.get_effective()

    # --- helper -------------------------------------------------------------
    @staticmethod
    def _validate_values(target: dict[str, Any]) -> None:
        try:
            ZoneInfo(str(target["timezone"]))
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise SettingsValidationError("timezone IANA non valida") from exc
        if target["currency"] not in SUPPORTED_CURRENCIES:
            raise SettingsValidationError(
                f"valuta non supportata: scegli fra {', '.join(SUPPORTED_CURRENCIES)}"
            )
