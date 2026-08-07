"""Test del calcolo metriche dell'arena (modulo puro, nessun DB)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from etoro_bot.arena.metrics import compute_agent_metrics, trade_direction

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _trade(pnl, amount=100.0, hours=4.0, short=False, closed_at=NOW):
    reason = "[SHORT] scommessa ribassista" if short else "trend rialzista"
    return SimpleNamespace(
        pnl_usd=pnl,
        amount_usd=amount,
        opened_at=closed_at - timedelta(hours=hours),
        closed_at=closed_at,
        open_reason=reason,
    )


def _eq(days_ago, equity):
    return SimpleNamespace(ts=NOW - timedelta(days=days_ago), equity_usd=equity)


def test_trade_direction():
    # colonna persistita
    assert trade_direction(SimpleNamespace(direction="short", open_reason="")) == "short"
    assert trade_direction(SimpleNamespace(direction="long", open_reason="")) == "long"
    # righe vecchie: la direzione stava solo nel testo
    assert trade_direction(_trade(1.0)) == "long"
    assert trade_direction(_trade(1.0, short=True)) == "short"


def test_metrics_empty():
    m = compute_agent_metrics(
        starting_capital_usd=10_000.0, cash_usd=10_000.0, invested_usd=0.0,
        born_at=NOW - timedelta(days=3), trades=[], equity_points=[], now=NOW,
    )
    assert m["n_trades"] == 0
    assert m["win_rate_pct"] is None
    assert m["profit_factor"] is None
    assert m["max_drawdown_pct"] is None
    assert m["sharpe"] is None
    assert m["insufficient_sample"] is True
    assert m["pnl_usd"] == 0.0
    assert m["exposure_pct"] == 0.0
    assert m["cash_pct"] == 100.0
    assert m["long"] == {"n": 0, "pnl_usd": 0.0, "win_rate_pct": None}


def test_metrics_basic_counts_and_ratios():
    trades = [
        _trade(30.0), _trade(60.0), _trade(-30.0),
        _trade(-15.0, short=True), _trade(45.0, short=True),
    ]
    m = compute_agent_metrics(
        starting_capital_usd=10_000.0, cash_usd=9_500.0, invested_usd=590.0,
        born_at=NOW - timedelta(days=5), trades=trades, equity_points=[], now=NOW,
    )
    assert m["n_trades"] == 5
    assert m["n_wins"] == 3 and m["n_losses"] == 2
    assert m["win_rate_pct"] == 60.0
    assert m["profit_factor"] == round(135.0 / 45.0, 2)
    assert m["expectancy_usd"] == 18.0
    assert m["best_trade_usd"] == 60.0 and m["worst_trade_usd"] == -30.0
    assert m["avg_win_usd"] == 45.0 and m["avg_loss_usd"] == -22.5
    assert m["long"]["n"] == 3 and m["short"]["n"] == 2
    assert m["short"]["pnl_usd"] == 30.0
    assert m["short"]["win_rate_pct"] == 50.0
    assert m["pnl_usd"] == 90.0          # 9500 + 590 - 10000
    assert m["return_pct"] == 0.9
    assert m["trades_per_day"] == 1.0
    assert m["avg_holding_hours"] == 4.0


def test_profit_factor_null_senza_perdite():
    m = compute_agent_metrics(
        starting_capital_usd=10_000.0, cash_usd=10_100.0, invested_usd=0.0,
        born_at=NOW - timedelta(days=1), trades=[_trade(50.0), _trade(50.0)],
        equity_points=[], now=NOW,
    )
    assert m["profit_factor"] is None
    assert m["n_losses"] == 0 and m["avg_loss_usd"] is None


def test_max_drawdown_e_sharpe():
    pts = [_eq(6, 10_000), _eq(5, 10_400), _eq(4, 9_880),
           _eq(3, 10_100), _eq(2, 10_500), _eq(1, 10_300)]
    m = compute_agent_metrics(
        starting_capital_usd=10_000.0, cash_usd=10_300.0, invested_usd=0.0,
        born_at=NOW - timedelta(days=7), trades=[], equity_points=pts, now=NOW,
    )
    # picco 10400 -> minimo 9880: -5.0%
    assert m["max_drawdown_pct"] == -5.0
    assert m["volatility_pct"] is not None
    assert m["sharpe"] is not None


def test_intraday_collassa_a_ultimo_punto_del_giorno():
    # Due punti lo stesso giorno: per i rendimenti giornalieri conta l'ultimo.
    pts = [
        SimpleNamespace(ts=NOW - timedelta(days=2, hours=5), equity_usd=10_000),
        SimpleNamespace(ts=NOW - timedelta(days=2, hours=1), equity_usd=10_200),
        SimpleNamespace(ts=NOW - timedelta(days=1, hours=1), equity_usd=10_100),
    ]
    m = compute_agent_metrics(
        starting_capital_usd=10_000.0, cash_usd=10_100.0, invested_usd=0.0,
        born_at=NOW - timedelta(days=3), trades=[], equity_points=pts, now=NOW,
    )
    # solo 2 punti giornalieri -> volatilità/sharpe null, ma drawdown calcolato
    assert m["volatility_pct"] is None and m["sharpe"] is None
    # drawdown sulla serie completa: picco 10200 -> 10100 = -0.98%
    assert m["max_drawdown_pct"] == -0.98


def test_drawdown_usa_la_serie_completa_non_le_chiusure_giornaliere():
    # Tre punti nello stesso giorno: il minimo intraday NON deve sparire.
    day = NOW - timedelta(days=1)
    pts = [
        SimpleNamespace(ts=day - timedelta(hours=6), equity_usd=10_000),
        SimpleNamespace(ts=day - timedelta(hours=3), equity_usd=9_000),
        SimpleNamespace(ts=day, equity_usd=10_000),
    ]
    m = compute_agent_metrics(
        starting_capital_usd=10_000.0, cash_usd=10_000.0, invested_usd=0.0,
        born_at=NOW - timedelta(days=2), trades=[], equity_points=pts, now=NOW,
    )
    # picco 10000 -> minimo 9000: -10.0%
    assert m["max_drawdown_pct"] == -10.0
    # una sola chiusura giornaliera -> niente rendimenti giornalieri
    assert m["volatility_pct"] is None and m["sharpe"] is None
    assert m["insufficient_sample"] is True
