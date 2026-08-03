"""API REST del bot (FastAPI) — contratto §12.3.

L'identità arriva dagli header del proxy Next (`X-Trading-User-Id`), che a sua
volta la ricava dalla sessione Authentik. Quegli header sono fidati solo se la
richiesta viene davvero dal proxy: con `TRADING_INTERNAL_TOKEN` configurato,
ogni richiesta (tranne /health) deve portare `X-Trading-Internal-Token` uguale
al segreto, altrimenti è 401 — senza, chiunque raggiunga la rete interna
potrebbe dichiararsi "system" e comandare denaro reale. Variabile assente =
comportamento storico (sviluppo in locale).

Le chiavi API non sono mai esposte (solo configured sì/no).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from etoro_bot.config import load_breaker_rules, load_settings
from etoro_bot.db.repo import Repository, make_engine, make_session_factory
from etoro_bot.safety.circuit_breaker import CircuitBreaker
from etoro_bot.safety.circuit_breaker import get_breaker as shared_breaker
from etoro_bot.safety.kill_switch import (
    engage_kill_switch,
    kill_switch_active,
    release_kill_switch,
)

# Senza handler sul root gli INFO dell'arena sarebbero invisibili nei log del
# container (uvicorn configura solo i propri logger). No-op se già configurato.
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

log = logging.getLogger("etoro_bot.api")

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _start_scheduler()
    yield


app = FastAPI(title="Trading Bot API", version="3.0.0", lifespan=_lifespan)

INTERNAL_TOKEN_HEADER = "x-trading-internal-token"
# Rotte raggiungibili senza token: solo la sonda di salute del container.
_TOKEN_EXEMPT_PATHS = frozenset({"/health"})


def internal_token() -> str:
    return os.environ.get("TRADING_INTERNAL_TOKEN", "").strip()


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


@dataclass(frozen=True)
class UserIdentity:
    user_id: str
    email: str | None = None
    name: str | None = None


def current_user(
    x_trading_user_id: str = Header("system"),
    x_trading_user_email: str | None = Header(None),
    x_trading_user_name: str | None = Header(None),
) -> UserIdentity:
    return UserIdentity(
        user_id=x_trading_user_id.strip() or "system",
        email=x_trading_user_email,
        name=x_trading_user_name,
    )


def require_owner(identity: UserIdentity, action: str) -> None:
    """Solo il proprietario delle chiavi eToro (o i job di sistema) può mutare.

    Finché nessuno ha configurato le chiavi non c'è un proprietario e il
    controllo è un no-op: è il primo utente che si registra a diventarlo.
    """
    try:
        owner = get_repo().owner_user_id()
    except Exception:
        owner = None
    if owner and identity.user_id not in ("system", owner):
        raise HTTPException(403, f"solo il proprietario può {action}")


def _user_keys(identity: UserIdentity):
    from etoro_bot.services.user_credentials import get_user_keys

    return get_user_keys(get_repo(), identity.user_id)


def _arena_deps():
    """ArenaDeps di sistema: chiavi del proprietario, settings completi."""
    from etoro_bot.arena.engine import ArenaDeps

    settings = _full_settings()
    llm_cfg = settings.get("llm") or {}
    return ArenaDeps(
        repo=get_repo(),
        client=_system_etoro_client(),
        settings=settings,
        llm=_system_llm(),
        model=str(llm_cfg.get("model", "gpt-5.6-terra")),
        max_tokens=int(llm_cfg.get("max_tokens", 2048)),
    )


def _start_scheduler() -> None:
    if os.environ.get("DISABLE_SCHEDULER") == "1":
        return
    try:
        from etoro_bot.services.scheduler import start_scheduler

        def _cycle_job() -> None:
            from etoro_bot.arena.engine import run_training_cycle
            from etoro_bot.arena.live import run_live_cycle
            from etoro_bot.arena.market import build_snapshot
            from etoro_bot.services.app_settings import arena_state

            deps = _arena_deps()
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

            deps = _arena_deps()
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
                             run_live_eod(deps, breaker=get_breaker()))

        def _evolve_job() -> None:
            from etoro_bot.arena.engine import bootstrap_if_needed
            from etoro_bot.arena.evolution import maybe_evolve

            deps = _arena_deps()
            bootstrap_if_needed(deps)
            outcome = maybe_evolve(deps)
            if outcome:
                log.info("arena: evoluzione %s", outcome)

        def _news_job() -> None:
            from etoro_bot.knowledge.pipeline import run_news_pipeline

            run_news_pipeline(
                kb=_kb(),
                settings=_full_settings(),
                client=_system_etoro_client(),
                llm=_system_llm(),
            )
            _mark_fetch(UserIdentity("system"))

        start_scheduler(
            get_settings=_full_settings,
            cycle_job=_cycle_job,
            eod_job=_eod_job,
            evolve_job=_evolve_job,
            news_job=_news_job,
        )
    except Exception:
        log.exception("scheduler non avviato")


@lru_cache(maxsize=1)
def get_repo() -> Repository:
    return Repository(make_session_factory(make_engine()))


def get_breaker() -> CircuitBreaker:
    """Istanza condivisa: lo stato del breaker è uno solo per tutto il processo."""
    return shared_breaker(load_breaker_rules())


def get_settings_service():
    from etoro_bot.services.app_settings import AppSettingsService

    return AppSettingsService(get_repo())


def _full_settings() -> dict[str, Any]:
    """Settings completi: default yaml + override runtime (DB > yaml).

    get_effective() restituisce SOLO le chiavi runtime gestite dal DB
    (valuta, timezone, stato arena): per watchlist, news_feeds,
    universe_discovery, knowledge e llm serve la base yaml.
    """
    return {**load_settings(), **get_settings_service().get_effective()}


# --- health & status --------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/status")
def status() -> dict[str, Any]:
    from etoro_bot.services.app_settings import arena_state
    from etoro_bot.services.scheduler import (
        market_is_open,
        next_cycle_at,
        open_sessions,
    )

    settings = _full_settings()
    breaker = get_breaker()
    repo = get_repo()

    equity_usd = None
    equity_change_day_pct = None
    champion = None
    try:
        series = repo.equity_series()
        if series:
            equity_usd = series[-1].equity_usd
            if len(series) >= 2 and series[-2].equity_usd:
                equity_change_day_pct = (
                    (series[-1].equity_usd - series[-2].equity_usd)
                    / series[-2].equity_usd * 100
                )
        champ = repo.champion()
        if champ is not None:
            champion = {"id": str(champ.id), "name": champ.name,
                        "generation": champ.generation}
    except Exception:  # DB giù: lo status resta consultabile
        log.warning("stato arena non disponibile", exc_info=True)

    arena = arena_state(repo)
    return {
        "kill_switch_active": kill_switch_active(),
        "circuit_breaker": {
            "tripped": breaker.blocks_openings(),
            "reason": breaker.state.reason,
            "until": breaker.state.cooloff_until,
        },
        "arena": arena,
        "champion": champion,
        "market_open": market_is_open(settings, datetime.now(timezone.utc)),
        "open_sessions": open_sessions(settings, datetime.now(timezone.utc)),
        "next_cycle_at": next_cycle_at(settings),
        "equity_usd": equity_usd,
        "equity_change_day_pct": equity_change_day_pct,
    }


# --- executions -------------------------------------------------------------


@app.get("/executions")
def list_executions(limit: int = Query(50, le=500)) -> dict[str, Any]:
    rows = get_repo().list_executions(limit=limit)
    return {
        "executions": [
            {
                "id": str(e.id),
                "run_id": e.run_id,
                "symbol": e.symbol,
                "side": e.side,
                "amount_usd": e.amount_usd,
                "status": e.status,
                "detail": e.detail,
                "execution_price": e.execution_price,
                "etoro_position_id": e.etoro_position_id,
                "created_at": e.created_at.isoformat(),
            }
            for e in rows
        ]
    }


# --- portfolio (solo posizioni bot, §7) -------------------------------------


@app.get("/portfolio")
def portfolio(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    repo = get_repo()
    positions = repo.open_positions()

    rates: dict[int, float] = {}
    directions: dict[int, str] = {}
    try:
        client = _make_client(identity)
        account_portfolio = client.get_portfolio()
        if account_portfolio.get("credit") is None:
            raise ValueError("portfolio eToro senza campo credit")
        for row in account_portfolio.get("positions") or []:
            if not isinstance(row, dict):
                continue
            try:
                pid = int(row.get("positionId"))
            except (TypeError, ValueError):
                continue
            is_buy = row.get("isBuy")
            directions[pid] = "long" if is_buy is None or is_buy else "short"
        cash_usd = max(float(account_portfolio["credit"]), 0.0)
        raw = client.get_rates([p.instrument_id for p in positions]) if positions else {}
        for iid, r in raw.items():
            price = r.get("lastExecution") or r.get("bid")
            if price:
                rates[int(iid)] = float(price)
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("portafoglio eToro non disponibile", exc_info=True)
        raise HTTPException(502, f"Capitale eToro non disponibile: {exc}") from exc

    invested = sum(p.amount_usd for p in positions)

    out = []
    for p in positions:
        cur = rates.get(p.instrument_id)
        direction = directions.get(p.etoro_position_id, "long")
        pnl = None
        pnl_pct = None
        if cur is not None and p.entry_price > 0:
            change = (cur - p.entry_price) / p.entry_price
            if direction == "short":
                change = -change
            pnl = p.amount_usd * change
            pnl_pct = change * 100
        out.append(
            {
                "etoro_position_id": p.etoro_position_id,
                "symbol": p.symbol,
                "instrument_id": p.instrument_id,
                "amount_usd": p.amount_usd,
                "direction": direction,
                "entry_price": p.entry_price,
                "current_price": cur,
                "unrealized_pnl_usd": pnl,
                "unrealized_pnl_pct": pnl_pct,
                "sector": p.sector,
                "opened_at": p.opened_at.isoformat(),
            }
        )

    # size massima teorica per ordine live, dal DNA del campione (o default)
    from etoro_bot.arena.dna import clamp_dna

    champ = repo.champion()
    dna = clamp_dna(champ.dna if champ else None)
    return {
        "positions": out,
        "cash_usd": cash_usd,
        "equity_usd": cash_usd + invested,
        "exposure_usd": invested,
        "max_trade_amount_usd": (cash_usd + invested) * dna["max_position_pct"] / 100.0,
        "capital_source": "etoro",
        "anomalies": [],
    }


def _make_client(identity: UserIdentity):
    from etoro_bot.etoro.client import EtoroClient

    keys = _user_keys(identity)
    if not keys.etoro_configured:
        raise HTTPException(422, "Configura le chiavi eToro personali in Impostazioni")
    return EtoroClient(api_key=keys.etoro_api_key, user_key=keys.etoro_user_key)


# --- backtest ---------------------------------------------------------------


def _backtest_service(identity: UserIdentity):
    from etoro_bot.services.backtest import BacktestService

    def price_fetcher(symbol: str, start_date):
        from datetime import date as _date

        client = _make_client(identity)
        found = client.search_instruments(
            {"internalSymbolFull": symbol},
            fields=["instrumentId", "internalSymbolFull"],
        )
        if not found:
            return {}
        iid = int(found[0]["instrumentId"])
        days = max((datetime.now(timezone.utc).date() - start_date).days + 5, 30)
        candles = client.get_candles(iid, interval="OneDay", count=min(days, 1000))
        out: dict[_date, float] = {}
        for c in candles:
            day = datetime.fromisoformat(c["fromDate"].replace("Z", "+00:00")).date()
            out[day] = float(c["close"])
        return out

    return BacktestService(get_repo(), price_fetcher)


def _pct(value: float | None) -> float | None:
    return None if value is None else value * 100.0


@app.get("/backtest/summary")
def backtest_summary(
    date_from: date | None = None,
    date_to: date | None = None,
    identity: UserIdentity = Depends(current_user),
) -> dict[str, Any]:
    s = _backtest_service(identity).summary(date_from=date_from, date_to=date_to)
    return {
        "metrics": {
            "total_return_pct": _pct(s["total_return"]),
            "cagr_pct": _pct(s["cagr"]),
            "volatility_pct": _pct(s["annualized_volatility"]),
            "sharpe": s["sharpe"],
            "sortino": s["sortino"],
            "max_drawdown_pct": _pct(s["max_drawdown"]),
            "calmar": s["calmar"],
            "alpha": s["alpha"],
            "beta": s["beta"],
            "information_ratio": s["information_ratio"],
            "win_rate_pct": _pct(s["win_rate"]),
            "profit_factor": s["profit_factor"],
            "recovery_factor": s["recovery_factor"],
            "expectancy_usd": s["expectancy"],
            "exposure_pct": s["exposure_pct"],
            "max_win_usd": s["max_win_usd"],
            "max_loss_usd": s["max_loss_usd"],
            "std_win_usd": s["std_win_usd"],
            "std_loss_usd": s["std_loss_usd"],
        },
        "n_closed_trades": s["n_closed_trades"],
        "n_days": s["n_days"],
        "insufficient_sample": s["insufficient_sample"],
        "annualization_available": s["annualization_available"],
        "risk_free_rate_pct": s["risk_free_rate_pct"],
    }


SPY_DIVIDEND_NOTE = (
    "Il prezzo SPY non include i dividendi (~1.3-1.5%/anno di total return in più): "
    "il confronto sottostima leggermente il benchmark."
)


@app.get("/backtest/equity-curve")
def backtest_equity_curve(
    benchmark: str = "spy",
    date_from: date | None = None,
    date_to: date | None = None,
    identity: UserIdentity = Depends(current_user),
) -> dict[str, Any]:
    return {
        "points": _backtest_service(identity).equity_curve(
            benchmark=benchmark, date_from=date_from, date_to=date_to
        ),
        "note_dividends": SPY_DIVIDEND_NOTE,
    }


@app.get("/backtest/trades")
def backtest_trades(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    return {"trades": _backtest_service(identity).trades()}


@app.get("/backtest/monthly-returns")
def backtest_monthly_returns(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    return {"rows": _backtest_service(identity).monthly_returns()}


# --- knowledge --------------------------------------------------------------


def _kb():
    from etoro_bot.knowledge.kb import KnowledgeBase

    return KnowledgeBase()


def _system_etoro_client():
    """Client eToro con le chiavi dell'account proprietario, per i job di
    sistema (discovery universo). None se le chiavi non sono configurate:
    la pipeline news degrada senza refresh dell'universo."""
    try:
        from etoro_bot.etoro.client import EtoroClient
        from etoro_bot.services.user_credentials import get_user_keys

        repo = get_repo()
        user_id = repo.owner_user_id() or "system"
        keys = get_user_keys(repo, user_id)
        if not keys.etoro_api_key or not keys.etoro_user_key:
            return None
        return EtoroClient(api_key=keys.etoro_api_key, user_key=keys.etoro_user_key)
    except Exception:
        log.warning("client eToro di sistema non disponibile", exc_info=True)
        return None


def _system_llm():
    """call_llm con la chiave OpenAI del proprietario, per i job di sistema
    (scout universo, sintesi memorie ticker). None se non configurata: i
    consumatori degradano da soli (fallback regex/headline)."""
    try:
        from functools import partial

        from etoro_bot.services.user_credentials import get_user_keys

        repo = get_repo()
        keys = get_user_keys(repo, repo.owner_user_id() or "system")
        if not keys.openai_api_key:
            return None
        from etoro_bot.llm import call_llm, make_openai_client

        return partial(call_llm, client=make_openai_client(keys.openai_api_key))
    except Exception:
        log.warning("LLM di sistema non disponibile", exc_info=True)
        return None


def _user_setting_key(prefix: str, user_id: str) -> str:
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}:{digest}"


def _rss_feeds(identity: UserIdentity) -> list[str]:
    stored = get_repo().get_setting(_user_setting_key("rss", identity.user_id))
    if isinstance(stored, list):
        return [str(item) for item in stored]
    feeds = load_settings().get("news_feeds", {})
    return list(feeds.get("generic", [])) + list(feeds.get("per_ticker", []))


@app.get("/knowledge/status")
def knowledge_status(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    st = _kb().status()
    st["rss_feeds"] = _rss_feeds(identity)
    st["last_fetch"] = _last_fetch_marker(identity)
    return st


_LAST_FETCH_FILE = "last_news_fetch"


def _user_state_file(identity: UserIdentity, name: str) -> Path:
    digest = hashlib.sha256(identity.user_id.encode("utf-8")).hexdigest()[:24]
    path = Path(os.environ.get("KILL_SWITCH_DIR", ".")) / "users" / digest
    path.mkdir(parents=True, exist_ok=True)
    return path / name


def _last_fetch_marker(identity: UserIdentity) -> str | None:
    path = _user_state_file(identity, _LAST_FETCH_FILE)
    if path.exists():
        return path.read_text(encoding="utf-8").strip() or None
    return None


def _mark_fetch(identity: UserIdentity) -> None:
    path = _user_state_file(identity, _LAST_FETCH_FILE)
    path.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")


class RssFeedsBody(BaseModel):
    feeds: list[str]


@app.put("/knowledge/rss-feeds")
def update_rss_feeds(
    body: RssFeedsBody, identity: UserIdentity = Depends(current_user)
) -> dict[str, Any]:
    from etoro_bot.knowledge.safe_fetch import UnsafeUrlError, assert_public_url

    feeds: list[str] = []
    for raw in body.feeds:
        value = raw.strip()
        if not value:
            continue
        if not value.startswith(("https://", "http://")):
            raise HTTPException(422, f"URL feed non valido: {value}")
        # Il backend scarica questi URL da dentro la rete Docker: un feed che
        # punta a un servizio interno lo trasformerebbe in una sonda, con la
        # risposta indicizzata e poi leggibile dalla pagina News.
        try:
            assert_public_url(value)
        except UnsafeUrlError as exc:
            raise HTTPException(422, f"URL feed non ammesso: {exc}") from exc
        if value not in feeds:
            feeds.append(value)
    if len(feeds) > 50:
        raise HTTPException(422, "Massimo 50 feed RSS")
    get_repo().set_setting(_user_setting_key("rss", identity.user_id), feeds, source="api")
    return {"rss_feeds": feeds}


def _news_file(identity: UserIdentity) -> Path:
    return _user_state_file(identity, "latest_news.json")


@app.get("/news")
def latest_news(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    path = _news_file(identity)
    if not path.exists():
        return {"items": [], "updated_at": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"items": [], "updated_at": None}
    return payload


@app.post("/knowledge/fetch-news", status_code=202)
def knowledge_fetch_news(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    def _job() -> None:
        try:
            from etoro_bot.knowledge.fetch_news import fetch_all
            from etoro_bot.knowledge.pipeline import run_news_pipeline

            settings = load_settings()
            settings["news_feeds"] = {"generic": _rss_feeds(identity), "per_ticker": []}
            items = fetch_all(settings)
            run_news_pipeline(
                kb=_kb(), settings=settings, items=items,
                client=_system_etoro_client(), llm=_system_llm(),
            )
            updated_at = datetime.now(timezone.utc).isoformat()
            _news_file(identity).write_text(
                json.dumps({"items": items[:60], "updated_at": updated_at}, ensure_ascii=False),
                encoding="utf-8",
            )
            _mark_fetch(identity)
            log.info("fetch news: %d item indicizzati", len(items))
        except Exception:
            log.exception("fetch news fallito")

    threading.Thread(target=_job, daemon=True).start()
    return {"status": "accepted"}


_UPLOAD_SUFFIXES = (".pdf", ".docx", ".pptx", ".xlsx", ".md", ".txt")


@app.post("/knowledge/ingest")
async def knowledge_ingest(
    file: UploadFile = File(...),
    identity: UserIdentity = Depends(current_user),
) -> dict[str, Any]:
    from etoro_bot.knowledge.ingest import ingest_upload
    from etoro_bot.knowledge.parsers import UnsupportedFileTypeError

    filename = file.filename or ""
    if not filename.lower().endswith(_UPLOAD_SUFFIXES):
        raise HTTPException(
            415,
            f"estensione non supportata per '{filename}': estensioni ammesse "
            f"{', '.join(_UPLOAD_SUFFIXES)}",
        )

    content = await file.read()
    try:
        outcome = ingest_upload(filename, content, kb=_kb())
    except (UnsupportedFileTypeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    safe_name = Path(filename).name
    upload_dir = Path(os.environ.get("KNOWLEDGE_BASE_DIR", "/app/knowledge_base")) / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(identity.user_id.encode("utf-8")).hexdigest()[:12]
    (upload_dir / f"{digest}-{safe_name}").write_bytes(content)
    return {
        "filename": filename,
        "chunks_indexed": outcome.chunks,
        "tickers": outcome.tickers,
    }


# --- trade operativi e storico --------------------------------------------


@app.get("/trades")
def trades(
    statuses: str | None = None,
    symbol: str | None = None,
    identity: UserIdentity = Depends(current_user),
) -> dict[str, Any]:
    repo = get_repo()
    rows: list[dict[str, Any]] = []
    for position in repo.open_positions():
        rows.append(
            {
                "id": f"position:{position.etoro_position_id}",
                "position_id": position.etoro_position_id,
                "execution_id": None,
                "symbol": position.symbol,
                "side": "buy",
                "status": "open",
                "amount_usd": position.amount_usd,
                "entry_price": position.entry_price,
                "current_price": None,
                "pnl_usd": None,
                "created_at": position.opened_at.isoformat(),
                "detail": "Posizione aperta",
                "can_close": True,
                "can_cancel": False,
            }
        )
    for execution in repo.list_executions(limit=500):
        if execution.status == "filled" and execution.etoro_position_id:
            continue
        rows.append(
            {
                "id": f"execution:{execution.id}",
                "position_id": execution.etoro_position_id,
                "execution_id": str(execution.id),
                "symbol": execution.symbol,
                "side": execution.side,
                "status": execution.status,
                "amount_usd": execution.amount_usd,
                "entry_price": execution.execution_price,
                "current_price": None,
                "pnl_usd": None,
                "created_at": execution.created_at.isoformat(),
                "detail": execution.detail,
                "can_close": False,
                "can_cancel": execution.status == "pending",
            }
        )
    selected_statuses = {
        value.strip() for value in (statuses or "").split(",") if value.strip()
    }
    if selected_statuses:
        rows = [row for row in rows if row["status"] in selected_statuses]
    if symbol:
        needle = symbol.strip().upper()
        rows = [row for row in rows if needle in row["symbol"].upper()]
    rows.sort(key=lambda row: row["created_at"], reverse=True)
    return {"trades": rows}


class CloseTradeBody(BaseModel):
    confirmation: str


@app.post("/trades/{position_id}/close")
def close_trade(
    position_id: int,
    body: CloseTradeBody,
    identity: UserIdentity = Depends(current_user),
) -> dict[str, Any]:
    if body.confirmation != "CHIUDI":
        raise HTTPException(422, "Conferma non valida: digita CHIUDI")
    require_owner(identity, "chiudere una posizione reale")
    repo = get_repo()
    position = repo.get_open_position(position_id)
    if position is None:
        raise HTTPException(404, "Posizione aperta non trovata")
    client = _make_client(identity)
    client.close_position(position_id, position.instrument_id)
    close_price = None
    pnl = None
    try:
        # finestra corta: la chiusura è di adesso, e senza minDate l'API
        # pagina l'intero storico del conto
        recent = datetime.now(timezone.utc) - timedelta(days=3)
        for item in client.get_trade_history(min_date=recent):
            if int(item.get("positionId") or -1) == position_id:
                close_price = item.get("closeRate")
                pnl = item.get("netProfit")
                break
    except Exception:
        log.warning("chiusura %s eseguita, dettaglio PnL non ancora disponibile", position_id)
    repo.close_position(
        position_id,
        close_price=float(close_price) if close_price is not None else None,
        realized_pnl_usd=float(pnl) if pnl is not None else None,
        close_reason="manual_close",
        # PnL non ancora pubblicato dal broker: la posizione resta in attesa e
        # la liquidazione del prossimo ciclo live la completerà
        pnl_settled=pnl is not None,
    )
    return {"status": "closed", "position_id": position_id}


@app.post("/executions/{execution_id}/cancel")
def cancel_execution(
    execution_id: uuid.UUID, identity: UserIdentity = Depends(current_user)
) -> dict[str, Any]:
    require_owner(identity, "annullare un ordine")
    if not get_repo().cancel_pending_execution(execution_id):
        raise HTTPException(409, "L'ordine non è annullabile: è già terminale o inesistente")
    return {"status": "cancelled", "execution_id": str(execution_id)}


@app.get("/trade-history")
def trade_history(
    statuses: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    symbol: str | None = None,
    identity: UserIdentity = Depends(current_user),
) -> dict[str, Any]:
    repo = get_repo()
    items: list[dict[str, Any]] = []
    for position in repo.open_positions():
        items.append(
            {
                "id": f"position:{position.etoro_position_id}",
                "symbol": position.symbol,
                "side": "buy",
                "status": "open",
                "amount_usd": position.amount_usd,
                "price": position.entry_price,
                "pnl_usd": None,
                "opened_at": position.opened_at.isoformat(),
                "closed_at": None,
                "detail": "Posizione aperta",
            }
        )
    for position in repo.closed_positions():
        items.append(
            {
                "id": f"position:{position.etoro_position_id}",
                "symbol": position.symbol,
                "side": "sell",
                "status": "closed",
                "amount_usd": position.amount_usd,
                "price": position.close_price,
                "pnl_usd": position.realized_pnl_usd,
                "opened_at": position.opened_at.isoformat(),
                "closed_at": position.closed_at.isoformat() if position.closed_at else None,
                "detail": position.close_reason,
            }
        )
    for execution in repo.list_executions(limit=500):
        if execution.status == "filled":
            continue
        items.append(
            {
                "id": f"execution:{execution.id}",
                "symbol": execution.symbol,
                "side": execution.side,
                "status": execution.status,
                "amount_usd": execution.amount_usd,
                "price": execution.execution_price,
                "pnl_usd": None,
                "opened_at": execution.created_at.isoformat(),
                "closed_at": execution.created_at.isoformat(),
                "detail": execution.detail,
            }
        )
    selected_statuses = {
        value.strip() for value in (statuses or "").split(",") if value.strip()
    }
    if selected_statuses:
        items = [item for item in items if item["status"] in selected_statuses]
    if symbol:
        needle = symbol.strip().upper()
        items = [item for item in items if needle in item["symbol"].upper()]
    if date_from or date_to:
        def in_range(item: dict[str, Any]) -> bool:
            raw = item["closed_at"] or item["opened_at"]
            day = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
            return (date_from is None or day >= date_from) and (
                date_to is None or day <= date_to
            )
        items = [item for item in items if in_range(item)]
    items.sort(key=lambda row: row["closed_at"] or row["opened_at"], reverse=True)
    return {"items": items}


# --- settings ---------------------------------------------------------------


class SettingsBody(BaseModel):
    timezone: str | None = None
    currency: str | None = None


class CredentialsBody(BaseModel):
    etoro_api_key: str | None = None
    etoro_user_key: str | None = None
    openai_api_key: str | None = None


@app.get("/account/credentials")
def account_credentials(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    keys = _user_keys(identity)
    return {
        "user_id": identity.user_id,
        "email": identity.email,
        "display_name": identity.name,
        "etoro_api_key_configured": bool(keys.etoro_api_key),
        "etoro_user_key_configured": bool(keys.etoro_user_key),
        "openai_api_key_configured": bool(keys.openai_api_key),
    }


@app.put("/account/credentials")
def put_account_credentials(
    body: CredentialsBody, identity: UserIdentity = Depends(current_user)
) -> dict[str, Any]:
    from etoro_bot.services.user_credentials import update_user_keys

    keys = update_user_keys(
        get_repo(),
        identity.user_id,
        email=identity.email,
        display_name=identity.name,
        **body.model_dump(),
    )
    return {
        "user_id": identity.user_id,
        "email": identity.email,
        "display_name": identity.name,
        "etoro_api_key_configured": bool(keys.etoro_api_key),
        "etoro_user_key_configured": bool(keys.etoro_user_key),
        "openai_api_key_configured": bool(keys.openai_api_key),
    }


@app.get("/fx/rates")
def fx_rates(refresh: bool = False) -> dict[str, Any]:
    """Tassi USD→valuta + elenco delle valute selezionabili.

    Il journal resta in dollari (eToro ragiona in USD): la conversione è solo
    di presentazione e avviene lato UI con questi tassi.
    """
    from etoro_bot.services.fx import CURRENCY_LABELS, SUPPORTED_CURRENCIES, get_rates

    payload = get_rates(force=refresh)
    payload["currencies"] = [
        {"code": code, "label": CURRENCY_LABELS.get(code, code)}
        for code in SUPPORTED_CURRENCIES
    ]
    return payload


@app.get("/settings")
def get_settings(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    svc = get_settings_service()
    effective = svc.get_effective()
    effective.update(
        {
            "api_keys_configured": _user_keys(identity).etoro_configured,
            "openai_configured": bool(_user_keys(identity).openai_api_key),
        }
    )
    return effective


@app.put("/settings")
def put_settings(
    body: SettingsBody, identity: UserIdentity = Depends(current_user)
) -> dict[str, Any]:
    from etoro_bot.services.app_settings import SettingsValidationError

    require_owner(identity, "cambiare le impostazioni")
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        return get_settings_service().update(
            changes,
            source="api",
            etoro_configured=_user_keys(identity).etoro_configured,
        )
    except SettingsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/settings/audit")
def settings_audit() -> dict[str, Any]:
    rows = get_repo().settings_audit()
    return {
        "entries": [
            {
                "id": str(r.id),
                "changed_at": r.changed_at.isoformat(),
                "key": r.key,
                "old_value": r.old_value,
                "new_value": r.new_value,
                "source": r.source,
            }
            for r in rows
        ]
    }


# --- arena (allenamento evolutivo) ------------------------------------------


def _agent_payload(repo: Repository, agent, with_memory: bool = True) -> dict[str, Any]:
    from etoro_bot.arena.engine import position_direction

    positions = repo.sim_positions(agent.id)
    invested = sum(p.amount_usd for p in positions)
    pnl_month = agent.cash_usd + invested - agent.starting_capital_usd
    payload = {
        "id": str(agent.id),
        "name": agent.name,
        "generation": agent.generation,
        "status": agent.status,
        "is_champion": agent.is_champion,
        "parent_id": str(agent.parent_id) if agent.parent_id else None,
        "born_at": agent.born_at.isoformat() if agent.born_at else None,
        "died_at": agent.died_at.isoformat() if agent.died_at else None,
        "death_reason": agent.death_reason,
        "month": agent.month,
        "dna": agent.dna,
        "starting_capital_usd": agent.starting_capital_usd,
        "cash_usd": agent.cash_usd,
        "invested_usd": round(invested, 2),
        "equity_usd": round(agent.cash_usd + invested, 2),
        "pnl_month_usd": round(pnl_month, 2),
        "open_positions": [
            {
                "id": str(p.id),
                "symbol": p.symbol,
                "amount_usd": p.amount_usd,
                "entry_price": p.entry_price,
                "opened_at": p.opened_at.isoformat(),
                "open_reason": p.open_reason,
                "direction": position_direction(p),
            }
            for p in positions
        ],
    }
    if with_memory:
        payload["memory"] = agent.memory
    return payload


@app.get("/arena")
def arena_overview() -> dict[str, Any]:
    from etoro_bot.services.app_settings import arena_state
    from etoro_bot.services.scheduler import (
        market_is_open,
        market_sessions,
        next_cycle_at,
        open_sessions,
    )

    repo = get_repo()
    settings = _full_settings()
    agents = repo.all_agents()
    alive = [a for a in agents if a.status == "alive"]
    champ = repo.champion()
    now = datetime.now(timezone.utc)
    from calendar import monthrange

    days_left = monthrange(now.year, now.month)[1] - now.day
    now_open = set(open_sessions(settings, now))
    return {
        "state": arena_state(repo),
        "market_open": market_is_open(settings, now),
        "sessions": [
            {"name": name, "open_utc": window[0], "close_utc": window[1],
             "open_now": name in now_open}
            for name, window in sorted(market_sessions(settings).items())
        ],
        "next_cycle_at": next_cycle_at(settings),
        "days_to_evaluation": days_left,
        "generation": max((a.generation for a in alive), default=0),
        "agents": [_agent_payload(repo, a) for a in alive],
        "champion": _agent_payload(repo, champ) if champ else None,
        "lineage": [_agent_payload(repo, a, with_memory=False) for a in agents],
    }


@app.get("/arena/agents/{agent_id}")
def arena_agent_detail(agent_id: uuid.UUID) -> dict[str, Any]:
    from etoro_bot.arena.metrics import compute_agent_metrics, trade_direction

    repo = get_repo()
    agent = repo.get_agent(agent_id)
    if agent is None:
        raise HTTPException(404, "agente non trovato")
    payload = _agent_payload(repo, agent)
    trades = repo.sim_trades(agent_id)
    equity_points = repo.sim_equity_series(agent_id)
    return {
        "agent": payload,
        "equity": [
            {"ts": p.ts.isoformat(), "equity_usd": p.equity_usd}
            for p in equity_points
        ],
        "trades": [
            {
                "id": str(t.id),
                "symbol": t.symbol,
                "amount_usd": t.amount_usd,
                "entry_price": t.entry_price,
                "close_price": t.close_price,
                "pnl_usd": t.pnl_usd,
                "opened_at": t.opened_at.isoformat(),
                "closed_at": t.closed_at.isoformat(),
                "open_reason": t.open_reason,
                "close_reason": t.close_reason,
                "direction": trade_direction(t),
                "holding_hours": round(
                    (t.closed_at - t.opened_at).total_seconds() / 3600.0, 1
                ),
                "return_pct": round(t.pnl_usd / t.amount_usd * 100.0, 2)
                if t.amount_usd
                else None,
            }
            for t in trades
        ],
        "metrics": compute_agent_metrics(
            starting_capital_usd=agent.starting_capital_usd,
            cash_usd=agent.cash_usd,
            # già sommato dal payload dell'agente: evita una query in più
            invested_usd=payload["invested_usd"],
            born_at=agent.born_at,
            trades=trades,
            equity_points=equity_points,
        ),
    }


@app.get("/arena/events")
def arena_events(limit: int = Query(100, le=500)) -> dict[str, Any]:
    rows = get_repo().arena_events(limit=limit)
    return {
        "events": [
            {"id": str(e.id), "ts": e.ts.isoformat(), "event": e.event,
             "payload": e.payload}
            for e in rows
        ]
    }


@app.post("/arena/pause")
def arena_pause(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    from etoro_bot.services.app_settings import set_arena_state

    require_owner(identity, "mettere in pausa l'arena")
    state = set_arena_state(get_repo(), paused=True)
    get_repo().add_arena_event("pause", {})
    return {"state": state}


@app.post("/arena/resume")
def arena_resume(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    from etoro_bot.services.app_settings import set_arena_state

    require_owner(identity, "riavviare l'arena")
    state = set_arena_state(get_repo(), paused=False)
    get_repo().add_arena_event("resume", {})
    return {"state": state}


@app.post("/arena/cycle", status_code=202)
def arena_trigger_cycle(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    """Forza un ciclo di allenamento adesso (per test/monitoraggio manuale)."""
    require_owner(identity, "forzare un ciclo")

    def _job() -> None:
        try:
            from etoro_bot.arena.engine import run_training_cycle

            deps = _arena_deps()
            log.info("arena: ciclo manuale %s", run_training_cycle(deps))
        except Exception:
            log.exception("ciclo manuale fallito")

    threading.Thread(target=_job, daemon=True).start()
    return {"status": "accepted"}


class LiveBody(BaseModel):
    confirmation: bool | None = None


@app.post("/live/enable")
def live_enable(
    body: LiveBody, identity: UserIdentity = Depends(current_user)
) -> dict[str, Any]:
    """Accende il trading live (denaro REALE) col DNA del campione."""
    require_owner(identity, "attivare il trading live")
    from etoro_bot.services.app_settings import (
        SettingsValidationError,
        check_live_activation,
        set_arena_state,
    )

    if body.confirmation is not True:
        raise HTTPException(422, "l'attivazione del live richiede confirmation: true")
    try:
        check_live_activation(
            get_repo(), etoro_configured=_user_keys(identity).etoro_configured
        )
    except SettingsValidationError as exc:
        raise HTTPException(422, exc.message) from exc
    state = set_arena_state(get_repo(), live_enabled=True)
    get_repo().add_arena_event("live_on", {"by": identity.user_id})
    return {"state": state}


@app.post("/live/disable")
def live_disable(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    from etoro_bot.services.app_settings import set_arena_state

    require_owner(identity, "spegnere il trading live")
    state = set_arena_state(get_repo(), live_enabled=False)
    get_repo().add_arena_event("live_off", {"by": identity.user_id})
    return {"state": state}


# --- kill switch --------------------------------------------------------------


@app.post("/kill-switch")
def activate_kill_switch(
    identity: UserIdentity = Depends(current_user),
) -> dict[str, Any]:
    require_owner(identity, "attivare il kill switch")
    engage_kill_switch("api")
    return {"kill_switch_active": True}


@app.delete("/kill-switch")
def deactivate_kill_switch(
    identity: UserIdentity = Depends(current_user),
) -> dict[str, Any]:
    """Rilascia il freno d'emergenza: mutazione critica, solo il proprietario."""
    require_owner(identity, "disattivare il kill switch")
    release_kill_switch()
    return {"kill_switch_active": kill_switch_active()}
