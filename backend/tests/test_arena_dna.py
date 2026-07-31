"""Test del DNA degli agenti: default, clamp e mutazione."""

from __future__ import annotations

import random

from etoro_bot.arena.dna import (
    DEFAULT_DNA,
    NUMERIC_BOUNDS,
    clamp_dna,
    mutate,
    survival_creed,
)


def test_default_dna_is_within_bounds():
    for key, (lo, hi, _is_int) in NUMERIC_BOUNDS.items():
        assert lo <= DEFAULT_DNA[key] <= hi


def test_holding_days_gene_allows_swing():
    """L'orizzonte è materiale genetico: da 1 seduta (intraday) fino a 60."""
    lo, hi, is_int = NUMERIC_BOUNDS["max_holding_days"]
    assert (lo, hi, is_int) == (1, 60, True)
    assert DEFAULT_DNA["max_holding_days"] == 10  # baseline: swing autorizzato


def test_clamp_fills_missing_and_bounds_extremes():
    dna = clamp_dna({"stop_loss_pct": 999.0, "max_positions": 0})
    lo_sl, hi_sl, _ = NUMERIC_BOUNDS["stop_loss_pct"]
    lo_mp, hi_mp, _ = NUMERIC_BOUNDS["max_positions"]
    assert dna["stop_loss_pct"] == hi_sl
    assert dna["max_positions"] == lo_mp
    # i campi mancanti arrivano dal default
    assert dna["strategy"] == DEFAULT_DNA["strategy"]
    assert dna["conviction_scale"] == DEFAULT_DNA["conviction_scale"]


def test_mutate_changes_at_least_one_numeric_field_within_bounds():
    rng = random.Random(42)
    child = mutate(DEFAULT_DNA, rng)
    assert child != DEFAULT_DNA
    changed = [k for k in NUMERIC_BOUNDS if child[k] != DEFAULT_DNA[k]]
    assert changed, "la mutazione deve toccare almeno un parametro numerico"
    for key, (lo, hi, is_int) in NUMERIC_BOUNDS.items():
        assert lo <= child[key] <= hi
        if is_int:
            assert isinstance(child[key], int)


def test_mutate_rewrites_strategy_via_llm_when_available():
    def fake_llm(system_blocks, user_prompt, model, max_tokens):
        return "Nuova strategia: contrarian sui gap down con volumi anomali."

    rng = random.Random(7)
    child = mutate(DEFAULT_DNA, rng, llm=fake_llm, model="m", max_tokens=256)
    assert child["strategy"].startswith("Nuova strategia")


def test_mutate_without_llm_keeps_strategy_text():
    rng = random.Random(1)
    child = mutate(DEFAULT_DNA, rng)
    assert child["strategy"] == DEFAULT_DNA["strategy"]


def test_survival_creed_mentions_death_and_profit():
    creed = survival_creed()
    assert "profitto" in creed.lower()
    assert "mor" in creed.lower()  # morte/morirai
