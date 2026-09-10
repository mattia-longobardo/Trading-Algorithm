"""Test della pipeline multi-stadio: candidati, stadi, veto del risk judge."""

from __future__ import annotations

import json

from etoro_bot.arena.pipeline import (
    _judge_actions,
    llm_models,
    pipeline_enabled,
    run_pipeline,
    select_candidates,
)

MARKET = {
    "AAPL": {"instrument_id": 1, "price": 200.0, "day_pct": 5.0, "view": "AAPL"},
    "MSFT": {"instrument_id": 2, "price": 400.0, "day_pct": 0.1, "view": "MSFT"},
    "NVDA": {"instrument_id": 3, "price": 100.0, "day_pct": -4.0, "view": "NVDA"},
}


def test_select_candidates_ranks_by_signal_and_keeps_held():
    chosen = select_candidates(MARKET, held={"MSFT"}, limit=2)
    assert "MSFT" in chosen           # in portafoglio: entra sempre
    assert chosen[-1] != "MSFT" or len(chosen) >= 2
    assert "AAPL" in chosen           # |day_pct| più alto tra i non detenuti


def test_llm_models_fallback_to_single_model():
    assert llm_models({"llm": {"model": "x"}}) == ("x", "x")
    assert llm_models({"llm": {"model": "x", "model_quick": "q", "model_deep": "d"}}) == (
        "q", "d",
    )


def test_pipeline_disabled_by_default():
    assert pipeline_enabled({}) is False
    assert pipeline_enabled({"arena": {"pipeline": {"enabled": True}}}) is True


def test_judge_veto_and_reduce():
    actions = [
        {"action": "open", "symbol": "AAPL", "size_pct": 20.0, "reason": "a"},
        {"action": "open", "symbol": "NVDA", "size_pct": 30.0, "reason": "b"},
        {"action": "close", "symbol": "MSFT", "reason": "c"},
    ]
    raw = json.dumps([
        {"symbol": "AAPL", "verdict": "veto", "reason": "troppo esteso"},
        {"symbol": "NVDA", "verdict": "reduce", "reason": "vol alta"},
        {"symbol": "MSFT", "verdict": "veto", "reason": "ignorato: è una chiusura"},
    ])
    kept, applied = _judge_actions(raw, actions)
    symbols = [(a["action"], a["symbol"]) for a in kept]
    assert ("open", "AAPL") not in symbols          # veto
    assert ("close", "MSFT") in symbols             # chiusure mai toccate
    nvda = next(a for a in kept if a["symbol"] == "NVDA")
    assert nvda["size_pct"] == 15.0                 # reduce = dimezza
    assert {v["symbol"] for v in applied} == {"AAPL", "NVDA"}


def test_judge_garbage_output_changes_nothing():
    actions = [{"action": "open", "symbol": "AAPL", "size_pct": 20.0, "reason": "a"}]
    kept, applied = _judge_actions("nessun json", actions)
    assert kept == actions and applied == []


def test_run_pipeline_stages_and_journal():
    stages: list[str] = []
    calls: list[str] = []

    def llm(system_blocks, user_prompt, model, max_tokens):
        calls.append(user_prompt.split("\n")[0][:30])
        if "analista" in user_prompt:
            return "AAPL: sale; news ok; rischio basso"
        if "RIALZISTA" in user_prompt and "avvocato" in user_prompt:
            return "long AAPL"
        if "RIBASSISTA" in user_prompt and "avvocato" in user_prompt:
            return "cautela"
        if "risk manager" in user_prompt:
            return json.dumps([{"symbol": "AAPL", "verdict": "reduce", "reason": "r"}])
        # trader
        return json.dumps([{"action": "open", "symbol": "AAPL",
                            "size_pct": 20, "reason": "dal dibattito"}])

    outcome = run_pipeline(
        llm,
        settings={"arena": {"pipeline": {"enabled": True, "candidates": 3}}},
        max_tokens=256,
        trader_prompt="PROMPT BASE",
        market=MARKET,
        held=set(),
        journal=lambda stage, payload: stages.append(stage),
    )
    assert stages == ["analyst", "debate", "risk"]
    assert len(calls) == 5  # analyst, bull, bear, trader, judge
    assert outcome.actions[0]["size_pct"] == 10.0  # reduce applicato
    assert outcome.violation is None
