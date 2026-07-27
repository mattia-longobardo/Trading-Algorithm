"""Test del trading live guidato dal campione: freni operativi e esecuzione."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from etoro_bot.arena.dna import DEFAULT_DNA, clamp_dna
from etoro_bot.arena.engine import ArenaDeps
from etoro_bot.arena.live import run_live_cycle, run_live_eod
from etoro_bot.config import CircuitBreakerRules
from etoro_bot.safety.circuit_breaker import CircuitBreaker

NOW = datetime(2026, 7, 27, 15, 0, tzinfo=timezone.utc)

MARKET = {
    "AAPL": {"instrument_id": 1, "price": 200.0, "view": "AAPL 200.0"},
    "MSFT": {"instrument_id": 2, "price": 400.0, "view": "MSFT 400.0"},
}


class FakeLiveClient:
    def __init__(self, credit=10_000.0):
        self.credit = credit
        self.open_calls = []
        self.close_calls = []
        self.next_position_id = 500

    def get_portfolio(self):
        return {"positions": [], "credit": self.credit}

    def get_trade_history(self, min_date=None, page_size=100):
        return []

    def open_position(self, instrument_id, amount_usd, request_id):
        self.open_calls.append((instrument_id, amount_usd, request_id))
        self.next_position_id += 1
        return {"position_id": self.next_position_id, "execution_price": 100.0}

    def close_position(self, position_id, instrument_id):
        self.close_calls.append((position_id, instrument_id))


@pytest.fixture(autouse=True)
def _isolate_safety(tmp_path, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH_DIR", str(tmp_path))
    monkeypatch.delenv("ETORO_BOT_KILL", raising=False)


def open_llm(symbol="AAPL", size_pct=20.0):
    def llm(system_blocks, user_prompt, model, max_tokens):
        return json.dumps(
            [{"action": "open", "symbol": symbol, "size_pct": size_pct, "reason": "live"}]
        )
    return llm


def make_deps(repo, client, llm=None):
    return ArenaDeps(
        repo=repo, client=client,
        settings={"arena": {"starting_capital_eur": 10_000}},
        llm=llm, model="test-model", max_tokens=512,
    )


def _with_champion(repo):
    agent_id = repo.create_agent(
        "G1-Alfa", 1, clamp_dna(DEFAULT_DNA), "memoria", "2026-06", 10_000.0
    )
    repo.retire_agent(agent_id)
    repo.set_champion(agent_id)
    return agent_id


def test_live_cycle_skips_without_champion(repo, tmp_path):
    deps = make_deps(repo, FakeLiveClient(), llm=open_llm())
    assert run_live_cycle(deps, market=MARKET, now=NOW) == {"skipped": "no_champion"}


def test_live_cycle_skips_on_kill_switch(repo, monkeypatch):
    monkeypatch.setenv("ETORO_BOT_KILL", "1")
    _with_champion(repo)
    deps = make_deps(repo, FakeLiveClient(), llm=open_llm())
    assert run_live_cycle(deps, market=MARKET, now=NOW) == {"skipped": "kill_switch"}


def test_live_cycle_opens_real_position_and_journals(repo):
    _with_champion(repo)
    client = FakeLiveClient()
    deps = make_deps(repo, client, llm=open_llm("AAPL", 20.0))
    summary = run_live_cycle(deps, market=MARKET, now=NOW)
    assert summary["opened"] == 1
    assert len(client.open_calls) == 1
    positions = repo.open_positions()
    assert [p.symbol for p in positions] == ["AAPL"]
    executions = repo.list_executions()
    assert executions and executions[0].side == "buy"
    run = repo.get_run(f"live-{NOW:%Y%m%d}")
    assert run is not None and run.environment == "live"


def test_live_cycle_stop_loss_closes_real_position(repo):
    _with_champion(repo)
    client = FakeLiveClient()
    # LLM che non propone nulla: resta solo l'enforcement automatico SL/TP
    deps = make_deps(repo, client, llm=lambda **kw: "[]")
    run_id = f"live-{NOW:%Y%m%d}"
    repo.create_run(run_id, environment="live")
    repo.register_open_position(900, run_id, "AAPL", 1, 500.0, 300.0, NOW)
    run_live_cycle(deps, market=MARKET, now=NOW)  # AAPL a 200: -33% → stop loss
    assert client.close_calls == [(900, 1)]
    assert repo.open_positions() == []


def test_live_cycle_breaker_blocks_openings(repo):
    _with_champion(repo)
    client = FakeLiveClient()
    deps = make_deps(repo, client, llm=open_llm())
    rules = CircuitBreakerRules(max_consecutive_losses=1)
    breaker = CircuitBreaker(rules, state_dir=None)
    breaker.record_closed_trade(-100.0, 10_000.0)
    assert breaker.blocks_openings()
    summary = run_live_cycle(deps, breaker=breaker, market=MARKET, now=NOW)
    assert summary["blocked"] == 1 and summary["opened"] == 0
    assert client.open_calls == []


def test_live_session_close_then_eod_snapshot(repo):
    from etoro_bot.arena.live import close_live_market_positions

    _with_champion(repo)  # DNA di default: max_holding_days=1
    client = FakeLiveClient()
    deps = make_deps(repo, client, llm=None)
    run_id = f"live-{NOW:%Y%m%d}"
    repo.create_run(run_id, environment="live")
    repo.register_open_position(901, run_id, "MSFT", 2, 400.0, 400.0, NOW)

    summary = close_live_market_positions(deps, "usa", market=MARKET, now=NOW)
    assert summary["closed"] == 1
    assert client.close_calls == [(901, 2)]
    assert repo.open_positions() == []

    eod = run_live_eod(deps, now=NOW)
    assert eod["snapshot"] is True
    assert repo.equity_series()  # snapshot registrato
