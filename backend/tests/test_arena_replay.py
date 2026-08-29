"""Test del replay storico: stesso engine del training su candele passate."""

from __future__ import annotations

import json

import pytest

from etoro_bot.arena.engine import ArenaDeps
from etoro_bot.arena.replay import (
    WARMUP_BARS,
    replay_metrics,
    run_replay,
    snapshot_from_history,
)
from etoro_bot.safety.mandate import Mandate


def make_candles(closes: list[float], start_day: int = 1) -> list[dict]:
    return [
        {
            "fromDate": f"2026-06-{min(day, 28):02d}T00:00:00Z",
            "open": c, "high": c, "low": c, "close": c, "volume": 1000,
        }
        for day, c in enumerate(closes, start=start_day)
    ]


def deps_for(repo, llm=None):
    return ArenaDeps(
        repo=repo, client=None,
        settings={"arena": {"starting_capital_usd": 10_000}},
        llm=llm, model="m", max_tokens=256,
        mandate=Mandate.unlimited(),
    )


def test_snapshot_uses_only_past_closes():
    """Anti look-ahead: il bar N vede il prezzo N e le chiusure < N."""
    closes = [100.0] * 25 + [110.0, 999.0]
    candles = {"AAPL": make_candles(closes)}
    snap, bar_date = snapshot_from_history(candles, {"AAPL": 7}, 25)
    row = snap["AAPL"]
    assert row["price"] == 110.0
    assert row["instrument_id"] == 7
    # day_pct = 110 vs chiusura precedente (100), MAI vs la futura 999
    assert row["day_pct"] == pytest.approx(10.0)
    assert bar_date is not None and bar_date.year == 2026
    # niente memoria news nel view del replay
    assert "DATI_NON_FIDATI" not in row["view"]


def test_replay_metrics_drawdown_and_winrate():
    m = replay_metrics([10_000, 11_000, 9_900, 10_500], [100.0, -50.0], 10_000)
    assert m["return_pct"] == pytest.approx(5.0)
    assert m["trades"] == 2
    assert m["win_rate_pct"] == 50.0
    assert m["max_drawdown_pct"] == pytest.approx(10.0)


def test_run_replay_full_cycle_and_cleanup(repo):
    """Una corsa completa: apre col LLM, liquida a fine corsa, uccide l'agente."""
    calls = {"n": 0}

    def llm(system_blocks, user_prompt, model, max_tokens):
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps(
                [{"action": "open", "symbol": "AAPL", "size_pct": 30, "reason": "replay"}]
            )
        return "[]"

    closes = [100.0] * WARMUP_BARS + [100.0, 105.0, 110.0]
    history = ({"AAPL": make_candles(closes)}, {"AAPL": 7})
    result = run_replay(
        deps_for(repo, llm=llm), dna={}, symbols=["AAPL"], history=history
    )
    assert "error" not in result
    assert result["bars"] == 3
    assert result["trades"] == 1
    # comprato a 100 al primo bar, liquidato a 110: ritorno positivo
    assert result["return_pct"] > 0
    # l'agente di replay non resta mai nel torneo
    replay_agents = [a for a in repo.all_agents() if a.month.startswith("replay")]
    assert replay_agents and all(a.status == "dead" for a in replay_agents)
    assert all(repo.sim_positions(a.id) == [] for a in replay_agents)


def test_run_replay_short_history_errors(repo):
    history = ({"AAPL": make_candles([100.0] * 5)}, {"AAPL": 7})
    result = run_replay(deps_for(repo), dna={}, symbols=["AAPL"], history=history)
    assert "error" in result
