"""Test dello scheduler multi-sessione: Europa + USA, slot e prossimo ciclo."""

from __future__ import annotations

from datetime import datetime, timezone

from etoro_bot.services.scheduler import (
    cycle_slot,
    final_session,
    market_is_open,
    market_sessions,
    next_cycle_at,
    open_sessions,
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
