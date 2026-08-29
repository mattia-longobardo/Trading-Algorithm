"""Test del Mandate: policy di rischio pre-trade congelata, fail-closed.

Pattern Vibe-Trading: il mandate è letto al boot da risk_rules.yaml, l'LLM non
ha alcun path di scrittura, e l'assenza del file NON produce default permissivi.
"""

from __future__ import annotations

from etoro_bot.safety.mandate import Mandate, check_open, load_mandate

ACTIVE = Mandate(
    trading_state="ACTIVE",
    max_notional_per_order_usd=2_500.0,
    max_total_exposure_pct=100.0,
    max_symbol_exposure_pct=25.0,
    max_orders_per_day=20,
    min_stop_loss_pct=0.5,
    denylist=("GME",),
)

BASE = {
    "symbol": "AAPL",
    "amount_usd": 1_000.0,
    "equity_usd": 10_000.0,
    "total_exposure_usd": 0.0,
    "symbol_exposure_usd": 0.0,
    "opens_today": 0,
    "stop_loss_pct": 2.0,
}


def test_mandate_allows_a_sane_order():
    assert check_open(ACTIVE, **BASE) is None


def test_mandate_default_is_fail_closed():
    """Senza configurazione esplicita non si apre niente."""
    assert Mandate().trading_state == "HALTED"
    assert check_open(Mandate(), **BASE) == "halted"


def test_mandate_reducing_blocks_opens():
    m = Mandate(**{**ACTIVE.__dict__, "trading_state": "REDUCING"})
    assert check_open(m, **BASE) == "reducing"


def test_mandate_denylist():
    assert check_open(ACTIVE, **{**BASE, "symbol": "GME"}) == "denylist"


def test_mandate_max_notional():
    assert check_open(ACTIVE, **{**BASE, "amount_usd": 3_000.0}) == "max_notional"


def test_mandate_total_exposure_cap():
    assert (
        check_open(ACTIVE, **{**BASE, "total_exposure_usd": 9_500.0})
        == "max_total_exposure"
    )


def test_mandate_symbol_exposure_cap():
    # 25% di 10k = 2500; 2000 già a mercato + 1000 nuovo = oltre
    assert (
        check_open(ACTIVE, **{**BASE, "symbol_exposure_usd": 2_000.0})
        == "max_symbol_exposure"
    )


def test_mandate_orders_per_day_cap():
    assert check_open(ACTIVE, **{**BASE, "opens_today": 20}) == "max_orders_per_day"


def test_mandate_stop_loss_floor():
    """Uno stop a 0 è una violazione, non una scelta genetica: i bound del DNA
    sono rappresentabilità, la policy di rischio vive qui."""
    assert check_open(ACTIVE, **{**BASE, "stop_loss_pct": 0.0}) == "stop_loss_floor"
    assert check_open(ACTIVE, **{**BASE, "stop_loss_pct": 0.4}) == "stop_loss_floor"


def test_load_mandate_reads_repo_config():
    """Il risk_rules.yaml del repo esiste e definisce uno stato operativo."""
    m = load_mandate()
    assert m.trading_state in {"ACTIVE", "REDUCING", "HALTED"}
    assert m.max_notional_per_order_usd > 0


def test_load_mandate_missing_file_is_halted(tmp_path, monkeypatch):
    from etoro_bot import config as config_module

    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
    load_mandate.cache_clear()
    try:
        assert load_mandate().trading_state == "HALTED"
    finally:
        load_mandate.cache_clear()
