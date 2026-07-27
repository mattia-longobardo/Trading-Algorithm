"""Caricamento configurazione da file YAML + env.

La precedenza runtime completa (app_settings DB > yaml > env) è composta in
services/app_settings.py; qui vivono solo i default da file, senza dipendenze
dal database così che risk/safety restino usabili anche a DB giù.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", Path(__file__).resolve().parents[2] / "config"))


def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass(frozen=True)
class CircuitBreakerRules:
    max_daily_loss_pct: float = 2.0
    max_consecutive_losses: int = 4
    cooloff_hours: int = 24


def load_breaker_rules() -> CircuitBreakerRules:
    """Regole del circuit breaker (unico freno di rischio configurato su file)."""
    raw = _load_yaml("risk_rules.yaml")
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
