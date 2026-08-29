"""Test della decay state machine del campione live."""

from __future__ import annotations

from datetime import date, timedelta

from etoro_bot.arena.champion_health import (
    evaluate_champion_health,
    rolling_metrics,
)
from etoro_bot.arena.dna import DEFAULT_DNA, clamp_dna
from etoro_bot.arena.engine import ArenaDeps
from etoro_bot.safety.mandate import Mandate

SETTINGS = {
    "arena": {
        "champion_decay": {
            "enabled": True, "window_days": 30,
            "min_sharpe": -0.5, "max_drawdown_pct": 15, "breach_days": 2,
        }
    }
}


def _deps(repo):
    return ArenaDeps(repo=repo, client=None, settings=SETTINGS, llm=None,
                     model="m", max_tokens=64, mandate=Mandate.unlimited())


def test_rolling_metrics_drawdown_and_sharpe():
    flat = [100.0] * 10
    m = rolling_metrics(flat)
    assert m["max_drawdown_pct"] == 0.0
    crash = [100.0, 101.0, 102.0, 80.0, 82.0, 81.0]
    m = rolling_metrics(crash)
    assert m["max_drawdown_pct"] > 15.0
    assert m["sharpe"] is not None and m["sharpe"] < 0
    assert rolling_metrics([100.0, 101.0]) == {"sharpe": None, "max_drawdown_pct": None}


def _write_equity(repo, values, start=date(2026, 8, 1)):
    for i, v in enumerate(values):
        repo.record_equity_snapshot(start + timedelta(days=i), v, v, 0.0)


def test_champion_decay_state_machine(repo):
    champ = repo.create_agent("G1-Alfa", 1, clamp_dna(DEFAULT_DNA), "", "2026-08", 10_000.0)
    repo.retire_agent(champ)
    repo.set_champion(champ)
    repo.set_setting("arena", {"live_enabled": True}, source="test")
    # equity in caduta libera: drawdown ben oltre il 15%
    _write_equity(repo, [10_000 - 400 * i for i in range(10)])
    deps = _deps(repo)

    states = [evaluate_champion_health(deps, SETTINGS)["state"] for _ in range(4)]
    # breach_days=2: active, monitoring (2a violazione), monitoring, decayed (4a)
    assert states == ["active", "monitoring", "monitoring", "decayed"]
    assert repo.get_setting("arena")["live_enabled"] is False
    events = {e.event for e in repo.arena_events(limit=20)}
    assert {"champion_monitoring", "champion_decayed"} <= events


def test_champion_health_recovers(repo):
    champ = repo.create_agent("G1-Alfa", 1, clamp_dna(DEFAULT_DNA), "", "2026-08", 10_000.0)
    repo.retire_agent(champ)
    repo.set_champion(champ)
    _write_equity(repo, [10_000 + 10 * i for i in range(10)])
    deps = _deps(repo)
    result = evaluate_champion_health(deps, SETTINGS)
    assert result["state"] == "active" and result["breaches"] == 0


def test_champion_health_noop_without_champion(repo):
    assert evaluate_champion_health(_deps(repo), SETTINGS) is None
