"""Test del wrapper Kronos: parti pure (senza torch) + degradazione a no-op."""

from __future__ import annotations

from etoro_bot.forecast.kronos import (
    cached_forecasts,
    cross_rank,
    kronos_config,
    refresh_forecasts,
    summarize_prediction,
)


def test_summarize_prediction_ret_and_vol():
    pred = [
        {"close": 102.0, "high": 103.0, "low": 101.0},
        {"close": 105.0, "high": 106.0, "low": 104.0},
    ]
    s = summarize_prediction(100.0, pred)
    assert s["kronos_ret_pred"] == 5.0
    # range medio: (2/102 + 2/105)/2 * 100
    assert 1.8 < s["kronos_vol_pred"] < 2.0
    assert summarize_prediction(0.0, pred) is None
    assert summarize_prediction(100.0, []) is None


def test_cross_rank_percentiles():
    forecasts = {
        "A": {"kronos_ret_pred": -2.0},
        "B": {"kronos_ret_pred": 0.0},
        "C": {"kronos_ret_pred": 3.0},
    }
    cross_rank(forecasts)
    assert forecasts["A"]["kronos_rank"] == 0.0
    assert forecasts["B"]["kronos_rank"] == 50.0
    assert forecasts["C"]["kronos_rank"] == 100.0


def test_refresh_disabled_is_noop():
    assert refresh_forecasts(object(), {})["skipped"] == "disabled"
    assert refresh_forecasts(None, {"forecast": {"kronos": {"enabled": True}}})[
        "skipped"
    ] == "no_client"


def test_annotate_snapshot_merges_cached(monkeypatch):
    import etoro_bot.forecast.kronos as fk

    monkeypatch.setattr(
        fk, "cached_forecasts",
        lambda: {"AAPL": {"kronos_ret_pred": 1.2, "kronos_vol_pred": 2.0,
                          "kronos_rank": 87.0}},
    )
    snap = {"AAPL": {"price": 200.0, "view": "AAPL 200"},
            "MSFT": {"price": 400.0, "view": "MSFT 400"}}
    fk.annotate_snapshot(snap)
    assert snap["AAPL"]["kronos_rank"] == 87.0
    assert "kronos +1.2% (rank 87)" in snap["AAPL"]["view"]
    assert "kronos" not in snap["MSFT"]["view"]


def test_kronos_config_defaults():
    cfg = kronos_config({})
    assert cfg["enabled"] is False and cfg["pred_len"] == 5
    assert cached_forecasts() == {}
