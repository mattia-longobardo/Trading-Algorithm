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
La direzione viene SCRITTA a registro all'apertura e riletta da lì: il
portafoglio del broker serve solo a corroborarla. Quando registro e conto si
contraddicono — o il registro tace — le chiusure automatiche su quella
posizione vengono saltate: presumere long invertirebbe stop loss e take
profit su uno short (stop in guadagno, take profit in perdita).
Orizzonte: intraday o swing secondo max_holding_days del DNA.
"""

from __future__ import annotations

import inspect
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from etoro_bot.arena.dna import clamp_dna, risk_close_reason
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
# Grazia prima di dare per chiusa una posizione assente dal portafoglio: un
# ordine appena eseguito può non comparire subito nella lettura del conto.
CLOSE_GRACE_MINUTES = 15
# Letture consecutive senza la posizione necessarie per dichiararla chiusa
# quando la trade history non la conferma (una risposta parziale del broker è
# indistinguibile da una completa).
_MISSING_READS_TO_CLOSE = 2
# positionId → letture consecutive del portafoglio in cui non compare.
# ponytail: contatore in memoria di processo, si azzera al riavvio (costo: un
# ciclo di attesa in più). Persisterlo vorrebbe una colonna su bot_positions.
_missing_from_portfolio: dict[int, int] = {}

# Marcatore del reference id (= x-request-id dell'ordine) nel dettaglio di
# un'esecuzione fallita: è la chiave con cui il reconcile ritrova un ordine che
# potrebbe essere passato lo stesso.
_REF_PREFIX = "ref="
_REF_RE = re.compile(r"ref=([0-9a-fA-F-]{36})")

# Stati di un'apertura che non è conclusa a registro: il reconcile li ripesca e
# chiede al broker che fine ha fatto quel reference id. "cancelled" c'è perché
# l'annullamento manuale dalla UI non annulla nulla sul broker.
_UNSETTLED_STATUSES = (
    ExecutionStatus.PENDING.value,
    ExecutionStatus.FAILED.value,
    ExecutionStatus.CANCELLED.value,
)

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


def live_directions(
    deps: ArenaDeps, portfolio: dict[str, Any] | None = None
) -> dict[int, str]:
    """positionId → 'long'|'short' dal portafoglio reale (campo isBuy di eToro).

    Non è la fonte della direzione — quella è la colonna a registro — ma solo
    la controprova: un portafoglio irraggiungibile qui ritorna {} e non deve
    mai far degradare una posizione a long. `portfolio` già letto = si riusa
    quella lettura invece di interrogare di nuovo il broker.
    """
    out: dict[int, str] = {}
    for row in _portfolio_positions(deps, portfolio):
        try:
            position_id = int(row.get("positionId"))
        except (TypeError, ValueError):
            continue
        direction = _row_direction(row)
        if direction is not None:
            out[position_id] = direction
    return out


def _row_direction(row: dict[str, Any] | None) -> str | None:
    """Direzione dichiarata da una riga di portafoglio eToro; None se tace."""
    is_buy = (row or {}).get("isBuy")
    return None if is_buy is None else (LONG if is_buy else SHORT)


def registry_direction(pos) -> str | None:
    """Direzione persistita della posizione; None se il registro non la sa."""
    value = str(getattr(pos, "direction", "") or "").lower()
    return value if value in (LONG, SHORT) else None


def sweep_direction(pos, broker: dict[int, str]) -> str | None:
    """Direzione su cui basare una chiusura automatica; None se non è certa.

    Decide il registro, il conto reale corrobora. Se si contraddicono la
    direzione non è nota: chi chiama salta la chiusura invece di ripiegare su
    long.
    """
    recorded = registry_direction(pos)
    if recorded is None:
        return None
    seen = broker.get(int(pos.etoro_position_id))
    if seen is not None and seen != recorded:
        logger.error(
            "live: posizione %s (%s) a registro è %s ma il conto la dà %s: "
            "direzione non attendibile",
            pos.etoro_position_id, pos.symbol, recorded, seen,
        )
        return None
    return recorded


# "non ancora letto", distinto sia da None (lettura fallita) sia da {} (conto
# vuoto): permette a chi ha già il portafoglio in mano di passarlo senza che il
# chiamante di turno interroghi di nuovo il broker.
_UNREAD: Any = object()


def _portfolio_read(deps: ArenaDeps) -> dict[str, Any] | None:
    """Portafoglio del broker; None se la LETTURA è fallita.

    None e {} non sono la stessa cosa: il primo è ignoranza, il secondo è un
    conto senza posizioni. Chi ne trae conclusioni (le chiusure adottate) deve
    poterli distinguere.
    """
    try:
        return deps.client.get_portfolio() or {}
    except Exception as exc:
        logger.warning("live: portafoglio non disponibile: %s", exc)
        return None


def _portfolio_positions(
    deps: ArenaDeps, portfolio: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    if portfolio is None:
        portfolio = _portfolio_read(deps) or {}
    rows = portfolio.get("positions") or []
    return [row for row in rows if isinstance(row, dict)]


def position_change_pct(pos, price: float, direction: str) -> float:
    """Variazione % a favore della posizione reale, secondo la sua direzione."""
    change = (float(price) / float(pos.entry_price) - 1.0) * 100.0
    return -change if direction == SHORT else change


def estimated_pnl_usd(pos, price: float | None, direction: str | None) -> float:
    """Stima mark-to-market del PnL di chiusura (0 senza prezzo o senza entry).

    Serve a dare subito qualcosa al circuit breaker: il netProfit del broker
    arriva minuti dopo, e nel frattempo il pavimento di sopravvivenza deve
    comunque vedere il drawdown. Senza direzione certa la stima resta 0: un
    segno sbagliato avvelenerebbe il breaker più della sua assenza.
    """
    if not price or not pos.entry_price or direction is None:
        return 0.0
    return float(pos.amount_usd) * position_change_pct(pos, price, direction) / 100.0


def live_position_value(pos, price: float | None) -> float:
    """Valore corrente della posizione: costo + PnL non realizzato.

    Senza prezzo (o senza entry) resta il costo storico: è la stima migliore
    disponibile, non un errore.
    """
    return float(pos.amount_usd) + estimated_pnl_usd(
        pos, price, pos.direction or LONG
    )


def live_equity(positions, prices: dict[str, float] | None, cash: float) -> float:
    """Equity live mark-to-market: cassa + valore corrente delle posizioni.

    UNICA definizione, usata da circuit breaker, sizing e `/portfolio`. A costo
    storico i tre numeri divergevano fra loro e dal conto vero: il denominatore
    del drawdown giornaliero e la size massima per ordine restavano fermi al
    prezzo di ingresso mentre il mercato si muoveva.
    """
    return float(cash) + sum(
        live_position_value(p, (prices or {}).get(p.symbol)) for p in positions
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_run(deps: ArenaDeps, now: datetime) -> str:
    run_id = f"live-{now:%Y%m%d}"
    if deps.repo.get_run(run_id) is None:
        deps.repo.create_run(run_id, environment=LIVE_ENV)
    return run_id


def _live_cash(deps: ArenaDeps) -> float:
    """Liquidità reale del conto. Solleva se il broker non pubblica `credit`.

    Un `credit` assente NON è zero: trattarlo come 0 azzererebbe il sizing,
    falserebbe equity e circuit breaker e lo farebbe in silenzio. Sulla stessa
    lettura il percorso `/portfolio` risponde 502 — qui la severità è la stessa,
    chi chiama salta invece di operare su un capitale inventato.
    """
    portfolio = deps.client.get_portfolio() or {}
    credit = portfolio.get("credit")
    if credit is None:
        raise ValueError("portfolio eToro senza campo credit")
    return float(credit)


def _slot_key(deps: ArenaDeps, now: datetime) -> str:
    """Indice logico del ciclo: base delle chiavi di idempotenza degli ordini."""
    from etoro_bot.services.scheduler import cycle_slot_key

    return cycle_slot_key(deps.settings or {}, now)


def _lookup_outcome(client, reference_id: str) -> tuple[dict[str, Any] | None, bool]:
    """(fill, answered) di un ordine dal suo reference id.

    `answered=False` SOLO quando il broker non ha risposto affatto (client
    senza lookup, chiamata esplosa): è incertezza, non un esito. Un "non
    eseguito" confermato è invece una risposta a tutti gli effetti — un rifiuto
    ordinario (fondi, taglia minima, 429) non deve essere scambiato per un
    ordine potenzialmente a mercato.
    """
    from etoro_bot.etoro.client import fill_from_lookup

    lookup = getattr(client, "lookup_order", None)
    if lookup is None:
        return None, False
    try:
        info = lookup(reference_id=reference_id)
    except Exception as exc:
        logger.warning("live: lookup ordine ref=%s fallita: %s", reference_id, exc)
        return None, False
    fill = fill_from_lookup(info)
    return (fill if fill and fill.get("position_id") is not None else None), True


def _lookup_fill(client, reference_id: str) -> dict[str, Any] | None:
    """Esito reale di un ordine dal suo reference id; None se non risulta eseguito."""
    return _lookup_outcome(client, reference_id)[0]


# ------------------------------------------------------------------ chiusure


def record_close(repo, etoro_position_id: int, **fields: Any) -> bool:
    """Scrive la chiusura a registro, con un secondo tentativo. False = non
    scritta.

    L'ordine al broker è già partito e NON va mai ripetuto in loop: se la
    scrittura non passa nemmeno al secondo colpo, la posizione resta aperta a
    registro e sarà il reconcile a chiuderla (non la trova più sul conto).
    La scrittura è idempotente sull'etoro_position_id: una chiusura già
    registrata non viene sovrascritta.
    """
    for attempt in (1, 2):
        try:
            repo.close_position(etoro_position_id, **fields)
            return True
        except Exception:
            logger.exception(
                "live: scrittura della chiusura %s fallita (tentativo %d/2)",
                etoro_position_id, attempt,
            )
    return False


def _as_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _history_by_position(deps: ArenaDeps, min_date: datetime) -> dict[int, dict] | None:
    """Trade history indicizzata per positionId; None se il broker non risponde."""
    try:
        history = deps.client.get_trade_history(min_date=min_date)
    except Exception as exc:
        logger.warning("live: trade history non disponibile: %s", exc)
        return None
    out: dict[int, dict] = {}
    for trade in history or []:
        try:
            out[int(trade.get("positionId"))] = trade
        except (TypeError, ValueError):
            continue
    return out


def _close_real_position(
    deps: ArenaDeps, breaker, run_id: str, pos, price: float | None, reason: str,
    equity: float, direction: str | None = None, now: datetime | None = None,
) -> None:
    """Chiude a mercato e registra la chiusura con PnL STIMATO (non liquidato).

    Il PnL vero arriva in trade history con ritardo: aspettarlo qui significava
    scrivere sempre None (breaker cieco e realized_pnl_usd avvelenato). Vedi
    settle_pending_closes. `direction=None` = direzione non certa: si chiude
    comunque (una chiusura non si nega mai) ma senza stima di PnL.
    """
    if direction is None:
        logger.warning(
            "live: %s chiusa senza direzione certa: nessuna stima di PnL, il "
            "breaker aspetta il netProfit del broker", pos.symbol,
        )
    # idempotenza della chiusura: stesso ciclo logico ritentato = stesso
    # x-request-id, il broker deduplica invece di chiudere due volte.
    request_id = order_request_id(
        run_id,
        f"close#{pos.etoro_position_id}#{_slot_key(deps, now or _utcnow())}",
        Side.SELL,
    )
    outcome = deps.client.close_position(
        pos.etoro_position_id, pos.instrument_id, request_id
    ) or {}
    close_order_id = outcome.get("order_id") if isinstance(outcome, dict) else None
    estimate = estimated_pnl_usd(pos, price, direction)
    if not record_close(
        deps.repo, pos.etoro_position_id, close_price=price,
        realized_pnl_usd=estimate, close_reason=reason,
        close_order_id=int(close_order_id) if close_order_id is not None else None,
        pnl_settled=False,
    ):
        # Chiusa al broker ma non a registro: niente breaker (lo conterebbe due
        # volte quando il reconcile adotterà la chiusura) e niente riga a
        # giornale. Soprattutto: nessun altro ordine di chiusura da qui.
        logger.error(
            "live: %s (%s) chiusa al broker ma NON a registro: la chiusura "
            "verrà adottata dal reconcile",
            pos.symbol, pos.etoro_position_id,
        )
        return
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
    deps: ArenaDeps, breaker=None, now: datetime | None = None,
    portfolio: Any = _UNREAD,
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
    by_position = _history_by_position(deps, min_date)
    if by_position is None:
        return {"settled": 0, "pending": len(pending)}

    # equity ignota NON è zero: con 0 il breaker salta in silenzio il controllo
    # sul drawdown giornaliero (circuit_breaker.py: `equity_usd > 0`), cioè si
    # disattiverebbe proprio mentre arrivano le perdite reali. Meglio non
    # aggiornarlo affatto e dirlo a voce alta.
    if portfolio is _UNREAD:
        portfolio = _portfolio_read(deps)
    cash = _as_float((portfolio or {}).get("credit"))
    equity: float | None = (
        None if cash is None
        else live_equity(deps.repo.open_positions(), None, cash)
    )
    if equity is None:
        logger.warning("live: equity non disponibile per la liquidazione")

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
            if equity is None:
                logger.warning(
                    "live: scarto di %.2f USD sulla posizione %s NON contato dal "
                    "circuit breaker: equity non calcolabile in questo batch",
                    pnl - estimate, pos.etoro_position_id,
                )
            else:
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
    detail = order["reason"] or "apertura live"
    if direction == SHORT:
        detail = f"[SHORT] {detail}"
    # WRITE-AHEAD: l'intento è a giornale col reference id PRIMA che l'ordine
    # parta. Qualunque cosa succeda dopo — crash del processo, DB che rifiuta
    # la registrazione, broker che risponde a metà — resta una riga pending da
    # cui il reconcile può risalire alla posizione reale. Senza, un fill
    # seguito da un errore non lascia traccia: posizione orfana, invisibile,
    # senza stop loss, e il ciclo dopo riaprirebbe lo stesso simbolo.
    execution_id = deps.repo.add_execution(
        run_id,
        ExecutionResult(
            symbol=order["symbol"], side=side, amount_usd=order["amount_usd"],
            status=ExecutionStatus.PENDING,
            detail=f"{detail} [{_REF_PREFIX}{request_id}]",
        ),
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
            failure = OrderFailed(str(exc), request_id)
            deps.repo.update_execution(
                execution_id, ExecutionStatus.FAILED, detail=str(failure)
            )
            raise failure from exc
        logger.warning(
            "live: apertura %s ha sollevato (%s) ma l'ordine RISULTA ESEGUITO "
            "(ref=%s, position=%s): posizione adottata",
            order["symbol"], exc, request_id, recovered.get("position_id"),
        )
        fill = recovered

    position_id = fill.get("position_id")
    entry_price = _entry_price(deps, fill, request_id, price, order["symbol"])
    # Prima la riga a giornale, poi il registry: se register_open_position
    # solleva, il positionId è già scritto e il reconcile adotta la posizione.
    # Il dettaglio resta quello del write-ahead, reference id compreso.
    # Fill senza positionId: la riga NON passa a filled, resterebbe conclusa e
    # senza id, cioè fuori dal radar del reconcile per entrambe le sorgenti.
    deps.repo.update_execution(
        execution_id,
        ExecutionStatus.FILLED if position_id is not None else ExecutionStatus.PENDING,
        execution_price=entry_price or fill.get("execution_price"),
        etoro_position_id=int(position_id) if position_id is not None else None,
    )
    if position_id is not None:
        deps.repo.register_open_position(
            etoro_position_id=int(position_id),
            run_id=run_id,
            symbol=order["symbol"],
            instrument_id=order["instrument_id"],
            amount_usd=order["amount_usd"],
            entry_price=entry_price,
            opened_at=now,
            direction=direction,
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
    deps: ArenaDeps, run_id: str, now: datetime,
    market: dict[str, dict[str, Any]] | None = None, breaker=None,
    portfolio: Any = _UNREAD,
) -> dict[str, Any]:
    """Allinea il registry al conto reale, nei due sensi.

    APERTURE — due sorgenti, entrambe con un marcatore che le lega al bot:
    1. esecuzioni NON CONCLUSE (pending, failed, cancelled) che portano il
       reference id del nostro ordine: se il broker dice che quell'ordine è
       invece passato, la posizione è nostra;
    2. posizioni del portafoglio il cui positionId compare già nel giornale
       delle esecuzioni (l'ordine era andato, la registrazione no).

    CHIUSURE — posizioni nostre che il conto non ha più (chiusura manuale su
    eToro, margin call, o ordine di chiusura andato con la scrittura a registro
    fallita): vedi _adopt_broker_closures.

    Le posizioni che il bot non ha aperto non vengono MAI toccate.
    """
    # lettura fallita (None): nessuna conclusione sul conto, tanto meno chiusure.
    if portfolio is _UNREAD:
        portfolio = _portfolio_read(deps)
    rows = _portfolio_positions(deps, portfolio or {})
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
    # simboli con un ordine ancora in sospeso che il broker non ha confermato
    # né smentito: chi apre deve saltarli, potrebbero già essere a mercato.
    unresolved: set[str] = set()

    # 1. ordini non conclusi (write-ahead interrotto) o dati per falliti che il
    #    broker considera invece eseguiti. Query dedicata e non a numero di
    #    righe: in una giornata attiva 500 esecuzioni non coprono 24h.
    since = now - timedelta(hours=RECONCILE_LOOKBACK_HOURS)
    for execution in deps.repo.executions_by_status(_UNSETTLED_STATUSES, since):
        if execution.etoro_position_id:
            continue
        match = _REF_RE.search(execution.detail or "")
        if not match:
            continue
        fill, answered = _lookup_outcome(deps.client, match.group(1))
        if fill is None:
            if answered:
                # il broker ha smentito: esito noto, il simbolo è libero.
                # Nessun ciclo concorrente qui (il lock serializza le aperture):
                # un pending smentito è un ordine mai partito.
                if execution.status == ExecutionStatus.PENDING.value:
                    deps.repo.update_execution(execution.id, ExecutionStatus.FAILED)
            else:
                # il broker non ha risposto: l'ordine può essere a mercato.
                # La riga resta com'è (un pending NON viene declassato) e il
                # simbolo resta bloccato finché non si sa.
                unresolved.add(execution.symbol)
            continue
        position_id = int(fill["position_id"])
        # l'ordine è passato: la riga smette di essere non-conclusa, così non
        # viene ri-interrogata al broker a ogni ciclo per le prossime 24h.
        deps.repo.update_execution(
            execution.id, ExecutionStatus.FILLED,
            execution_price=fill.get("execution_price"),
            etoro_position_id=position_id,
        )
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
            direction=_adopted_direction(open_ids.get(position_id), execution),
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
            direction=_adopted_direction(row, execution),
        )
        known.add(position_id)
        adopted += 1
        logger.error(
            "live: POSIZIONE ORFANA — %s (%s) è aperta sul conto ma mancava dal "
            "registry: adottata",
            execution.symbol, position_id,
        )

    prices = {
        s: float(r["price"]) for s, r in (market or {}).items() if r.get("price")
    }
    closed = _adopt_broker_closures(deps, portfolio, open_ids, now, breaker, prices)
    return {
        "adopted": adopted,
        "unresolved_symbols": sorted(unresolved),
        "closed_externally": closed,
    }


def _adopt_broker_closures(
    deps: ArenaDeps, portfolio: dict[str, Any] | None,
    open_ids: dict[int, dict[str, Any]], now: datetime, breaker,
    prices: dict[str, float] | None = None,
) -> int:
    """Chiude a registro le posizioni del bot che il conto non ha più.

    Chiusura manuale su eToro, margin call, o ordine di chiusura eseguito con
    la scrittura a registro fallita: in tutti i casi la posizione continuerebbe
    a gonfiare exposure ed equity, falsando circuit breaker, sizing e
    /portfolio, e resterebbe lì per sempre.

    Si agisce SOLO su una lettura del portafoglio riuscita e NON VUOTA: una
    lettura fallita o parziale è indistinguibile da un conto vuoto, e chiudere
    tutto il registry sulla base di un errore di rete sarebbe il danno
    peggiore. Le posizioni aperte da pochi minuti hanno una finestra di grazia:
    un fill appena eseguito può non comparire subito nel portafoglio.

    Serve comunque una PROVA POSITIVA prima di dichiarare chiusa una posizione,
    perché una risposta parziale del broker è indistinguibile da una completa:
    o la riga in trade history, o una seconda assenza consecutiva. Chiudere a
    registro una posizione ancora viva la renderebbe irrecuperabile —
    known_position_ids() contiene anche le chiuse, quindi il reconcile non la
    riadotterebbe mai più: resterebbe senza stop loss e col simbolo di nuovo
    libero, cioè doppia esposizione.

    Il PnL è il netProfit del broker; se non è ancora pubblicato la chiusura
    resta non liquidata (stima 0) e settle_pending_closes la completerà.
    """
    if portfolio is None or not open_ids:
        return 0
    positions = deps.repo.open_positions()
    grace = now - timedelta(minutes=CLOSE_GRACE_MINUTES)
    missing = [
        p for p in positions
        if int(p.etoro_position_id) not in open_ids
        and (p.opened_at is None or p.opened_at <= grace)
    ]
    missing_ids = {int(p.etoro_position_id) for p in missing}
    # riapparsa nel portafoglio (o rientrata in grazia) = la lettura di prima
    # era parziale: il conteggio delle assenze riparte da zero.
    for position_id in list(_missing_from_portfolio):
        if position_id not in missing_ids:
            del _missing_from_portfolio[position_id]
    if not missing:
        return 0
    # history irraggiungibile: si chiude lo stesso (la posizione NON c'è più sul
    # conto) ma senza PnL, che resterà in sospeso per la liquidazione.
    by_position = _history_by_position(
        deps, now - timedelta(days=HISTORY_LOOKBACK_DAYS)
    ) or {}
    # l'esposizione delle posizioni che stiamo dichiarando sparite non fa più
    # parte dell'equity su cui il breaker misura il drawdown. Cassa assente =
    # equity ignota, non zero: si chiude comunque a registro (il conto non ha
    # più quelle posizioni) ma il breaker non viene aggiornato su un numero finto.
    cash = _as_float(portfolio.get("credit"))
    equity = None if cash is None else live_equity(
        [p for p in positions if int(p.etoro_position_id) not in missing_ids],
        prices, cash,
    )

    closed = 0
    for pos in missing:
        position_id = int(pos.etoro_position_id)
        trade = by_position.get(position_id) or {}
        pnl = _as_float(trade.get("netProfit"))
        absences = _missing_from_portfolio[position_id] = (
            _missing_from_portfolio.get(position_id, 0) + 1
        )
        if not trade and absences < _MISSING_READS_TO_CLOSE:
            logger.warning(
                "live: %s (%s) non compare nel portafoglio e non risulta in "
                "trade history: nessuna prova che sia chiusa, si aspetta la "
                "lettura successiva",
                pos.symbol, position_id,
            )
            continue
        if not record_close(
            deps.repo, pos.etoro_position_id,
            close_price=_as_float(trade.get("closeRate")),
            realized_pnl_usd=pnl if pnl is not None else 0.0,
            close_reason="chiusa fuori dal bot (assente dal portafoglio)",
            pnl_settled=pnl is not None,
        ):
            continue
        del _missing_from_portfolio[position_id]
        # il breaker deve vedere la perdita: una margin call è esattamente il
        # momento in cui il pavimento di sopravvivenza serve.
        if breaker is not None:
            if equity is None:
                logger.warning(
                    "live: chiusura esterna di %s NON contata dal circuit "
                    "breaker: cassa eToro non disponibile in questa lettura",
                    pos.symbol,
                )
            else:
                breaker.record_closed_trade(pnl or 0.0, equity)
        closed += 1
        logger.error(
            "live: POSIZIONE SPARITA DAL CONTO — %s (%s) non è più nel "
            "portafoglio: chiusa a registro con PnL %s",
            pos.symbol, pos.etoro_position_id,
            f"{pnl:.2f} USD" if pnl is not None else "ancora ignoto",
        )
    return closed


def _adopted_direction(row: dict[str, Any] | None, execution) -> str:
    """Direzione di una posizione adottata: il conto se la dichiara, altrimenti
    il lato dell'ordine che l'ha aperta a giornale."""
    return _row_direction(row) or (
        SHORT if execution.side == Side.SELL.value else LONG
    )


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
    # NIENTE uscita anticipata per LLM mancante: reconcile, liquidazione e
    # sweep di stop loss / take profit devono girare comunque. Una chiave del
    # modello scaduta non deve mai lasciare posizioni reali senza protezione.
    if market is None:
        from etoro_bot.arena.market import build_snapshot

        market = build_snapshot(deps.client, deps.settings)
    prices = {s: float(r["price"]) for s, r in market.items() if r.get("price")}
    dna = clamp_dna(champion.dna)
    run_id = _ensure_run(deps, now)

    # UNA sola lettura del portafoglio per ciclo: reconcile, controprova delle
    # direzioni, cassa e liquidazione lavorano tutti sulla stessa fotografia.
    # Letture separate sprecavano budget di rate limit e davano viste diverse
    # dello stesso conto nello stesso ciclo.
    portfolio = _portfolio_read(deps)

    # Prima di qualsiasi decisione: il libro mastro deve rispecchiare il conto
    # reale, e le chiusure in sospeso devono arrivare al circuit breaker.
    try:
        reconciled = reconcile_live_positions(
            deps, run_id, now, market=market, breaker=breaker, portfolio=portfolio
        )
    except Exception as exc:
        logger.warning("live: reconcile fallito: %s", exc)
        reconciled = {"adopted": 0}
    try:
        settled = settle_pending_closes(
            deps, breaker=breaker, now=now, portfolio=portfolio
        )
    except Exception as exc:
        logger.warning("live: liquidazione PnL fallita: %s", exc)
        settled = {"settled": 0}

    positions = deps.repo.open_positions()
    # sola controprova del registro, dalla lettura già in mano
    broker_directions = live_directions(deps, portfolio)
    # Capitale reale: se il broker non lo pubblica NON vale zero. Il ciclo non
    # deciderà e non aprirà (sizing e breaker sarebbero calcolati sul nulla), ma
    # le protezioni qui sotto girano lo stesso: una chiusura non si nega mai.
    cash = _as_float((portfolio or {}).get("credit"))
    if cash is None:
        logger.error(
            "live: capitale eToro non disponibile: stop loss e take profit "
            "girano comunque, decisione e aperture SALTATE"
        )
    # senza credit certo l'equity resta la sola esposizione nota: sottostimata,
    # quindi il breaker è più prudente, mai più permissivo.
    equity = live_equity(positions, prices, cash or 0.0)

    # stop loss / take profit del DNA (0 = disattivati: decide solo il campione).
    # Girano PRIMA della chiamata all'LLM: un modello lento non deve poter
    # ritardare uno stop loss.
    for pos in list(positions):
        price = prices.get(pos.symbol)
        if not price or not pos.entry_price:
            continue
        direction = sweep_direction(pos, broker_directions)
        if direction is None:
            # mai assumere long: su uno short lo stop loss scatterebbe in
            # guadagno e il take profit in perdita.
            logger.error(
                "live: direzione della posizione %s (%s) non accertabile: "
                "stop loss e take profit automatici SALTATI su questa posizione",
                pos.etoro_position_id, pos.symbol,
            )
            continue
        # stesse soglie e stessi messaggi del ciclo simulato (arena/dna.py)
        reason = risk_close_reason(dna, position_change_pct(pos, price, direction))
        if reason is None:
            continue
        try:
            _close_real_position(deps, breaker, run_id, pos, price, reason, equity,
                                 direction, now=now)
        except Exception as exc:
            # una chiusura fallita non deve impedire le altre: sono stop loss
            logger.warning("live: chiusura automatica %s fallita: %s", pos.symbol, exc)

    slot = _slot_key(deps, now)
    protections = {
        "adopted": reconciled.get("adopted", 0),
        "closed_externally": reconciled.get("closed_externally", 0),
        "settled": settled.get("settled", 0),
    }
    # Da qui in giù si DECIDE e si APRE: serve capitale certo e serve l'LLM. Le
    # protezioni sopra sono già girate, quindi mancare l'uno o l'altro degrada
    # il ciclo a sola sorveglianza invece di lasciare le posizioni scoperte.
    # niente rilettura del portafoglio: la cassa è quella di inizio ciclo, quindi
    # non conta ancora l'incasso delle chiusure automatiche qui sopra. Sizing e
    # riserva restano più stretti del vero — prudente, e una lettura in meno.
    positions = deps.repo.open_positions()
    skipped = "no_cash" if cash is None else "no_llm" if deps.llm is None else None
    if skipped is not None:
        if skipped == "no_llm":
            logger.warning(
                "live: nessun LLM configurato: protezioni eseguite, nessuna "
                "nuova decisione e nessuna apertura"
            )
        summary = {"opened": 0, "closed": 0, "blocked": 0, "skipped": skipped,
                   **protections}
        deps.repo.finish_run(run_id, {"cycle": slot, **summary})
        return summary

    equity = live_equity(positions, prices, cash)
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
            f"- {p.symbol} {(registry_direction(p) or 'direzione ignota').upper()}: "
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
    # una posizione si tocca UNA volta per ciclo: `enforce` già deduplica le
    # azioni, ma un `close` senza direzione e uno con direzione sullo stesso
    # simbolo restano due azioni diverse che colpirebbero la stessa riga. Il
    # marchio si mette PRIMA del tentativo: un ordine andato in timeout può
    # essere passato lo stesso, e un secondo invio sarebbe una doppia chiusura.
    closed_ids: set[int] = set()
    for close in closes:
        wanted = close.get("direction")
        for pos in [p for p in positions if p.symbol == close["symbol"]]:
            if int(pos.etoro_position_id) in closed_ids:
                continue
            # la chiusura mirata si seleziona sul registro: l'ordine parte per
            # position_id ed è agnostico alla direzione, rifiutarlo lascerebbe
            # la posizione ingestibile. La stima di PnL invece resta firmata
            # solo se anche il conto conferma (sweep_direction).
            if wanted and registry_direction(pos) != wanted:
                continue
            closed_ids.add(int(pos.etoro_position_id))
            try:
                _close_real_position(deps, breaker, run_id, pos, prices.get(pos.symbol),
                                     close["reason"] or "chiusura live", equity,
                                     sweep_direction(pos, broker_directions), now=now)
                executed["closed"] += 1
            except Exception as exc:
                logger.warning("live: chiusura %s fallita: %s", close["symbol"], exc)

    blocks_openings = breaker is not None and breaker.blocks_openings()
    # Ordini che il broker non ha né confermato né smentito: quel simbolo
    # potrebbe già essere a mercato senza comparire nel registry. Con
    # allow_pyramiding attivo held_symbols non basta a fermare il doppione.
    unresolved_symbols = set(reconciled.get("unresolved_symbols") or ())
    for order in opens:
        if kill_switch_active() or blocks_openings:
            executed["blocked"] += 1
            continue
        if order["symbol"] in unresolved_symbols:
            logger.error(
                "live: apertura %s SALTATA: un ordine precedente sullo stesso "
                "simbolo è ancora irrisolto (il broker non conferma né smentisce): "
                "aprire ora rischierebbe la doppia esposizione",
                order["symbol"],
            )
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
            # la riga a giornale esiste già (write-ahead) e porta il reference
            # id: resta pending se non sappiamo l'esito, failed se il broker ha
            # confermato il non-eseguito. In entrambi i casi il reconcile del
            # ciclo successivo può ancora scoprire che l'ordine era passato.
            logger.warning("live: apertura %s fallita: %s", order["symbol"], exc)
    for item in closes + [dict(o, action="open") for o in opens]:
        deps.repo.add_decision(run_id, item.get("symbol", "?"), "trader",
                               {k: v for k, v in item.items() if k != "instrument_id"})
    summary = {**executed, **protections}
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
    # una sola lettura del portafoglio: da lì liquidità e controprova delle
    # direzioni (una seconda chiamata al broker non aggiungerebbe nulla).
    try:
        portfolio = deps.client.get_portfolio() or {}
    except Exception as exc:
        logger.warning("live: portafoglio non disponibile a fine sessione: %s", exc)
        portfolio = {}
    cash = _as_float(portfolio.get("credit"))
    if cash is None:
        # qui NON si salta: la chiusura di fine sessione va fatta comunque. Con
        # la sola esposizione l'equity è sottostimata, quindi il breaker misura
        # un drawdown più grande del vero: prudente, mai permissivo.
        logger.warning(
            "live: cassa eToro non disponibile a fine sessione %s: equity "
            "sottostimata per il circuit breaker", market_name,
        )
    broker_directions = live_directions(deps, portfolio)
    equity = live_equity(deps.repo.open_positions(), prices, cash or 0.0)
    closed = 0
    for pos in expired:
        try:
            held = trading_days_held(pos.opened_at, now)
            _close_real_position(
                deps, breaker, run_id, pos, prices.get(pos.symbol),
                f"fine sessione {market_name}: holding massimo raggiunto "
                f"({held}/{max_days} giorni)", equity,
                sweep_direction(pos, broker_directions), now=now,
            )
            closed += 1
        except Exception as exc:
            logger.warning("live: chiusura sessione %s fallita: %s", pos.symbol, exc)
    return {"closed": closed, "market": market_name}


def run_live_eod(
    deps: ArenaDeps, breaker=None, now: datetime | None = None,
    market: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ultima campanella live: liquida i PnL in sospeso e fotografa l'equity.

    Le chiusure per scadenza holding avvengono ai singoli fine-sessione.
    """
    if not _live_lock.acquire(timeout=_EOD_LOCK_WAIT_S):
        logger.error("live: EOD saltato, ciclo bloccato")
        return {"snapshot": False, "skipped": "cycle_in_progress"}
    try:
        return _run_live_eod_locked(deps, breaker, now, market)
    finally:
        _live_lock.release()


def _run_live_eod_locked(
    deps: ArenaDeps, breaker, now: datetime | None,
    market: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
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
        # snapshot mark-to-market come /portfolio e come il breaker: una serie
        # equity a costo storico si muoverebbe solo alle chiusure.
        prices = {
            s: float(r["price"]) for s, r in (market or {}).items() if r.get("price")
        }
        exposure = sum(live_position_value(p, prices.get(p.symbol)) for p in positions)
        deps.repo.record_equity_snapshot(now.date(), cash + exposure, cash, exposure)
        return {"snapshot": True, "equity_usd": round(cash + exposure, 2),
                "settled": settled}
    except Exception as exc:
        logger.warning("live: snapshot equity fallito: %s", exc)
        return {"snapshot": False, "settled": settled}
