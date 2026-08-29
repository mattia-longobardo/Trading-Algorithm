"""Snapshot di mercato per i trader dell'arena e per il live.

Universo = watchlist + scoperta dinamica dalle news, su DUE mercati (Europa e
USA): lo snapshot include solo i titoli la cui borsa è aperta adesso. Per ogni
titolo: prezzo corrente eToro, variazione del giorno, 5 sedute, distanza dalla
SMA20 e un estratto della memoria ticker (news recenti). Tutto il calcolo è
puro; le chiamate di rete passano dal client iniettato.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

TYPE_IDS = {5: "stock", 6: "etf"}
CANDLES = 21  # ~1 mese di sedute: bastano per SMA20 e 5 giorni
MAX_SYMBOLS = 80  # default se arena.max_symbols non è configurato

# Le candele sono GIORNALIERE: dentro la seduta non cambiano, e con cicli da 5'
# rifarne una per titolo a ogni ciclo brucerebbe da sola metà del budget del
# pool market-data (una chiamata per strumento). Restano in cache per un'ora;
# i prezzi correnti invece si rileggono sempre.
_CANDLES_TTL_S = 3600.0
_candles_cache: dict[int, tuple[float, list[float]]] = {}
_candles_lock = threading.Lock()


def clear_candles_cache() -> None:
    """Svuota la cache delle candele (test, o cambio di universo forzato)."""
    with _candles_lock:
        _candles_cache.clear()


def _closes(client: Any, instrument_id: int, now_ts: float | None = None) -> list[float]:
    """Chiusure giornaliere dello strumento, con cache a TTL (vedi sopra)."""
    now_ts = time.monotonic() if now_ts is None else now_ts
    with _candles_lock:
        cached = _candles_cache.get(instrument_id)
        if cached is not None and now_ts - cached[0] < _CANDLES_TTL_S:
            return cached[1]
    try:
        candles = client.get_candles(instrument_id, interval="OneDay", count=CANDLES)
        closes = [float(c["close"]) for c in candles if c.get("close")]
    except Exception:
        return [] if cached is None else cached[1]
    with _candles_lock:
        _candles_cache[instrument_id] = (now_ts, closes)
    return closes

# Suffissi eToro delle borse europee (symbolFull "ENEL.MI", "SAP.DE", ...).
# ".US" e ".RTH" sono varianti di quotazione americane; senza punto = USA.
EU_SUFFIXES = frozenset(
    {"MI", "DE", "PA", "NV", "MC", "L", "BR", "LS", "ST", "OL", "HE", "CO", "VI", "ZU"}
)

MARKET_LABELS = {"europe": "EU", "usa": "USA"}


def market_of_symbol(symbol: str) -> str:
    """Mercato di un simbolo eToro: 'europe' per i suffissi di borse UE, altrimenti 'usa'."""
    symbol = str(symbol).upper()
    if "." in symbol and symbol.rsplit(".", 1)[1] in EU_SUFFIXES:
        return "europe"
    return "usa"


def metrics_from_closes(price: float, closes: list[float]) -> dict[str, float | None]:
    """day/week % e distanza dalla SMA20 dal prezzo corrente e dalle chiusure."""
    out: dict[str, float | None] = {"day_pct": None, "week_pct": None, "sma20_dist_pct": None}
    if not price or not closes:
        return out
    if closes[-1]:
        out["day_pct"] = round((price / closes[-1] - 1.0) * 100.0, 2)
    if len(closes) >= 6 and closes[-6]:
        out["week_pct"] = round((price / closes[-6] - 1.0) * 100.0, 2)
    if len(closes) >= 20:
        sma20 = sum(closes[-20:]) / 20.0
        if sma20:
            out["sma20_dist_pct"] = round((price / sma20 - 1.0) * 100.0, 2)
    return out


def _memory_hint(symbol: str) -> str:
    """Estratto di news per il titolo, sanificato e marcato come non fidato.

    Il testo arriva da feed esterni e finisce in un prompt che muove denaro
    reale: viene neutralizzato (marcatori di ruolo, imperativi di iniezione) e
    racchiuso nei delimitatori che il prompt dichiara come sola informazione.
    """
    try:
        from etoro_bot.knowledge.ticker_memory import memory_context
        from etoro_bot.knowledge.untrusted import wrap_untrusted_inline

        text = " ".join((memory_context(symbol) or "").split())
        return wrap_untrusted_inline(text[:220])
    except Exception:
        return ""


def _fmt(value: float | None) -> str:
    return f"{value:+.1f}%" if value is not None else "n/d"


def build_snapshot(
    client: Any,
    settings: dict[str, Any],
    now: datetime | None = None,
    only_open: bool = True,
) -> dict[str, dict[str, Any]]:
    """symbol → {instrument_id, price, market, day_pct, week_pct, sma20_dist_pct, view}.

    Con only_open=True (default) restano solo i titoli delle sessioni aperte
    adesso: non si apre né si chiude ciò che non è quotabile. only_open=False
    serve agli EOD (prezzo di chiusura appena dopo la campanella).
    """
    from etoro_bot.services.scheduler import open_sessions

    now = now or datetime.now(timezone.utc)
    tradable = set(open_sessions(settings, now))
    try:
        from etoro_bot.services.universe import effective_universe

        universe = [str(s).upper() for s in effective_universe(settings)]
    except Exception as exc:
        logger.warning("arena: universo dinamico non disponibile: %s", exc)
        universe = [str(s).upper() for s in settings.get("watchlist") or []]
    if only_open:
        universe = [s for s in universe if market_of_symbol(s) in tradable]
    universe = universe[: int((settings.get("arena") or {}).get("max_symbols", MAX_SYMBOLS))]

    catalogue: dict[str, dict] = {}
    for type_id in TYPE_IDS:
        try:
            for row in client.get_instruments_by_type(type_id):
                symbol = str(row.get("symbolFull") or "").upper()
                if symbol and symbol in universe:
                    catalogue.setdefault(symbol, row)
        except Exception as exc:
            logger.warning("arena: catalogo tipo %s non disponibile: %s", type_id, exc)

    ids = [int(row["instrumentID"]) for row in catalogue.values()]
    try:
        rates = client.get_rates(ids)
    except Exception as exc:
        logger.warning("arena: prezzi non disponibili: %s", exc)
        rates = {}

    snapshot: dict[str, dict[str, Any]] = {}
    for symbol, row in catalogue.items():
        instrument_id = int(row["instrumentID"])
        rate = rates.get(instrument_id) or {}
        price = rate.get("lastExecution") or rate.get("bid") or rate.get("ask")
        if not price:
            continue
        m = metrics_from_closes(float(price), _closes(client, instrument_id))
        hint = _memory_hint(symbol)
        market = market_of_symbol(symbol)
        view = (
            f"[{MARKET_LABELS.get(market, market)}] {symbol} {float(price):.2f} | "
            f"oggi {_fmt(m['day_pct'])} | 5g {_fmt(m['week_pct'])} | "
            f"vs SMA20 {_fmt(m['sma20_dist_pct'])}"
        )
        if hint:
            view += f" | {hint}"
        snapshot[symbol] = {
            "instrument_id": instrument_id,
            "price": float(price),
            "market": market,
            **m,
            "view": view,
        }
    return snapshot
