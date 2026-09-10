"""Grounding gate deterministico sull'output del trader (pattern Vibe-Trading).

`untrusted.py` protegge l'INPUT (prompt injection dalle news); questo modulo
protegge l'OUTPUT: nessuna azione su un simbolo che lo snapshot non contiene,
nessuna motivazione che cita come "prezzo" un numero in contraddizione con lo
snapshot passato nel prompt. Solo verifiche meccanicamente decidibili, zero
LLM: il conflitto specifico viene rimandato al modello (max 2 retry), poi
scatta l'astensione forzata sulle azioni in conflitto.

Le CHIUSURE non passano dal gate simboli: una posizione aperta su un titolo
oggi fuori snapshot deve restare chiudibile.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from etoro_bot.arena.trader import DecisionOutcome, decide

logger = logging.getLogger(__name__)

MAX_GROUNDING_RETRIES = 2
PRICE_TOLERANCE_PCT = 2.0

# Solo affermazioni esplicite di prezzo corrente: "prezzo 123.4", "price: 123",
# "@ 123", "quota 123". Target, stop e percentuali non sono verificabili
# meccanicamente e NON vengono toccati.
_PRICE_CLAIM = re.compile(
    r"(?:prezzo|price|quota|@)\s*[:=]?\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE
)


def check_grounding(
    actions: list[dict[str, Any]], market: dict[str, dict[str, Any]]
) -> list[str]:
    """Conflitti tra azioni proposte e snapshot. Lista vuota = tutto fondato."""
    conflicts: list[str] = []
    for act in actions:
        symbol = act.get("symbol") or ""
        if act.get("action") == "open" and symbol not in market:
            conflicts.append(
                f"{symbol}: simbolo assente dallo snapshot di mercato di questo "
                "ciclo — non puoi aprire posizioni su titoli che non ti sono "
                "stati mostrati"
            )
            continue
        row = market.get(symbol)
        if not row or not row.get("price"):
            continue
        price = float(row["price"])
        for raw in _PRICE_CLAIM.findall(str(act.get("reason") or "")):
            claimed = float(raw.replace(",", "."))
            if claimed <= 0:
                continue
            if abs(claimed - price) / price * 100.0 > PRICE_TOLERANCE_PCT:
                conflicts.append(
                    f"{symbol}: la motivazione cita prezzo {claimed} ma lo "
                    f"snapshot dice {price:.2f}"
                )
    return conflicts


def _flagged_symbols(conflicts: list[str]) -> set[str]:
    return {c.split(":", 1)[0] for c in conflicts}


def grounded_decide(
    llm: Callable[..., str],
    *,
    model: str,
    max_tokens: int,
    prompt: str,
    market: dict[str, dict[str, Any]],
    max_retries: int = MAX_GROUNDING_RETRIES,
) -> DecisionOutcome:
    """`decide` + gate di grounding con retry mirato e astensione forzata."""
    current = prompt
    outcome = DecisionOutcome()
    for attempt in range(max_retries + 1):
        outcome = decide(llm, model=model, max_tokens=max_tokens, prompt=current)
        if outcome.violation:
            return outcome
        conflicts = check_grounding(outcome.actions, market)
        if not conflicts:
            return outcome
        if attempt < max_retries:
            logger.info("grounding: %d conflitti, retry %d", len(conflicts), attempt + 1)
            current = (
                prompt
                + "\n\nCORREZIONE RICHIESTA — la tua risposta precedente conteneva "
                "affermazioni in conflitto con i dati:\n- "
                + "\n- ".join(conflicts)
                + "\nRispondi di nuovo usando SOLO simboli e prezzi presenti nel "
                "mercato mostrato sopra."
            )
            continue
        bad = _flagged_symbols(conflicts)
        kept = [a for a in outcome.actions if (a.get("symbol") or "") not in bad]
        logger.warning(
            "grounding: astensione forzata su %s dopo %d retry", sorted(bad), max_retries
        )
        return DecisionOutcome(
            actions=kept,
            violation="grounding_forced_abstention: " + "; ".join(conflicts),
        )
    return outcome
