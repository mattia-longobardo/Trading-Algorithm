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
    assert body["scheduler_active"] is False  # DISABLE_SCHEDULER=1


def test_status_says_scheduler_inactive_when_lock_is_taken(
    repo, pg_url, tmp_path, monkeypatch, caplog
):
    """Seconda istanza: API su, scheduler no — e /status lo dice."""
    import importlib

    from fastapi.testclient import TestClient

    from etoro_bot.db.repo import try_scheduler_lock

    held = try_scheduler_lock(pg_url)  # "prima istanza"
    assert held is not None
    try:
        monkeypatch.setenv("DATABASE_URL", pg_url)
        monkeypatch.setenv("KILL_SWITCH_DIR", str(tmp_path))
        monkeypatch.setenv("STATE_DIR", str(tmp_path))
        monkeypatch.setenv("KNOWLEDGE_BASE_DIR", str(tmp_path / "knowledge_base"))
        monkeypatch.delenv("DISABLE_SCHEDULER", raising=False)
        monkeypatch.delenv("ETORO_BOT_KILL", raising=False)

        from etoro_bot.api import server

        importlib.reload(server)
        server.get_repo.cache_clear()
        with caplog.at_level("CRITICAL"), TestClient(server.app) as tc:
            body = tc.get("/status").json()
        assert body["scheduler_active"] is False
        assert server._scheduler_lock is None
        assert "advisory lock non acquisito" in caplog.text
    finally:
        held.close()


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
    assert client.get("/executions").json() == {"executions": []}


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


def _seed_arena_agent(repo):
    """Agente con un long vincente, uno short perdente e una posizione aperta."""
    from datetime import datetime, timedelta, timezone

    from etoro_bot.arena.dna import DEFAULT_DNA, clamp_dna

    agent_id = repo.create_agent(
        "G1-Alfa", 1, clamp_dna(DEFAULT_DNA), "memoria", "2026-07", 10_000.0
    )
    now = datetime.now(timezone.utc)

    # trade long vincente: 1.000 $ a 100 chiusi a 110 → +100 $
    repo.open_sim_position(
        agent_id, "AAPL", 1, 1_000.0, 100.0, "long ok",
        opened_at=now - timedelta(hours=5),
    )
    repo.close_sim_position(repo.sim_positions(agent_id)[0].id, 110.0, "tp")
    # trade short perdente scritto alla vecchia maniera: la direzione sta solo
    # nel marcatore [SHORT] della open_reason (parsing legacy)
    repo.open_sim_position(
        agent_id, "TSLA", 2, 1_000.0, 100.0, "[SHORT] short ko",
        opened_at=now - timedelta(hours=2),
    )
    repo.close_sim_position(repo.sim_positions(agent_id)[0].id, 90.0, "sl")
    # posizione ancora aperta, short
    repo.open_sim_position(agent_id, "NVDA", 3, 500.0, 50.0, "[SHORT] aperta")

    repo.record_sim_equity(agent_id, now - timedelta(days=1), 10_000.0)
    repo.record_sim_equity(agent_id, now, 9_900.0)
    return agent_id


def test_arena_agent_detail_metrics_and_directions(client, repo):
    """Il dettaglio agente espone metriche, direzione e durata dei trade."""
    agent_id = _seed_arena_agent(repo)

    body = client.get(f"/arena/agents/{agent_id}").json()

    metrics = body["metrics"]
    assert metrics["n_trades"] == 2
    assert metrics["win_rate_pct"] == 50.0
    assert metrics["long"]["n"] == 1 and metrics["short"]["n"] == 1
    assert metrics["insufficient_sample"] is True

    assert body["agent"]["invested_usd"] == 500.0
    assert body["agent"]["open_positions"][0]["direction"] == "short"

    assert {"direction", "holding_hours", "return_pct"} <= set(body["trades"][0])
    assert {t["direction"] for t in body["trades"]} == {"long", "short"}
    by_dir = {t["direction"]: t for t in body["trades"]}
    assert by_dir["long"]["return_pct"] == 10.0
    assert by_dir["short"]["return_pct"] == -10.0
    assert by_dir["long"]["holding_hours"] == 5.0


def test_arena_agent_metrics_see_all_trades_not_just_the_shown_ones(
    client, repo, monkeypatch
):
    """La lista trade è cappata per la UI, le metriche contano tutto lo storico."""
    from etoro_bot.api import server

    agent_id = _seed_arena_agent(repo)
    # il limite del repo tronca davvero, e `None` restituisce tutto
    assert len(repo.sim_trades(agent_id, limit=1)) == 1
    assert len(repo.sim_trades(agent_id, limit=None)) == 2

    monkeypatch.setattr(server, "ARENA_TRADES_SHOWN", 1)  # cap ridotto, stesso effetto
    body = client.get(f"/arena/agents/{agent_id}").json()
    assert len(body["trades"]) == 1
    assert body["metrics"]["n_trades"] == 2
    assert body["metrics"]["long"]["n"] == 1 and body["metrics"]["short"]["n"] == 1


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
    # senza campione vale il DNA di default: max_position_pct 35%
    assert body["max_trade_amount_usd"] == pytest.approx(51_073.77 * 0.35)
    assert body["capital_source"] == "etoro"


# --- token interno e controllo proprietario ---------------------------------


def test_internal_token_rejects_requests_without_the_shared_secret(client, monkeypatch):
    """Con TRADING_INTERNAL_TOKEN configurato il backend accetta solo le
    richieste che arrivano dal proxy: gli header di identità non bastano."""
    monkeypatch.setenv("TRADING_INTERNAL_TOKEN", "segreto-di-prova")

    assert client.get("/status").status_code == 401
    assert client.post("/kill-switch").status_code == 401
    # sonda di salute del container: sempre raggiungibile
    assert client.get("/health").status_code == 200

    headers = {"x-trading-internal-token": "segreto-di-prova"}
    assert client.get("/status", headers=headers).status_code == 200
    assert client.get("/status", headers={"x-trading-internal-token": "sbagliato"}
                      ).status_code == 401


def test_mutating_endpoints_require_the_owner(client, repo, monkeypatch):
    """Con un proprietario configurato, un'altra identità non può toccare
    kill switch, live o ordini."""
    monkeypatch.setenv("TRADING_CREDENTIALS_SECRET", "chiave-di-prova")
    from etoro_bot.services.user_credentials import update_user_keys

    update_user_keys(
        repo, "proprietario", email=None, display_name=None,
        etoro_api_key="a", etoro_user_key="b", openai_api_key=None,
    )
    intruder = {"x-trading-user-id": "estraneo"}

    assert client.post("/kill-switch", headers=intruder).status_code == 403
    assert client.delete("/kill-switch", headers=intruder).status_code == 403
    assert client.post("/live/disable", headers=intruder).status_code == 403
    assert client.post(
        "/executions/00000000-0000-0000-0000-000000000000/cancel", headers=intruder
    ).status_code == 403
    # la knowledge base è globale e alimenta i prompt del trader: un estraneo
    # non può avvelenarla
    assert client.put(
        "/knowledge/rss-feeds", json={"feeds": []}, headers=intruder
    ).status_code == 403
    assert client.post("/knowledge/fetch-news", headers=intruder).status_code == 403
    assert client.post(
        "/knowledge/ingest", files={"file": ("nota.txt", b"testo")}, headers=intruder
    ).status_code == 403
    # salvare le proprie chiavi eToro significa diventare proprietario
    # (`owner_user_id()` prende l'ultimo che le ha salvate): è una mutazione
    # dello stato condiviso, non un dato personale
    assert client.put(
        "/account/credentials", json={"etoro_api_key": "x", "etoro_user_key": "y"},
        headers=intruder,
    ).status_code == 403
    # il proprietario passa
    owner = {"x-trading-user-id": "proprietario"}
    assert client.post("/live/disable", headers=owner).status_code == 200


def test_owner_check_returns_503_when_the_database_is_unreadable(client, monkeypatch):
    """DB giù: il proprietario non è verificabile. Fail-closed (503), non
    un via libera a chiunque sul kill switch."""
    from etoro_bot.api import server

    def boom():
        raise RuntimeError("Postgres irraggiungibile")

    monkeypatch.setattr(server.get_repo(), "owner_user_id", boom)

    assert client.post("/kill-switch").status_code == 503
    assert client.put("/settings", json={"timezone": "UTC"}).status_code == 503


def test_system_identity_is_rejected_over_http(client):
    """«system» è l'identità dei job interni, che non passano da FastAPI:
    via HTTP nessuno può indossarla."""
    system = {"x-trading-user-id": "system"}

    assert client.get("/settings", headers=system).status_code == 403
    assert client.post("/kill-switch", headers=system).status_code == 403
    assert client.post("/live/disable", headers=system).status_code == 403


def test_startup_aborts_without_internal_token_and_without_dev_flag(monkeypatch):
    """Nessun token e nessun flag di sviluppo: il processo non deve partire."""
    from fastapi.testclient import TestClient

    from etoro_bot.api import server

    monkeypatch.delenv("TRADING_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("TRADING_DEV_MODE", raising=False)
    importlib.reload(server)

    with pytest.raises(RuntimeError, match="TRADING_INTERNAL_TOKEN"), TestClient(server.app):
        pass

    # col flag esplicito di sviluppo l'avvio è consentito
    monkeypatch.setenv("TRADING_DEV_MODE", "1")
    server._require_internal_token_configured()


def test_close_trade_returns_502_when_the_broker_fails(client, repo, monkeypatch):
    """Broker giù: è un guasto a monte (502), non un errore del backend (500).
    La posizione resta aperta a registro: nessun ordine è mai partito."""
    from datetime import UTC, datetime

    from etoro_bot.api import server

    class FakeEtoro:
        def close_position(self, position_id, instrument_id, request_id=None):
            raise RuntimeError("eToro irraggiungibile")

    repo.create_run("live-close-502", environment="live")
    repo.register_open_position(
        4242, "live-close-502", "AAPL", 1, 100.0, 10.0, datetime.now(UTC)
    )
    monkeypatch.setattr(server, "_make_client", lambda *_: FakeEtoro())

    resp = client.post("/trades/4242/close", json={"confirmation": "CHIUDI"})
    assert resp.status_code == 502
    assert repo.get_open_position(4242) is not None


def test_close_trade_does_not_500_when_the_registry_write_fails(client, repo, monkeypatch):
    """Posizione chiusa al broker ma scrittura a registro KO: un 500 inviterebbe
    l'utente a ripetere l'ordine. Si risponde chiuso, con il registry in attesa
    del reconcile."""
    from datetime import UTC, datetime

    from etoro_bot.api import server

    class FakeEtoro:
        def close_position(self, position_id, instrument_id, request_id=None):
            return {"order_id": 7, "position_id": position_id}

        def get_trade_history(self, min_date=None, page_size=100):
            return []

    repo.create_run("live-close-db", environment="live")
    repo.register_open_position(
        4343, "live-close-db", "AAPL", 1, 100.0, 10.0, datetime.now(UTC)
    )
    monkeypatch.setattr(server, "_make_client", lambda *_: FakeEtoro())
    monkeypatch.setattr(
        server.get_repo(), "close_position",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("DB KO")),
    )

    resp = client.post("/trades/4343/close", json={"confirmation": "CHIUDI"})
    assert resp.status_code == 200
    assert resp.json()["registry"] == "pending_reconcile"
