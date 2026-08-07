"""Impostazioni utente, credenziali e cambi valuta."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from etoro_bot.api import dependencies as deps
from etoro_bot.api.dependencies import UserIdentity, current_user, require_owner

log = logging.getLogger("etoro_bot.api")

router = APIRouter()


class SettingsBody(BaseModel):
    timezone: str | None = None
    currency: str | None = None


class CredentialsBody(BaseModel):
    etoro_api_key: str | None = None
    etoro_user_key: str | None = None
    openai_api_key: str | None = None



@router.get("/account/credentials")
def account_credentials(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    keys = deps.user_keys(identity)
    return {
        "user_id": identity.user_id,
        "email": identity.email,
        "display_name": identity.name,
        "etoro_api_key_configured": bool(keys.etoro_api_key),
        "etoro_user_key_configured": bool(keys.etoro_user_key),
        "openai_api_key_configured": bool(keys.openai_api_key),
    }



@router.put("/account/credentials")
def put_account_credentials(
    body: CredentialsBody, identity: UserIdentity = Depends(current_user)
) -> dict[str, Any]:
    from etoro_bot.services.user_credentials import update_user_keys

    # Le credenziali sono per-utente, ma `Repository.owner_user_id()` elegge
    # proprietario l'ultimo account che ha salvato entrambe le chiavi eToro:
    # senza questo controllo un estraneo si autoproclama proprietario con una
    # PUT e aggira ogni altro `require_owner` (oltre a scippare l'accesso al
    # legittimo proprietario). Finché nessuno ha configurato le chiavi il
    # controllo è un no-op, quindi il primo utente si registra normalmente.
    require_owner(identity, "cambiare le credenziali")
    keys = update_user_keys(
        deps.get_repo(),
        identity.user_id,
        email=identity.email,
        display_name=identity.name,
        **body.model_dump(),
    )
    return {
        "user_id": identity.user_id,
        "email": identity.email,
        "display_name": identity.name,
        "etoro_api_key_configured": bool(keys.etoro_api_key),
        "etoro_user_key_configured": bool(keys.etoro_user_key),
        "openai_api_key_configured": bool(keys.openai_api_key),
    }



@router.get("/fx/rates")
def fx_rates(refresh: bool = False) -> dict[str, Any]:
    """Tassi USD→valuta + elenco delle valute selezionabili.

    Il journal resta in dollari (eToro ragiona in USD): la conversione è solo
    di presentazione e avviene lato UI con questi tassi.
    """
    from etoro_bot.services.fx import CURRENCY_LABELS, SUPPORTED_CURRENCIES, get_rates

    payload = get_rates(force=refresh)
    payload["currencies"] = [
        {"code": code, "label": CURRENCY_LABELS.get(code, code)}
        for code in SUPPORTED_CURRENCIES
    ]
    return payload



@router.get("/settings")
def get_settings(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    svc = deps.get_settings_service()
    effective = svc.get_effective()
    effective.update(
        {
            "api_keys_configured": deps.user_keys(identity).etoro_configured,
            "openai_configured": bool(deps.user_keys(identity).openai_api_key),
        }
    )
    return effective



@router.put("/settings")
def put_settings(
    body: SettingsBody, identity: UserIdentity = Depends(current_user)
) -> dict[str, Any]:
    from etoro_bot.services.app_settings import SettingsValidationError

    require_owner(identity, "cambiare le impostazioni")
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        return deps.get_settings_service().update(
            changes,
            source="api",
            etoro_configured=deps.user_keys(identity).etoro_configured,
        )
    except SettingsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc



@router.get("/settings/audit")
def settings_audit() -> dict[str, Any]:
    rows = deps.get_repo().settings_audit()
    return {
        "entries": [
            {
                "id": str(r.id),
                "changed_at": r.changed_at.isoformat(),
                "key": r.key,
                "old_value": r.old_value,
                "new_value": r.new_value,
                "source": r.source,
            }
            for r in rows
        ]
    }
