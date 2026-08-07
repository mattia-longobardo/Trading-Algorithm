"""Scheduler dell'arena: sessioni di borsa Europa + USA, cicli intraday, EOD.

Tutto in UTC. Un solo job APScheduler al minuto (tick) che rilegge le settings:
- evoluzione mensile: primo tick di ogni giorno (no-op se il mese non è cambiato);
- news: una volta al giorno, 30' prima della prima apertura;
- ciclo di trading (training + live): ogni `cycle_minutes` quando almeno una
  sessione è aperta;
- EOD per sessione: alla chiusura di ogni mercato si chiudono le posizioni di
  QUEL mercato (day trading); alla chiusura dell'ultima sessione parte anche
  la riflessione serale degli agenti e lo snapshot equity.

I job girano in thread dedicati: il tick non si blocca mai e lo stesso job
non corre mai in due istanze sovrapposte.

Lo stato del tick (ultimo slot eseguito, EOD fatti, giorno di evoluzione e
news) è persistito su file JSON in `state/`: un riavvio a metà slot NON deve
rilanciare il ciclo, perché sarebbe una nuova decisione LLM — cioè un possibile
secondo trade nella stessa finestra. Lo stato si scrive PRIMA di lanciare il
job, come marcatura: meglio un ciclo saltato che uno doppio. L'EOD è l'unica
eccezione (si marca DOPO): chiude posizioni vere, rifarlo è un no-op, saltarlo
lascerebbe esposizione aperta oltre la campanella.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("etoro_bot.scheduler")

STATE_FILENAME = "scheduler_state.json"

# Sessioni di default (UTC, orari estivi): Europa 09:00-17:30 CEST,
# USA 9:30-16:00 ET. Configurabili da settings.yaml arena.markets.
DEFAULT_SESSIONS: dict[str, tuple[str, str]] = {
    "europe": ("07:00", "15:30"),
    "usa": ("13:30", "20:00"),
}
# Cadenza di default dei cicli di decisione (era 5'): un quarto d'ora dà agli
# agenti abbastanza prezzo nuovo da giudicare e triplica il margine sui rate
# limit del broker. Ogni ciclo è un'occasione di operare per ogni agente: la
# frequenza qui e max_orders_per_cycle nel DNA sono i due moltiplicatori del
# volume di trade.
DEFAULT_CYCLE_MINUTES = 15


def _parse_hhmm(value: Any, fallback: str) -> time:
    try:
        hh, mm = str(value).split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        hh, mm = fallback.split(":")
        return time(int(hh), int(mm))


def arena_cfg(settings: dict[str, Any]) -> dict[str, Any]:
    return settings.get("arena") or {}


def market_sessions(settings: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """name → (open_utc, close_utc). Config nuova, legacy o default EU+USA."""
    cfg = arena_cfg(settings)
    raw = cfg.get("markets")
    if isinstance(raw, dict) and raw:
        out: dict[str, tuple[str, str]] = {}
        for name, window in raw.items():
            window = window or {}
            fallback = DEFAULT_SESSIONS.get(str(name), ("13:30", "20:00"))
            out[str(name)] = (
                str(window.get("open_utc", fallback[0])),
                str(window.get("close_utc", fallback[1])),
            )
        return out
    if cfg.get("market_open_utc") or cfg.get("market_close_utc"):  # config legacy
        return {
            "usa": (
                str(cfg.get("market_open_utc", "13:30")),
                str(cfg.get("market_close_utc", "20:00")),
            )
        }
    return dict(DEFAULT_SESSIONS)


def _session_times(settings: dict[str, Any]) -> dict[str, tuple[time, time]]:
    return {
        name: (_parse_hhmm(o, "13:30"), _parse_hhmm(c, "20:00"))
        for name, (o, c) in market_sessions(settings).items()
    }


def open_sessions(settings: dict[str, Any], now: datetime) -> list[str]:
    """Sessioni aperte adesso (giorni feriali UTC), in ordine stabile."""
    if now.weekday() >= 5:
        return []
    return [
        name
        for name, (open_t, close_t) in sorted(_session_times(settings).items())
        if open_t <= now.time() < close_t
    ]


def market_is_open(settings: dict[str, Any], now: datetime) -> bool:
    return bool(open_sessions(settings, now))


def _earliest_open(settings: dict[str, Any]) -> time:
    return min(t for t, _ in _session_times(settings).values())


def final_session(settings: dict[str, Any]) -> str:
    """La sessione che chiude per ultima: al suo EOD parte la riflessione."""
    times = _session_times(settings)
    return max(times, key=lambda name: times[name][1])


def cycle_slot(settings: dict[str, Any], now: datetime) -> int | None:
    """Indice del ciclo dalla prima apertura del giorno; None a mercati chiusi."""
    if not open_sessions(settings, now):
        return None
    open_t = _earliest_open(settings)
    minutes = int(arena_cfg(settings).get("cycle_minutes", DEFAULT_CYCLE_MINUTES)) \
        or DEFAULT_CYCLE_MINUTES
    elapsed = (now.hour * 60 + now.minute) - (open_t.hour * 60 + open_t.minute)
    return elapsed // minutes


def cycle_slot_key(settings: dict[str, Any], now: datetime) -> str:
    """Chiave stabile del ciclo logico: "YYYYMMDD#<indice>".

    È la base delle chiavi di idempotenza degli ordini: un ciclo che va in
    crash e viene ritentato dentro la stessa finestra deve produrre gli
    STESSI request id, cosa che l'orologio al minuto non garantiva. A mercati
    chiusi (EOD, trigger manuali) l'indice si calcola comunque, a partire da
    mezzanotte UTC, con la stessa cadenza.
    """
    slot = cycle_slot(settings, now)
    if slot is not None:
        return f"{now:%Y%m%d}#{slot}"
    minutes = int(arena_cfg(settings).get("cycle_minutes", DEFAULT_CYCLE_MINUTES)) \
        or DEFAULT_CYCLE_MINUTES
    return f"{now:%Y%m%d}#off{(now.hour * 60 + now.minute) // minutes}"


def next_cycle_at(settings: dict[str, Any], now: datetime | None = None) -> str:
    """Prossimo ciclo di trading in ISO 8601 UTC (per la UI)."""
    now = now or datetime.now(timezone.utc)
    open_t = _earliest_open(settings)
    minutes = int(arena_cfg(settings).get("cycle_minutes", DEFAULT_CYCLE_MINUTES)) \
        or DEFAULT_CYCLE_MINUTES
    open_minutes = open_t.hour * 60 + open_t.minute
    probe = now.replace(second=0, microsecond=0)
    for _ in range(10 * 24 * 60):
        probe += timedelta(minutes=1)
        if cycle_slot(settings, probe) is None:
            continue
        if ((probe.hour * 60 + probe.minute) - open_minutes) % minutes == 0:
            return probe.isoformat()
    return (now + timedelta(days=1)).replace(
        hour=open_t.hour, minute=open_t.minute, second=0, microsecond=0
    ).isoformat()


def state_path(state_dir: str | Path | None = None) -> Path:
    base = Path(
        state_dir
        or os.environ.get("STATE_DIR", os.environ.get("KILL_SWITCH_DIR", "."))
    )
    return base / STATE_FILENAME


def _empty_state() -> dict[str, Any]:
    return {
        "evolve_day": None,
        "news_day": None,
        "cycle_slot": None,
        "eod_done": set(),  # {"YYYY-MM-DD#sessione"}
    }


def _load_state(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {
            "evolve_day": raw.get("evolve_day"),
            "news_day": raw.get("news_day"),
            "cycle_slot": raw.get("cycle_slot"),
            "eod_done": set(raw.get("eod_done") or ()),
        }
    except FileNotFoundError:
        return _empty_state()  # primo avvio
    except (OSError, json.JSONDecodeError, TypeError, AttributeError):
        # corrotto o di forma sbagliata (JSON valido ma non dict): si riparte da
        # zero, al più un ciclo in più. Mai lasciare il processo senza scheduler.
        log.warning("stato scheduler illeggibile (%s): si riparte da zero", path)
        return _empty_state()


def _save_state(path: Path, state: dict[str, Any]) -> None:
    """Scrittura atomica tmp+replace, come il circuit breaker."""
    payload = {**state, "eod_done": sorted(state["eod_done"])}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        log.exception("stato scheduler non salvato (%s)", path)


def start_scheduler(
    get_settings: Callable[[], dict[str, Any]],
    *,
    cycle_job: Callable[[], None],
    eod_job: Callable[[str, bool], None],
    evolve_job: Callable[[], None],
    news_job: Callable[[], None],
    state_dir: str | Path | None = None,
):
    """Avvia APScheduler; eod_job riceve (mercato, ultima_sessione_del_giorno)."""
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler(timezone="UTC")
    path = state_path(state_dir)
    state = _load_state(path)
    running: set[str] = set()  # job lunghi in thread: mai due istanze uguali
    save_lock = threading.Lock()

    def _persist() -> None:
        """Snapshot + scrittura nella STESSA sezione critica.

        Il tick e i thread EOD salvano lo stesso dict: senza lock un EOD che
        snapshota prima che il tick scriva `cycle_slot` e salva dopo
        rimetterebbe su disco lo slot vecchio — al riavvio il ciclo ripartirebbe
        nella stessa finestra, cioè il trade doppio che tutto questo evita. E
        due `replace` sullo stesso .tmp lascerebbero un file troncato.
        """
        with save_lock:
            _save_state(path, state)

    def tick(now: datetime | None = None) -> None:
        try:
            settings = get_settings()
        except Exception:
            log.warning("settings non disponibili, tick saltato", exc_info=True)
            return
        now = now or datetime.now(timezone.utc)
        today = now.date().isoformat()

        if state["evolve_day"] != today:
            state["evolve_day"] = today
            # giorno nuovo: gli EOD dei giorni passati non servono più
            state["eod_done"] = {k for k in state["eod_done"] if k.startswith(today)}
            _persist()
            _safe(evolve_job, "evolve")

        if now.weekday() >= 5:
            return

        open_t = _earliest_open(settings)
        news_at = (
            datetime.combine(now.date(), open_t, tzinfo=timezone.utc)
            - timedelta(minutes=30)
        )
        if state["news_day"] != today and now >= news_at:
            state["news_day"] = today
            _persist()
            _safe(news_job, "news")

        slot = cycle_slot(settings, now)
        slot_key = None if slot is None else f"{today}#{slot}"
        if slot_key is not None and state["cycle_slot"] != slot_key:
            state["cycle_slot"] = slot_key
            _persist()
            _safe(cycle_job, "cycle")

        last = final_session(settings)
        for name, (_open_t, close_t) in _session_times(settings).items():
            key = f"{today}#{name}"
            if key not in state["eod_done"] and now.time() >= close_t:
                # L'EOD si marca DOPO: chiude posizioni vere, e un'esecuzione
                # persa lascerebbe esposizione aperta oltre la campanella,
                # mentre rifarlo è un no-op (itera sulle posizioni ancora
                # aperte). Il doppio lancio in-process lo impedisce `running`.
                def _eod(n=name, k=key) -> None:
                    eod_job(n, n == last)
                    state["eod_done"].add(k)
                    _persist()

                _safe(_eod, f"eod-{name}")

    def _safe(job: Callable[[], None], name: str) -> None:
        """Esegue il job in un thread: il tick non si blocca mai (news e cicli
        durano minuti) e lo stesso job non gira mai in due istanze."""
        if name in running:
            log.warning("job '%s' ancora in corso: questa occorrenza è saltata", name)
            return
        running.add(name)

        def _run() -> None:
            try:
                job()
            except Exception:
                log.exception("job schedulato '%s' fallito", name)
            finally:
                running.discard(name)

        threading.Thread(target=_run, daemon=True, name=f"arena-{name}").start()

    scheduler.add_job(tick, "interval", minutes=1, id="etoro-bot-tick")
    scheduler.start()
    return scheduler
