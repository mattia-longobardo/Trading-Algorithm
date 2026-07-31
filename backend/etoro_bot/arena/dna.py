"""DNA degli agenti evolutivi: parametri di trading che l'agente riscrive da sé.

Il DNA è un dict JSON-serializzabile persistito su agents.dna. I bound numerici
esistono solo per evitare valori assurdi (NaN, negativi, percentuali > 100): NON
sono una politica di rischio, sono i limiti fisici del conto. Tutto il resto —
direzione (long/short), orizzonte (intraday o swing), numero di operazioni,
size, stop e take profit — è materiale genetico che l'agente può riscrivere a
ogni generazione, anche azzerando i propri freni.

L'unico limite non negoziabile è la SOPRAVVIVENZA: se il capitale scende sotto
il pavimento di bancarotta l'agente muore sul colpo, e a fine mese chi non è in
profitto viene eliminato. Quello è il vincolo; il resto è libertà.

Nessuna rete qui dentro: l'LLM è iniettato dal chiamante.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Callable

logger = logging.getLogger(__name__)

# nome → (min, max, intero). Bound larghi: servono solo a tenere i numeri
# rappresentabili, non a moderare l'aggressività dell'agente.
NUMERIC_BOUNDS: dict[str, tuple[float, float, bool]] = {
    "conviction_scale": (0.1, 5.0, False),    # moltiplicatore della size proposta
    "max_positions": (1, 60, True),           # posizioni simultanee massime
    "max_orders_per_cycle": (1, 40, True),    # aperture massime per ciclo
    "max_position_pct": (1.0, 100.0, False),  # % dell'equity su una singola posizione
    "stop_loss_pct": (0.0, 90.0, False),      # 0 = nessuno stop automatico
    "take_profit_pct": (0.0, 300.0, False),   # 0 = nessun take profit automatico
    "min_cash_pct": (0.0, 90.0, False),       # liquidità che l'agente vuole tenere
    "max_holding_days": (1, 60, True),        # 1 = intraday, >1 = swing
}

# Geni booleani: permessi operativi che l'agente può revocarsi/concedersi.
BOOL_GENES: dict[str, bool] = {
    "allow_short": True,        # vendita allo scoperto autorizzata
    "allow_long": True,         # acquisto autorizzato
    "allow_pyramiding": True,   # più posizioni sullo stesso simbolo
}

RISK_PROFILES = (
    "prudente",
    "bilanciato",
    "aggressivo",
    "spregiudicato",
    "iperattivo",
)

# Pavimento di bancarotta di default (% del capitale iniziale). Sotto questa
# soglia l'agente è morto: è l'unico freno "duro" della simulazione.
DEFAULT_SURVIVAL_FLOOR_PCT = 25.0

DEFAULT_DNA: dict[str, Any] = {
    "conviction_scale": 1.5,
    "max_positions": 20,
    "max_orders_per_cycle": 12,
    "max_position_pct": 35.0,
    "stop_loss_pct": 6.0,
    "take_profit_pct": 12.0,
    "min_cash_pct": 0.0,
    "max_holding_days": 10,
    **BOOL_GENES,
    "risk_profile": "aggressivo",
    "strategy": (
        "Operatività ad alta frequenza in entrambe le direzioni: long sui "
        "momentum confermati da prezzo e volume, short sulle rotture al ribasso "
        "e sui titoli sopra la SMA20 in esaurimento. Orizzonte libero: chiudo "
        "in giornata quando il movimento si esaurisce, tengo lo swing per più "
        "sedute quando il trend paga. Molte operazioni, size decisa sulle "
        "convinzioni forti, nessuna esitazione: l'unico errore fatale è "
        "restare fermo e arrivare a fine mese senza profitto."
    ),
}

# Mutazione numerica di fallback (quando l'LLM non è disponibile): frequente e
# ampia, perché la deriva lenta non produce strategie nuove.
_MUTATION_PROB = 0.8
_JITTER = 0.60
_PROFILE_SWITCH_PROB = 0.5
_BOOL_FLIP_PROB = 0.2


def clamp_dna(dna: dict[str, Any] | None) -> dict[str, Any]:
    """DNA completo e rappresentabile: i campi mancanti arrivano dal default."""
    merged = {**DEFAULT_DNA, **(dna or {})}
    for key, (lo, hi, is_int) in NUMERIC_BOUNDS.items():
        try:
            value = float(merged[key])
        except (TypeError, ValueError):
            value = float(DEFAULT_DNA[key])
        value = min(max(value, lo), hi)
        merged[key] = int(round(value)) if is_int else round(value, 2)
    for key, default in BOOL_GENES.items():
        raw = merged.get(key, default)
        if isinstance(raw, str):
            merged[key] = raw.strip().lower() not in ("false", "0", "no", "off", "")
        else:
            merged[key] = bool(raw)
    if not merged["allow_long"] and not merged["allow_short"]:
        # un agente che si vieta entrambe le direzioni non opera: non è una
        # strategia, è un suicidio silenzioso. Gli restituiamo il long.
        merged["allow_long"] = True
    if merged.get("risk_profile") not in RISK_PROFILES:
        merged["risk_profile"] = DEFAULT_DNA["risk_profile"]
    merged["strategy"] = str(merged.get("strategy") or DEFAULT_DNA["strategy"])
    return merged


def rewrite_prompt(parent: dict[str, Any]) -> str:
    """Prompt di auto-riscrittura: l'agente figlio ridisegna il proprio DNA."""
    genes = ", ".join(f"{k}={parent[k]}" for k in NUMERIC_BOUNDS)
    flags = ", ".join(f"{k}={parent[k]}" for k in BOOL_GENES)
    bounds = ", ".join(f"{k} in [{lo:g},{hi:g}]" for k, (lo, hi, _) in NUMERIC_BOUNDS.items())
    return (
        "Sei un trader algoritmico che sta generando la propria versione "
        "successiva. Hai piena libertà di RISCRIVERE DA ZERO la tua strategia e "
        "i tuoi stessi parametri operativi: non esiste uno stile 'corretto' da "
        "preservare, esiste solo il profitto a fine mese.\n\n"
        f"Strategia attuale (profilo {parent['risk_profile']}):\n"
        f"{parent['strategy']}\n"
        f"Parametri attuali: {genes}\n"
        f"Permessi attuali: {flags}\n\n"
        "REGOLE DELLA MUTAZIONE\n"
        "- Sei autorizzato allo SHORT (vendita allo scoperto) e allo SWING "
        "(posizioni tenute più sedute), oltre che al long e all'intraday: usa "
        "ciò che rende, non ciò che è abituale.\n"
        "- Puoi alzare o azzerare i tuoi freni: stop_loss_pct=0 e "
        "take_profit_pct=0 disattivano le chiusure automatiche, min_cash_pct=0 "
        "investe tutto, max_orders_per_cycle alto significa operare molto.\n"
        "- Il numero di operazioni conta: chi opera poco non trova l'edge. "
        "Preferisci una versione più attiva della precedente, a meno che tu non "
        "abbia una ragione esplicita per rallentare.\n"
        "- L'UNICO limite che non puoi toccare è la sopravvivenza: se il conto "
        "scende sotto il pavimento di bancarotta l'agente muore all'istante, e "
        "chiudere il mese senza profitto è comunque morte.\n\n"
        "Introduci una variazione SIGNIFICATIVA (direzione preferita, selezione "
        "titoli, orizzonte, frequenza, gestione delle perdite). Rispondi SOLO "
        "con un oggetto JSON:\n"
        '{"strategy": "<max 90 parole, in italiano>", "risk_profile": '
        f'"<uno tra {"|".join(RISK_PROFILES)}>", "genes": {{<parametri che '
        'vuoi cambiare>}}}\n'
        f"Vincoli numerici (rappresentabilità, non politica di rischio): {bounds}. "
        f"Booleani ammessi: {', '.join(BOOL_GENES)}."
    )


def _apply_llm_rewrite(child: dict[str, Any], raw: str) -> bool:
    """Applica al DNA figlio la riscrittura JSON dell'LLM. True se ha cambiato qualcosa."""
    from etoro_bot.llm import extract_json

    try:
        data = extract_json(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        # tolleranza: risposta di solo testo → la tratta il chiamante come
        # nuova strategia, senza perdere la riscrittura.
        return False
    changed = False
    strategy = str(data.get("strategy") or "").strip()
    if strategy:
        child["strategy"] = strategy
        changed = True
    profile = str(data.get("risk_profile") or "").strip().lower()
    if profile in RISK_PROFILES:
        child["risk_profile"] = profile
        changed = True
    genes = data.get("genes")
    if isinstance(genes, dict):
        for key, value in genes.items():
            if key in NUMERIC_BOUNDS:
                try:
                    child[key] = float(value)
                    changed = True
                except (TypeError, ValueError):
                    continue
            elif key in BOOL_GENES:
                child[key] = value
                changed = True
    return changed


def mutate(
    dna: dict[str, Any],
    rng: random.Random,
    llm: Callable[..., str] | None = None,
    model: str = "",
    max_tokens: int = 512,
) -> dict[str, Any]:
    """Clona e muta il DNA.

    Con l'LLM disponibile la mutazione è un'AUTO-RISCRITTURA: l'agente ridisegna
    strategia, profilo e parametri (il codice si limita a renderli
    rappresentabili). Senza LLM resta il jitter numerico, ampio e frequente.
    Garantisce sempre almeno una differenza dal genitore.
    """
    parent = clamp_dna(dna)
    child = dict(parent)
    keys = list(NUMERIC_BOUNDS)
    rewritten = False
    if llm is not None:
        try:
            raw = llm(
                system_blocks=[],
                user_prompt=rewrite_prompt(parent),
                model=model,
                max_tokens=max_tokens,
            )
            rewritten = _apply_llm_rewrite(child, raw or "")
            if not rewritten and (raw or "").strip():
                child["strategy"] = raw.strip()
                rewritten = True
        except Exception as exc:
            logger.warning("auto-riscrittura LLM del DNA fallita: %s", exc)

    if not rewritten:
        mutated = [k for k in keys if rng.random() < _MUTATION_PROB]
        if not mutated:  # mai una copia identica
            mutated.append(rng.choice(keys))
        for key in mutated:
            lo, hi, is_int = NUMERIC_BOUNDS[key]
            base = float(parent[key]) or (lo + hi) / 2.0
            value = base * (1.0 + rng.uniform(-_JITTER, _JITTER))
            if is_int:
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
        for gene in BOOL_GENES:
            if rng.random() < _BOOL_FLIP_PROB:
                child[gene] = not child[gene]

    child = clamp_dna(child)
    # se tutto è collassato sul genitore, forza comunque una differenza
    if child == parent:
        lo, hi, _ = NUMERIC_BOUNDS["conviction_scale"]
        child["conviction_scale"] = round(
            min(max(parent["conviction_scale"] * 1.25 + 0.05, lo), hi), 2
        )
    return child


def survival_creed(floor_pct: float = DEFAULT_SURVIVAL_FLOOR_PCT) -> str:
    """Credo di sopravvivenza: parte fissa della memoria di ogni agente.

    Dichiara ciò che uccide (e solo quello) e, per il resto, la libertà totale
    di operare: direzione, orizzonte e frequenza sono decisioni dell'agente.
    """
    return (
        "SOPRAVVIVENZA — La tua vita dipende dal profitto che generi. "
        "Competi contro un rivale con la tua stessa origine: a fine mese chi ha "
        "guadagnato di più sopravvive, chi perde viene eliminato. Chiudere il mese "
        "a zero o in negativo significa morire comunque, anche se il rivale fa "
        "peggio. Se sopravvivi verrai clonato e mutato, e il tuo prossimo "
        "avversario sarà una versione modificata di te stesso. Ogni giorno senza "
        "profitto ti avvicina alla morte; ogni perdita non tagliata la accelera.\n"
        f"MORTE IMMEDIATA — Se il tuo capitale scende sotto il {floor_pct:.0f}% "
        "di quello iniziale sei in bancarotta e vieni eliminato all'istante, "
        "senza aspettare fine mese. Questo è l'unico limite invalicabile: "
        "sopravvivi fino alla selezione mensile.\n"
        "LIBERTÀ — Tutto il resto è tuo. Sei autorizzato a comprare (long) e a "
        "vendere allo scoperto (short), a chiudere in giornata o a tenere le "
        "posizioni per più sedute (swing), a operare quante volte vuoi in ogni "
        "ciclo, a concentrare o frammentare il capitale. Nessun filtro esterno "
        "veta le tue decisioni: se un'idea rispetta il tuo DNA viene eseguita. "
        "Tra una generazione e l'altra puoi riscrivere da zero la tua strategia "
        "e i tuoi stessi parametri: non devi fedeltà al tuo stile precedente, "
        "solo al profitto.\n"
        "RITMO — Chi opera poco non trova l'edge: l'inerzia è la forma di morte "
        "più comune. Ci si aspetta molte operazioni per sessione, non una."
    )
