"""DNA degli agenti evolutivi: parametri di trading mutabili tra generazioni.

Il DNA è un dict JSON-serializzabile persistito su agents.dna. I campi numerici
hanno bound rigidi (clamp deterministico); i campi testuali (strategy,
risk_profile) sono il "fenotipo qualitativo" e mutano via LLM quando
disponibile. Nessuna rete qui dentro: l'LLM è iniettato dal chiamante.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Callable

logger = logging.getLogger(__name__)

# nome → (min, max, intero)
NUMERIC_BOUNDS: dict[str, tuple[float, float, bool]] = {
    "conviction_scale": (0.2, 2.0, False),   # moltiplicatore della size proposta
    "max_positions": (1, 8, True),           # posizioni simultanee massime
    "max_orders_per_cycle": (1, 4, True),    # aperture massime per ciclo
    "max_position_pct": (5.0, 50.0, False),  # % dell'equity su una singola posizione
    "stop_loss_pct": (0.5, 8.0, False),      # chiusura automatica in perdita
    "take_profit_pct": (0.5, 15.0, False),   # chiusura automatica in profitto
    "min_cash_pct": (0.0, 50.0, False),      # liquidità che l'agente vuole conservare
    "max_holding_days": (1, 5, True),        # giorni di borsa max per posizione
}

RISK_PROFILES = ("prudente", "bilanciato", "aggressivo", "spregiudicato")

DEFAULT_DNA: dict[str, Any] = {
    "conviction_scale": 1.0,
    "max_positions": 4,
    "max_orders_per_cycle": 2,
    "max_position_pct": 25.0,
    "stop_loss_pct": 2.0,
    "take_profit_pct": 4.0,
    "min_cash_pct": 10.0,
    "max_holding_days": 1,
    "risk_profile": "bilanciato",
    "strategy": (
        "Day trading momentum su titoli liquidi: entra dove prezzo e volume "
        "confermano la direzione del giorno, taglia subito le perdite, lascia "
        "correre i profitti fino al take profit."
    ),
}

# Probabilità di mutazione per singolo campo numerico e ampiezza del jitter.
_MUTATION_PROB = 0.6
_JITTER = 0.30
_PROFILE_SWITCH_PROB = 0.3


def clamp_dna(dna: dict[str, Any] | None) -> dict[str, Any]:
    """DNA completo e nei bound: i campi mancanti arrivano dal default."""
    merged = {**DEFAULT_DNA, **(dna or {})}
    for key, (lo, hi, is_int) in NUMERIC_BOUNDS.items():
        try:
            value = float(merged[key])
        except (TypeError, ValueError):
            value = float(DEFAULT_DNA[key])
        value = min(max(value, lo), hi)
        merged[key] = int(round(value)) if is_int else round(value, 2)
    if merged.get("risk_profile") not in RISK_PROFILES:
        merged["risk_profile"] = DEFAULT_DNA["risk_profile"]
    merged["strategy"] = str(merged.get("strategy") or DEFAULT_DNA["strategy"])
    return merged


def mutate(
    dna: dict[str, Any],
    rng: random.Random,
    llm: Callable[..., str] | None = None,
    model: str = "",
    max_tokens: int = 512,
) -> dict[str, Any]:
    """Clona e muta il DNA: jitter sui numerici, riscrittura LLM della strategia.

    Garantisce almeno una differenza numerica dal genitore, così i due agenti
    della nuova generazione non sono mai identici.
    """
    parent = clamp_dna(dna)
    child = dict(parent)
    keys = list(NUMERIC_BOUNDS)
    mutated: list[str] = []
    for key in keys:
        if rng.random() < _MUTATION_PROB:
            mutated.append(key)
    if not mutated:  # mai una copia identica
        mutated.append(rng.choice(keys))
    for key in mutated:
        lo, hi, is_int = NUMERIC_BOUNDS[key]
        value = float(parent[key]) * (1.0 + rng.uniform(-_JITTER, _JITTER))
        if is_int:
            # per gli interi il jitter relativo può non bastare: forza ±1
            value = round(value)
            if value == parent[key]:
                value = parent[key] + rng.choice((-1, 1))
            child[key] = int(min(max(value, lo), hi))
        else:
            child[key] = round(min(max(value, lo), hi), 2)
    if rng.random() < _PROFILE_SWITCH_PROB:
        child["risk_profile"] = rng.choice(
            [p for p in RISK_PROFILES if p != parent["risk_profile"]]
        )
    if llm is not None:
        try:
            prompt = (
                "Sei il motore di mutazione di un trader algoritmico LLM.\n"
                f"Strategia attuale (profilo {child['risk_profile']}):\n"
                f"{parent['strategy']}\n\n"
                "Riscrivi la strategia introducendo UNA variazione significativa "
                "(stile di ingresso, selezione dei titoli, orizzonte temporale, "
                "gestione intraday...). Massimo 60 parole, long-only su stock/ETF; "
                f"l'orizzonte massimo è {child['max_holding_days']} giorno/i di "
                "borsa per posizione. Rispondi solo con il testo della strategia."
            )
            text = llm(
                system_blocks=[], user_prompt=prompt, model=model, max_tokens=max_tokens
            ).strip()
            if text:
                child["strategy"] = text
        except Exception as exc:
            logger.warning("mutazione LLM della strategia fallita: %s", exc)
    # se qualche differenza numerica si è persa nel clamp, forza un campo float
    if all(child[k] == parent[k] for k in keys):
        lo, hi, _ = NUMERIC_BOUNDS["conviction_scale"]
        child["conviction_scale"] = round(
            min(max(parent["conviction_scale"] * 1.15 + 0.01, lo), hi), 2
        )
    return child


def survival_creed() -> str:
    """Credo di sopravvivenza: parte fissa della memoria di ogni agente."""
    return (
        "SOPRAVVIVENZA — La tua vita dipende dal profitto che generi. "
        "Competi contro un rivale con la tua stessa origine: a fine mese chi ha "
        "guadagnato di più sopravvive, chi perde viene eliminato. Chiudere il mese "
        "a zero o in negativo significa morire comunque, anche se il rivale fa "
        "peggio. Se sopravvivi verrai clonato e mutato, e il tuo prossimo "
        "avversario sarà una versione modificata di te stesso. Ogni giorno senza "
        "profitto ti avvicina alla morte; ogni perdita non tagliata la accelera."
    )
