"""Trader LLM dell'arena: una chiamata per ciclo, azioni JSON, vincoli in codice.

L'LLM propone; il codice dispone. Ogni vincolo del DNA (numero posizioni,
size, riserva di liquidità) è applicato deterministicamente da enforce():
un output LLM fuori dai limiti viene ridimensionato o scartato, mai eseguito.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from etoro_bot.llm import extract_json

logger = logging.getLogger(__name__)

MIN_ORDER_USD = 10.0  # sotto: polvere, scartato


def parse_actions(raw: str) -> list[dict[str, Any]]:
    """Normalizza l'output LLM in azioni {action, symbol, size_pct, reason}."""
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
        actions.append(
            {
                "action": action,
                "symbol": symbol,
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Applica i vincoli del DNA alle azioni proposte.

    Ritorna (aperture, chiusure). Aperture: {symbol, instrument_id, amount_usd,
    reason}. Chiusure: {symbol, reason}. Tutto ciò che viola un vincolo viene
    scartato in silenzio (l'agente ne vedrà l'effetto nel proprio conto).
    """
    opens: list[dict[str, Any]] = []
    closes: list[dict[str, Any]] = []
    planned = set()
    cash_left = max(cash, 0.0)
    reserve = max(equity, 0.0) * float(dna["min_cash_pct"]) / 100.0
    max_amount = max(equity, 0.0) * float(dna["max_position_pct"]) / 100.0
    max_new = int(dna["max_orders_per_cycle"])
    max_positions = int(dna["max_positions"])
    scale = float(dna["conviction_scale"])

    for act in actions:
        symbol = act["symbol"]
        if act["action"] == "close":
            if symbol in held_symbols:
                closes.append({"symbol": symbol, "reason": act["reason"]})
            continue
        # apertura
        if len(opens) >= max_new:
            continue
        if symbol in held_symbols or symbol in planned or symbol not in market:
            continue
        if len(held_symbols) + len(planned) + 1 > max_positions:
            continue
        size_pct = act["size_pct"] if act["size_pct"] and act["size_pct"] > 0 else 10.0
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
                "amount_usd": amount,
                "reason": act["reason"],
            }
        )
        planned.add(symbol)
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
    """Prompt del ciclo di trading: identità, DNA, memoria, conto, mercato."""
    positions_text = "\n".join(positions_view) or "(nessuna posizione aperta)"
    market_text = "\n".join(market_view) or "(nessun dato di mercato)"
    if int(dna["max_holding_days"]) <= 1:
        holding_text = (
            "Tutte le posizioni vengono chiuse d'ufficio a fine sessione "
            "(day trading puro)."
        )
    else:
        holding_text = (
            f"Puoi tenere una posizione al massimo {dna['max_holding_days']} "
            "giorni di borsa: alla chiusura di sessione del giorno limite viene "
            "liquidata d'ufficio."
        )
    return (
        f"Sei {name}, un trader autonomo (long-only, stock/ETF, USD).\n"
        f"{survival}\n\n"
        f"LA TUA STRATEGIA (profilo {dna['risk_profile']}): {dna['strategy']}\n"
        f"I tuoi vincoli (applicati dal codice, non sprecare ordini oltre): "
        f"max {dna['max_positions']} posizioni, max {dna['max_orders_per_cycle']} "
        f"aperture per ciclo, max {dna['max_position_pct']}% dell'equity per "
        f"posizione, riserva di liquidità {dna['min_cash_pct']}%. Stop loss "
        f"{dna['stop_loss_pct']}% e take profit {dna['take_profit_pct']}% sono "
        f"automatici. {holding_text}\n\n"
        f"LA TUA MEMORIA:\n{memory}\n\n"
        f"IL TUO CONTO: cash {cash:.2f} USD, equity {equity:.2f} USD.\n"
        f"POSIZIONI APERTE:\n{positions_text}\n\n"
        f"MERCATO ADESSO:\n{market_text}\n\n"
        "Decidi le azioni di questo ciclo. Rispondi SOLO con un array JSON "
        '[{"action": "open|close|hold", "symbol": "...", "size_pct": <float % del '
        'cash da impiegare>, "reason": "..."}]. Usa "hold" se non fai nulla.'
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
