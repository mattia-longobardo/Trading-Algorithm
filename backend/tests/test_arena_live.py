"""Test del trading live guidato dal campione: freni operativi e esecuzione."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from etoro_bot.arena.dna import DEFAULT_DNA, clamp_dna
from etoro_bot.arena.engine import ArenaDeps
from etoro_bot.arena.live import run_live_cycle, run_live_eod
from etoro_bot.config import CircuitBreakerRules
from etoro_bot.safety.circuit_breaker import CircuitBreaker

NOW = datetime(2026, 7, 27, 15, 0, tzinfo=timezone.utc)

MARKET = {
    "AAPL": {"instrument_id": 1, "price": 200.0, "view": "AAPL 200.0"},
    "MSFT": {"instrument_id": 2, "price": 400.0, "view": "MSFT 400.0"},
}


class FakeLiveClient:
    """Client eToro finto con la stessa firma di quello vero (direction inclusa)."""

    def __init__(self, credit=10_000.0):
        self.credit = credit
        self.open_calls = []
        self.close_calls = []
        self.next_position_id = 500
        self.positions = []       # righe di portafoglio esposte da get_portfolio
        self.history = []         # trade chiusi visibili al broker
        self.lookups = []
        self.orders = {}          # reference_id → risposta di orders:lookup
        self.fail_open = False    # l'apertura solleva (ma l'ordine può passare)

    def get_portfolio(self):
        return {"positions": list(self.positions), "credit": self.credit}

    def get_trade_history(self, min_date=None, page_size=100):
        return list(self.history)

    def lookup_order(self, order_id=None, reference_id=None):
        self.lookups.append(reference_id or order_id)
        return self.orders.get(reference_id, {"status": {"id": 1}})

    def open_position(self, instrument_id, amount_usd, request_id, direction="buy"):
        self.open_calls.append((instrument_id, amount_usd, request_id, direction))
        if self.fail_open:
            raise RuntimeError("timeout in attesa del fill")
        self.next_position_id += 1
        return {"position_id": self.next_position_id, "execution_price": 100.0}

    def close_position(self, position_id, instrument_id):
        self.close_calls.append((position_id, instrument_id))
        return {"order_id": 9000 + position_id, "position_id": position_id}


def filled_order(position_id, price=100.0):
    """Risposta orders:lookup di un ordine eseguito."""
    return {
        "status": {"id": 3, "name": "Filled"},
        "positionExecutions": [
            {"positionId": position_id, "openingData": {"avgPrice": price}}
        ],
    }


@pytest.fixture(autouse=True)
def _isolate_safety(tmp_path, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH_DIR", str(tmp_path))
    monkeypatch.delenv("ETORO_BOT_KILL", raising=False)


def open_llm(symbol="AAPL", size_pct=20.0, direction="long"):
    def llm(system_blocks, user_prompt, model, max_tokens):
        return json.dumps(
            [{"action": "open", "symbol": symbol, "direction": direction,
              "size_pct": size_pct, "reason": "live"}]
        )
    return llm


def make_deps(repo, client, llm=None):
    return ArenaDeps(
        repo=repo, client=client,
        settings={"arena": {"starting_capital_eur": 10_000}},
        llm=llm, model="test-model", max_tokens=512,
    )


def _with_champion(repo):
    agent_id = repo.create_agent(
        "G1-Alfa", 1, clamp_dna(DEFAULT_DNA), "memoria", "2026-06", 10_000.0
    )
    repo.retire_agent(agent_id)
    repo.set_champion(agent_id)
    return agent_id


def test_live_cycle_skips_without_champion(repo, tmp_path):
    deps = make_deps(repo, FakeLiveClient(), llm=open_llm())
    assert run_live_cycle(deps, market=MARKET, now=NOW) == {"skipped": "no_champion"}


def test_live_cycle_skips_on_kill_switch(repo, monkeypatch):
    monkeypatch.setenv("ETORO_BOT_KILL", "1")
    _with_champion(repo)
    deps = make_deps(repo, FakeLiveClient(), llm=open_llm())
    assert run_live_cycle(deps, market=MARKET, now=NOW) == {"skipped": "kill_switch"}


def test_live_cycle_opens_real_position_and_journals(repo):
    _with_champion(repo)
    client = FakeLiveClient()
    deps = make_deps(repo, client, llm=open_llm("AAPL", 20.0))
    summary = run_live_cycle(deps, market=MARKET, now=NOW)
    assert summary["opened"] == 1
    assert len(client.open_calls) == 1
    positions = repo.open_positions()
    assert [p.symbol for p in positions] == ["AAPL"]
    executions = repo.list_executions()
    assert executions and executions[0].side == "buy"
    run = repo.get_run(f"live-{NOW:%Y%m%d}")
    assert run is not None and run.environment == "live"


def test_live_cycle_stop_loss_closes_real_position(repo):
    _with_champion(repo)
    client = FakeLiveClient()
    # LLM che non propone nulla: resta solo l'enforcement automatico SL/TP
    deps = make_deps(repo, client, llm=lambda **kw: "[]")
    run_id = f"live-{NOW:%Y%m%d}"
    repo.create_run(run_id, environment="live")
    repo.register_open_position(900, run_id, "AAPL", 1, 500.0, 300.0, NOW)
    run_live_cycle(deps, market=MARKET, now=NOW)  # AAPL a 200: -33% → stop loss
    assert client.close_calls == [(900, 1)]
    assert repo.open_positions() == []


def test_live_cycle_breaker_blocks_openings(repo):
    _with_champion(repo)
    client = FakeLiveClient()
    deps = make_deps(repo, client, llm=open_llm())
    rules = CircuitBreakerRules(max_consecutive_losses=1)
    breaker = CircuitBreaker(rules, state_dir=None)
    breaker.record_closed_trade(-100.0, 10_000.0)
    assert breaker.blocks_openings()
    summary = run_live_cycle(deps, breaker=breaker, market=MARKET, now=NOW)
    assert summary["blocked"] == 1 and summary["opened"] == 0
    assert client.open_calls == []


def test_live_session_close_then_eod_snapshot(repo):
    """Fine sessione live: si chiude solo ciò che ha esaurito l'holding del DNA.

    Col default (max_holding_days=10) lo swing reale passa la campanella; la
    posizione vecchia di settimane no. L'EOD fotografa comunque l'equity.
    """
    from datetime import timedelta

    from etoro_bot.arena.live import close_live_market_positions

    _with_champion(repo)
    client = FakeLiveClient()
    deps = make_deps(repo, client, llm=None)
    run_id = f"live-{NOW:%Y%m%d}"
    repo.create_run(run_id, environment="live")
    repo.register_open_position(901, run_id, "MSFT", 2, 400.0, 400.0, NOW)
    repo.register_open_position(
        902, run_id, "AAPL", 1, 200.0, 200.0, NOW - timedelta(days=25)
    )

    summary = close_live_market_positions(deps, "usa", market=MARKET, now=NOW)
    assert summary["closed"] == 1
    assert client.close_calls == [(902, 1)]
    assert [p.etoro_position_id for p in repo.open_positions()] == [901]

    eod = run_live_eod(deps, now=NOW)
    assert eod["snapshot"] is True
    assert repo.equity_series()  # snapshot registrato


# ------------------------------------------------- correttezza degli ordini


def test_short_order_reaches_the_client_as_a_sell(repo):
    """Uno short del campione parte davvero in vendita, non convertito in long."""
    _with_champion(repo)
    client = FakeLiveClient()
    deps = make_deps(repo, client, llm=open_llm("AAPL", 20.0, direction="short"))
    summary = run_live_cycle(deps, market=MARKET, now=NOW)
    assert summary["opened"] == 1
    assert client.open_calls[0][3] == "sell"
    execution = repo.list_executions()[0]
    assert execution.side == "sell"
    assert execution.detail.startswith("[SHORT]")


def test_open_position_maps_direction_to_the_etoro_payload():
    """Il client vero traduce la direzione nel campo `transaction` del payload."""
    from etoro_bot.etoro.client import EtoroClient

    sent = {}

    class _Session:
        def request(self, method, url, params=None, json=None, headers=None, timeout=None):
            sent["body"] = json

            class _Resp:
                status_code = 200
                text = ""

                @staticmethod
                def json():
                    return {"orderId": 77}

            return _Resp()

    client = EtoroClient("k", "u", session=_Session())
    client._wait_for_fill = lambda order_id: {"position_id": 1, "order_id": order_id}
    client.open_position(42, 100.0, "req-1", direction="sell")
    assert sent["body"]["transaction"] == "sell"
    client.open_position(42, 100.0, "req-2")
    assert sent["body"]["transaction"] == "buy"


def test_failed_open_is_recovered_when_the_order_actually_filled(repo):
    """L'apertura solleva ma l'ordine è passato: la posizione va adottata,
    non registrata come fallita — nessuno la chiuderebbe mai."""
    _with_champion(repo)
    client = FakeLiveClient()
    client.fail_open = True
    deps = make_deps(repo, client, llm=open_llm("AAPL", 20.0))

    # il broker conferma il fill per il reference id dell'ordine
    def _lookup(order_id=None, reference_id=None):
        client.lookups.append(reference_id)
        return filled_order(777, price=201.0)

    client.lookup_order = _lookup

    summary = run_live_cycle(deps, market=MARKET, now=NOW)
    assert summary["opened"] == 1
    positions = repo.open_positions()
    assert [(p.etoro_position_id, p.symbol, p.entry_price) for p in positions] == [
        (777, "AAPL", 201.0)
    ]
    assert repo.list_executions()[0].status == "filled"


def test_orphan_position_is_adopted_by_the_reconcile(repo):
    """Ordine dato per fallito, posizione invece aperta sul conto: il ciclo
    successivo la ritrova dal reference id e la adotta nel registry."""
    from etoro_bot.arena.live import reconcile_live_positions
    from etoro_bot.domain import ExecutionResult, ExecutionStatus, Side, order_request_id

    _with_champion(repo)
    client = FakeLiveClient()
    deps = make_deps(repo, client, llm=None)
    run_id = f"live-{NOW:%Y%m%d}"
    repo.create_run(run_id, environment="live")
    request_id = order_request_id(run_id, "AAPL#20260727#3", Side.BUY)
    repo.add_execution(
        run_id,
        ExecutionResult(
            symbol="AAPL", side=Side.BUY, amount_usd=500.0,
            status=ExecutionStatus.FAILED,
            detail=f"errore di rete [ref={request_id}]",
        ),
    )
    client.orders[request_id] = filled_order(888, price=199.0)
    client.positions = [{"positionId": 888, "instrumentID": 1, "isBuy": True}]

    assert reconcile_live_positions(deps, run_id, NOW, market=MARKET) == {"adopted": 1}
    adopted = repo.open_positions()
    assert [(p.etoro_position_id, p.symbol, p.instrument_id) for p in adopted] == [
        (888, "AAPL", 1)
    ]
    # una posizione che il bot non ha mai aperto NON viene toccata
    client.positions.append({"positionId": 999, "instrumentID": 5, "isBuy": True})
    assert reconcile_live_positions(deps, run_id, NOW, market=MARKET) == {"adopted": 0}
    assert {p.etoro_position_id for p in repo.open_positions()} == {888}


def test_request_id_is_stable_across_a_retried_cycle(repo):
    """Stesso ciclo logico ritentato = stessi request id: il broker deduplica."""
    from datetime import timedelta

    _with_champion(repo)
    client = FakeLiveClient()
    deps = make_deps(repo, client, llm=open_llm("AAPL", 20.0))
    settings = {"arena": {"cycle_minutes": 5, "markets": {"usa": {
        "open_utc": "13:30", "close_utc": "20:00"}}}}
    deps.settings.update(settings)

    run_live_cycle(deps, market=MARKET, now=NOW)
    # il ciclo va in crash e viene ritentato 2 minuti dopo: stessa finestra
    run_live_cycle(deps, market=MARKET, now=NOW + timedelta(minutes=2))
    assert client.open_calls[0][2] == client.open_calls[1][2]

    # il ciclo successivo (finestra diversa) usa un id nuovo
    run_live_cycle(deps, market=MARKET, now=NOW + timedelta(minutes=6))
    assert client.open_calls[2][2] != client.open_calls[0][2]


# ---------------------------------------------- liquidazione differita del PnL


def test_close_records_estimate_then_settles_real_pnl_into_the_breaker(repo):
    """Alla chiusura il PnL reale non esiste ancora: il breaker riceve subito
    la stima mark-to-market e poi la correzione col netProfit del broker."""
    from etoro_bot.arena.live import settle_pending_closes

    _with_champion(repo)
    client = FakeLiveClient()
    deps = make_deps(repo, client, llm=lambda **kw: "[]")
    breaker = CircuitBreaker(CircuitBreakerRules(), state_dir=None)
    run_id = f"live-{NOW:%Y%m%d}"
    repo.create_run(run_id, environment="live")
    # AAPL comprata a 300 e quotata 200: -33% su 500 USD ≈ -166.67 di stima
    repo.register_open_position(900, run_id, "AAPL", 1, 500.0, 300.0, NOW)

    run_live_cycle(deps, breaker=breaker, market=MARKET, now=NOW)
    closed = repo.closed_positions()[0]
    assert closed.pnl_settled is False
    assert closed.close_order_id == 9900
    assert closed.realized_pnl_usd == pytest.approx(-166.67, abs=0.01)
    assert breaker.state.daily_pnl_usd == pytest.approx(-166.67, abs=0.01)

    # il broker pubblica il PnL vero: -180 (slippage e commissioni)
    client.history = [{"positionId": "900", "netProfit": -180.0, "closeRate": 198.0}]
    assert settle_pending_closes(deps, breaker=breaker, now=NOW)["settled"] == 1
    settled = repo.closed_positions()[0]
    assert settled.pnl_settled is True
    assert settled.realized_pnl_usd == -180.0
    assert settled.close_price == 198.0
    assert breaker.state.daily_pnl_usd == pytest.approx(-180.0, abs=0.01)
    # già liquidata: una seconda passata non la conta di nuovo
    assert settle_pending_closes(deps, breaker=breaker, now=NOW)["settled"] == 0
    assert breaker.state.daily_pnl_usd == pytest.approx(-180.0, abs=0.01)


def test_survival_breaker_trips_on_estimated_drawdown(repo):
    """Senza la stima il breaker resterebbe cieco fino al giorno dopo: con
    essa il pavimento di sopravvivenza scatta nello stesso ciclo."""
    _with_champion(repo)
    client = FakeLiveClient(credit=100.0)
    deps = make_deps(repo, client, llm=lambda **kw: "[]")
    breaker = CircuitBreaker(CircuitBreakerRules(max_daily_loss_pct=25.0),
                             state_dir=None)
    run_id = f"live-{NOW:%Y%m%d}"
    repo.create_run(run_id, environment="live")
    repo.register_open_position(910, run_id, "AAPL", 1, 900.0, 300.0, NOW)

    run_live_cycle(deps, breaker=breaker, market=MARKET, now=NOW)
    assert breaker.blocks_openings() is True
    assert "drawdown giornaliero" in (breaker.state.reason or "")


def test_entry_price_never_persisted_as_zero(repo):
    """Fill senza prezzo: si ricade sulla ri-lettura dell'ordine e poi sullo
    snapshot — entry_price=0 disattiverebbe stop loss e take profit."""
    _with_champion(repo)
    client = FakeLiveClient()
    client.open_position = lambda instrument_id, amount_usd, request_id, direction="buy": (
        client.open_calls.append((instrument_id, amount_usd, request_id, direction))
        or {"position_id": 950, "execution_price": None}
    )
    client.lookup_order = lambda order_id=None, reference_id=None: {"status": {"id": 1}}
    deps = make_deps(repo, client, llm=open_llm("AAPL", 20.0))

    run_live_cycle(deps, market=MARKET, now=NOW)
    assert [p.entry_price for p in repo.open_positions()] == [200.0]  # prezzo snapshot
