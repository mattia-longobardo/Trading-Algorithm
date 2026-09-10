"""Pipeline decisionale multi-stadio (pattern TradingAgents, senza LangGraph).

Quattro stadi sequenziali con cap deterministici — mai loop aperti:

  1. Analyst (modello quick): report strutturato sui candidati (tecnica +
     news dalla KB + memoria trade), fan-in su stato tipizzato;
  2. Bull vs Bear (quick, UN round ciascuno): tesi contrapposte sul report;
  3. Trader (modello deep): decide con rating/confidence, passa dal
     grounding gate;
  4. Risk judge (deep): può solo ATTENUARE o porre VETO alle aperture,
     mai aumentarle; le chiusure non si toccano.

Ogni stadio viene persistito dal chiamante via callback `journal(stage,
payload)`: l'engine sim la mappa su arena_events, il live su decisions
(lo schema Decision.stage prevedeva questa pipeline da sempre).

Costi: 5 chiamate LLM per agente per ciclo invece di 1 — per questo i
candidati sono pre-filtrati (`select_candidates`) e i due tier di modello
(llm.model_quick / llm.model_deep) sono la leva principale.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from etoro_bot.arena.grounding import grounded_decide
from etoro_bot.arena.trader import DecisionOutcome

logger = logging.getLogger(__name__)

DEFAULT_CANDIDATES = 12
JUDGE_VERDICTS = {"approve", "reduce", "veto"}
REDUCE_FACTOR = 0.5


def pipeline_enabled(settings: dict[str, Any]) -> bool:
    return bool(((settings.get("arena") or {}).get("pipeline") or {}).get("enabled", False))


def llm_models(settings: dict[str, Any]) -> tuple[str, str]:
    """(quick, deep): due tier di modello, fallback sul modello unico."""
    cfg = settings.get("llm") or {}
    base = str(cfg.get("model", "gpt-5.6-terra"))
    return str(cfg.get("model_quick", base)), str(cfg.get("model_deep", base))


def select_candidates(
    market: dict[str, dict[str, Any]], held: set[str], limit: int = DEFAULT_CANDIDATES
) -> list[str]:
    """Simboli con più segnale: movimento, volume anomalo, estremi RSI.

    I titoli già in portafoglio entrano sempre (vanno gestiti comunque).
    """

    def score(row: dict[str, Any]) -> float:
        s = abs(float(row.get("day_pct") or 0.0))
        volume_rel = row.get("volume_rel")
        if volume_rel is not None:
            s += abs(float(volume_rel) - 1.0) * 2.0
        rsi = row.get("rsi14")
        if rsi is not None:
            s += abs(float(rsi) - 50.0) / 10.0
        kronos = row.get("kronos_rank")
        if kronos is not None:  # percentile 0-100: gli estremi pesano
            s += abs(float(kronos) - 50.0) / 10.0
        return s

    ranked = sorted(
        (sym for sym in market if sym not in held),
        key=lambda sym: score(market[sym]),
        reverse=True,
    )
    chosen = [s for s in market if s in held] + ranked
    return chosen[:max(limit, len([s for s in market if s in held]))]


def _kb_context(candidates: list[str]) -> str:
    """News e memoria trade dalla KB per i candidati (canale ampio, non 220 char)."""
    try:
        from etoro_bot.knowledge.kb import KnowledgeBase
        from etoro_bot.knowledge.untrusted import wrap_untrusted

        kb = KnowledgeBase()
        if not kb.available:
            return ""
        blocks: list[str] = []
        for symbol in candidates:
            hits = kb.search_news(f"{symbol} outlook notizie", tickers=[symbol], limit=2)
            for h in hits:
                text = str(h.get("text") or "")[:280]
                if text:
                    blocks.append(f"[{symbol}] {text}")
        lessons = kb.search_trade_memory(" ".join(candidates[:6]), limit=3)
        for les in lessons:
            text = str(les.get("text") or "")[:200]
            if text:
                blocks.append(f"[esito passato] {text}")
        if not blocks:
            return ""
        return wrap_untrusted("\n".join(blocks), label="knowledge base")
    except Exception as exc:
        logger.debug("pipeline: contesto KB non disponibile: %s", exc)
        return ""


def _call(llm: Callable[..., str], model: str, max_tokens: int, prompt: str) -> str:
    try:
        return llm(
            system_blocks=[], user_prompt=prompt, model=model, max_tokens=max_tokens
        ).strip()
    except Exception as exc:
        logger.warning("pipeline: chiamata LLM fallita: %s", exc)
        return ""


def _judge_actions(
    raw: str, actions: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Applica i verdetti del risk judge alle APERTURE: approve/reduce/veto."""
    from etoro_bot.llm import extract_json

    try:
        data = extract_json(raw)
    except ValueError:
        return actions, []  # giudice muto = nessuna modifica (mai inventare)
    verdicts: dict[str, dict[str, Any]] = {}
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper()
        verdict = str(item.get("verdict") or "").lower()
        if symbol and verdict in JUDGE_VERDICTS:
            verdicts[symbol] = {"verdict": verdict,
                                "reason": str(item.get("reason") or "")}
    kept: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    for act in actions:
        if act.get("action") != "open":
            kept.append(act)  # le chiusure non si toccano mai
            continue
        v = verdicts.get(act.get("symbol") or "")
        if v is None or v["verdict"] == "approve":
            kept.append(act)
            continue
        applied.append({"symbol": act.get("symbol"), **v})
        if v["verdict"] == "veto":
            continue
        reduced = dict(act)
        if reduced.get("size_pct"):
            reduced["size_pct"] = round(float(reduced["size_pct"]) * REDUCE_FACTOR, 2)
        reduced["reason"] = f"{reduced.get('reason', '')} [risk: size ridotta]".strip()
        kept.append(reduced)
    return kept, applied


def run_pipeline(
    llm: Callable[..., str],
    *,
    settings: dict[str, Any],
    max_tokens: int,
    trader_prompt: str,
    market: dict[str, dict[str, Any]],
    held: set[str],
    journal: Callable[[str, dict[str, Any]], None],
) -> DecisionOutcome:
    """Analyst → Bull/Bear → Trader (grounded) → Risk judge."""
    quick, deep = llm_models(settings)
    candidates = select_candidates(
        market, held,
        int(((settings.get("arena") or {}).get("pipeline") or {})
            .get("candidates", DEFAULT_CANDIDATES)),
    )
    views = "\n".join(str(market[s].get("view", s)) for s in candidates if s in market)
    kb_block = _kb_context(candidates)

    # 1. Analyst (quick): report tipizzato, niente chat history (fan-in)
    analyst_report = _call(
        llm, quick, max_tokens,
        "Sei l'analista di un desk di trading. Per OGNI titolo qui sotto scrivi "
        "una riga: `SIMBOLO: quadro tecnico; notizie rilevanti; rischio "
        "principale`. Sintetico e fattuale, nessun consiglio operativo.\n\n"
        f"TITOLI:\n{views}\n"
        + (f"\nCONTESTO DALLA KNOWLEDGE BASE:\n{kb_block}\n" if kb_block else ""),
    )
    journal("analyst", {"report": analyst_report[:4000], "candidates": candidates})

    # 2. Bull vs Bear (quick), UN round ciascuno: cap deterministico
    bull = _call(
        llm, quick, max_tokens,
        "Sei l'avvocato RIALZISTA. Dal report, argomenta le 2-3 migliori "
        "occasioni LONG (o nessuna, se non ce ne sono). Concludi con: cosa "
        "smentirebbe la tua tesi.\n\nREPORT:\n" + analyst_report,
    )
    bear = _call(
        llm, quick, max_tokens,
        "Sei l'avvocato RIBASSISTA. Dal report e dalla tesi del rialzista, "
        "argomenta i 2-3 rischi maggiori o le migliori occasioni SHORT. "
        "Concludi con: cosa smentirebbe la tua tesi.\n\n"
        f"REPORT:\n{analyst_report}\n\nTESI RIALZISTA:\n{bull}",
    )
    journal("debate", {"bull": bull[:3000], "bear": bear[:3000]})

    # 3. Trader (deep) col grounding gate
    full_prompt = (
        trader_prompt
        + f"\n\nREPORT ANALISTA:\n{analyst_report}"
        + f"\n\nDIBATTITO:\nRIALZISTA:\n{bull}\nRIBASSISTA:\n{bear}"
    )
    outcome = grounded_decide(
        llm, model=deep, max_tokens=max_tokens, prompt=full_prompt, market=market
    )
    if not outcome.actions:
        return outcome

    # 4. Risk judge (deep): solo attenuazione o veto, mai più esposizione
    import json as _json

    judge_raw = _call(
        llm, deep, max_tokens,
        "Sei il risk manager. Valuta SOLO le aperture proposte: per ciascuna "
        'rispondi con un array JSON [{"symbol": "...", "verdict": '
        '"approve|reduce|veto", "reason": "..."}]. Non puoi aumentare '
        "l'esposizione, solo approvare, dimezzare (reduce) o bloccare (veto). "
        "Riserva il veto ai rischi concreti.\n\n"
        f"APERTURE PROPOSTE:\n{_json.dumps(outcome.actions, ensure_ascii=False)}\n\n"
        f"DIBATTITO:\nRIALZISTA:\n{bull}\nRIBASSISTA:\n{bear}",
    )
    actions, applied = _judge_actions(judge_raw, outcome.actions)
    journal("risk", {"verdicts": applied, "raw": judge_raw[:2000]})
    return DecisionOutcome(actions=actions, violation=outcome.violation)
