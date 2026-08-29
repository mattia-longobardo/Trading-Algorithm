"""Test del grounding gate: verifiche deterministiche sull'output del trader."""

from __future__ import annotations

import json

from etoro_bot.arena.grounding import check_grounding, grounded_decide

MARKET = {"AAPL": {"instrument_id": 1, "price": 200.0}}


def _open(symbol, reason=""):
    return {"action": "open", "symbol": symbol, "direction": "long",
            "size_pct": 10.0, "rating": None, "confidence": None, "reason": reason}


def test_unknown_open_symbol_is_a_conflict():
    conflicts = check_grounding([_open("TSLA")], MARKET)
    assert len(conflicts) == 1 and conflicts[0].startswith("TSLA:")


def test_close_on_absent_symbol_is_not_a_conflict():
    """Una posizione su un titolo fuori snapshot deve restare chiudibile."""
    close = {"action": "close", "symbol": "TSLA", "direction": None,
             "size_pct": None, "rating": None, "confidence": None, "reason": ""}
    assert check_grounding([close], MARKET) == []


def test_price_claim_far_from_snapshot_is_a_conflict():
    conflicts = check_grounding([_open("AAPL", "compro a prezzo 150")], MARKET)
    assert len(conflicts) == 1 and "150" in conflicts[0]


def test_price_claim_within_tolerance_ok_and_targets_ignored():
    assert check_grounding([_open("AAPL", "prezzo 199.5, breakout")], MARKET) == []
    # target/stop non sono affermazioni di prezzo corrente: mai flaggati
    assert check_grounding([_open("AAPL", "target 260, stop 180")], MARKET) == []


def test_grounded_decide_retries_with_conflict_then_succeeds():
    prompts = []

    def llm(system_blocks, user_prompt, model, max_tokens):
        prompts.append(user_prompt)
        if len(prompts) == 1:
            return json.dumps([{"action": "open", "symbol": "TSLA",
                                "size_pct": 10, "reason": "x"}])
        return json.dumps([{"action": "open", "symbol": "AAPL",
                            "size_pct": 10, "reason": "ok"}])

    outcome = grounded_decide(llm, model="m", max_tokens=64, prompt="P", market=MARKET)
    assert outcome.violation is None
    assert [a["symbol"] for a in outcome.actions] == ["AAPL"]
    assert len(prompts) == 2
    assert "CORREZIONE RICHIESTA" in prompts[1] and "TSLA" in prompts[1]


def test_grounded_decide_forces_abstention_after_retries():
    def llm(system_blocks, user_prompt, model, max_tokens):
        return json.dumps([
            {"action": "open", "symbol": "TSLA", "size_pct": 10, "reason": "x"},
            {"action": "open", "symbol": "AAPL", "size_pct": 10, "reason": "ok"},
        ])

    outcome = grounded_decide(llm, model="m", max_tokens=64, prompt="P",
                              market=MARKET, max_retries=1)
    # l'azione infondata cade, quella fondata sopravvive, la violazione è a verbale
    assert [a["symbol"] for a in outcome.actions] == ["AAPL"]
    assert outcome.violation is not None
    assert outcome.violation.startswith("grounding_forced_abstention")
