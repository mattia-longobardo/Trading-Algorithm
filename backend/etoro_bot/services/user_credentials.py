"""Cifratura e accesso alle chiavi personali associate all'identità SSO.

La chiave Fernet è DERIVATA dal segreto, non è il segreto: PBKDF2-HMAC-SHA256
con salt d'applicazione e 600k iterazioni, così il segreto di deploy (che è
anche quello di Auth.js) non è direttamente la chiave di cifratura del
database e un segreto corto non si trasforma in una chiave debole.

Le righe già in tabella sono cifrate con la derivazione vecchia (sha256 secco):
la lettura prova in ordine le derivazioni note e, se una riga viene aperta con
una chiave non preferita, la riscrive con quella preferita. Nessuna migrazione
manuale, nessuna credenziale persa.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# Salt fisso d'applicazione: il segreto è unico per deploy e le righe da
# derivare sono poche, quindi non serve un salt per riga (che andrebbe
# comunque persistito accanto al dato).
_PBKDF2_SALT = b"etoro-bot/user-credentials/v2"
_PBKDF2_ITERATIONS = 600_000

_ciphers: dict[tuple[str, str], Fernet] = {}


@dataclass(frozen=True)
class UserKeys:
    etoro_api_key: str = ""
    etoro_user_key: str = ""
    openai_api_key: str = ""

    @property
    def etoro_configured(self) -> bool:
        return bool(self.etoro_api_key and self.etoro_user_key)


def _fernet(secret: str, kdf: str) -> Fernet:
    """Fernet per (segreto, derivazione), con cache: PBKDF2 costa ~0.3s."""
    cached = _ciphers.get((secret, kdf))
    if cached is not None:
        return cached
    if kdf == "pbkdf2":
        raw = hashlib.pbkdf2_hmac(
            "sha256", secret.encode("utf-8"), _PBKDF2_SALT, _PBKDF2_ITERATIONS
        )
    else:  # derivazione storica: sha256 secco del segreto
        raw = hashlib.sha256(secret.encode("utf-8")).digest()
    cipher = Fernet(base64.urlsafe_b64encode(raw))
    _ciphers[(secret, kdf)] = cipher
    return cipher


def _key_chain() -> list[Fernet]:
    """Chiavi da provare in lettura, la preferita per prima.

    1. PBKDF2 del segreto dedicato (TRADING_CREDENTIALS_SECRET, con fallback
       ad AUTH_SECRET perché è ciò che il compose passa oggi);
    2. sha256 dello stesso segreto (righe scritte prima di questa modifica);
    3. sha256 di AUTH_SECRET, se diverso dal segreto dedicato.
    """
    primary = os.environ.get("TRADING_CREDENTIALS_SECRET", "").strip()
    auth_secret = os.environ.get("AUTH_SECRET", "").strip()
    secret = primary or auth_secret
    if not secret:
        raise RuntimeError(
            "TRADING_CREDENTIALS_SECRET non configurato (né AUTH_SECRET come ripiego)"
        )
    chain = [_fernet(secret, "pbkdf2"), _fernet(secret, "sha256")]
    if auth_secret and auth_secret != secret:
        chain.append(_fernet(auth_secret, "sha256"))
    return chain


def _encrypt(value: str) -> str:
    return _key_chain()[0].encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt_with_chain(value: str | None) -> tuple[str, bool]:
    """(valore in chiaro, ri-cifratura necessaria). ("", False) se vuoto."""
    if not value:
        return "", False
    token = value.encode("ascii")
    for index, cipher in enumerate(_key_chain()):
        try:
            return cipher.decrypt(token).decode("utf-8"), index > 0
        except InvalidToken:
            continue
    raise RuntimeError("credenziale cifrata non decifrabile")


def _decrypt(value: str | None) -> str:
    return _decrypt_with_chain(value)[0]


def get_user_keys(repo, user_id: str) -> UserKeys:
    """Chiavi personali dell'identità SSO. Nessun fallback da environment:
    eToro e OpenAI si configurano solo da Impostazioni → Chiavi API personali.

    Le righe aperte con una derivazione superata vengono riscritte con quella
    preferita, in modo trasparente e senza perdere nulla se la riscrittura
    fallisce (il valore in chiaro è già stato recuperato)."""
    row = repo.get_user_credentials(user_id)
    if row is None:
        return UserKeys()
    etoro_api, stale_api = _decrypt_with_chain(row.etoro_api_key_encrypted)
    etoro_user, stale_user = _decrypt_with_chain(row.etoro_user_key_encrypted)
    openai_key, stale_openai = _decrypt_with_chain(row.openai_api_key_encrypted)
    keys = UserKeys(
        etoro_api_key=etoro_api,
        etoro_user_key=etoro_user,
        openai_api_key=openai_key,
    )
    if stale_api or stale_user or stale_openai:
        try:
            repo.set_user_credentials(
                user_id,
                email=row.email,
                display_name=row.display_name,
                etoro_api_key_encrypted=_encrypt(etoro_api) if etoro_api else None,
                etoro_user_key_encrypted=_encrypt(etoro_user) if etoro_user else None,
                openai_api_key_encrypted=_encrypt(openai_key) if openai_key else None,
            )
            logger.info("credenziali di %s ri-cifrate con la chiave corrente", user_id)
        except Exception:
            logger.warning("ri-cifratura credenziali fallita, riprovo alla prossima "
                           "lettura", exc_info=True)
    return keys


def update_user_keys(
    repo,
    user_id: str,
    *,
    email: str | None,
    display_name: str | None,
    etoro_api_key: str | None,
    etoro_user_key: str | None,
    openai_api_key: str | None,
) -> UserKeys:
    current = get_user_keys(repo, user_id)
    updated = UserKeys(
        etoro_api_key=current.etoro_api_key if etoro_api_key is None else etoro_api_key.strip(),
        etoro_user_key=current.etoro_user_key if etoro_user_key is None else etoro_user_key.strip(),
        openai_api_key=current.openai_api_key if openai_api_key is None else openai_api_key.strip(),
    )
    repo.set_user_credentials(
        user_id,
        email=email,
        display_name=display_name,
        etoro_api_key_encrypted=_encrypt(updated.etoro_api_key) if updated.etoro_api_key else None,
        etoro_user_key_encrypted=_encrypt(updated.etoro_user_key) if updated.etoro_user_key else None,
        openai_api_key_encrypted=_encrypt(updated.openai_api_key) if updated.openai_api_key else None,
    )
    return updated
