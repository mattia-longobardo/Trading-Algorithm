"""Costruzione delle dipendenze dell'arena per i job di sistema.

Sta qui e non in `api/server.py` perché non è roba del layer HTTP: la usano lo
scheduler dentro l'API e la CLI (`python -m etoro_bot.main`). Prima la CLI
importava una funzione privata del server, cioè dipendeva dal web server per
girare un ciclo di allenamento.

"Di sistema" = con le chiavi del proprietario: i job non hanno una richiesta
HTTP da cui ricavare l'identità. Ogni pezzo mancante (chiavi eToro, chiave
OpenAI) degrada a None e i consumatori lo gestiscono da soli.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def effective_settings(repo) -> dict[str, Any]:
    """Settings completi: default yaml + override runtime dal DB.

    `get_effective()` restituisce SOLO le chiavi gestite dal DB: per watchlist,
    news_feeds, universe_discovery, knowledge e llm serve la base yaml.
    """
    from etoro_bot.config import load_settings
    from etoro_bot.services.app_settings import AppSettingsService

    return {**load_settings(), **AppSettingsService(repo).get_effective()}


def system_etoro_client(repo):
    """Client eToro con le chiavi dell'account proprietario. None se non
    configurate: la pipeline news degrada senza refresh dell'universo."""
    try:
        from etoro_bot.etoro.client import EtoroClient
        from etoro_bot.services.user_credentials import get_user_keys

        keys = get_user_keys(repo, repo.owner_user_id() or "system")
        if not keys.etoro_api_key or not keys.etoro_user_key:
            return None
        return EtoroClient(api_key=keys.etoro_api_key, user_key=keys.etoro_user_key)
    except Exception:
        logger.warning("client eToro di sistema non disponibile", exc_info=True)
        return None


def system_llm(repo):
    """`call_llm` con la chiave OpenAI del proprietario. None se non
    configurata: i consumatori degradano da soli (fallback regex/headline)."""
    try:
        from functools import partial

        from etoro_bot.llm import call_llm, make_openai_client
        from etoro_bot.services.user_credentials import get_user_keys

        keys = get_user_keys(repo, repo.owner_user_id() or "system")
        if not keys.openai_api_key:
            return None
        return partial(call_llm, client=make_openai_client(keys.openai_api_key))
    except Exception:
        logger.warning("LLM di sistema non disponibile", exc_info=True)
        return None


def build_arena_deps(repo, settings: dict[str, Any] | None = None):
    """ArenaDeps di sistema: chiavi del proprietario, settings completi."""
    from etoro_bot.arena.engine import ArenaDeps

    settings = effective_settings(repo) if settings is None else settings
    llm_cfg = settings.get("llm") or {}
    return ArenaDeps(
        repo=repo,
        client=system_etoro_client(repo),
        settings=settings,
        llm=system_llm(repo),
        model=str(llm_cfg.get("model", "gpt-5.6-terra")),
        max_tokens=int(llm_cfg.get("max_tokens", 2048)),
    )
