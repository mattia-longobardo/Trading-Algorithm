"""Test dei guardrail delle impostazioni e dell'attivazione live."""

import pytest

from etoro_bot.services.app_settings import (
    AppSettingsService,
    SettingsValidationError,
    arena_state,
    check_live_activation,
    set_arena_state,
)


@pytest.fixture(autouse=True)
def safety_env(monkeypatch, tmp_path):
    """Kill switch/breaker su directory pulita e nessun kill via env."""
    monkeypatch.setenv("KILL_SWITCH_DIR", str(tmp_path))
    monkeypatch.delenv("ETORO_BOT_KILL", raising=False)


@pytest.fixture()
def svc(repo):
    return AppSettingsService(repo)


# --- lettura -----------------------------------------------------------------


def test_effective_defaults_from_yaml(svc):
    effective = svc.get_effective()
    assert effective["timezone"] == "Europe/Rome"
    assert effective["currency"] in ("USD", "EUR")
    assert effective["arena"]["paused"] is False
    assert effective["arena"]["live_enabled"] is False


def test_db_value_overrides_yaml(svc, repo):
    repo.set_setting("timezone", "UTC")
    assert svc.get_effective()["timezone"] == "UTC"


def test_arena_state_roundtrip(repo):
    assert arena_state(repo)["paused"] is False
    state = set_arena_state(repo, paused=True, month="2026-07")
    assert state["paused"] is True and state["month"] == "2026-07"
    # update parziale: le altre chiavi restano
    state = set_arena_state(repo, live_enabled=True)
    assert state["paused"] is True and state["live_enabled"] is True


# --- guardrail attivazione live ------------------------------------------------


def test_live_blocked_without_etoro_keys(repo):
    with pytest.raises(SettingsValidationError) as exc:
        check_live_activation(repo, etoro_configured=False)
    assert "chiavi eToro" in str(exc.value)


def test_live_blocked_without_champion(repo):
    with pytest.raises(SettingsValidationError) as exc:
        check_live_activation(repo, etoro_configured=True)
    assert "campione" in str(exc.value)


def test_live_blocked_by_kill_switch(repo):
    from etoro_bot.arena.dna import DEFAULT_DNA, clamp_dna
    from etoro_bot.safety.kill_switch import engage_kill_switch

    agent_id = repo.create_agent("G1-Alfa", 1, clamp_dna(DEFAULT_DNA), "", "2026-06",
                                 10_000.0)
    repo.set_champion(agent_id)
    engage_kill_switch("test")
    with pytest.raises(SettingsValidationError) as exc:
        check_live_activation(repo, etoro_configured=True)
    assert "kill switch" in str(exc.value)


def test_live_usa_il_breaker_condiviso(repo):
    """Lo stato in memoria del breaker singleton blocca il live.

    Con un'istanza effimera il trip non ancora salvato su file resterebbe
    invisibile (e il suo _save() sovrascriverebbe lo stato condiviso).
    """
    from etoro_bot.arena.dna import DEFAULT_DNA, clamp_dna
    from etoro_bot.config import load_breaker_rules
    from etoro_bot.safety.circuit_breaker import get_breaker

    agent_id = repo.create_agent("G1-Alfa", 1, clamp_dna(DEFAULT_DNA), "", "2026-06",
                                 10_000.0)
    repo.set_champion(agent_id)
    breaker = get_breaker(load_breaker_rules())
    breaker.state.tripped = True  # solo in memoria: il file di stato resta pulito
    breaker.state.cooloff_until = None

    with pytest.raises(SettingsValidationError) as exc:
        check_live_activation(repo, etoro_configured=True)
    assert "circuit breaker" in str(exc.value)


def test_live_allowed_with_champion_and_keys(repo):
    from etoro_bot.arena.dna import DEFAULT_DNA, clamp_dna

    agent_id = repo.create_agent("G1-Alfa", 1, clamp_dna(DEFAULT_DNA), "", "2026-06",
                                 10_000.0)
    repo.set_champion(agent_id)
    check_live_activation(repo, etoro_configured=True)  # non solleva


# --- scrittura -----------------------------------------------------------------


def test_audit_recorded_on_change(svc, repo):
    svc.update({"timezone": "UTC"}, source="test")
    entries = [e for e in repo.settings_audit() if e.key == "timezone"]
    assert entries
    assert entries[0].new_value == {"value": "UTC"}
    assert entries[0].source == "test"


@pytest.mark.parametrize(
    "changes",
    [
        {"timezone": "Marte/Olympus"},
        {"currency": "???"},
        {"environment": "real"},   # chiave rimossa col vecchio dual-env
        {"risk_limits": {}},       # chiave rimossa col vecchio risk gate
        {"unknown_key": 1},
    ],
)
def test_invalid_values_rejected(svc, changes):
    with pytest.raises(SettingsValidationError):
        svc.update(changes)
