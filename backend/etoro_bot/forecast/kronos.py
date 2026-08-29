"""Servizio di forecast Kronos (foundation model per candele, MIT).

Modelli `NeoQuasar/Kronos-small` (24.7M, contesto 512) o `Kronos-mini` (4.1M,
contesto 2048) da HuggingFace; il codice del modello è vendorizzato in
`forecast/vendor/` (repo shiyu-coder/Kronos). torch è un extra opzionale
(`pip install -e backend[forecast]`): senza, tutto degrada a no-op e lo
snapshot resta identico a prima.

Uso (dal piano, framing RankIC del paper): il segnale è più credibile come
RANKING cross-sezionale che come forecast puntuale — `kronos_rank` è il
percentile del ritorno predetto sull'universo. `kronos_vol_pred` (range medio
delle candele predette) serve a sizing/stop del risk judge. Gira nel job news
giornaliero, MAI nel percorso critico del ciclo; i risultati restano in cache
di modulo e vengono fusi nello snapshot da `annotate_snapshot`.

ponytail: niente kronos_up_prob — servirebbero i path Monte-Carlo pre-media
che il predictor vendorizzato non espone; da aggiungere se si patcha il vendor.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "enabled": False,          # opt-in: richiede l'extra torch installato
    "model": "NeoQuasar/Kronos-small",
    "tokenizer": "NeoQuasar/Kronos-Tokenizer-base",
    "lookback": 200,           # barre giornaliere di contesto (<= max_context 512)
    "pred_len": 5,             # orizzonte in barre
    "sample_count": 3,         # path campionati (mediati dal predictor)
    "max_symbols": 40,         # bound sul costo di inferenza
}

_FORECASTS: dict[str, dict[str, float]] = {}
_FORECASTS_TS: float = 0.0
_lock = threading.Lock()
_predictor: Any = None
_torch_missing_logged = False


def kronos_config(settings: dict[str, Any]) -> dict[str, Any]:
    raw = (settings.get("forecast") or {}).get("kronos") or {}
    return {**DEFAULTS, **{k: raw[k] for k in raw if k in DEFAULTS}}


def summarize_prediction(last_close: float, pred_rows: list[dict]) -> dict[str, float] | None:
    """Segnali da un path predetto: ritorno atteso e volatilità (range medio)."""
    if not pred_rows or not last_close:
        return None
    closes = [float(r.get("close") or 0.0) for r in pred_rows]
    if not closes or closes[-1] <= 0:
        return None
    ranges = [
        (float(r.get("high") or 0.0) - float(r.get("low") or 0.0)) / float(r["close"])
        for r in pred_rows
        if r.get("close")
    ]
    return {
        "kronos_ret_pred": round((closes[-1] / last_close - 1.0) * 100.0, 2),
        "kronos_vol_pred": round(sum(ranges) / len(ranges) * 100.0, 2) if ranges else 0.0,
    }


def cross_rank(forecasts: dict[str, dict[str, float]]) -> None:
    """Aggiunge `kronos_rank` (percentile 0-100 del ritorno predetto) in place."""
    rets = sorted(
        (f["kronos_ret_pred"], s) for s, f in forecasts.items()
        if f.get("kronos_ret_pred") is not None
    )
    n = len(rets)
    for i, (_, symbol) in enumerate(rets):
        forecasts[symbol]["kronos_rank"] = round(100.0 * i / max(n - 1, 1), 1)


def _load_predictor(cfg: dict[str, Any]) -> Any:
    global _predictor, _torch_missing_logged
    if _predictor is not None:
        return _predictor
    try:
        from etoro_bot.forecast.vendor import Kronos, KronosPredictor, KronosTokenizer

        tokenizer = KronosTokenizer.from_pretrained(cfg["tokenizer"])
        model = Kronos.from_pretrained(cfg["model"])
        _predictor = KronosPredictor(model, tokenizer, max_context=512)
        logger.info("kronos: modello %s caricato su %s", cfg["model"], _predictor.device)
        return _predictor
    except Exception as exc:
        if not _torch_missing_logged:
            _torch_missing_logged = True
            logger.warning(
                "kronos: non disponibile (%s) — installare l'extra [forecast]", exc
            )
        return None


def refresh_forecasts(client: Any, settings: dict[str, Any]) -> dict[str, Any]:
    """Inferenza batch sull'universo; risultati nella cache di modulo."""
    global _FORECASTS_TS
    cfg = kronos_config(settings)
    if not cfg["enabled"] or client is None:
        return {"skipped": "disabled" if not cfg["enabled"] else "no_client"}
    predictor = _load_predictor(cfg)
    if predictor is None:
        return {"skipped": "model_unavailable"}

    import pandas as pd

    from etoro_bot.arena.replay import load_history

    symbols = [str(s).upper() for s in settings.get("watchlist") or []]
    symbols = symbols[: int(cfg["max_symbols"])]
    candles, _meta = load_history(client, symbols, int(cfg["lookback"]))

    df_list, ts_list, y_ts_list, order = [], [], [], []
    lookback = int(cfg["lookback"])
    pred_len = int(cfg["pred_len"])
    for symbol, rows in candles.items():
        rows = rows[-lookback:]
        if len(rows) < lookback:
            continue  # predict_batch vuole lookback identico per tutti
        frame = pd.DataFrame(
            {
                "open": [float(r.get("open") or r["close"]) for r in rows],
                "high": [float(r.get("high") or r["close"]) for r in rows],
                "low": [float(r.get("low") or r["close"]) for r in rows],
                "close": [float(r["close"]) for r in rows],
                "volume": [float(r.get("volume") or 0.0) for r in rows],
                "amount": [0.0] * len(rows),
            }
        )
        x_ts = pd.Series(pd.to_datetime([r.get("fromDate") for r in rows]))
        y_ts = pd.Series(
            pd.date_range(x_ts.iloc[-1], periods=pred_len + 1, freq="B")[1:]
        )
        df_list.append(frame)
        ts_list.append(x_ts)
        y_ts_list.append(y_ts)
        order.append(symbol)
    if not df_list:
        return {"skipped": "no_history"}

    started = time.monotonic()
    try:
        preds = predictor.predict_batch(
            df_list, ts_list, y_ts_list, pred_len=pred_len,
            T=1.0, top_p=0.9, sample_count=int(cfg["sample_count"]), verbose=False,
        )
    except Exception as exc:
        logger.warning("kronos: inferenza fallita: %s", exc)
        return {"error": str(exc)}

    forecasts: dict[str, dict[str, float]] = {}
    for symbol, frame, pred in zip(order, df_list, preds):
        summary = summarize_prediction(
            float(frame["close"].iloc[-1]), pred.to_dict("records")
        )
        if summary:
            forecasts[symbol] = summary
    cross_rank(forecasts)
    with _lock:
        _FORECASTS.clear()
        _FORECASTS.update(forecasts)
        _FORECASTS_TS = time.time()
    elapsed = round(time.monotonic() - started, 1)
    logger.info("kronos: forecast per %d simboli in %.1fs", len(forecasts), elapsed)
    return {"symbols": len(forecasts), "seconds": elapsed}


def cached_forecasts() -> dict[str, dict[str, float]]:
    with _lock:
        return {s: dict(f) for s, f in _FORECASTS.items()}


def annotate_snapshot(snapshot: dict[str, dict[str, Any]]) -> None:
    """Fonde i forecast in cache nello snapshot (campi + riga view), in place."""
    forecasts = cached_forecasts()
    if not forecasts:
        return
    for symbol, row in snapshot.items():
        f = forecasts.get(symbol)
        if not f:
            continue
        row.update(f)
        rank = f.get("kronos_rank")
        bits = f"kronos {f['kronos_ret_pred']:+.1f}%"
        if rank is not None:
            bits += f" (rank {rank:.0f})"
        row["view"] = f"{row.get('view', symbol)} | {bits}"
