"""Dipendenze condivise dalle route: identità, repository, client, settings.

Stanno qui e non in `server.py` perché le usano tutti i router: importarle
dal modulo che li monta sarebbe un ciclo.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Header, HTTPException

from etoro_bot.config import load_breaker_rules
from etoro_bot.db.repo import Repository, make_engine, make_session_factory
from etoro_bot.safety.circuit_breaker import CircuitBreaker
from etoro_bot.safety.circuit_breaker import get_breaker as shared_breaker

log = logging.getLogger("etoro_bot.api")


@dataclass(frozen=True)
class UserIdentity:
    user_id: str
    email: str | None = None
    name: str | None = None


SYSTEM_USER_ID = "system"


# Identità di chi arriva senza header: non è nessuno, quindi non è il
# proprietario. Mai "system", che è riservato alle chiamate interne.
ANONYMOUS_USER_ID = "anonimo"


def current_user(
    x_trading_user_id: str | None = Header(None),
    x_trading_user_email: str | None = Header(None),
    x_trading_user_name: str | None = Header(None),
) -> UserIdentity:
    user_id = (x_trading_user_id or "").strip()
    if user_id == SYSTEM_USER_ID:
        raise HTTPException(403, "identità 'system' riservata alle chiamate interne")
    return UserIdentity(
        user_id=user_id or ANONYMOUS_USER_ID,
        email=x_trading_user_email,
        name=x_trading_user_name,
    )


def require_owner(identity: UserIdentity, action: str) -> None:
    """Solo il proprietario delle chiavi eToro può mutare.

    Finché nessuno ha configurato le chiavi non c'è un proprietario e il
    controllo è un no-op: è il primo utente che si registra a diventarlo.
    Se il proprietario non è *verificabile* (DB giù) si risponde 503: mai
    lasciar passare l'azione per un guasto.
    """
    try:
        owner = get_repo().owner_user_id()
    except Exception as exc:
        log.warning("proprietario non verificabile: %s negata", action, exc_info=True)
        raise HTTPException(503, "proprietario non verificabile: riprova") from exc
    if owner and identity.user_id != owner:
        raise HTTPException(403, f"solo il proprietario può {action}")


def user_keys(identity: UserIdentity):
    from etoro_bot.services.user_credentials import get_user_keys

    return get_user_keys(get_repo(), identity.user_id)


def arena_deps():
    """ArenaDeps di sistema (la logica vive in services/deps.py: la usa anche
    la CLI, che non deve dipendere dal layer API)."""
    from etoro_bot.services.deps import build_arena_deps

    return build_arena_deps(get_repo(), full_settings())



@lru_cache(maxsize=1)
def get_repo() -> Repository:
    return Repository(make_session_factory(make_engine()))


def get_breaker() -> CircuitBreaker:
    """Istanza condivisa: lo stato del breaker è uno solo per tutto il processo."""
    return shared_breaker(load_breaker_rules())


def get_settings_service():
    from etoro_bot.services.app_settings import AppSettingsService

    return AppSettingsService(get_repo())


def full_settings() -> dict[str, Any]:
    """Settings completi: default yaml + override runtime (DB > yaml)."""
    from etoro_bot.services.deps import effective_settings

    return effective_settings(get_repo())


def make_client(identity: UserIdentity):
    from etoro_bot.etoro.client import EtoroClient

    keys = user_keys(identity)
    if not keys.etoro_configured:
        raise HTTPException(422, "Configura le chiavi eToro personali in Impostazioni")
    return EtoroClient(api_key=keys.etoro_api_key, user_key=keys.etoro_user_key)


def kb():
    from etoro_bot.knowledge.kb import KnowledgeBase

    return KnowledgeBase()


def system_etoro_client():
    """Client eToro con le chiavi del proprietario, per i job di sistema."""
    from etoro_bot.services.deps import system_etoro_client

    return system_etoro_client(get_repo())


def system_llm():
    """LLM con la chiave del proprietario, per i job di sistema."""
    from etoro_bot.services.deps import system_llm

    return system_llm(get_repo())


def user_setting_key(prefix: str, user_id: str) -> str:
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}:{digest}"


_LAST_FETCH_FILE = "last_news_fetch"


def user_state_file(identity: UserIdentity, name: str) -> Path:
    digest = hashlib.sha256(identity.user_id.encode("utf-8")).hexdigest()[:24]
    path = Path(os.environ.get("KILL_SWITCH_DIR", ".")) / "users" / digest
    path.mkdir(parents=True, exist_ok=True)
    return path / name


def last_fetch_marker(identity: UserIdentity) -> str | None:
    path = user_state_file(identity, _LAST_FETCH_FILE)
    if path.exists():
        return path.read_text(encoding="utf-8").strip() or None
    return None


def mark_fetch(identity: UserIdentity) -> None:
    path = user_state_file(identity, _LAST_FETCH_FILE)
    path.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")


# Stato dello scheduler: lo scrive `server.py` all'avvio, lo legge /status.
# Uno scheduler morto è invisibile: senza questo flag /status prometterebbe un
# next_cycle_at che nessuno eseguirà.
_scheduler_running = False


def scheduler_running() -> bool:
    return _scheduler_running


def set_scheduler_running(value: bool) -> None:
    global _scheduler_running
    _scheduler_running = value
