"""Smoke test dell'API FastAPI con TestClient su Postgres effimero."""

import importlib

import pytest


@pytest.fixture()
def client(repo, pg_url, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("KILL_SWITCH_DIR", str(tmp_path))
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("KNOWLEDGE_BASE_DIR", str(tmp_path / "knowledge_base"))
    monkeypatch.setenv("DISABLE_SCHEDULER", "1")
    monkeypatch.delenv("ETORO_BOT_KILL", raising=False)

    from fastapi.testclient import TestClient

    from etoro_bot.api import server

    importlib.reload(server)  # ricostruisce app e cache col nuovo env
    server.get_repo.cache_clear()
    with TestClient(server.app) as tc:
        yield tc


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_status_shape(client):
    body = client.get("/status").json()
    assert body["kill_switch_active"] is False
    assert body["circuit_breaker"]["tripped"] is False
    assert body["arena"]["paused"] is False
    assert body["arena"]["live_enabled"] is False
    assert body["champion"] is None
    assert "market_open" in body
    assert body["next_cycle_at"]


def test_settings_get_and_update(client):
    body = client.get("/settings").json()
    assert body["api_keys_configured"] is False
    assert body["timezone"] == "Europe/Rome"
    assert "arena" in body

    resp = client.put("/settings", json={"timezone": "UTC"})
    assert resp.status_code == 200
    assert client.get("/settings").json()["timezone"] == "UTC"

    # chiave sconosciuta → 422 (environment non esiste più)
    assert client.put("/settings", json={"timezone": "not-a-zone"}).status_code == 422

    audit = client.get("/settings/audit").json()
    assert len(audit["entries"]) >= 1


def test_kill_switch_roundtrip(client):
    assert client.post("/kill-switch").json()["kill_switch_active"] is True
    assert client.get("/status").json()["kill_switch_active"] is True
    assert client.delete("/kill-switch").json()["kill_switch_active"] is False


def test_empty_journal_endpoints(client):
    assert client.get("/runs").json() == {"runs": []}
    assert client.get("/executions").json() == {"executions": []}
    assert client.get("/runs/nope/decisions").status_code == 404


def test_backtest_endpoints_empty(client):
    summary = client.get("/backtest/summary").json()
    assert summary["n_closed_trades"] == 0
    assert summary["insufficient_sample"] is True
    assert summary["metrics"]["cagr_pct"] is None

    curve = client.get("/backtest/equity-curve").json()
    assert curve["points"] == []
    assert "dividendi" in curve["note_dividends"]

    assert client.get("/backtest/trades").json() == {"trades": []}
    assert client.get("/backtest/monthly-returns").status_code == 200


def test_arena_overview_empty(client):
    body = client.get("/arena").json()
    assert body["agents"] == []
    assert body["champion"] is None
    assert body["generation"] == 0
    assert body["state"]["paused"] is False
    assert "days_to_evaluation" in body


def test_arena_pause_resume(client):
    assert client.post("/arena/pause").json()["state"]["paused"] is True
    assert client.get("/status").json()["arena"]["paused"] is True
    assert client.post("/arena/resume").json()["state"]["paused"] is False
    events = client.get("/arena/events").json()["events"]
    assert {e["event"] for e in events} >= {"pause", "resume"}


def test_arena_agent_detail_404(client):
    assert client.get(
        "/arena/agents/00000000-0000-0000-0000-000000000000"
    ).status_code == 404


def test_live_enable_requires_confirmation_and_champion(client):
    # senza conferma → 422
    assert client.post("/live/enable", json={}).status_code == 422
    # con conferma ma senza chiavi/campione → 422 (guardrail backend)
    resp = client.post("/live/enable", json={"confirmation": True})
    assert resp.status_code == 422
    # lo stato non è cambiato
    assert client.get("/status").json()["arena"]["live_enabled"] is False
    # disable è sempre permesso e idempotente
    assert client.post("/live/disable").json()["state"]["live_enabled"] is False


def test_knowledge_status_degraded(client, monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://localhost:1")  # niente Qdrant
    body = client.get("/knowledge/status").json()
    assert body["qdrant_up"] is False
    assert isinstance(body["rss_feeds"], list) and body["rss_feeds"]


def test_portfolio_empty(client, monkeypatch):
    from etoro_bot.api import server

    class FakeEtoro:
        def get_portfolio(self):
            return {"credit": 51_073.77}

    monkeypatch.setattr(server, "_make_client", lambda *_: FakeEtoro())
    body = client.get("/portfolio").json()
    assert body["positions"] == []
    assert body["equity_usd"] == body["cash_usd"] == 51_073.77
    # senza campione vale il DNA di default: max_position_pct 25%
    assert body["max_trade_amount_usd"] == pytest.approx(51_073.77 * 0.25)
    assert body["capital_source"] == "etoro"


def test_delete_run_endpoint(client, repo):
    repo.create_run("run-api-del", environment="live")
    assert client.delete("/runs/run-api-del").json() == {
        "deleted": True, "run_id": "run-api-del",
    }
    assert client.get("/runs").json()["runs"] == []


def test_delete_unknown_run_is_404(client):
    assert client.delete("/runs/inesistente").status_code == 404


def test_universe_view_includes_yaml_watchlist(client):
    """Regressione: /universe deve vedere i settings COMPLETI (yaml + runtime),
    non solo le chiavi runtime di get_effective() — altrimenti watchlist vuota."""
    body = client.get("/universe").json()
    assert "AAPL" in body["watchlist"]
    assert body["enabled"] is True
    assert body["discovered"] == []


def test_ticker_memory_endpoint_empty(client):
    assert client.get("/knowledge/ticker-memory").json() == {"memories": []}
