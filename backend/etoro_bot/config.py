"""Caricamento configurazione da file YAML + env.

La precedenza runtime completa (app_settings DB > yaml > env) è composta in
services/app_settings.py; qui vivono solo i default da file, senza dipendenze
dal database così che risk/safety restino usabili anche a DB giù.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", Path(__file__).resolve().parents[2] / "config"))


def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass(frozen=True)
class CircuitBreakerRules:
    """Soglie del circuit breaker: sopravvivenza, non prudenza.

    max_daily_loss_pct è un drawdown da bancarotta (un quarto dell'equity in un
    giorno), non un limite di rischio quotidiano; max_consecutive_losses = 0
    disattiva il blocco per perdite in serie (era un freno di aggressività, non
    di sopravvivenza); cooloff_hours è breve perché il bot deve tornare a
    operare, non restare fermo un giorno intero.
    """

    max_daily_loss_pct: float = 25.0
    max_consecutive_losses: int = 0
    cooloff_hours: int = 1


def load_breaker_rules() -> CircuitBreakerRules:
    """Regole del circuit breaker (unico freno di sopravvivenza su file)."""
    raw = _load_yaml("risk_rules.yaml")
    if not raw:
        # Mai più default silenziosi: il file è policy, la sua assenza va vista.
        logger.warning("risk_rules.yaml assente o vuoto: circuit breaker con default di codice")
    cb = raw.get("circuit_breaker", {}) or {}
    known = {k: v for k, v in cb.items() if k in CircuitBreakerRules.__dataclass_fields__}
    return CircuitBreakerRules(**known)


def load_settings() -> dict[str, Any]:
    """Default da config/settings.yaml (senza sovrapposizioni DB)."""
    return _load_yaml("settings.yaml")


def database_url() -> str:
    return os.environ.get(
        "DATABASE_URL", "postgresql+psycopg://bot:bot@localhost:5432/etoro_bot"
    )
