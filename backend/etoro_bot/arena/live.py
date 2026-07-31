"""Trading live: il DNA del campione (vincitore dell'ultimo mese) muove denaro
REALE sul conto eToro, con la stessa logica di ciclo dell'arena.

Unici freni operativi, e sono freni di SOPRAVVIVENZA, non di analisi: kill
switch (blocca tutto) e circuit breaker di drawdown estremo (blocca solo le
aperture, mai le chiusure). Nessun altro filtro veta le decisioni del campione.
Idempotenza aperture: request_id = UUID5 di (run_id, symbol#ciclo, side), dove
"ciclo" è l'indice logico del ciclo (services.scheduler.cycle_slot_key), non
l'orologio: un ciclo che va in crash e viene ritentato riusa gli stessi id e
non duplica gli ordini.

Contabilità delle chiusure: quando l'ordine di chiusura parte, il PnL reale non
è ancora in trade history. La posizione viene chiusa a registro con una STIMA
mark-to-market (che alimenta subito il circuit breaker, altrimenti cieco) e
marcata non liquidata; una passata successiva (ciclo o EOD) sostituisce la
stima col netProfit del broker e corregge il breaker della differenza.

Direzione: long e short seguono il DNA del campione. Lo short reale richiede
che il client eToro sappia inviare l'ordine in vendita (parametro di direzione
su open_position): se il client in uso non lo supporta, l'ordine short viene
registrato come SKIPPED invece di essere silenziosamente convertito in long.
Orizzonte: intraday o swing secondo max_holding_days del DNA.
"""

from __future__ import annotations

import inspect
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from etoro_bot.arena.dna import clamp_dna
from etoro_bot.arena.engine import ArenaDeps
from etoro_bot.arena.trader import LONG, SHORT, build_prompt, decide, enforce
from etoro_bot.domain import (
    ExecutionResult,
    ExecutionStatus,
    Side,
    order_request_id,
)
from etoro_bot.safety.kill_switch import kill_switch_active

logger = logging.getLogger(__name__)

LIVE_ENV = "live"

# Finestra di trade history interrogata per il PnL: le chiusure recenti stanno
# tutte qui dentro, e limitarla evita di paginare l'intero storico del conto.
HISTORY_LOOKBACK_DAYS = 3
# Oltre questo, una chiusura che il broker non ha mai pubblicato smette di
# essere ri-interrogata a ogni ciclo: resta la stima, con un warning.
SETTLE_GIVE_UP_DAYS = 7
# Le esecuzioni fallite si riverificano solo per questa finestra: un ordine
# fallito ieri non va richiesto al broker per sempre.
RECONCILE_LOOKBACK_HOURS = 24

# Marcatore del reference id (= x-request-id dell'ordine) nel dettaglio di
# un'esecuzione fallita: è la chiave con cui il reconcile ritrova un ordine che
# potrebbe essere passato lo stesso.
_REF_PREFIX = "ref="
_REF_RE = re.compile(r"ref=([0-9a-fA-F-]{36})")

# Nomi possibili del parametro di direzione su client.open_position, in ordine
# di preferenza → valore da passare per una vendita allo scoperto.
_SHORT_KWARGS: tuple[tuple[str, Any], ...] = (
    ("direction", "sell"),
    ("side", "sell"),
    ("transaction", "sell"),
    ("is_buy", False),
    ("buy", False),
)


class ShortNotSupported(RuntimeError):
    """Il client eToro configurato non sa aprire posizioni short."""


def short_kwargs(client) -> dict[str, Any] | None:
    """kwargs da passare a open_position per uno short; None se non supportato."""
    try:
        params = inspect.signature(client.open_position).parameters
    except (TypeError, ValueError):  # client non introspezionabile (mock esotici)
        return None
    for name, value in _SHORT_KWARGS:
        if name in params:
            return {name: value}
    return None


def live_directions(deps: ArenaDeps) -> dict[int, str]:
    """positionId → 'long'|'short' dal portafoglio reale (campo isBuy di eToro)."""
    out: dict[int, str] = {}
    for row in _portfolio_positions(deps):
        try:
            position_id = int(row.get("positionId"))
        except (TypeError, ValueError):
            continue
        is_buy = row.get("isBuy")
        out[position_id] = LONG if is_buy is None or is_buy else SHORT
    return out


def _portfolio_positions(deps: ArenaDeps) -> list[dict[str, Any]]:
    try:
        portfolio = deps.client.get_portfolio() or {}
    except Exception as exc:
        logger.warning("live: portafoglio non disponibile: %s", exc)
        return []
    rows = portfolio.get("positions") or []
    return [row for row in rows if isinstance(row, dict)]


def position_change_pct(pos, price: float, direction: str) -> float:
    """Variazione % a favore della posizione reale, secondo la sua direzione."""
    change = (float(price) / float(pos.entry_price) - 1.0) * 100.0
    return -change if direction == SHORT else change


def estimated_pnl_usd(pos, price: float | None, direction: str) -> float:
    """Stima mark-to-market del PnL di chiusura (0 senza prezzo o senza entry).

    Serve a dare subito qualcosa al circuit breaker: il netProfit del broker
    arriva minuti dopo, e nel frattempo il pavimento di sopravvivenza deve
    comunque vedere il drawdown.
    """
    if not price or not pos.entry_price:
        return 0.0
    return float(pos.amount_usd) * position_change_pct(pos, price, direction) / 100.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_run(deps: ArenaDeps, now: datetime) -> str:
    run_id = f"live-{now:%Y%m%d}"
    if deps.repo.get_run(run_id) is None:
        deps.repo.create_run(run_id, environment=LIVE_ENV)
    return run_id


def _live_cash(deps: ArenaDeps) -> float:
    portfolio = deps.client.get_portfolio()
    return float(portfolio.get("credit") or 0.0)


def _slot_key(deps: ArenaDeps, now: datetime) -> str:
    """Indice logico del ciclo: base delle chiavi di idempotenza degli ordini."""
    from etoro_bot.services.scheduler import cycle_slot_key

    return cycle_slot_key(deps.settings or {}, now)


def _lookup_fill(client, reference_id: str) -> dict[str, Any] | None:
    """Esito reale di un ordine dal suo reference id; None se non risulta eseguito."""
    from etoro_bot.etoro.client import fill_from_lookup

    lookup = getattr(client, "lookup_order", None)
    if lookup is None:
        return None
    try:
        info = lookup(reference_id=reference_id)
    except Exception as exc:
        logger.warning("live: lookup ordine ref=%s fallita: %s", reference_id, exc)
        return None
    fill = fill_from_lookup(info)
    return fill if fill and fill.get("position_id") is not None else None


# ------------------------------------------------------------------ chiusure


def _close_real_position(
    deps: ArenaDeps, breaker, run_id: str, pos, price: float | None, reason: str,
    equity: float, direction: str = LONG,
) -> None:
    """Chiude a mercato e registra la chiusura con PnL STIMATO (non liquidato).

    Il PnL vero arriva in trade history con ritardo: aspettarlo qui significava
    scrivere sempre None (breaker cieco e realized_pnl_usd avvelenato). Vedi
    settle_pending_closes.
    """
    outcome = deps.client.close_position(pos.etoro_position_id, pos.instrument_id) or {}
    close_order_id = outcome.get("order_id") if isinstance(outcome, dict) else None
    estimate = estimated_pnl_usd(pos, price, direction)
    deps.repo.close_position(
        pos.etoro_position_id, close_price=price,
        realized_pnl_usd=estimate, close_reason=reason,
        close_order_id=int(close_order_id) if close_order_id is not None else None,
        pnl_settled=False,
    )
    if breaker is not None:
        breaker.record_closed_trade(estimate, equity)
    deps.repo.add_execution(
        run_id,
        ExecutionResult(
            symbol=pos.symbol, side=Side.SELL, amount_usd=pos.amount_usd,
            status=ExecutionStatus.FILLED, detail=reason,
            execution_price=price, etoro_position_id=pos.etoro_position_id,
        ),
    )


def settle_pending_closes(
    deps: ArenaDeps, breaker=None, now: datetime | None = None
) -> dict[str, Any]:
    """Sostituisce le stime col PnL reale del broker per le chiusure in sospeso.

    Gira all'inizio di ogni ciclo live e all'EOD. Il breaker riceve solo la
    DIFFERENZA rispetto alla stima già contabilizzata, così il drawdown
    giornaliero converge sul dato vero senza contare due volte lo stesso trade.
    """
    now = now or _utcnow()
    pending = deps.repo.unsettled_closed_positions()
    # chiusure che il broker non ha mai pubblicato: si tiene la stima e si
    # smette di interrogarlo, altrimenti la trade history verrebbe ripaginata
    # da quella data a ogni ciclo, per sempre.
    stale = [
        p for p in pending
        if p.closed_at and (now - p.closed_at) > timedelta(days=SETTLE_GIVE_UP_DAYS)
    ]
    for pos in stale:
        deps.repo.settle_position_pnl(
            pos.etoro_position_id, realized_pnl_usd=float(pos.realized_pnl_usd or 0.0)
        )
        logger.warning(
            "live: PnL reale mai arrivato per la posizione %s (chiusa il %s): "
            "resta la stima %.2f USD",
            pos.etoro_position_id, pos.closed_at, float(pos.realized_pnl_usd or 0.0),
        )
    pending = [p for p in pending if p not in stale]
    if not pending:
        return {"settled": 0, "pending": 0}
    min_date = min(
        [p.closed_at for p in pending if p.closed_at]
        or [now - timedelta(days=HISTORY_LOOKBACK_DAYS)]
    ) - timedelta(days=1)
    try:
        history = deps.client.get_trade_history(min_date=min_date)
    except Exception as exc:
        logger.warning("live: trade history non disponibile per la liquidazione: %s", exc)
        return {"settled": 0, "pending": len(pending)}

    by_position: dict[int, dict] = {}
    for trade in history or []:
        try:
            by_position[int(trade.get("positionId"))] = trade
        except (TypeError, ValueError):
            continue

    equity = 0.0
    try:
        equity = _live_cash(deps) + sum(p.amount_usd for p in deps.repo.open_positions())
    except Exception as exc:
        logger.warning("live: equity non disponibile per la liquidazione: %s", exc)

    settled = 0
    for pos in pending:
        trade = by_position.get(int(pos.etoro_position_id))
        if trade is None or trade.get("netProfit") is None:
            continue
        try:
            pnl = float(trade["netProfit"])
        except (TypeError, ValueError):
            continue
        close_rate = trade.get("closeRate")
        estimate = float(pos.realized_pnl_usd or 0.0)
        deps.repo.settle_position_pnl(
            pos.etoro_position_id,
            realized_pnl_usd=pnl,
            close_price=float(close_rate) if close_rate is not None else None,
        )
        if breaker is not None and abs(pnl - estimate) > 1e-9:
            breaker.record_closed_trade(pnl - estimate, equity, count_streak=False)
        settled += 1
        logger.info(
            "live: PnL liquidato per posizione %s: stima %.2f → reale %.2f USD",
            pos.etoro_position_id, estimate, pnl,
        )
    return {"settled": settled, "pending": len(pending) - settled}


# ------------------------------------------------------------------ aperture


class OrderFailed(RuntimeError):
    """Apertura fallita e confermata non eseguita: porta con sé il reference id."""

    def __init__(self, message: str, request_id: str) -> None:
        super().__init__(f"{message} [{_REF_PREFIX}{request_id}]")
        self.request_id = request_id


def _open_real_position(
    deps: ArenaDeps, run_id: str, order: dict[str, Any], slot: str, now: datetime,
    price: float | None = None,
) -> None:
    direction = order.get("direction", LONG)
    side = Side.SELL if direction == SHORT else Side.BUY
    request_id = order_request_id(run_id, f"{order['symbol']}#{slot}", side)
    extra: dict[str, Any] = {}
    if direction == SHORT:
        extra = short_kwargs(deps.client) or {}
        if not extra:
            raise ShortNotSupported(
                "client eToro senza parametro di direzione su open_position: "
                "short non inviabile"
            )
    try:
        fill = deps.client.open_position(
            order["instrument_id"], order["amount_usd"], request_id, **extra
        )
    except Exception as exc:
        # L'ordine può essere passato lo stesso: la conferma può fallire dopo
        # l'esecuzione (timeout del fill, rete, 5xx). Prima di dichiararlo
        # fallito si chiede al broker che fine ha fatto quel reference id: una
        # posizione reale non registrata è una posizione che nessuno chiuderà.
        recovered = _lookup_fill(deps.client, request_id)
        if recovered is None:
            raise OrderFailed(str(exc), request_id) from exc
        logger.warning(
            "live: apertura %s ha sollevato (%s) ma l'ordine RISULTA ESEGUITO "
            "(ref=%s, position=%s): posizione adottata",
            order["symbol"], exc, request_id, recovered.get("position_id"),
        )
        fill = recovered

    position_id = fill.get("position_id")
    entry_price = _entry_price(deps, fill, request_id, price, order["symbol"])
    if position_id is not None:
        deps.repo.register_open_position(
            etoro_position_id=int(position_id),
            run_id=run_id,
            symbol=order["symbol"],
            instrument_id=order["instrument_id"],
            amount_usd=order["amount_usd"],
            entry_price=entry_price,
            opened_at=now,
        )
    detail = order["reason"] or "apertura live"
    if direction == SHORT:
        detail = f"[SHORT] {detail}"
    deps.repo.add_execution(
        run_id,
        ExecutionResult(
            symbol=order["symbol"], side=side, amount_usd=order["amount_usd"],
            status=ExecutionStatus.FILLED, detail=detail,
            execution_price=entry_price or fill.get("execution_price"),
            etoro_position_id=int(position_id) if position_id is not None else None,
        ),
    )


def _entry_price(
    deps: ArenaDeps, fill: dict[str, Any], request_id: str,
    snapshot_price: float | None, symbol: str,
) -> float:
    """Prezzo d'ingresso reale; mai 0, che disattiverebbe stop loss e take profit.

    Ordine di preferenza: prezzo del fill → ri-lettura dell'ordine dal broker →
    prezzo dello snapshot di mercato.
    """
    try:
        price = float(fill.get("execution_price") or 0.0)
    except (TypeError, ValueError):
        price = 0.0
    if price > 0:
        return price
    recovered = _lookup_fill(deps.client, request_id)
    if recovered is not None:
        try:
            price = float(recovered.get("execution_price") or 0.0)
        except (TypeError, ValueError):
            price = 0.0
    if price > 0:
        return price
    if snapshot_price:
        logger.warning(
            "live: %s aperto senza prezzo di esecuzione: uso il prezzo di "
            "snapshot %.4f (stop loss e take profit userebbero 0)",
            symbol, float(snapshot_price),
        )
        return float(snapshot_price)
    logger.error(
        "live: %s aperto senza alcun prezzo d'ingresso (ref=%s): stop loss e "
        "take profit resteranno inattivi su questa posizione",
        symbol, request_id,
    )
    return 0.0


# ------------------------------------------------------------------ reconcile


def reconcile_live_positions(
    deps: ArenaDeps, run_id: str, now: datetime, market: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Adotta le posizioni reali che sono nostre ma mancano dal registry.

    Due sorgenti, entrambe con un marcatore che le lega al bot:
    1. esecuzioni FALLITE che portano il reference id del nostro ordine: se il
       broker dice che quell'ordine è invece passato, la posizione è nostra;
    2. posizioni del portafoglio il cui positionId compare già nel giornale
       delle esecuzioni (l'ordine era andato, la registrazione no).

    Le posizioni che il bot non ha aperto non vengono MAI toccate: qui si
    adotta soltanto, non si chiude nulla.
    """
    rows = _portfolio_positions(deps)
    open_ids: dict[int, dict[str, Any]] = {}
    for row in rows:
        try:
            open_ids[int(row.get("positionId"))] = row
        except (TypeError, ValueError):
            continue
    try:
        known = deps.repo.known_position_ids()
    except Exception as exc:
        logger.warning("live: registry non leggibile, reconcile saltato: %s", exc)
        return {"adopted": 0}

    executions = deps.repo.list_executions(limit=500)
    adopted = 0

    # 1. ordini dichiarati falliti che il broker considera invece eseguiti
    since = now - timedelta(hours=RECONCILE_LOOKBACK_HOURS)
    for execution in executions:
        if execution.status != ExecutionStatus.FAILED.value or execution.etoro_position_id:
            continue
        if execution.created_at and execution.created_at < since:
            continue
        match = _REF_RE.search(execution.detail or "")
        if not match:
            continue
        fill = _lookup_fill(deps.client, match.group(1))
        if fill is None:
            continue
        position_id = int(fill["position_id"])
        if position_id in known:
            continue
        instrument_id = _instrument_of(open_ids.get(position_id), market, execution.symbol)
        deps.repo.register_open_position(
            etoro_position_id=position_id,
            run_id=run_id,
            symbol=execution.symbol,
            instrument_id=instrument_id,
            amount_usd=execution.amount_usd,
            entry_price=float(fill.get("execution_price") or 0.0),
            opened_at=execution.created_at or now,
        )
        known.add(position_id)
        adopted += 1
        logger.error(
            "live: ORDINE ORFANO RECUPERATO — %s risultava fallito ma la "
            "posizione %s è aperta sul conto: adottata nel registry",
            execution.symbol, position_id,
        )

    # 2. posizioni aperte sul conto che il giornale attribuisce già al bot
    journal = {
        int(e.etoro_position_id): e for e in executions if e.etoro_position_id is not None
    }
    for position_id, row in open_ids.items():
        if position_id in known:
            continue
        execution = journal.get(position_id)
        if execution is None:
            continue  # non è nostra: non si tocca
        instrument_id = _instrument_of(row, market, execution.symbol)
        deps.repo.register_open_position(
            etoro_position_id=position_id,
            run_id=run_id,
            symbol=execution.symbol,
            instrument_id=instrument_id,
            amount_usd=execution.amount_usd,
            entry_price=float(
                execution.execution_price or row.get("openRate") or 0.0
            ),
            opened_at=execution.created_at or now,
        )
        known.add(position_id)
        adopted += 1
        logger.error(
            "live: POSIZIONE ORFANA — %s (%s) è aperta sul conto ma mancava dal "
            "registry: adottata",
            execution.symbol, position_id,
        )
    return {"adopted": adopted}


def _instrument_of(
    row: dict[str, Any] | None, market: dict[str, dict[str, Any]] | None, symbol: str
) -> int:
    """instrumentId di una posizione: dal portafoglio, dallo snapshot, o 0."""
    for key in ("instrumentID", "instrumentId", "instrument_id"):
        value = (row or {}).get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    entry = (market or {}).get(symbol) or {}
    try:
        return int(entry.get("instrument_id") or 0)
    except (TypeError, ValueError):
        return 0


# Mai due cicli live sovrapposti: ordini reali, niente doppioni. Lo stesso lock
# copre le chiusure di fine sessione e l'EOD, che agiscono sulle stesse
# posizioni e sullo stesso circuit breaker.
_live_lock = threading.Lock()

# Le chiusure di fine giornata non si saltano: aspettano che il ciclo in corso
# finisca (un ciclo dura minuti, non ore).
_EOD_LOCK_WAIT_S = 600.0


def run_live_cycle(
    deps: ArenaDeps,
    breaker=None,
    market: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Un ciclo live guidato dal campione. Ritorna un summary sintetico."""
    if not _live_lock.acquire(blocking=False):
        return {"skipped": "cycle_in_progress"}
    try:
        return _run_live_cycle_locked(deps, breaker, market, now)
    finally:
        _live_lock.release()


def _run_live_cycle_locked(
    deps: ArenaDeps,
    breaker,
    market: dict[str, dict[str, Any]] | None,
    now: datetime | None,
) -> dict[str, Any]:
    now = now or _utcnow()
    if kill_switch_active():
        return {"skipped": "kill_switch"}
    champion = deps.repo.champion()
    if champion is None:
        return {"skipped": "no_champion"}
    if deps.llm is None:
        return {"skipped": "no_llm"}
    if market is None:
        from etoro_bot.arena.market import build_snapshot

        market = build_snapshot(deps.client, deps.settings)
    prices = {s: float(r["price"]) for s, r in market.items() if r.get("price")}
    dna = clamp_dna(champion.dna)
    run_id = _ensure_run(deps, now)

    # Prima di qualsiasi decisione: il libro mastro deve rispecchiare il conto
    # reale, e le chiusure in sospeso devono arrivare al circuit breaker.
    try:
        reconciled = reconcile_live_positions(deps, run_id, now, market=market)
    except Exception as exc:
        logger.warning("live: reconcile fallito: %s", exc)
        reconciled = {"adopted": 0}
    try:
        settled = settle_pending_closes(deps, breaker=breaker, now=now)
    except Exception as exc:
        logger.warning("live: liquidazione PnL fallita: %s", exc)
        settled = {"settled": 0}

    positions = deps.repo.open_positions()
    directions = live_directions(deps)
    cash = _live_cash(deps)
    equity = cash + sum(p.amount_usd for p in positions)

    # stop loss / take profit del DNA (0 = disattivati: decide solo il campione).
    # Girano PRIMA della chiamata all'LLM: un modello lento non deve poter
    # ritardare uno stop loss.
    sl = float(dna["stop_loss_pct"])
    tp = float(dna["take_profit_pct"])
    for pos in list(positions):
        price = prices.get(pos.symbol)
        if not price or not pos.entry_price:
            continue
        direction = directions.get(pos.etoro_position_id, LONG)
        change_pct = position_change_pct(pos, price, direction)
        reason = None
        if sl > 0 and change_pct <= -sl:
            reason = f"stop loss automatico ({change_pct:+.2f}%)"
        elif tp > 0 and change_pct >= tp:
            reason = f"take profit automatico ({change_pct:+.2f}%)"
        if reason is None:
            continue
        try:
            _close_real_position(deps, breaker, run_id, pos, price, reason, equity,
                                 direction)
        except Exception as exc:
            # una chiusura fallita non deve impedire le altre: sono stop loss
            logger.warning("live: chiusura automatica %s fallita: %s", pos.symbol, exc)

    positions = deps.repo.open_positions()
    cash = _live_cash(deps)
    equity = cash + sum(p.amount_usd for p in positions)
    prompt = build_prompt(
        name=f"{champion.name} (CAMPIONE LIVE)",
        dna=dna,
        memory=champion.memory,
        survival=(
            "Operi con denaro REALE: sei la versione vincente dell'ultimo mese, "
            "e il tuo DNA è quello che ha battuto il rivale. Continua a operare "
            "come hai imparato: long e short, intraday o swing, quante volte "
            "serve. Gli unici freni sono di sopravvivenza — kill switch manuale "
            "e blocco delle aperture in caso di drawdown estremo — e non "
            "impediscono mai di CHIUDERE una posizione."
        ),
        cash=cash,
        equity=equity,
        positions_view=[
            f"- {p.symbol} {directions.get(p.etoro_position_id, LONG).upper()}: "
            f"{p.amount_usd:.2f} USD @ {p.entry_price:.2f}"
            for p in positions
        ],
        market_view=[str(r.get("view", s)) for s, r in market.items()],
    )
    actions = decide(deps.llm, model=deps.model, max_tokens=deps.max_tokens, prompt=prompt)
    opens, closes = enforce(
        actions, dna=dna, cash=cash, equity=equity,
        held_symbols={p.symbol for p in positions}, market=market,
        held_count=len(positions),
    )

    executed = {"opened": 0, "closed": 0, "blocked": 0}
    for close in closes:
        wanted = close.get("direction")
        for pos in [p for p in positions if p.symbol == close["symbol"]]:
            direction = directions.get(pos.etoro_position_id, LONG)
            if wanted and direction != wanted:
                continue
            try:
                _close_real_position(deps, breaker, run_id, pos, prices.get(pos.symbol),
                                     close["reason"] or "chiusura live", equity, direction)
                executed["closed"] += 1
            except Exception as exc:
                logger.warning("live: chiusura %s fallita: %s", close["symbol"], exc)

    blocks_openings = breaker is not None and breaker.blocks_openings()
    slot = _slot_key(deps, now)
    for order in opens:
        if kill_switch_active() or blocks_openings:
            executed["blocked"] += 1
            continue
        side = Side.SELL if order.get("direction") == SHORT else Side.BUY
        try:
            _open_real_position(deps, run_id, order, slot, now,
                                price=prices.get(order["symbol"]))
            executed["opened"] += 1
        except ShortNotSupported as exc:
            # niente conversione silenziosa in long: l'ordine non parte e resta
            # a giornale come saltato, così il limite è visibile.
            logger.warning("live: short %s non inviabile: %s", order["symbol"], exc)
            executed["blocked"] += 1
            deps.repo.add_execution(
                run_id,
                ExecutionResult(
                    symbol=order["symbol"], side=side,
                    amount_usd=order["amount_usd"],
                    status=ExecutionStatus.SKIPPED, detail=str(exc),
                ),
            )
        except Exception as exc:
            # il dettaglio porta il reference id: il reconcile del ciclo
            # successivo può ancora scoprire che l'ordine era passato.
            logger.warning("live: apertura %s fallita: %s", order["symbol"], exc)
            deps.repo.add_execution(
                run_id,
                ExecutionResult(
                    symbol=order["symbol"], side=side,
                    amount_usd=order["amount_usd"],
                    status=ExecutionStatus.FAILED, detail=str(exc),
                ),
            )
    for item in closes + [dict(o, action="open") for o in opens]:
        deps.repo.add_decision(run_id, item.get("symbol", "?"), "trader",
                               {k: v for k, v in item.items() if k != "instrument_id"})
    summary = {
        **executed,
        "adopted": reconciled.get("adopted", 0),
        "settled": settled.get("settled", 0),
    }
    deps.repo.finish_run(run_id, {"cycle": slot, **summary})
    return summary


def close_live_market_positions(
    deps: ArenaDeps,
    market_name: str,
    breaker=None,
    market: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fine sessione live: chiude le posizioni reali di QUEL mercato che hanno
    raggiunto il holding massimo del DNA del campione (1 = intraday, >1 = swing
    autorizzato: le posizioni restano aperte oltre la campanella).

    Prende lo stesso lock del ciclo live: senza, la campanella e un ciclo in
    corso potrebbero chiudere due volte la stessa posizione."""
    if not _live_lock.acquire(timeout=_EOD_LOCK_WAIT_S):
        logger.error("live: chiusura sessione %s saltata, ciclo bloccato", market_name)
        return {"skipped": "cycle_in_progress", "market": market_name}
    try:
        return _close_live_market_positions_locked(deps, market_name, breaker, market, now)
    finally:
        _live_lock.release()


def _close_live_market_positions_locked(
    deps: ArenaDeps,
    market_name: str,
    breaker,
    market: dict[str, dict[str, Any]] | None,
    now: datetime | None,
) -> dict[str, Any]:
    from etoro_bot.arena.dna import clamp_dna
    from etoro_bot.arena.engine import trading_days_held
    from etoro_bot.arena.market import build_snapshot, market_of_symbol

    now = now or _utcnow()
    if kill_switch_active():
        return {"skipped": "kill_switch"}
    champion = deps.repo.champion()
    max_days = int(clamp_dna(champion.dna if champion else None)["max_holding_days"])
    expired = [
        p
        for p in deps.repo.open_positions()
        if market_of_symbol(p.symbol) == market_name
        and trading_days_held(p.opened_at, now) >= max_days
    ]
    if not expired:
        return {"closed": 0, "market": market_name}
    if market is None:
        market = build_snapshot(deps.client, deps.settings, now=now, only_open=False)
    prices = {s: float(r["price"]) for s, r in market.items() if r.get("price")}
    run_id = _ensure_run(deps, now)
    try:
        cash = _live_cash(deps)
    except Exception:
        cash = 0.0
    equity = cash + sum(p.amount_usd for p in deps.repo.open_positions())
    directions = live_directions(deps)
    closed = 0
    for pos in expired:
        try:
            held = trading_days_held(pos.opened_at, now)
            _close_real_position(
                deps, breaker, run_id, pos, prices.get(pos.symbol),
                f"fine sessione {market_name}: holding massimo raggiunto "
                f"({held}/{max_days} giorni)", equity,
                directions.get(pos.etoro_position_id, LONG),
            )
            closed += 1
        except Exception as exc:
            logger.warning("live: chiusura sessione %s fallita: %s", pos.symbol, exc)
    return {"closed": closed, "market": market_name}


def run_live_eod(deps: ArenaDeps, breaker=None, now: datetime | None = None) -> dict[str, Any]:
    """Ultima campanella live: liquida i PnL in sospeso e fotografa l'equity.

    Le chiusure per scadenza holding avvengono ai singoli fine-sessione.
    """
    if not _live_lock.acquire(timeout=_EOD_LOCK_WAIT_S):
        logger.error("live: EOD saltato, ciclo bloccato")
        return {"snapshot": False, "skipped": "cycle_in_progress"}
    try:
        return _run_live_eod_locked(deps, breaker, now)
    finally:
        _live_lock.release()


def _run_live_eod_locked(deps: ArenaDeps, breaker, now: datetime | None) -> dict[str, Any]:
    now = now or _utcnow()
    positions = deps.repo.open_positions()
    if not positions and deps.repo.champion() is None:
        return {"snapshot": False}
    settled = 0
    try:
        settled = settle_pending_closes(deps, breaker=breaker, now=now)["settled"]
    except Exception as exc:
        logger.warning("live: liquidazione PnL serale fallita: %s", exc)
    try:
        cash = _live_cash(deps)
        exposure = sum(p.amount_usd for p in positions)
        deps.repo.record_equity_snapshot(now.date(), cash + exposure, cash, exposure)
        return {"snapshot": True, "equity_usd": round(cash + exposure, 2),
                "settled": settled}
    except Exception as exc:
        logger.warning("live: snapshot equity fallito: %s", exc)
        return {"snapshot": False, "settled": settled}
