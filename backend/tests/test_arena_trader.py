"""Test del trader: normalizzazione azioni LLM e enforcement dei vincoli DNA."""

from __future__ import annotations

import json

from etoro_bot.arena.dna import clamp_dna
from etoro_bot.arena.trader import enforce, parse_actions

DNA = clamp_dna({
    "conviction_scale": 1.0,
    "max_positions": 3,
    "max_orders_per_cycle": 2,
    "max_position_pct": 30.0,
    "min_cash_pct": 0.0,
})

MARKET = {
    "AAPL": {"instrument_id": 1, "price": 200.0},
    "MSFT": {"instrument_id": 2, "price": 400.0},
    "NVDA": {"instrument_id": 3, "price": 100.0},
    "SPY": {"instrument_id": 4, "price": 500.0},
}


class FakePosition:
    def __init__(self, symbol):
        self.symbol = symbol


def test_parse_actions_normalizes_and_discards_garbage():
    raw = json.dumps([
        {"action": "open", "symbol": "aapl", "size_pct": 20, "reason": "breakout"},
        {"action": "open", "symbol": "nvda", "direction": "ribasso", "size_pct": 5,
         "reason": "rottura"},
        {"action": "close", "symbol": "MSFT", "reason": "target raggiunto"},
        {"action": "hold"},
        {"action": "open"},                      # senza simbolo: scartata
        {"symbol": "NVDA"},                      # senza azione: scartata
        "spazzatura",
    ])
    actions = parse_actions(raw)
    assert actions == [
        # senza direzione esplicita un'apertura è long
        {"action": "open", "symbol": "AAPL", "direction": "long", "size_pct": 20.0,
         "rating": None, "confidence": None, "reason": "breakout"},
        # sinonimi accettati: "ribasso" è uno short
        {"action": "open", "symbol": "NVDA", "direction": "short", "size_pct": 5.0,
         "rating": None, "confidence": None, "reason": "rottura"},
        # sulle chiusure la direzione è un filtro opzionale: None = tutte
        {"action": "close", "symbol": "MSFT", "direction": None, "size_pct": None,
         "rating": None, "confidence": None, "reason": "target raggiunto"},
    ]


def test_enforce_respects_max_orders_per_cycle_and_position_cap():
    actions = [
        {"action": "open", "symbol": "AAPL", "size_pct": 100.0, "reason": "a"},
        {"action": "open", "symbol": "MSFT", "size_pct": 10.0, "reason": "b"},
        {"action": "open", "symbol": "NVDA", "size_pct": 10.0, "reason": "c"},
    ]
    opens, closes = enforce(
        actions, dna=DNA, cash=10_000.0, equity=10_000.0,
        held_symbols=set(), market=MARKET,
    )
    assert closes == []
    assert len(opens) == 2  # max_orders_per_cycle
    # size 100% del cash ma cap al 30% dell'equity
    assert opens[0]["symbol"] == "AAPL"
    assert opens[0]["amount_usd"] == 3_000.0
    assert opens[1]["amount_usd"] == 1_000.0


def test_enforce_allows_pyramiding_skips_unknown_and_respects_max_positions():
    """Il piramidaggio è autorizzato di default: un simbolo già in mano non è
    un motivo per scartare l'ordine. Restano i limiti contabili (simbolo fuori
    mercato, tetto di posizioni simultanee)."""
    actions = [
        {"action": "open", "symbol": "AAPL", "size_pct": 10.0, "reason": "raddoppio"},
        {"action": "open", "symbol": "ZZZZ", "size_pct": 10.0, "reason": "sconosciuto"},
        {"action": "open", "symbol": "MSFT", "size_pct": 10.0, "reason": "troppe"},
        {"action": "open", "symbol": "NVDA", "size_pct": 10.0, "reason": "troppe"},
    ]
    opens, _ = enforce(
        actions, dna=DNA, cash=10_000.0, equity=10_000.0,
        held_symbols={"AAPL", "SPY"}, market=MARKET,
    )
    # AAPL passa (piramidaggio), ZZZZ non è in mercato, MSFT/NVDA sforerebbero
    # max_positions=3 (2 già aperte + 1 pianificata)
    assert [o["symbol"] for o in opens] == ["AAPL"]


def test_enforce_skips_held_symbol_when_agent_forbids_pyramiding():
    """allow_pyramiding=False è un gene: se l'agente se lo vieta, torna il
    vecchio comportamento (una sola posizione per simbolo)."""
    dna = clamp_dna({**DNA, "allow_pyramiding": False, "max_positions": 5})
    actions = [
        {"action": "open", "symbol": "AAPL", "size_pct": 10.0, "reason": "già in mano"},
        {"action": "open", "symbol": "MSFT", "size_pct": 10.0, "reason": "ok"},
    ]
    opens, _ = enforce(
        actions, dna=dna, cash=10_000.0, equity=10_000.0,
        held_symbols={"AAPL", "SPY"}, market=MARKET,
    )
    assert [o["symbol"] for o in opens] == ["MSFT"]


def test_enforce_close_only_for_held_symbols():
    actions = [
        {"action": "close", "symbol": "AAPL", "reason": "tp"},
        {"action": "close", "symbol": "MSFT", "reason": "non in mano"},
    ]
    opens, closes = enforce(
        actions, dna=DNA, cash=0.0, equity=1_000.0,
        held_symbols={"AAPL"}, market=MARKET,
    )
    assert opens == []
    assert [c["symbol"] for c in closes] == ["AAPL"]


def test_enforce_honours_min_cash_reserve():
    dna = clamp_dna({**DNA, "min_cash_pct": 50.0, "max_position_pct": 50.0,
                     "max_orders_per_cycle": 4})
    actions = [
        {"action": "open", "symbol": "AAPL", "size_pct": 100.0, "reason": "a"},
        {"action": "open", "symbol": "MSFT", "size_pct": 100.0, "reason": "b"},
    ]
    opens, _ = enforce(
        actions, dna=dna, cash=1_000.0, equity=1_000.0,
        held_symbols=set(), market=MARKET,
    )
    # riserva 50% di 1000 = 500 → spazio totale 500: una sola apertura da 500
    assert len(opens) == 1
    assert opens[0]["amount_usd"] == 500.0


def test_enforce_conviction_scale_multiplies_size():
    dna = clamp_dna({**DNA, "conviction_scale": 0.5})
    actions = [{"action": "open", "symbol": "AAPL", "size_pct": 20.0, "reason": "a"}]
    opens, _ = enforce(
        actions, dna=dna, cash=10_000.0, equity=10_000.0,
        held_symbols=set(), market=MARKET,
    )
    assert opens[0]["amount_usd"] == 1_000.0  # 20% × 0.5


def test_enforce_discards_dust_orders():
    actions = [{"action": "open", "symbol": "AAPL", "size_pct": 0.01, "reason": "a"}]
    opens, _ = enforce(
        actions, dna=DNA, cash=1_000.0, equity=1_000.0,
        held_symbols=set(), market=MARKET,
    )
    assert opens == []


# ------------------------------------------------------- rating e violazioni


def test_parse_actions_reads_rating_and_confidence():
    from etoro_bot.arena.trader import parse_actions

    raw = ('[{"action":"open","symbol":"aapl","direction":"long","size_pct":20,'
           '"rating":"Overweight","confidence":0.7,"reason":"r"}]')
    act = parse_actions(raw)[0]
    assert act["rating"] == "overweight"
    assert act["confidence"] == 0.7


def test_enforce_hold_rating_is_abstention():
    from etoro_bot.arena.trader import enforce

    actions = [{"action": "open", "symbol": "AAPL", "direction": "long",
                "size_pct": 50.0, "rating": "hold", "confidence": None, "reason": ""}]
    opens, _ = enforce(
        actions, dna=DNA, cash=10_000.0, equity=10_000.0,
        held_symbols=set(), market=MARKET, held_count=0,
    )
    assert opens == []


def test_enforce_moderate_rating_scales_size_down():
    from etoro_bot.arena.trader import enforce

    base = {"action": "open", "symbol": "AAPL", "direction": "long",
            "size_pct": 20.0, "confidence": None, "reason": ""}
    full, _ = enforce([{**base, "rating": "buy"}], dna=DNA, cash=10_000.0,
                      equity=10_000.0, held_symbols=set(), market=MARKET, held_count=0)
    reduced, _ = enforce([{**base, "rating": "overweight"}], dna=DNA, cash=10_000.0,
                         equity=10_000.0, held_symbols=set(), market=MARKET, held_count=0)
    assert reduced[0]["amount_usd"] == round(full[0]["amount_usd"] * 0.6, 2)


def test_decide_reports_contract_violation_on_bad_output():
    from etoro_bot.arena.trader import decide

    outcome = decide(lambda **k: "nessun json qui", model="m", max_tokens=10, prompt="p")
    assert outcome.actions == []
    assert outcome.violation == "invalid_json"


def test_decide_reports_llm_error():
    from etoro_bot.arena.trader import decide

    def boom(**kwargs):
        raise RuntimeError("giù")

    outcome = decide(boom, model="m", max_tokens=10, prompt="p")
    assert outcome.actions == []
    assert outcome.violation is not None and outcome.violation.startswith("llm_error")


def test_decide_empty_array_is_abstention_not_violation():
    from etoro_bot.arena.trader import decide

    outcome = decide(lambda **k: "[]", model="m", max_tokens=10, prompt="p")
    assert outcome.actions == []
    assert outcome.violation is None
