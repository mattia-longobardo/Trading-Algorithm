"""Concorrenza vera (thread) e divergenza broker ↔ registro.

La suite storica era tutta sequenziale: i lock, il kill switch a metà ciclo e
il rate limiter passavano i test anche se non funzionavano sotto carico. Qui
si usano thread veri, perché è così che gira il processo: scheduler, richieste
API e job manuali insistono sugli stessi oggetti.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from etoro_bot.arena.dna import DEFAULT_DNA, clamp_dna
from etoro_bot.arena.engine import ArenaDeps
from etoro_bot.arena.live import run_live_cycle
from etoro_bot.etoro.rate_limiter import RateLimiter
from etoro_bot.safety.kill_switch import engage_kill_switch

NOW = datetime(2026, 7, 27, 15, 0, tzinfo=timezone.utc)

MARKET = {
    "AAPL": {"instrument_id": 1, "price": 200.0, "view": "AAPL 200.0"},
    "MSFT": {"instrument_id": 2, "price": 400.0, "view": "MSFT 400.0"},
    "NVDA": {"instrument_id": 3, "price": 100.0, "view": "NVDA 100.0"},
}


class SlowLiveClient:
    """Client finto che impiega tempo reale ad aprire: serve a far collidere
    davvero due cicli concorrenti invece di simularne la collisione."""

    def __init__(self, open_delay_s: float = 0.05):
        self.open_delay_s = open_delay_s
        self.open_calls: list[tuple] = []
        self.close_calls: list[tuple] = []
        self.next_position_id = 700
        self.concurrent = 0
        self.max_concurrent = 0
        self._lock = threading.Lock()

    def get_portfolio(self):
        return {"positions": [], "credit": 10_000.0}

    def get_trade_history(self, min_date=None, page_size=100):
        return []

    def lookup_order(self, order_id=None, reference_id=None):
        return {"status": {"id": 1}}

    def get_rates(self, instrument_ids):
        return {}

    def open_position(self, instrument_id, amount_usd, request_id, direction="buy"):
        with self._lock:
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            time.sleep(self.open_delay_s)
            self.open_calls.append((instrument_id, amount_usd, request_id, direction))
            self.next_position_id += 1
            return {"position_id": self.next_position_id, "execution_price": 100.0}
        finally:
            with self._lock:
                self.concurrent -= 1

    def close_position(self, position_id, instrument_id, request_id=None):
        self.close_calls.append((position_id, instrument_id))
        return {"order_id": 9000 + position_id, "position_id": position_id}


def _deps(repo, client, llm):
    from etoro_bot.safety.mandate import Mandate

    return ArenaDeps(
        repo=repo, client=client,
        settings={"arena": {"starting_capital_usd": 10_000}},
        llm=llm, model="test-model", max_tokens=512,
        mandate=Mandate.unlimited(),
    )


def _champion(repo):
    agent_id = repo.create_agent(
        "G1-Alfa", 1, clamp_dna(DEFAULT_DNA), "memoria", "2026-07", 10_000.0
    )
    repo.retire_agent(agent_id)
    repo.set_champion(agent_id)
    return agent_id


def _open_llm(*symbols):
    def llm(system_blocks, user_prompt, model, max_tokens):
        return json.dumps([
            {"action": "open", "symbol": s, "direction": "long",
             "size_pct": 10.0, "reason": "live"}
            for s in symbols
        ])
    return llm


def test_two_live_cycles_never_overlap_under_real_threads(repo, monkeypatch):
    """`_live_lock` è l'invariante mono-ciclo: due cicli concorrenti non devono
    mai stare dentro la fase di apertura nello stesso momento, altrimenti lo
    stesso simbolo verrebbe comprato due volte."""
    monkeypatch.delenv("ETORO_BOT_KILL", raising=False)
    _champion(repo)
    client = SlowLiveClient()
    deps = _deps(repo, client, _open_llm("AAPL", "MSFT"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(run_live_cycle, deps, None, MARKET, NOW) for _ in range(2)
        ]
        summaries = [f.result() for f in futures]

    assert client.max_concurrent <= 1  # mai due aperture in volo insieme
    # uno dei due cicli trova il lock occupato e si tira indietro senza operare
    assert any(s.get("skipped") == "cycle_in_progress" for s in summaries)


def test_rate_limiter_never_exceeds_the_budget_under_real_threads():
    """Il limitatore è condiviso fra thread: la finestra deve reggere l'accesso
    concorrente, non solo quello sequenziale dei test storici."""
    limiter = RateLimiter(budgets={"default": (10, 60.0)}, safety_factor=1.0)
    concessi: list[float] = []
    lock = threading.Lock()
    barriera = threading.Barrier(8)

    def prendi() -> None:
        barriera.wait()  # partenza simultanea: massima probabilità di collisione
        limiter.acquire()
        with lock:
            concessi.append(time.monotonic())

    with ThreadPoolExecutor(max_workers=8) as pool:
        for _ in range(8):
            pool.submit(prendi)

    assert len(concessi) == 8  # nessuno perso, nessun deadlock
    # 8 slot su 10 disponibili: nessuna attesa, ma soprattutto nessun doppio
    # conteggio dello stesso slot (che porterebbe a superare il budget vero)
    assert len(limiter._events["default"]) == 8


def test_kill_switch_engaged_mid_cycle_stops_the_remaining_orders(
    repo, monkeypatch, tmp_path
):
    """Il kill switch è un freno d'emergenza: deve fermare gli ordini ANCORA da
    inviare, non solo impedire l'inizio del ciclo successivo."""
    monkeypatch.setenv("KILL_SWITCH_DIR", str(tmp_path))
    monkeypatch.delenv("ETORO_BOT_KILL", raising=False)
    _champion(repo)

    client = SlowLiveClient(open_delay_s=0.0)
    original = client.open_position

    def open_and_pull_the_plug(instrument_id, amount_usd, request_id, direction="buy"):
        result = original(instrument_id, amount_usd, request_id, direction)
        engage_kill_switch()  # emergenza dichiarata a metà ciclo
        return result

    client.open_position = open_and_pull_the_plug
    deps = _deps(repo, client, _open_llm("AAPL", "MSFT", "NVDA"))

    summary = run_live_cycle(deps, market=MARKET, now=NOW)

    assert len(client.open_calls) == 1  # il primo passa, gli altri no
    assert summary["opened"] == 1
    assert summary["blocked"] == 2
