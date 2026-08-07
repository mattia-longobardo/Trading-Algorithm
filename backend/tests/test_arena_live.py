"""Test del trading live guidato dal campione: freni operativi e esecuzione."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

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
        self.close_request_ids = []
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

    def close_position(self, position_id, instrument_id, request_id=None):
        self.close_calls.append((position_id, instrument_id))
        self.close_request_ids.append(request_id)
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
    from etoro_bot.arena import live

    monkeypatch.setenv("KILL_SWITCH_DIR", str(tmp_path))
    monkeypatch.delenv("ETORO_BOT_KILL", raising=False)
    live._missing_from_portfolio.clear()  # contatore di modulo: mai fra i test


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


def test_live_cycle_protects_positions_without_llm(repo):
    """Chiave del modello scaduta = nessuna decisione, MAI posizioni scoperte:
    reconcile, liquidazione e sweep stop loss / take profit girano lo stesso."""
    _with_champion(repo)
    client = FakeLiveClient()
    deps = make_deps(repo, client, llm=None)
    run_id = f"live-{NOW:%Y%m%d}"
    repo.create_run(run_id, environment="live")
    repo.register_open_position(900, run_id, "AAPL", 1, 500.0, 300.0, NOW)

    summary = run_live_cycle(deps, market=MARKET, now=NOW)  # AAPL a 200: -33%

    assert summary["skipped"] == "no_llm"
    assert client.close_calls == [(900, 1)]  # stop loss eseguito comunque
    assert repo.open_positions() == []
    assert client.open_calls == []  # senza LLM non si apre nulla


def test_live_cycle_skips_when_the_broker_publishes_no_credit(repo):
    """`credit` assente non è liquidità zero: il ciclo si ferma prima di
    decidere invece di dimensionare gli ordini su un capitale inventato."""
    _with_champion(repo)
    client = FakeLiveClient()
    client.get_portfolio = lambda: {"positions": []}  # nessun campo credit
    deps = make_deps(repo, client, llm=open_llm("AAPL", 20.0))

    summary = run_live_cycle(deps, market=MARKET, now=NOW)

    assert summary["skipped"] == "no_cash"
    assert summary["opened"] == 0
    assert client.open_calls == []
    assert repo.open_positions() == []


def test_live_cycle_skips_when_credit_vanishes_between_reads(repo):
    """La cassa viene riletta dopo le chiusure automatiche: se il broker smette
    di pubblicarla il ciclo salta, non solleva a metà lasciando il run aperto."""
    _with_champion(repo)
    client = FakeLiveClient()
    letture = []

    def portfolio_intermittente():
        letture.append(1)
        if len(letture) == 1:
            return {"positions": [], "credit": 10_000.0}
        return {"positions": []}  # credit sparito dalla seconda lettura

    client.get_portfolio = portfolio_intermittente
    deps = make_deps(repo, client, llm=open_llm("AAPL", 20.0))

    summary = run_live_cycle(deps, market=MARKET, now=NOW)

    assert summary["skipped"] == "no_cash"
    assert client.open_calls == []


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


def test_short_position_is_registered_with_its_direction(repo):
    """La direzione va a registro all'apertura: nessuno dovrà riderivarla."""
    _with_champion(repo)
    client = FakeLiveClient()
    deps = make_deps(repo, client, llm=open_llm("AAPL", 20.0, direction="short"))
    run_live_cycle(deps, market=MARKET, now=NOW)
    assert [p.direction for p in repo.open_positions()] == ["short"]


def test_short_is_not_closed_upside_down_when_the_account_hides_the_direction(repo):
    """Conto che non dichiara la direzione delle posizioni: decide il registro.

    Prima la direzione veniva riletta dal portafoglio e in sua assenza
    degradava a long: su questo short in guadagno del 33% sarebbe scattato uno
    "stop loss" mentre la posizione era in profitto.
    """
    _with_champion(repo)
    client = FakeLiveClient()          # get_portfolio non espone alcun isBuy
    deps = make_deps(repo, client, llm=lambda **kw: "[]")
    run_id = f"live-{NOW:%Y%m%d}"
    repo.create_run(run_id, environment="live")
    # short aperto a 300 su AAPL quotata 200 → +33% a favore
    repo.register_open_position(920, run_id, "AAPL", 1, 500.0, 300.0, NOW,
                                direction="short")

    run_live_cycle(deps, market=MARKET, now=NOW)

    assert client.close_calls == [(920, 1)]
    closed = repo.closed_positions()[0]
    assert "take profit" in closed.close_reason
    assert closed.realized_pnl_usd == pytest.approx(166.67, abs=0.01)


def test_sweep_is_skipped_when_registry_and_account_disagree(repo):
    """Registro e conto discordi: la direzione non è nota e la chiusura
    automatica salta. Presumere long chiuderebbe al contrario."""
    _with_champion(repo)
    client = FakeLiveClient()
    client.positions = [{"positionId": 930, "instrumentID": 1, "isBuy": True}]
    deps = make_deps(repo, client, llm=lambda **kw: "[]")
    run_id = f"live-{NOW:%Y%m%d}"
    repo.create_run(run_id, environment="live")
    repo.register_open_position(930, run_id, "AAPL", 1, 500.0, 300.0, NOW,
                                direction="short")

    run_live_cycle(deps, market=MARKET, now=NOW)

    assert client.close_calls == []
    assert [p.etoro_position_id for p in repo.open_positions()] == [930]


def test_llm_close_on_contradicted_direction_records_no_signed_estimate(repo):
    """Chiusura chiesta dal campione su direzione inattendibile: si chiude
    comunque (una chiusura non si nega mai) ma la stima di PnL non viene
    firmata col registro, né finisce nel breaker: si aspetta il broker."""
    _with_champion(repo)
    client = FakeLiveClient()
    client.positions = [{"positionId": 950, "instrumentID": 1, "isBuy": True}]

    def llm(system_blocks, user_prompt, model, max_tokens):
        return json.dumps([{"action": "close", "symbol": "AAPL", "reason": "esco"}])

    deps = make_deps(repo, client, llm=llm)
    breaker = CircuitBreaker(CircuitBreakerRules(), state_dir=None)
    run_id = f"live-{NOW:%Y%m%d}"
    repo.create_run(run_id, environment="live")
    # registro: short a 300; conto: isBuy=true. Discordi → direzione ignota
    repo.register_open_position(950, run_id, "AAPL", 1, 500.0, 300.0, NOW,
                                direction="short")

    summary = run_live_cycle(deps, breaker=breaker, market=MARKET, now=NOW)

    assert summary["closed"] == 1 and client.close_calls == [(950, 1)]
    closed = repo.closed_positions()[0]
    assert closed.realized_pnl_usd == 0.0
    assert closed.pnl_settled is False
    assert breaker.state.daily_pnl_usd == 0.0


def test_targeted_close_matches_on_the_registry_even_if_the_account_disagrees(repo):
    """Chiusura mirata a "short" sullo stesso setup contraddittorio: l'ordine
    parte per position_id, quindi si seleziona sul registro — rifiutarla
    lascerebbe la posizione ingestibile. La stima di PnL resta non firmata."""
    _with_champion(repo)
    client = FakeLiveClient()
    client.positions = [{"positionId": 960, "instrumentID": 1, "isBuy": True}]

    def llm(system_blocks, user_prompt, model, max_tokens):
        return json.dumps([{"action": "close", "symbol": "AAPL",
                            "direction": "short", "reason": "esco"}])

    deps = make_deps(repo, client, llm=llm)
    breaker = CircuitBreaker(CircuitBreakerRules(), state_dir=None)
    run_id = f"live-{NOW:%Y%m%d}"
    repo.create_run(run_id, environment="live")
    repo.register_open_position(960, run_id, "AAPL", 1, 500.0, 300.0, NOW,
                                direction="short")

    summary = run_live_cycle(deps, breaker=breaker, market=MARKET, now=NOW)

    assert summary["closed"] == 1 and client.close_calls == [(960, 1)]
    closed = repo.closed_positions()[0]
    assert closed.realized_pnl_usd == 0.0
    assert breaker.state.daily_pnl_usd == 0.0


def test_position_change_pct_takes_its_sign_from_the_persisted_direction(repo):
    from etoro_bot.arena.live import position_change_pct, registry_direction

    run_id = f"live-{NOW:%Y%m%d}"
    repo.create_run(run_id, environment="live")
    repo.register_open_position(940, run_id, "AAPL", 1, 500.0, 300.0, NOW,
                                direction="short")
    pos = repo.get_open_position(940)
    assert registry_direction(pos) == "short"
    # prezzo sceso da 300 a 200: per uno short è un +33%, non un -33%
    assert position_change_pct(pos, 200.0, registry_direction(pos)) == pytest.approx(
        33.33, abs=0.01
    )


def test_adopted_direction_prefers_the_account_then_the_journal():
    """Adozione di una posizione orfana: la direzione non si inventa."""
    from etoro_bot.arena.live import _adopted_direction

    sell = SimpleNamespace(side="sell")
    assert _adopted_direction({"isBuy": False}, sell) == "short"
    assert _adopted_direction({"isBuy": True}, sell) == "long"   # il conto vince
    assert _adopted_direction(None, sell) == "short"             # resta il giornale
    assert _adopted_direction({}, SimpleNamespace(side="buy")) == "long"


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


def _fails_once(repo, method, message):
    """Sostituto di un metodo del repo che solleva la prima volta e poi funziona.

    Serve a simulare il guasto puntuale (DB caduto, processo ucciso) senza
    rompere anche il reconcile che deve rimediare subito dopo.
    """
    real = getattr(repo, method)
    broken = []

    def once(*args, **kwargs):
        if not broken:
            broken.append(True)
            raise RuntimeError(message)
        return real(*args, **kwargs)

    return once


def test_broker_fill_survives_a_db_failure_at_registration(repo, monkeypatch):
    """Broker OK, DB KO: l'ordine è passato ma register_open_position solleva.

    Senza il write-ahead non resterebbe NIENTE a giornale: posizione reale
    orfana, invisibile, senza stop loss, e il ciclo dopo riaprirebbe lo stesso
    simbolo. Con il write-ahead la riga porta reference id e positionId, e il
    reconcile successivo adotta la posizione.
    """
    from etoro_bot.arena.live import reconcile_live_positions

    _with_champion(repo)
    client = FakeLiveClient()
    deps = make_deps(repo, client, llm=open_llm("AAPL", 20.0))
    monkeypatch.setattr(
        repo, "register_open_position",
        _fails_once(repo, "register_open_position", "connessione al DB persa"),
    )

    summary = run_live_cycle(deps, market=MARKET, now=NOW)
    assert summary["opened"] == 0
    position_id = client.next_position_id
    assert client.open_calls  # l'ordine è REALMENTE partito al broker
    assert repo.open_positions() == []  # e il registry non lo sa

    execution = repo.list_executions()[0]
    assert execution.status == "filled"
    assert execution.etoro_position_id == position_id
    assert f"ref={client.open_calls[0][2]}" in execution.detail

    # ciclo successivo: la posizione è sul conto e il giornale la attribuisce
    # al bot → adottata.
    client.positions = [{"positionId": position_id, "instrumentID": 1, "isBuy": True}]
    run_id = f"live-{NOW:%Y%m%d}"
    assert reconcile_live_positions(deps, run_id, NOW, market=MARKET)["adopted"] == 1
    assert [p.etoro_position_id for p in repo.open_positions()] == [position_id]


def test_crash_between_fill_and_registration_leaves_an_adoptable_pending(repo, monkeypatch):
    """Processo ucciso fra il fill e la registrazione: resta un pending col
    reference id, e alla ripartenza il reconcile lo risolve in posizione."""
    from etoro_bot.arena.live import reconcile_live_positions

    _with_champion(repo)
    client = FakeLiveClient()
    deps = make_deps(repo, client, llm=open_llm("AAPL", 20.0))
    monkeypatch.setattr(
        repo, "update_execution",
        _fails_once(repo, "update_execution", "processo terminato dopo il fill"),
    )

    run_live_cycle(deps, market=MARKET, now=NOW)
    request_id = client.open_calls[0][2]
    pending = repo.list_executions()[0]
    assert pending.status == "pending"
    assert f"ref={request_id}" in pending.detail
    assert repo.open_positions() == []

    # il processo riparte: il broker conferma che quel reference id è passato
    client.orders[request_id] = filled_order(654, price=201.0)
    client.positions = [{"positionId": 654, "instrumentID": 1, "isBuy": True}]
    run_id = f"live-{NOW:%Y%m%d}"
    assert reconcile_live_positions(deps, run_id, NOW, market=MARKET)["adopted"] == 1
    assert [(p.etoro_position_id, p.entry_price) for p in repo.open_positions()] == [
        (654, 201.0)
    ]
    assert repo.list_executions()[0].status == "filled"


def test_pending_never_reaching_the_broker_becomes_failed(repo):
    """Ordine mai partito: il pending non resta appeso per sempre in UI."""
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
            status=ExecutionStatus.PENDING, detail=f"apertura live [ref={request_id}]",
        ),
    )
    assert reconcile_live_positions(deps, run_id, NOW, market=MARKET)["adopted"] == 0
    assert repo.list_executions()[0].status == "failed"


def test_fill_without_position_id_stays_pending(repo):
    """Fill senza positionId: la riga NON può diventare filled senza id, o
    uscirebbe dal radar del reconcile (nessuna delle due sorgenti la vede)."""
    from etoro_bot.arena.live import reconcile_live_positions

    _with_champion(repo)
    client = FakeLiveClient()
    deps = make_deps(repo, client, llm=open_llm("AAPL", 20.0))

    def open_without_id(instrument_id, amount_usd, request_id, direction="buy"):
        client.open_calls.append((instrument_id, amount_usd, request_id, direction))
        return {"position_id": None, "execution_price": 100.0}

    client.open_position = open_without_id
    run_live_cycle(deps, market=MARKET, now=NOW)
    request_id = client.open_calls[0][2]
    row = repo.list_executions()[0]
    assert (row.status, row.etoro_position_id) == ("pending", None)
    assert f"ref={request_id}" in row.detail

    # il broker pubblica il positionId di quell'ordine: adottata
    client.orders[request_id] = filled_order(321, price=100.0)
    client.positions = [{"positionId": 321, "instrumentID": 1, "isBuy": True}]
    run_id = f"live-{NOW:%Y%m%d}"
    assert reconcile_live_positions(deps, run_id, NOW, market=MARKET)["adopted"] == 1
    assert [p.etoro_position_id for p in repo.open_positions()] == [321]


def _pending_order(repo, client, symbol="AAPL"):
    """Piazza a giornale un ordine pending col ref= e ritorna il reference id."""
    from etoro_bot.domain import ExecutionResult, ExecutionStatus, Side, order_request_id

    run_id = f"live-{NOW:%Y%m%d}"
    if repo.get_run(run_id) is None:
        repo.create_run(run_id, environment="live")
    request_id = order_request_id(run_id, f"{symbol}#20260727#3", Side.BUY)
    repo.add_execution(
        run_id,
        ExecutionResult(
            symbol=symbol, side=Side.BUY, amount_usd=500.0,
            status=ExecutionStatus.PENDING, detail=f"apertura live [ref={request_id}]",
        ),
    )
    return request_id


def test_cycle_opens_again_when_the_broker_denies_the_previous_order(repo):
    """Il broker SMENTISCE l'ordine precedente (rifiuto ordinario: fondi,
    taglia minima, 429): esito noto, nessuna posizione a mercato, il simbolo
    non va bloccato per 24h."""
    _with_champion(repo)
    client = FakeLiveClient()  # lookup_order risponde "non eseguito"
    deps = make_deps(repo, client, llm=open_llm("AAPL", 20.0))
    _pending_order(repo, client)

    summary = run_live_cycle(deps, market=MARKET, now=NOW)
    assert summary["opened"] == 1 and summary["blocked"] == 0
    assert len(client.open_calls) == 1
    assert [p.symbol for p in repo.open_positions()] == ["AAPL"]


def test_cycle_does_not_open_a_symbol_with_an_unresolved_order(repo):
    """Il broker NON risponde (lookup che solleva): l'ordine precedente può
    essere a mercato, quindi il ciclo non ne apre un altro sullo stesso
    simbolo. allow_pyramiding è attivo di default, quindi held_symbols non
    fermerebbe il doppione — e la posizione non compare nel registry."""
    _with_champion(repo)
    client = FakeLiveClient()
    deps = make_deps(repo, client, llm=open_llm("AAPL", 20.0))
    _pending_order(repo, client)

    def _unreachable(order_id=None, reference_id=None):
        raise RuntimeError("broker irraggiungibile")

    client.lookup_order = _unreachable

    summary = run_live_cycle(deps, market=MARKET, now=NOW)
    assert summary["opened"] == 0 and summary["blocked"] == 1
    assert client.open_calls == []  # nessun ordine reale è partito
    # blip di rete: la riga NON viene declassata, il blocco regge
    assert repo.list_executions()[0].status == "pending"


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

    assert reconcile_live_positions(deps, run_id, NOW, market=MARKET)["adopted"] == 1
    adopted = repo.open_positions()
    assert [(p.etoro_position_id, p.symbol, p.instrument_id) for p in adopted] == [
        (888, "AAPL", 1)
    ]
    # una posizione che il bot non ha mai aperto NON viene toccata
    client.positions.append({"positionId": 999, "instrumentID": 5, "isBuy": True})
    assert reconcile_live_positions(deps, run_id, NOW, market=MARKET)["adopted"] == 0
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


# ----------------------------------- chiusure affidabili e reconcile bidirezionale


def _open_at(repo, position_id, opened_at, symbol="AAPL", instrument_id=1,
             amount=500.0, entry=300.0):
    run_id = f"live-{NOW:%Y%m%d}"
    if repo.get_run(run_id) is None:
        repo.create_run(run_id, environment="live")
    repo.register_open_position(
        position_id, run_id, symbol, instrument_id, amount, entry, opened_at
    )
    return run_id


def test_broker_closes_but_the_db_write_fails_then_the_reconcile_finishes_it(repo, monkeypatch):
    """Ordine di chiusura ESEGUITO, scrittura a registro KO.

    Senza il fix la posizione resterebbe "aperta" a registro per sempre e ogni
    ciclo rimanderebbe un ordine di chiusura al broker. Con il fix: niente
    breaker sulla scrittura mancata, e il reconcile successivo la chiude col
    netProfit del broker senza inviare un secondo ordine.
    """
    from datetime import timedelta

    _with_champion(repo)
    client = FakeLiveClient()
    client.positions = [{"positionId": 900, "instrumentID": 1, "isBuy": True}]
    deps = make_deps(repo, client, llm=lambda **kw: "[]")
    breaker = CircuitBreaker(CircuitBreakerRules(), state_dir=None)
    _open_at(repo, 900, NOW - timedelta(hours=1))

    attempts = []

    def db_down(*a, **kw):
        attempts.append(1)
        raise RuntimeError("connessione al DB persa")

    monkeypatch.setattr(repo, "close_position", db_down)
    run_live_cycle(deps, breaker=breaker, market=MARKET, now=NOW)  # AAPL -33% → stop loss

    assert len(attempts) == 2                        # la sola scrittura è ritentata
    assert client.close_calls == [(900, 1)]          # l'ordine è partito davvero
    # chiave di idempotenza DETERMINISTICA (un uuid4 passerebbe un "non vuoto")
    from etoro_bot.arena.live import _slot_key
    from etoro_bot.domain import Side, order_request_id

    run_id = f"live-{NOW:%Y%m%d}"
    assert client.close_request_ids == [
        order_request_id(run_id, f"close#900#{_slot_key(deps, NOW)}", Side.SELL)
    ]
    assert [p.etoro_position_id for p in repo.open_positions()] == [900]
    assert breaker.state.daily_pnl_usd == 0.0        # mai contata due volte

    # ciclo successivo: il DB è tornato e il conto non ha più quella posizione
    monkeypatch.undo()
    client.positions = [{"positionId": 999, "instrumentID": 5, "isBuy": True}]
    client.history = [{"positionId": 900, "netProfit": -160.0, "closeRate": 200.0}]

    summary = run_live_cycle(deps, breaker=breaker, market=MARKET, now=NOW)

    assert summary["closed_externally"] == 1
    assert client.close_calls == [(900, 1)]          # nessun secondo ordine: no retry infinito
    assert repo.open_positions() == []
    closed = repo.closed_positions()[0]
    assert closed.realized_pnl_usd == -160.0 and closed.pnl_settled is True
    assert breaker.state.daily_pnl_usd == -160.0


def test_reconcile_adopts_a_close_made_outside_the_bot(repo):
    """Chiusura manuale su eToro o margin call: la posizione sparisce dal conto
    ma a registro resterebbe aperta, gonfiando exposure, equity e sizing."""
    from datetime import timedelta

    from etoro_bot.arena.live import reconcile_live_positions

    client = FakeLiveClient()
    client.positions = [{"positionId": 999, "instrumentID": 5, "isBuy": True}]
    client.history = [{"positionId": 901, "netProfit": -75.5, "closeRate": 210.0}]
    deps = make_deps(repo, client, llm=None)
    run_id = _open_at(repo, 901, NOW - timedelta(hours=2))
    breaker = CircuitBreaker(CircuitBreakerRules(), state_dir=None)

    out = reconcile_live_positions(deps, run_id, NOW, market=MARKET, breaker=breaker)

    assert out["closed_externally"] == 1
    assert repo.open_positions() == []
    closed = repo.closed_positions()[0]
    assert (closed.realized_pnl_usd, closed.close_price) == (-75.5, 210.0)
    assert closed.pnl_settled is True
    assert "portafoglio" in closed.close_reason
    assert breaker.state.daily_pnl_usd == -75.5


def test_reconcile_needs_positive_proof_before_declaring_a_position_closed(repo):
    """Senza riga in trade history una sola assenza NON basta: una risposta
    parziale del broker chiuderebbe a registro una posizione ancora viva, che
    nessun reconcile riadotterebbe più (known_position_ids include le chiuse).
    Serve una seconda assenza consecutiva; una riapparizione azzera il conto.

    Chiusa la partita, il PnL non ancora pubblicato resta in sospeso e la
    liquidazione lo completa.
    """
    from datetime import timedelta

    from etoro_bot.arena.live import reconcile_live_positions

    client = FakeLiveClient()
    other = {"positionId": 999, "instrumentID": 5, "isBuy": True}
    mine = {"positionId": 902, "instrumentID": 1, "isBuy": True}
    client.positions = [other]
    deps = make_deps(repo, client, llm=None)
    run_id = _open_at(repo, 902, NOW - timedelta(hours=2))

    # 1ª assenza, nessuna prova: non si tocca
    assert reconcile_live_positions(deps, run_id, NOW)["closed_externally"] == 0
    assert [p.etoro_position_id for p in repo.open_positions()] == [902]

    # la posizione riappare (la lettura di prima era parziale): conto azzerato
    client.positions = [other, mine]
    assert reconcile_live_positions(deps, run_id, NOW)["closed_externally"] == 0
    client.positions = [other]
    assert reconcile_live_positions(deps, run_id, NOW)["closed_externally"] == 0
    assert [p.etoro_position_id for p in repo.open_positions()] == [902]

    # seconda assenza CONSECUTIVA: ora è chiusa
    assert reconcile_live_positions(deps, run_id, NOW)["closed_externally"] == 1
    closed = repo.closed_positions()[0]
    assert closed.realized_pnl_usd == 0.0 and closed.pnl_settled is False

    client.history = [{"positionId": 902, "netProfit": 12.0}]
    from etoro_bot.arena.live import settle_pending_closes

    assert settle_pending_closes(deps, now=NOW)["settled"] == 1
    assert repo.closed_positions()[0].realized_pnl_usd == 12.0


def test_reconcile_never_closes_on_an_unreadable_or_empty_portfolio(repo):
    """Lettura del portafoglio fallita o vuota: astenersi. Chiudere l'intero
    registry per un errore di rete sarebbe il danno peggiore."""
    from datetime import timedelta

    from etoro_bot.arena.live import reconcile_live_positions

    client = FakeLiveClient()
    deps = make_deps(repo, client, llm=None)
    run_id = _open_at(repo, 903, NOW - timedelta(hours=2))

    def _unreachable():
        raise RuntimeError("broker irraggiungibile")

    client.get_portfolio = _unreachable
    assert reconcile_live_positions(deps, run_id, NOW)["closed_externally"] == 0
    assert [p.etoro_position_id for p in repo.open_positions()] == [903]

    # lettura riuscita ma vuota: indistinguibile da una risposta parziale
    client.get_portfolio = lambda: {"positions": [], "credit": 1_000.0}
    assert reconcile_live_positions(deps, run_id, NOW)["closed_externally"] == 0
    assert [p.etoro_position_id for p in repo.open_positions()] == [903]


def test_reconcile_spares_a_position_opened_moments_ago(repo):
    """Finestra di grazia: un fill appena eseguito può non comparire ancora nel
    portafoglio, e chiuderlo a registro lo renderebbe invisibile."""
    from datetime import timedelta

    from etoro_bot.arena.live import reconcile_live_positions

    client = FakeLiveClient()
    client.positions = [{"positionId": 999, "instrumentID": 5, "isBuy": True}]
    deps = make_deps(repo, client, llm=None)
    run_id = _open_at(repo, 904, NOW - timedelta(minutes=1))

    assert reconcile_live_positions(deps, run_id, NOW)["closed_externally"] == 0
    assert [p.etoro_position_id for p in repo.open_positions()] == [904]
