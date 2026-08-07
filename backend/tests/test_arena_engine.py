"""Test dell'arena: conto simulato, ciclo di trading, EOD, evoluzione mensile."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

import pytest

from etoro_bot.arena.dna import DEFAULT_DNA, clamp_dna, survival_creed
from etoro_bot.arena.engine import (
    ArenaDeps,
    agent_equity,
    auto_risk_closes,
    bootstrap_if_needed,
    close_market_positions,
    run_eod,
    run_training_cycle,
)
from etoro_bot.arena.evolution import current_month, maybe_evolve

NOW = datetime(2026, 7, 27, 15, 0, tzinfo=timezone.utc)

MARKET = {
    "AAPL": {"instrument_id": 1, "price": 200.0, "view": "AAPL 200.0"},
    "MSFT": {"instrument_id": 2, "price": 400.0, "view": "MSFT 400.0"},
}


def make_deps(repo, llm=None, settings=None):
    return ArenaDeps(
        repo=repo,
        client=None,
        settings={"arena": {"starting_capital_eur": 10_000}, **(settings or {})},
        llm=llm,
        model="test-model",
        max_tokens=512,
    )


def open_llm(symbol="AAPL", size_pct=50.0):
    def llm(system_blocks, user_prompt, model, max_tokens):
        return json.dumps(
            [{"action": "open", "symbol": symbol, "size_pct": size_pct, "reason": "test"}]
        )
    return llm


# ------------------------------------------------------------- conto simulato


def test_sim_account_roundtrip(repo):
    agent_id = repo.create_agent("T-1", 1, clamp_dna(DEFAULT_DNA), "", "2026-07", 1_000.0)
    assert not repo.open_sim_position(agent_id, "AAPL", 1, 2_000.0, 100.0, "troppo grande")
    assert repo.open_sim_position(agent_id, "AAPL", 1, 500.0, 100.0, "ok")
    agent = repo.get_agent(agent_id)
    assert agent.cash_usd == 500.0
    pos = repo.sim_positions(agent_id)[0]
    assert pos.units == 5.0
    pnl = repo.close_sim_position(pos.id, 110.0, "tp")
    assert pnl == 50.0
    agent = repo.get_agent(agent_id)
    assert agent.cash_usd == 1_050.0
    assert repo.sim_positions(agent_id) == []
    trade = repo.sim_trades(agent_id)[0]
    assert trade.pnl_usd == 50.0 and trade.close_reason == "tp"


# ------------------------------------------------------------------ stop/take


class _Pos:
    def __init__(self, symbol, entry_price):
        self.symbol = symbol
        self.entry_price = entry_price


def test_auto_risk_closes_triggers_sl_and_tp():
    dna = clamp_dna({"stop_loss_pct": 2.0, "take_profit_pct": 4.0})
    positions = [_Pos("AAPL", 100.0), _Pos("MSFT", 100.0), _Pos("SPY", 100.0)]
    prices = {"AAPL": 97.9, "MSFT": 104.1, "SPY": 100.5}
    closes = auto_risk_closes(dna, positions, prices)
    by_symbol = {pos.symbol: reason for pos, reason in closes}
    assert "stop loss" in by_symbol["AAPL"]
    assert "take profit" in by_symbol["MSFT"]
    assert "SPY" not in by_symbol


# ------------------------------------------------------------------ bootstrap


def test_bootstrap_creates_two_agents_and_arena_setting(repo):
    deps = make_deps(repo)
    assert bootstrap_if_needed(deps, now=NOW)
    agents = repo.alive_agents()
    assert len(agents) == 2
    assert {a.generation for a in agents} == {1}
    assert agents[0].dna != agents[1].dna  # il rivale è una mutazione
    assert all(a.starting_capital_usd > 0 for a in agents)
    assert all("profitto" in a.memory.lower() for a in agents)
    arena = repo.get_setting("arena")
    assert arena["month"] == "2026-07"
    assert arena["paused"] is False
    assert not bootstrap_if_needed(deps, now=NOW)  # idempotente


# ---------------------------------------------------------------------- ciclo


def test_training_cycle_applies_llm_actions(repo):
    deps = make_deps(repo, llm=open_llm("AAPL", 50.0))
    bootstrap_if_needed(deps, now=NOW)
    summary = run_training_cycle(deps, market=MARKET, now=NOW)
    assert summary["cycled"] == 2
    for agent in repo.alive_agents():
        positions = repo.sim_positions(agent.id)
        assert [p.symbol for p in positions] == ["AAPL"]
        series = repo.sim_equity_series(agent.id)
        assert len(series) == 1
        # l'equity al mark-to-market resta il capitale iniziale (prezzo invariato)
        assert abs(series[0].equity_usd - agent.starting_capital_usd) < 0.01


def test_short_direction_is_persisted_not_written_into_the_reason(repo):
    """La direzione simulata sta in colonna e sopravvive alla chiusura; la
    open_reason torna a essere solo il testo dell'agente."""
    from etoro_bot.arena.engine import effective_price, position_direction

    def llm(system_blocks, user_prompt, model, max_tokens):
        return json.dumps([{"action": "open", "symbol": "AAPL",
                            "direction": "short", "size_pct": 50.0,
                            "reason": "rottura al ribasso"}])

    deps = make_deps(repo, llm=llm)
    bootstrap_if_needed(deps, now=NOW)
    agent = repo.alive_agents()[0]
    run_training_cycle(deps, market=MARKET, now=NOW)

    pos = repo.sim_positions(agent.id)[0]
    assert pos.direction == "short"
    assert pos.open_reason == "rottura al ribasso"
    assert position_direction(pos) == "short"

    repo.close_sim_position(pos.id, effective_price(pos, 180.0), "chiusura")
    trade = repo.sim_trades(agent.id)[0]
    assert trade.direction == "short" and trade.pnl_usd > 0


def test_training_cycle_skips_when_paused(repo):
    deps = make_deps(repo, llm=open_llm())
    bootstrap_if_needed(deps, now=NOW)
    arena = repo.get_setting("arena")
    repo.set_setting("arena", {**arena, "paused": True})
    summary = run_training_cycle(deps, market=MARKET, now=NOW)
    assert summary == {"skipped": "paused"}
    assert all(not repo.sim_positions(a.id) for a in repo.alive_agents())


def test_training_cycle_closes_on_stop_loss_before_llm(repo):
    deps = make_deps(repo, llm=None)  # nessun LLM: solo enforcement SL/TP
    bootstrap_if_needed(deps, now=NOW)
    agent = repo.alive_agents()[0]
    repo.open_sim_position(agent.id, "AAPL", 1, 500.0, 300.0, "pre-esistente")
    crashed = {"AAPL": {"instrument_id": 1, "price": 200.0, "view": "AAPL 200"}}
    run_training_cycle(deps, market=crashed, now=NOW)
    assert repo.sim_positions(agent.id) == []
    trade = repo.sim_trades(agent.id)[0]
    assert "stop loss" in trade.close_reason


# ------------------------------------------------------------------------ EOD


def test_session_close_plus_eod_reflection(repo):
    """Alla campanella si liquida SOLO ciò che ha esaurito il proprio holding.

    Col DNA di default (max_holding_days=10) lo swing è autorizzato: la
    posizione aperta oggi passa la notte, quella vecchia di settimane no.
    """
    from datetime import timedelta

    def reflecting_llm(system_blocks, user_prompt, model, max_tokens):
        return "Lezione: non inseguire i breakout pomeridiani."

    deps = make_deps(repo, llm=reflecting_llm)
    bootstrap_if_needed(deps, now=NOW)
    agent = repo.alive_agents()[0]
    repo.open_sim_position(agent.id, "MSFT", 2, 400.0, 400.0, "swing fresco",
                           opened_at=NOW)
    repo.open_sim_position(agent.id, "AAPL", 1, 200.0, 200.0, "swing scaduto",
                           opened_at=NOW - timedelta(days=25))
    close_market_positions(deps, "usa", market=MARKET, now=NOW)
    run_eod(deps, market=MARKET, now=NOW)
    # il fresco resta aperto oltre la campanella, lo scaduto è stato liquidato
    assert [p.symbol for p in repo.sim_positions(agent.id)] == ["MSFT"]
    trades = repo.sim_trades(agent.id)
    assert [t.symbol for t in trades] == ["AAPL"]
    assert any("holding massimo" in t.close_reason for t in trades)
    refreshed = repo.get_agent(agent.id)
    assert survival_creed() in refreshed.memory
    assert "Lezione" in refreshed.memory
    assert repo.sim_equity_series(agent.id)  # snapshot serale registrato


# ------------------------------------------------------------------ evoluzione


def _finished_month(repo, pnl_a: float, pnl_b: float):
    """Due agenti a fine mese con PnL dati (conti già liquidati in cash)."""
    a = repo.create_agent("G1-Alfa", 1, clamp_dna(DEFAULT_DNA), "m", "2026-06", 10_000.0)
    b = repo.create_agent("G1-Beta", 1, clamp_dna(DEFAULT_DNA), "m", "2026-06", 10_000.0)
    with repo._sf.begin() as s:  # accesso diretto: solo setup di test
        from etoro_bot.db.models import Agent
        s.get(Agent, a).cash_usd = 10_000.0 + pnl_a
        s.get(Agent, b).cash_usd = 10_000.0 + pnl_b
    repo.set_setting("arena", {"month": "2026-06", "paused": False, "live_enabled": False})
    return a, b


def test_evolution_winner_survives_loser_dies(repo):
    a, b = _finished_month(repo, pnl_a=350.0, pnl_b=80.0)
    deps = make_deps(repo)
    result = maybe_evolve(deps, now=NOW)
    assert result is not None
    dead_b = repo.get_agent(b)
    assert dead_b.status == "dead"
    winner = repo.get_agent(a)
    assert winner.status == "evolved"
    assert winner.is_champion  # il vincitore del mese scorso guida il live
    new_gen = repo.alive_agents()
    assert len(new_gen) == 2
    assert {ag.generation for ag in new_gen} == {2}
    assert all(ag.parent_id == a for ag in new_gen)
    # il clone eredita il DNA del vincitore, il rivale è una mutazione
    dnas = [ag.dna for ag in new_gen]
    assert winner.dna in dnas
    assert any(d != winner.dna for d in dnas)
    assert repo.get_setting("arena")["month"] == "2026-07"


def test_evolution_zero_or_negative_kills_even_the_winner(repo):
    a, b = _finished_month(repo, pnl_a=0.0, pnl_b=-500.0)
    deps = make_deps(repo)
    maybe_evolve(deps, now=NOW)
    assert repo.get_agent(a).status == "dead"
    assert repo.get_agent(b).status == "dead"
    assert repo.champion() is None
    new_gen = repo.alive_agents()
    assert len(new_gen) == 2 and {ag.generation for ag in new_gen} == {2}


def test_evolution_liquidates_open_positions_before_judgement(repo):
    a, _b = _finished_month(repo, pnl_a=100.0, pnl_b=50.0)
    # una posizione swing ancora aperta a fine mese: cash 10.100 - 500 = 9.600
    repo.open_sim_position(a, "AAPL", 1, 500.0, 100.0, "swing di fine mese")
    deps = make_deps(repo)

    result = maybe_evolve(deps, now=NOW)

    assert result is not None
    assert repo.sim_positions(a) == []
    trades = repo.sim_trades(a)
    assert any("liquidazione di fine mese" in t.close_reason for t in trades)
    # liquidata al prezzo d'ingresso (nessun client): PnL mensile resta +100 → vince
    assert result["survivor"] == "G1-Alfa"
    assert result["results"][0]["pnl_usd"] == 100.0


def test_evolution_bootstraps_an_empty_arena(repo):
    """Su DB vergine l'evoluzione crea la generazione 1 (sotto lock) e si ferma."""
    deps = make_deps(repo)
    assert maybe_evolve(deps, now=NOW) is None
    assert len(repo.alive_agents()) == 2


def test_evolution_noop_within_same_month(repo):
    _finished_month(repo, 100.0, 50.0)
    repo.set_setting("arena", {"month": current_month(NOW), "paused": False,
                               "live_enabled": False})
    deps = make_deps(repo)
    assert maybe_evolve(deps, now=NOW) is None
    assert len(repo.alive_agents()) == 2  # nessuno tocca gli agenti


# ---------------------------------------------------------------- concorrenza


def test_evolution_waits_for_the_running_training_cycle(repo):
    """Evoluzione e ciclo non si sovrappongono: il verdetto vede conti fermi.

    Senza lock condiviso l'evoluzione liquiderebbe mentre il ciclo apre, e il
    cash finale dipenderebbe da chi scrive per ultimo.
    """
    a, _b = _finished_month(repo, pnl_a=350.0, pnl_b=80.0)
    order: list[str] = []
    llm_running = threading.Event()

    def slow_llm(system_blocks, user_prompt, model, max_tokens):
        order.append("llm-in")
        llm_running.set()
        time.sleep(0.4)  # finestra in cui l'evoluzione proverebbe a intromettersi
        order.append("llm-out")
        return json.dumps(
            [{"action": "open", "symbol": "AAPL", "size_pct": 50.0, "reason": "test"}]
        )

    deps = make_deps(repo, llm=slow_llm)
    cycle = threading.Thread(
        target=lambda: run_training_cycle(deps, market=MARKET, now=NOW)
    )
    cycle.start()
    assert llm_running.wait(5), "il ciclo non è partito"

    def _evolve() -> None:
        maybe_evolve(make_deps(repo), now=NOW)
        order.append("evolve-done")

    evolve = threading.Thread(target=_evolve)
    evolve.start()
    for t in (cycle, evolve):
        t.join(30)
        assert not t.is_alive()

    # l'evoluzione ha atteso la fine del ciclo, non si è infilata nel mezzo
    assert order.index("evolve-done") > order.index("llm-out")
    # cash deterministico: aperto e poi liquidato al prezzo d'ingresso
    assert repo.get_agent(a).cash_usd == pytest.approx(10_350.0, abs=0.01)
    assert repo.sim_positions(a) == []


def test_concurrent_closes_do_not_lose_cash_updates(repo):
    """Chiusure simultanee sullo stesso agente: nessun accredito perso."""
    agent_id = repo.create_agent("T-lock", 1, clamp_dna(DEFAULT_DNA), "", "2026-07", 10_000.0)
    for i in range(8):
        assert repo.open_sim_position(agent_id, f"S{i}", i, 1_000.0, 100.0, "setup")
    positions = repo.sim_positions(agent_id)
    barrier = threading.Barrier(len(positions))

    def _close(pos) -> None:
        barrier.wait(10)  # tutti leggono il cash nello stesso istante
        repo.close_sim_position(pos.id, 110.0, "chiusura concorrente")

    threads = [threading.Thread(target=_close, args=(p,)) for p in positions]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
        assert not t.is_alive()

    # 2.000 residui + 8 × 1.100 di ricavato
    assert repo.get_agent(agent_id).cash_usd == pytest.approx(10_800.0, abs=0.01)
    assert repo.sim_positions(agent_id) == []


# --------------------------------------------------------------------- equity


def test_agent_equity_marks_open_positions(repo):
    agent_id = repo.create_agent("T-2", 1, clamp_dna(DEFAULT_DNA), "", "2026-07", 1_000.0)
    repo.open_sim_position(agent_id, "AAPL", 1, 400.0, 100.0, "")
    agent = repo.get_agent(agent_id)
    positions = repo.sim_positions(agent_id)
    assert agent_equity(agent, positions, {"AAPL": 110.0}) == 1_040.0
    # prezzo mancante: la posizione vale il costo
    assert agent_equity(agent, positions, {}) == 1_000.0
