"""Test del mercato: classificazione EU/USA dei simboli e metriche snapshot."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from etoro_bot.arena.market import (
    build_snapshot,
    market_of_symbol,
    metrics_from_closes,
)


def test_market_of_symbol_european_suffixes():
    for symbol in ("ENEL.MI", "SAP.DE", "MC.PA", "ASML.NV", "SAN.MC",
                   "BARC.L", "ABI.BR", "VOLV-B.ST", "NOKIA.HE"):
        assert market_of_symbol(symbol) == "europe", symbol


def test_market_of_symbol_us_default_and_variants():
    for symbol in ("AAPL", "MSFT", "SPY", "AIR.US", "ASML.RTH", "BRK.B"):
        assert market_of_symbol(symbol) == "usa", symbol


def test_metrics_from_closes():
    closes = [100.0] * 19 + [102.0]
    m = metrics_from_closes(103.02, closes)
    assert m["day_pct"] == 1.0          # 103.02 vs ultima chiusura 102
    assert m["week_pct"] is not None
    assert m["sma20_dist_pct"] is not None


@pytest.fixture(autouse=True)
def _no_candles_cache():
    """Le candele sono in cache per un'ora: i test devono restare indipendenti."""
    from etoro_bot.arena.market import clear_candles_cache

    clear_candles_cache()
    yield
    clear_candles_cache()


class FakeMarketClient:
    def __init__(self, catalogue, rates):
        self.catalogue = catalogue
        self.rates = rates

    def get_instruments_by_type(self, type_id):
        return self.catalogue if type_id == 5 else []

    def get_rates(self, ids):
        return {i: self.rates[i] for i in ids if i in self.rates}

    def get_candles(self, instrument_id, interval="OneDay", count=21):
        return [{"close": 100.0} for _ in range(21)]


def _client():
    return FakeMarketClient(
        catalogue=[
            {"instrumentID": 1, "symbolFull": "AAPL"},
            {"instrumentID": 2, "symbolFull": "ENEL.MI"},
        ],
        rates={
            1: {"lastExecution": 200.0},
            2: {"lastExecution": 8.0},
        },
    )


SETTINGS = {"watchlist": ["AAPL", "ENEL.MI"], "universe_discovery": {"enabled": False}}


def test_snapshot_filters_by_open_session_morning_europe():
    morning = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
    snap = build_snapshot(_client(), SETTINGS, now=morning)
    assert set(snap) == {"ENEL.MI"}
    assert snap["ENEL.MI"]["market"] == "europe"
    assert "[EU]" in snap["ENEL.MI"]["view"]


def test_snapshot_overlap_includes_both_markets():
    overlap = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
    snap = build_snapshot(_client(), SETTINGS, now=overlap)
    assert set(snap) == {"AAPL", "ENEL.MI"}
    assert snap["AAPL"]["market"] == "usa"


def test_snapshot_closed_markets_includes_everything_when_requested():
    night = datetime(2026, 7, 27, 22, 0, tzinfo=timezone.utc)
    assert build_snapshot(_client(), SETTINGS, now=night) == {}
    snap = build_snapshot(_client(), SETTINGS, now=night, only_open=False)
    assert set(snap) == {"AAPL", "ENEL.MI"}


# ------------------------------------------------------------------ indicatori


def _mk_candles(closes, high_off=1.0, low_off=1.0, volume=1000.0):
    return [
        {"close": c, "high": c + high_off, "low": c - low_off, "volume": volume}
        for c in closes
    ]


def test_indicators_from_candles_flat_series():
    from etoro_bot.arena.market import indicators_from_candles

    candles = _mk_candles([100.0] * 25)
    ind = indicators_from_candles(100.0, candles)
    # serie piatta con range 2: ATR = 2% del prezzo, RSI neutro, vol nulla
    assert ind["atr14_pct"] == 2.0
    assert ind["rsi14"] == 50.0
    assert ind["vol20_pct"] == 0.0
    assert ind["volume_rel"] == 1.0


def test_indicators_from_candles_uptrend_rsi_high():
    from etoro_bot.arena.market import indicators_from_candles

    closes = [100.0 + i for i in range(25)]
    ind = indicators_from_candles(closes[-1], _mk_candles(closes))
    assert ind["rsi14"] == 100.0
    assert ind["vol20_pct"] is not None and ind["vol20_pct"] > 0


def test_indicators_from_candles_volume_spike():
    from etoro_bot.arena.market import indicators_from_candles

    candles = _mk_candles([100.0] * 25)
    candles[-1]["volume"] = 3000.0
    ind = indicators_from_candles(100.0, candles)
    assert ind["volume_rel"] == 3.0


def test_indicators_from_candles_short_history_is_none():
    from etoro_bot.arena.market import indicators_from_candles, indicators_view

    ind = indicators_from_candles(100.0, _mk_candles([100.0] * 5))
    assert ind == {"atr14_pct": None, "rsi14": None, "vol20_pct": None, "volume_rel": None}
    assert indicators_view(ind) == ""


def test_snapshot_view_includes_indicators():
    overlap = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
    snap = build_snapshot(_client(), SETTINGS, now=overlap)
    row = snap["AAPL"]
    assert "atr14_pct" in row and "rsi14" in row
