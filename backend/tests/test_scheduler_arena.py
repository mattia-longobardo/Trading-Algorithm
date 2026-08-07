"""Test dello scheduler multi-sessione: Europa + USA, slot e prossimo ciclo."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from etoro_bot.services.scheduler import (
    cycle_slot,
    final_session,
    market_is_open,
    market_sessions,
    next_cycle_at,
    open_sessions,
    start_scheduler,
)

SETTINGS = {
    "arena": {
        "cycle_minutes": 60,
        "markets": {
            "europe": {"open_utc": "07:00", "close_utc": "15:30"},
            "usa": {"open_utc": "13:30", "close_utc": "20:00"},
        },
    }
}


def _utc(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def test_open_sessions_by_time_of_day():
    # lunedì: solo Europa al mattino, sovrapposizione al pomeriggio, solo USA la sera
    assert open_sessions(SETTINGS, _utc(2026, 7, 27, 8, 0)) == ["europe"]
    assert open_sessions(SETTINGS, _utc(2026, 7, 27, 14, 0)) == ["europe", "usa"]
    assert open_sessions(SETTINGS, _utc(2026, 7, 27, 16, 0)) == ["usa"]
    assert open_sessions(SETTINGS, _utc(2026, 7, 27, 6, 59)) == []
    assert open_sessions(SETTINGS, _utc(2026, 7, 27, 20, 0)) == []
    assert open_sessions(SETTINGS, _utc(2026, 7, 26, 10, 0)) == []  # domenica


def test_market_is_open_union():
    assert market_is_open(SETTINGS, _utc(2026, 7, 27, 7, 0))
    assert market_is_open(SETTINGS, _utc(2026, 7, 27, 19, 59))
    assert not market_is_open(SETTINGS, _utc(2026, 7, 27, 20, 0))
    assert not market_is_open(SETTINGS, _utc(2026, 7, 25, 10, 0))  # sabato


def test_cycle_slot_counts_from_earliest_open():
    assert cycle_slot(SETTINGS, _utc(2026, 7, 27, 7, 0)) == 0
    assert cycle_slot(SETTINGS, _utc(2026, 7, 27, 8, 0)) == 1
    assert cycle_slot(SETTINGS, _utc(2026, 7, 27, 19, 59)) == 12
    assert cycle_slot(SETTINGS, _utc(2026, 7, 27, 20, 0)) is None


def test_final_session_is_the_last_to_close():
    assert final_session(SETTINGS) == "usa"


def test_next_cycle_at_evening_goes_to_europe_open():
    nxt = next_cycle_at(SETTINGS, _utc(2026, 7, 27, 21, 0))
    assert nxt.startswith("2026-07-28T07:00")


def test_next_cycle_at_friday_evening_skips_weekend():
    nxt = next_cycle_at(SETTINGS, _utc(2026, 7, 24, 21, 0))
    assert nxt.startswith("2026-07-27T07:00")


def test_legacy_single_market_config_still_works():
    legacy = {"arena": {"market_open_utc": "13:30", "market_close_utc": "20:00"}}
    assert market_sessions(legacy) == {"usa": ("13:30", "20:00")}
    assert not market_is_open(legacy, _utc(2026, 7, 27, 8, 0))
    assert market_is_open(legacy, _utc(2026, 7, 27, 14, 0))


def test_defaults_include_both_markets():
    assert open_sessions({}, _utc(2026, 7, 27, 8, 0)) == ["europe"]
    assert open_sessions({}, _utc(2026, 7, 27, 14, 0)) == ["europe", "usa"]


# --- invariante mono-processo: stato persistente + advisory lock -------------


def _join_arena_threads() -> None:
    """I job girano in thread `arena-*`: aspetta che finiscano davvero."""
    for t in threading.enumerate():
        if t.name.startswith("arena-"):
            t.join(2.0)


class _Jobs:
    """Job finti: registrano le chiamate (i job veri girano in thread)."""

    def __init__(self):
        self.calls: list[str] = []
        self._done = threading.Event()

    def _record(self, name: str) -> None:
        self.calls.append(name)
        self._done.set()

    def cycle(self) -> None:
        self._record("cycle")

    def eod(self, market: str, final: bool) -> None:
        self._record(f"eod:{market}")

    def evolve(self) -> None:
        self._record("evolve")

    def news(self) -> None:
        self._record("news")

    def settle(self) -> None:
        """Attende i thread appena lanciati (o conferma che non ce ne sono).

        Il record non basta: l'EOD marca lo stato DOPO il job, quindi bisogna
        aspettare la fine dei thread, non la prima chiamata.
        """
        self._done.wait(2.0)
        _join_arena_threads()
        self._done.clear()


def _tick_of(jobs: _Jobs, state_dir):
    """Avvia lo scheduler e restituisce il suo tick (il job APScheduler)."""
    sched = start_scheduler(
        get_settings=lambda: SETTINGS,
        cycle_job=jobs.cycle,
        eod_job=jobs.eod,
        evolve_job=jobs.evolve,
        news_job=jobs.news,
        state_dir=state_dir,
    )
    tick = sched.get_job("etoro-bot-tick").func
    sched.shutdown(wait=False)  # il tick lo chiamiamo noi, col tempo che vogliamo
    return tick


def test_restart_within_the_same_slot_does_not_rerun_the_cycle(tmp_path):
    first = _Jobs()
    tick = _tick_of(first, tmp_path)
    tick(now=_utc(2026, 7, 27, 8, 0))  # slot 1
    first.settle()
    assert "cycle" in first.calls

    # riavvio: nuovo processo, stesso file di stato, stesso slot
    second = _Jobs()
    tick = _tick_of(second, tmp_path)
    tick(now=_utc(2026, 7, 27, 8, 30))  # ancora slot 1
    second.settle()
    assert "cycle" not in second.calls
    assert "evolve" not in second.calls  # evoluzione del giorno già fatta

    tick(now=_utc(2026, 7, 27, 9, 0))  # slot 2: si riparte
    second.settle()
    assert "cycle" in second.calls


def test_restart_after_eod_does_not_close_the_session_twice(tmp_path):
    first = _Jobs()
    tick = _tick_of(first, tmp_path)
    tick(now=_utc(2026, 7, 27, 20, 0))  # entrambe le sessioni chiuse
    first.settle()
    assert {"eod:europe", "eod:usa"} <= set(first.calls)

    second = _Jobs()
    tick = _tick_of(second, tmp_path)
    tick(now=_utc(2026, 7, 27, 20, 5))
    second.settle()
    assert not [c for c in second.calls if c.startswith("eod:")]


def test_eod_is_retried_if_it_crashes(tmp_path):
    """L'EOD chiude posizioni vere: se fallisce DEVE ripartire al tick dopo."""
    boom = _Jobs()
    boom.eod = lambda market, final: (_ for _ in ()).throw(RuntimeError("crash"))
    tick = _tick_of(boom, tmp_path)
    tick(now=_utc(2026, 7, 27, 20, 0))
    _join_arena_threads()  # il job solleva: nessun record da attendere
    assert json.loads((tmp_path / "scheduler_state.json").read_text())["eod_done"] == []

    retry = _Jobs()
    tick = _tick_of(retry, tmp_path)
    tick(now=_utc(2026, 7, 27, 20, 5))
    retry.settle()
    assert {"eod:europe", "eod:usa"} <= set(retry.calls)


def test_corrupt_state_file_does_not_kill_the_scheduler(tmp_path):
    (tmp_path / "scheduler_state.json").write_text('"non un dict"')
    jobs = _Jobs()
    tick = _tick_of(jobs, tmp_path)  # niente TypeError alla lettura
    tick(now=_utc(2026, 7, 27, 8, 0))
    jobs.settle()
    assert "cycle" in jobs.calls


def test_state_file_is_written_atomically(tmp_path):
    jobs = _Jobs()
    tick = _tick_of(jobs, tmp_path)
    tick(now=_utc(2026, 7, 27, 20, 0))
    jobs.settle()
    saved = json.loads((tmp_path / "scheduler_state.json").read_text())
    assert saved["cycle_slot"] is None  # mercati chiusi alle 20:00
    assert saved["evolve_day"] == "2026-07-27"
    assert sorted(saved["eod_done"]) == ["2026-07-27#europe", "2026-07-27#usa"]
    assert not list(tmp_path.glob("*.tmp"))


def test_second_instance_does_not_get_the_scheduler_lock(pg_url):
    """Advisory lock Postgres: solo il primo processo avvia lo scheduler."""
    from etoro_bot.db.repo import try_scheduler_lock

    first = try_scheduler_lock(pg_url)
    assert first is not None
    try:
        assert try_scheduler_lock(pg_url) is None  # seconda istanza: niente scheduler
    finally:
        first.close()  # il processo muore → lock rilasciato
    again = try_scheduler_lock(pg_url)
    assert again is not None
    again.close()
