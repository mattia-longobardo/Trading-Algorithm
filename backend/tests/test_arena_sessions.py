"""Test chiusure di fine sessione col gene max_holding_days (day → swing)."""

from __future__ import annotations

from datetime import datetime, timezone

from etoro_bot.arena.dna import DEFAULT_DNA, clamp_dna
from etoro_bot.arena.engine import (
    ArenaDeps,
    close_market_positions,
    trading_days_held,
)
from etoro_bot.arena.live import close_live_market_positions

NOW = datetime(2026, 7, 27, 15, 30, tzinfo=timezone.utc)  # lunedì, chiusura EU

MARKET = {
    "ENEL.MI": {"instrument_id": 10, "price": 9.0, "market": "europe", "view": "x"},
    "SAP.DE": {"instrument_id": 11, "price": 250.0, "market": "europe", "view": "z"},
    "AAPL": {"instrument_id": 1, "price": 200.0, "market": "usa", "view": "y"},
}


def make_deps(repo, client=None):
    return ArenaDeps(
        repo=repo, client=client,
        settings={"arena": {"starting_capital_eur": 10_000}},
        llm=None, model="m", max_tokens=256,
    )


def test_trading_days_held_skips_weekends():
    opened_today = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)      # lunedì
    opened_friday = datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc)    # venerdì
    opened_thursday = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
    assert trading_days_held(opened_today, NOW) == 1
    assert trading_days_held(opened_friday, NOW) == 2   # ven + lun (weekend escluso)
    assert trading_days_held(opened_thursday, NOW) == 3


def test_day_trader_closes_at_session_end_swing_trader_holds(repo):
    day = repo.create_agent(
        "G1-Alfa", 1, clamp_dna({**DEFAULT_DNA, "max_holding_days": 1}),
        "", "2026-07", 10_000.0,
    )
    swing = repo.create_agent(
        "G1-Beta", 1, clamp_dna({**DEFAULT_DNA, "max_holding_days": 3}),
        "", "2026-07", 10_000.0,
    )
    for agent_id in (day, swing):
        repo.open_sim_position(agent_id, "ENEL.MI", 10, 800.0, 8.0, "eu",
                               opened_at=NOW.replace(hour=8))
        repo.open_sim_position(agent_id, "AAPL", 1, 1_000.0, 200.0, "us",
                               opened_at=NOW.replace(hour=14))
    deps = make_deps(repo)

    summary = close_market_positions(deps, "europe", market=MARKET, now=NOW)

    assert summary["closed"] == 1  # solo l'ENEL del day trader
    assert [p.symbol for p in repo.sim_positions(day)] == ["AAPL"]  # USA non toccata
    assert {p.symbol for p in repo.sim_positions(swing)} == {"ENEL.MI", "AAPL"}
    trade = repo.sim_trades(day)[0]
    assert trade.symbol == "ENEL.MI" and "holding" in trade.close_reason


def test_swing_position_expires_after_max_holding_days(repo):
    swing = repo.create_agent(
        "G1-Beta", 1, clamp_dna({**DEFAULT_DNA, "max_holding_days": 3}),
        "", "2026-07", 10_000.0,
    )
    opened_thursday = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
    repo.open_sim_position(swing, "SAP.DE", 11, 500.0, 250.0, "vecchia",
                           opened_at=opened_thursday)  # 3° giorno di borsa oggi
    deps = make_deps(repo)

    summary = close_market_positions(deps, "europe", market=MARKET, now=NOW)

    assert summary["closed"] == 1
    assert repo.sim_positions(swing) == []


class FakeLiveClient:
    def __init__(self):
        self.close_calls = []

    def get_portfolio(self):
        return {"positions": [], "credit": 5_000.0}

    def get_trade_history(self, min_date=None, page_size=100):
        return []

    def close_position(self, position_id, instrument_id):
        self.close_calls.append((position_id, instrument_id))


def test_live_session_close_respects_champion_holding_gene(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH_DIR", str(tmp_path))
    monkeypatch.delenv("ETORO_BOT_KILL", raising=False)
    champ = repo.create_agent(
        "G1-Alfa", 1, clamp_dna({**DEFAULT_DNA, "max_holding_days": 3}),
        "", "2026-06", 10_000.0,
    )
    repo.retire_agent(champ)
    repo.set_champion(champ)
    client = FakeLiveClient()
    deps = make_deps(repo, client=client)
    run_id = f"live-{NOW:%Y%m%d}"
    repo.create_run(run_id, environment="live")
    # aperta giovedì: oggi è il 3° giorno di borsa → scade; quella di oggi resta
    repo.register_open_position(
        700, run_id, "ENEL.MI", 10, 800.0, 8.0,
        datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc),
    )
    repo.register_open_position(701, run_id, "SAP.DE", 11, 500.0, 250.0,
                                NOW.replace(hour=8))

    summary = close_live_market_positions(deps, "europe", market=MARKET, now=NOW)

    assert summary["closed"] == 1
    assert client.close_calls == [(700, 10)]
    assert [p.symbol for p in repo.open_positions()] == ["SAP.DE"]
