"""API REST del bot (FastAPI) — contratto §12.3.

L'identità arriva dagli header del proxy Next (`X-Trading-User-Id`), che a sua
volta la ricava dalla sessione Authentik. Quegli header sono fidati solo se la
richiesta viene davvero dal proxy: ogni richiesta (tranne /health) deve portare
`X-Trading-Internal-Token` uguale a `TRADING_INTERNAL_TOKEN`, altrimenti è 401 —
senza, chiunque raggiunga la rete interna potrebbe comandare denaro reale.
Il token è **obbligatorio**: se manca, il processo rifiuta l'avvio, a meno di
`TRADING_DEV_MODE=1` (sviluppo in locale, mai in produzione).

L'identità "system" è riservata alle chiamate interne dello scheduler, che non
passano da FastAPI: via HTTP quel valore è rifiutato con 403.

Le chiavi API non sono mai esposte (solo configured sì/no).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from etoro_bot.api.dependencies import (
    UserIdentity,
    arena_deps,
    full_settings,
    get_breaker,
    get_repo,
    kb,
    mark_fetch,
    set_scheduler_running,
    system_etoro_client,
    system_llm,
)
from etoro_bot.api.routers import (
    arena,
    backtest,
    journal,
    knowledge,
    live,
    portfolio,
    safety,
    settings,
    status,
)
from etoro_bot.knowledge.ingest import MAX_UPLOAD_BYTES

# Senza handler sul root gli INFO dell'arena sarebbero invisibili nei log del
# container (uvicorn configura solo i propri logger). No-op se già configurato.
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

log = logging.getLogger("etoro_bot.api")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _require_internal_token_configured()
    _start_scheduler()
    yield


app = FastAPI(title="Trading Bot API", version="3.0.0", lifespan=_lifespan)

INTERNAL_TOKEN_HEADER = "x-trading-internal-token"
# Rotte raggiungibili senza token: solo la sonda di salute del container.
_TOKEN_EXEMPT_PATHS = frozenset({"/health"})


def internal_token() -> str:
    return os.environ.get("TRADING_INTERNAL_TOKEN", "").strip()


def _require_internal_token_configured() -> None:
    """Senza segreto condiviso gli header di identità sono falsificabili: il
    backend non parte. `TRADING_DEV_MODE=1` è l'unica deroga, per il locale."""
    if internal_token():
        return
    if os.environ.get("TRADING_DEV_MODE") == "1":
        log.warning(
            "TRADING_DEV_MODE=1: token interno disattivato. Solo sviluppo locale."
        )
        return
    raise RuntimeError(
        "TRADING_INTERNAL_TOKEN non configurato: avvio rifiutato. Genera un "
        "segreto (openssl rand -base64 32) oppure imposta TRADING_DEV_MODE=1 "
        "per lo sviluppo in locale."
    )


class _BodyTooLarge(HTTPException):
    """413 sollevata *mentre* si legge il corpo, non dopo averlo bufferizzato.

    Deve essere una `HTTPException`: quando il parsing del body fallisce
    FastAPI riscrive qualunque altra eccezione in un 400 generico, e solo le
    `HTTPException` vengono rilanciate intatte.
    """

    def __init__(self, max_bytes: int) -> None:
        super().__init__(
            413, f"corpo della richiesta oltre il limite di {max_bytes} byte"
        )


class LimitRequestBody:
    """Rifiuta i corpi oltre `max_bytes` PRIMA che vengano bufferizzati.

    Un controllo dentro l'endpoint arriverebbe troppo tardi: FastAPI risolve
    `UploadFile` (quindi legge tutto il multipart) prima di eseguire il corpo
    della funzione, e `await file.read()` porta l'upload in RAM. Un POST da
    1 GB basterebbe a far fuori il container da 2g, che ospita anche scheduler
    e circuit breaker.

    Qui si guarda prima il `Content-Length` dichiarato — se c'è, la richiesta
    muore senza leggere un byte — e comunque si contano i chunk ASGI mano a
    mano, così anche un `Transfer-Encoding: chunked` senza lunghezza dichiarata
    viene interrotto appena supera la soglia.
    """

    def __init__(self, app, max_bytes: int = MAX_UPLOAD_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope["headers"]:
            if name != b"content-length":
                continue
            try:
                declared = int(value)
            except ValueError:
                break  # header malformato: decide il conteggio a chunk
            if declared > self.max_bytes:
                await self._reject(send)
                return
            break

        received = 0
        started = False

        async def counted_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    log.warning(
                        "corpo oltre %d byte: lettura interrotta", self.max_bytes
                    )
                    raise _BodyTooLarge(self.max_bytes)
            return message

        async def watched_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, watched_send)
        except _BodyTooLarge:
            # Rete di sicurezza: se nessuno a valle ha tradotto la 413 in
            # risposta (rotta non FastAPI), la scriviamo qui.
            if not started:
                await self._reject(send)

    async def _reject(self, send) -> None:
        body = json.dumps(
            {"detail": f"corpo della richiesta oltre il limite di {self.max_bytes} byte"}
        ).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})


# Registrato prima del controllo del token: l'ultimo middleware aggiunto sta
# più all'esterno, quindi una richiesta senza token viene respinta per prima.
app.add_middleware(LimitRequestBody)


@app.middleware("http")
async def _require_internal_token(request, call_next):
    """Il backend accetta solo richieste che arrivano dal proxy autenticato."""
    expected = internal_token()
    if expected and request.url.path not in _TOKEN_EXEMPT_PATHS:
        provided = request.headers.get(INTERNAL_TOKEN_HEADER, "")
        if not hmac.compare_digest(provided, expected):
            log.warning("richiesta rifiutata senza token interno: %s", request.url.path)
            return JSONResponse({"detail": "Non autorizzato"}, status_code=401)
    return await call_next(request)


# Connessione che detiene l'advisory lock dello scheduler: il lock è di
# sessione, quindi questo riferimento deve vivere quanto il processo.
_scheduler_lock: Any = None
# Uno scheduler morto è invisibile: /status prometterebbe un next_cycle_at che
# non arriva mai. Questo flag lo dice a chi guarda.
def _start_scheduler() -> None:
    global _scheduler_lock

    if os.environ.get("DISABLE_SCHEDULER") == "1":
        return
    try:
        from etoro_bot.db.repo import try_scheduler_lock
        from etoro_bot.services.scheduler import start_scheduler

        _scheduler_lock = try_scheduler_lock()
        if _scheduler_lock is None:
            log.critical(
                "advisory lock non acquisito: un'altra istanza dello scheduler è già "
                "attiva su questo database. Scheduler DISATTIVATO in questo processo "
                "(API attiva). Il bot deve girare in un solo processo: mai "
                "--workers > 1, mai due repliche."
            )
            return

        def _cycle_job() -> None:
            from etoro_bot.arena.engine import run_training_cycle
            from etoro_bot.arena.live import run_live_cycle
            from etoro_bot.arena.market import build_snapshot
            from etoro_bot.services.app_settings import arena_state

            deps = arena_deps()
            market = build_snapshot(deps.client, deps.settings) if deps.client else {}
            log.info("arena: ciclo (%d titoli)", len(market))
            log.info("arena: training %s", run_training_cycle(deps, market=market))
            if arena_state(get_repo())["live_enabled"] and deps.client is not None:
                log.info("arena: live %s",
                         run_live_cycle(deps, breaker=get_breaker(), market=market))

        def _eod_job(market_name: str, final: bool) -> None:
            from etoro_bot.arena.engine import close_market_positions, run_eod
            from etoro_bot.arena.live import (
                close_live_market_positions,
                run_live_eod,
            )
            from etoro_bot.arena.market import build_snapshot
            from etoro_bot.services.app_settings import arena_state

            deps = arena_deps()
            market = (
                build_snapshot(deps.client, deps.settings, only_open=False)
                if deps.client else {}
            )
            live_on = arena_state(get_repo())["live_enabled"] and deps.client is not None
            log.info(
                "arena: chiusura sessione %s",
                close_market_positions(deps, market_name, market=market),
            )
            if live_on:
                log.info(
                    "arena: chiusura sessione live %s",
                    close_live_market_positions(
                        deps, market_name, breaker=get_breaker(), market=market
                    ),
                )
            if final:  # ultima campanella del giorno: riflessione + snapshot
                log.info("arena: EOD training %s", run_eod(deps, market=market))
                if live_on:
                    log.info("arena: EOD live %s",
                             run_live_eod(deps, breaker=get_breaker(), market=market))

        def _evolve_job() -> None:
            from etoro_bot.arena.evolution import maybe_evolve

            deps = arena_deps()
            outcome = maybe_evolve(deps)  # bootstrap incluso, sotto cycle_lock
            if outcome:
                log.info("arena: evoluzione %s", outcome)

        def _news_job() -> None:
            from etoro_bot.knowledge.pipeline import run_news_pipeline

            run_news_pipeline(
                kb=kb(),
                settings=full_settings(),
                client=system_etoro_client(),
                llm=system_llm(),
            )
            mark_fetch(UserIdentity("system"))
            # Kronos: inferenza giornaliera fuori dal percorso critico del
            # ciclo; no-op se disabilitato o senza l'extra torch installato.
            from etoro_bot.forecast.kronos import refresh_forecasts

            refresh_forecasts(system_etoro_client(), full_settings())

        start_scheduler(
            get_settings=full_settings,
            cycle_job=_cycle_job,
            eod_job=_eod_job,
            evolve_job=_evolve_job,
            news_job=_news_job,
        )
        set_scheduler_running(True)
    except Exception:
        log.exception("scheduler non avviato")


# I router stanno in api/routers/, uno per area: qui si montano e basta.
for _router in (status, journal, portfolio, backtest, knowledge, settings, arena, live,
                safety):
    app.include_router(_router.router)
