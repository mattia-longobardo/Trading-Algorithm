"""Riflessione outcome-graded (pattern TradingAgents Reflector).

Ogni trade chiuso viene valutato su ritorno realizzato E alpha vs benchmark
(SPY): battere il mercato, non solo guadagnare, è il metro. Gli esiti finiscono
in un registro append-only per agente (markdown, pattern TradingMemoryLog:
niente embeddings, retrieval deterministico = ultime N righe) e alimentano il
prompt di riflessione serale, così il diario dell'agente impara da numeri
verificati invece che dalla propria prosa.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SPY_ID_CACHE: int | None = None


def _state_dir() -> Path:
    return Path(os.environ.get("STATE_DIR", "state"))


def spy_closes(client: Any) -> dict[date, float]:
    """Chiusure giornaliere SPY per data (cache candele di market.py)."""
    global _SPY_ID_CACHE
    if client is None:
        return {}
    try:
        if _SPY_ID_CACHE is None:
            for row in client.get_instruments_by_type(6):  # 6 = ETF
                if str(row.get("symbolFull") or "").upper() == "SPY":
                    _SPY_ID_CACHE = int(row["instrumentID"])
                    break
        if _SPY_ID_CACHE is None:
            return {}
        from etoro_bot.arena.market import _candles

        out: dict[date, float] = {}
        for c in _candles(client, _SPY_ID_CACHE):
            try:
                day = datetime.fromisoformat(str(c["fromDate"])).date()
                out[day] = float(c["close"])
            except (KeyError, TypeError, ValueError):
                continue
        return out
    except Exception as exc:
        logger.debug("reflection: SPY non disponibile: %s", exc)
        return {}


def _close_at_or_before(closes: dict[date, float], day: date) -> float | None:
    if not closes:
        return None
    candidates = [d for d in closes if d <= day]
    return closes[max(candidates)] if candidates else None


def trade_outcomes(trades: list[Any], spy: dict[date, float]) -> list[dict[str, Any]]:
    """Esiti valutati dei trade: ritorno % e alpha % vs SPY sulla stessa finestra."""
    outcomes: list[dict[str, Any]] = []
    for t in trades:
        amount = float(t.amount_usd or 0.0)
        if amount <= 0:
            continue
        ret_pct = round(float(t.pnl_usd) / amount * 100.0, 2)
        alpha_pct: float | None = None
        opened = getattr(t, "opened_at", None)
        closed = getattr(t, "closed_at", None)
        if opened is not None and closed is not None:
            start = _close_at_or_before(spy, opened.date())
            end = _close_at_or_before(spy, closed.date())
            if start and end:
                bench_ret = (end / start - 1.0) * 100.0
                # per gli short il benchmark gioca al contrario
                if getattr(t, "direction", "long") == "short":
                    bench_ret = -bench_ret
                alpha_pct = round(ret_pct - bench_ret, 2)
        outcomes.append(
            {
                "symbol": t.symbol,
                "direction": getattr(t, "direction", "long"),
                "ret_pct": ret_pct,
                "alpha_pct": alpha_pct,
                "close_reason": getattr(t, "close_reason", ""),
                "closed_on": closed.date().isoformat() if closed is not None else "",
            }
        )
    return outcomes


def _ledger_path(agent_id: Any) -> Path:
    return _state_dir() / "reflections" / f"{agent_id}.md"


def append_outcomes(agent_id: Any, outcomes: list[dict[str, Any]]) -> None:
    """Registro append-only: una riga per trade valutato."""
    if not outcomes:
        return
    path = _ledger_path(agent_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for o in outcomes:
                alpha = f"{o['alpha_pct']:+.2f}%" if o["alpha_pct"] is not None else "n/d"
                fh.write(
                    f"- [{o['closed_on']} | {o['symbol']} {o['direction']}] "
                    f"ritorno {o['ret_pct']:+.2f}% | alpha vs SPY {alpha} "
                    f"({o['close_reason']})\n"
                )
    except OSError as exc:
        logger.warning("reflection: registro non scrivibile: %s", exc)


def index_trade_memory(agent_name: str, outcomes: list[dict[str, Any]]) -> None:
    """Indicizza gli esiti valutati nella KB (`trade_memory`, mai usata finora).

    La retrieval (`search_trade_memory`) avviene nello stadio Analyst della
    pipeline decisionale. Degrada a no-op senza ArangoDB.
    """
    if not outcomes:
        return
    try:
        from etoro_bot.knowledge.kb import KnowledgeBase

        kb = KnowledgeBase()
        if not kb.available:
            return
        for o in outcomes:
            alpha = f"{o['alpha_pct']:+.2f}%" if o["alpha_pct"] is not None else "n/d"
            text = (
                f"{o['symbol']} {o['direction']}: ritorno {o['ret_pct']:+.2f}%, "
                f"alpha vs SPY {alpha}, chiusura: {o['close_reason']}"
            )
            kb.add_trade_memory(
                text,
                {
                    "agent": agent_name,
                    "symbol": o["symbol"],
                    "direction": o["direction"],
                    "ret_pct": o["ret_pct"],
                    "alpha_pct": o["alpha_pct"],
                    "closed_on": o["closed_on"],
                    "ts": datetime.now(tz=None).timestamp(),
                },
            )
    except Exception as exc:
        logger.debug("reflection: indicizzazione trade_memory saltata: %s", exc)


def past_context(agent_id: Any, limit: int = 8) -> str:
    """Ultime N righe del registro (retrieval deterministico, niente embeddings)."""
    path = _ledger_path(agent_id)
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-limit:])


def outcomes_view(outcomes: list[dict[str, Any]]) -> str:
    """Blocco prompt con gli esiti valutati del giorno."""
    if not outcomes:
        return "(nessun trade chiuso oggi)"
    rows = []
    for o in outcomes:
        alpha = f"{o['alpha_pct']:+.2f}%" if o["alpha_pct"] is not None else "n/d"
        rows.append(
            f"- {o['symbol']} {o['direction']}: ritorno {o['ret_pct']:+.2f}%, "
            f"alpha vs SPY {alpha} ({o['close_reason']})"
        )
    return "\n".join(rows)
