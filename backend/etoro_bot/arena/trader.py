"""Trader LLM dell'arena: una chiamata per ciclo, azioni JSON, esecuzione fedele.

Filosofia: l'LLM decide, il codice esegue. Qui NON esistono filtri di opinione
(niente soglie di confidence, niente cooldown, niente veto direzionali): restano
solo i limiti contabili — quanto cash c'è davvero, quanto vale una posizione
secondo il DNA dell'agente, e l'ordine minimo eseguibile dal broker.

Long e short sono entrambi autorizzati; l'orizzonte (intraday o swing) è
deciso dal DNA dell'agente, non da questo modulo.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from etoro_bot.llm import extract_json

logger = logging.getLogger(__name__)

MIN_ORDER_USD = 10.0  # sotto: polvere, il broker non la esegue

LONG = "long"
SHORT = "short"

# Sinonimi accettati nell'output LLM per la direzione dell'ordine.
_SHORT_WORDS = {"short", "sell", "vendi", "ribasso", "bear", "down", "corto"}
_LONG_WORDS = {"long", "buy", "compra", "rialzo", "bull", "up", "lungo"}


def normalize_direction(value: Any, default: str = LONG) -> str:
    """'short'/'sell'/'ribasso' → short; qualsiasi altra cosa → long."""
    text = str(value or "").strip().lower()
    if text in _SHORT_WORDS:
        return SHORT
    if text in _LONG_WORDS:
        return LONG
    return default


def parse_actions(raw: str) -> list[dict[str, Any]]:
    """Normalizza l'output LLM in {action, symbol, direction, size_pct, reason}."""
    try:
        data = extract_json(raw)
    except ValueError:
        logger.warning("trader: risposta LLM senza JSON valido")
        return []
    actions: list[dict[str, Any]] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").lower()
        symbol = str(item.get("symbol") or "").upper()
        if action not in ("open", "close") or not symbol:
            continue
        size_pct: float | None
        try:
            size_pct = float(item["size_pct"])
        except (KeyError, TypeError, ValueError):
            size_pct = None
        raw_dir = item.get("direction", item.get("side"))
        actions.append(
            {
                "action": action,
                "symbol": symbol,
                # sulle chiusure la direzione è un filtro opzionale: None = tutte
                "direction": (
                    normalize_direction(raw_dir)
                    if action == "open"
                    else (normalize_direction(raw_dir, "") or None)
                ),
                "size_pct": size_pct,
                "reason": str(item.get("reason") or ""),
            }
        )
    return actions


def enforce(
    actions: list[dict[str, Any]],
    *,
    dna: dict[str, Any],
    cash: float,
    equity: float,
    held_symbols: set[str],
    market: dict[str, dict[str, Any]],
    held_count: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Traduce le azioni proposte in ordini eseguibili.

    Scarta solo ciò che NON è eseguibile: simbolo fuori mercato, direzione che
    l'agente si è vietato nel DNA, importo sotto il minimo d'ordine, cash
    insufficiente. I tetti (posizioni, aperture per ciclo, size, riserva di
    liquidità) vengono dal DNA dell'agente e sono suoi da alzare o azzerare.

    Ritorna (aperture, chiusure). Aperture: {symbol, instrument_id, direction,
    amount_usd, reason}. Chiusure: {symbol, direction|None, reason}.
    """
    opens: list[dict[str, Any]] = []
    closes: list[dict[str, Any]] = []
    cash_left = max(cash, 0.0)
    reserve = max(equity, 0.0) * float(dna["min_cash_pct"]) / 100.0
    max_amount = max(equity, 0.0) * float(dna["max_position_pct"]) / 100.0
    max_new = int(dna["max_orders_per_cycle"])
    max_positions = int(dna["max_positions"])
    scale = float(dna["conviction_scale"])
    allow_short = bool(dna.get("allow_short", True))
    allow_long = bool(dna.get("allow_long", True))
    pyramiding = bool(dna.get("allow_pyramiding", True))
    open_positions = len(held_symbols) if held_count is None else int(held_count)
    planned: list[str] = []

    # una posizione si chiude UNA volta sola: due `close` sullo stesso simbolo
    # (output plausibile del modello) manderebbero due ordini al broker e
    # conterebbero la stessa perdita due volte nel drawdown del breaker.
    closed_keys: set[tuple[str, str | None]] = set()

    for act in actions:
        symbol = act["symbol"]
        if act["action"] == "close":
            key = (symbol, act.get("direction"))
            if symbol in held_symbols and key not in closed_keys:
                closed_keys.add(key)
                closes.append(
                    {
                        "symbol": symbol,
                        "direction": act.get("direction"),
                        "reason": act["reason"],
                    }
                )
            continue
        # apertura
        direction = normalize_direction(act.get("direction"))
        if len(opens) >= max_new:
            continue
        if symbol not in market:
            continue
        if direction == SHORT and not allow_short:
            continue
        if direction == LONG and not allow_long:
            continue
        if not pyramiding and (symbol in held_symbols or symbol in planned):
            continue
        if open_positions + len(planned) + 1 > max_positions:
            continue
        size_pct = act["size_pct"] if act["size_pct"] and act["size_pct"] > 0 else 25.0
        # size_pct è riferita al cash che l'agente vedeva nel prompt (quello
        # iniziale del ciclo); la disponibilità residua fa solo da tetto.
        requested = max(cash, 0.0) * min(size_pct, 100.0) / 100.0 * scale
        spendable = max(cash_left - reserve, 0.0)
        amount = round(min(requested, max_amount, spendable), 2)
        if amount < MIN_ORDER_USD:
            continue
        opens.append(
            {
                "symbol": symbol,
                "instrument_id": int(market[symbol]["instrument_id"]),
                "direction": direction,
                "amount_usd": amount,
                "reason": act["reason"],
            }
        )
        planned.append(symbol)
        cash_left -= amount
    return opens, closes


def build_prompt(
    *,
    name: str,
    dna: dict[str, Any],
    memory: str,
    survival: str,
    cash: float,
    equity: float,
    positions_view: list[str],
    market_view: list[str],
) -> str:
    """Prompt del ciclo di trading: identità, libertà, DNA, memoria, conto, mercato."""
    from etoro_bot.knowledge.untrusted import INLINE_PREAMBLE

    positions_text = "\n".join(positions_view) or "(nessuna posizione aperta)"
    market_text = "\n".join(market_view) or "(nessun dato di mercato)"
    holding_days = int(dna["max_holding_days"])
    if holding_days <= 1:
        holding_text = (
            "Il tuo DNA tiene le posizioni al massimo una seduta: quello che "
            "apri oggi viene liquidato alla campanella (intraday puro)."
        )
    else:
        holding_text = (
            f"Il tuo DNA autorizza lo SWING fino a {holding_days} sedute per "
            "posizione: puoi tenere aperto oltre la campanella e lasciar "
            "correre il movimento su più giorni."
        )
    directions = []
    if dna.get("allow_long", True):
        directions.append('"long" (compri, guadagni se sale)')
    if dna.get("allow_short", True):
        directions.append('"short" (vendi allo scoperto, guadagni se scende)')
    directions_text = " e ".join(directions) or '"long"'
    auto_bits = []
    if float(dna["stop_loss_pct"]) > 0:
        auto_bits.append(f"stop loss automatico {dna['stop_loss_pct']}%")
    if float(dna["take_profit_pct"]) > 0:
        auto_bits.append(f"take profit automatico {dna['take_profit_pct']}%")
    auto_text = (
        ", ".join(auto_bits)
        if auto_bits
        else "nessuna chiusura automatica: le posizioni restano finché non le chiudi tu"
    )
    pyramiding_text = (
        "puoi aprire più posizioni sullo stesso simbolo"
        if dna.get("allow_pyramiding", True)
        else "una sola posizione per simbolo"
    )
    return (
        f"Sei {name}, un trader autonomo su stock ed ETF (conto in USD).\n"
        f"{survival}\n\n"
        f"LA TUA STRATEGIA (profilo {dna['risk_profile']}): {dna['strategy']}\n\n"
        "COSA SEI AUTORIZZATO A FARE\n"
        f"- Direzioni ammesse: {directions_text}. Lo short è pienamente "
        "autorizzato: se pensi che un titolo scenda, vendilo allo scoperto "
        "invece di stare a guardare.\n"
        f"- {holding_text}\n"
        f"- Fino a {dna['max_orders_per_cycle']} aperture in QUESTO ciclo e "
        f"fino a {dna['max_positions']} posizioni contemporanee; "
        f"{pyramiding_text}.\n"
        f"- Size: fino al {dna['max_position_pct']}% dell'equity per posizione, "
        f"riserva di liquidità {dna['min_cash_pct']}% (il resto è impiegabile).\n"
        f"- Gestione automatica: {auto_text}.\n"
        "- Nessun altro filtro ti ferma: non esistono soglie di confidence, "
        "cooldown o veti esterni. Ciò che proponi entro questi numeri viene "
        "eseguito.\n"
        "- Ci si aspetta ATTIVITÀ: usa più ordini per ciclo se vedi più "
        "occasioni. Restare fermi non protegge nulla, ti avvicina solo alla "
        "selezione mensile senza profitto.\n\n"
        f"LA TUA MEMORIA:\n{memory}\n\n"
        f"IL TUO CONTO: cash {cash:.2f} USD, equity {equity:.2f} USD.\n"
        f"POSIZIONI APERTE:\n{positions_text}\n\n"
        f"MERCATO ADESSO ({INLINE_PREAMBLE}):\n{market_text}\n\n"
        "Decidi le azioni di questo ciclo. Rispondi SOLO con un array JSON "
        '[{"action": "open|close", "symbol": "...", "direction": "long|short", '
        '"size_pct": <float % del cash da impiegare>, "reason": "..."}]. '
        'Sulle chiusure "direction" è opzionale (omessa = chiudi tutto quel '
        "simbolo). Array vuoto [] se davvero non vuoi fare nulla."
    )


def decide(
    llm: Callable[..., str],
    *,
    model: str,
    max_tokens: int,
    prompt: str,
) -> list[dict[str, Any]]:
    """Chiama l'LLM e ritorna le azioni normalizzate; [] su qualunque errore."""
    try:
        raw = llm(system_blocks=[], user_prompt=prompt, model=model, max_tokens=max_tokens)
    except Exception as exc:
        logger.warning("trader: chiamata LLM fallita: %s", exc)
        return []
    return parse_actions(raw)
