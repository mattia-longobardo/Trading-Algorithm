"""Repository: unico punto di accesso al journal per pipeline, servizi e API."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from etoro_bot.config import database_url
from etoro_bot.db.models import (
    Agent,
    AppSetting,
    ArenaEvent,
    BotPosition,
    Decision,
    EquitySnapshot,
    Execution,
    Run,
    SettingsAudit,
    SimEquityPoint,
    SimPosition,
    SimTrade,
    UserCredential,
)
from etoro_bot.domain import ExecutionResult, ExecutionStatus, SimCosts


def make_engine(url: str | None = None) -> Engine:
    return create_engine(url or database_url(), pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


# Chiave arbitraria ma fissa dell'advisory lock: identifica "lo scheduler di
# questo bot" su questo database.
SCHEDULER_LOCK_KEY = 8_421_337


def try_scheduler_lock(url: str | None = None) -> Connection | None:
    """Prende l'advisory lock dello scheduler; None se un'altra istanza ce l'ha.

    Tutta la sicurezza concorrente (lock in-process, breaker singleton) presume
    UN SOLO processo: due repliche o `--workers 2` moltiplicherebbero gli
    scheduler e quindi gli ordini. Il lock Postgres è per-sessione, quindi la
    connessione restituita va tenuta viva per tutta la vita del processo:
    chiuderla libera il lock. `NullPool` perché il pooling rimetterebbe in circolo
    una connessione che detiene ancora il lock.
    """
    engine = create_engine(url or database_url(), poolclass=NullPool)
    conn = engine.connect()
    try:
        acquired = bool(
            conn.execute(select(func.pg_try_advisory_lock(SCHEDULER_LOCK_KEY))).scalar()
        )
        conn.commit()  # niente transazione aperta a vita: il lock è di sessione
    except Exception:
        conn.close()
        raise
    if acquired:
        return conn
    conn.close()
    return None


class Repository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._sf = session_factory

    # --- runs -------------------------------------------------------------
    def create_run(self, run_id: str, environment: str) -> None:
        with self._sf.begin() as s:
            s.add(Run(run_id=run_id, environment=environment))

    def finish_run(self, run_id: str, summary: dict[str, Any]) -> None:
        with self._sf.begin() as s:
            run = s.get(Run, run_id)
            if run is not None:
                run.summary_json = summary

    def list_runs(self, limit: int = 50) -> list[Run]:
        with self._sf() as s:
            return list(
                s.scalars(select(Run).order_by(Run.started_at.desc()).limit(limit))
            )

    def delete_run(self, run_id: str) -> bool:
        """Cancella una run e tutto ciò che vi punta; False se non esiste.

        L'ordine è quello delle foreign key: decisioni ed esecuzioni prima,
        posizioni registrate poi, la run per ultima. Le posizioni del bot
        vengono rimosse insieme alla run che le ha aperte, altrimenti il
        registry conserverebbe righe orfane che il reconcile rileggerebbe.
        """
        with self._sf.begin() as s:
            run = s.get(Run, run_id)
            if run is None:
                return False
            s.query(Decision).filter(Decision.run_id == run_id).delete()
            s.query(Execution).filter(Execution.run_id == run_id).delete()
            s.query(BotPosition).filter(BotPosition.run_id == run_id).delete()
            s.delete(run)
            return True

    def get_run(self, run_id: str) -> Run | None:
        with self._sf() as s:
            return s.get(Run, run_id)

    # --- decisions / executions -------------------------------------------
    def add_decision(
        self, run_id: str, symbol: str, stage: str, payload: dict[str, Any]
    ) -> None:
        with self._sf.begin() as s:
            s.add(Decision(run_id=run_id, symbol=symbol, stage=stage, payload=payload))

    def get_run_decisions(self, run_id: str) -> list[Decision]:
        with self._sf() as s:
            return list(
                s.scalars(
                    select(Decision)
                    .where(Decision.run_id == run_id)
                    .order_by(Decision.created_at)
                )
            )

    def add_execution(self, run_id: str, result: ExecutionResult) -> uuid.UUID:
        """Scrive la riga a giornale e ne ritorna l'id, per poterla aggiornare."""
        with self._sf.begin() as s:
            row = Execution(
                run_id=run_id,
                symbol=result.symbol,
                side=result.side.value,
                amount_usd=result.amount_usd,
                status=result.status.value,
                detail=result.detail,
                execution_price=result.execution_price,
                etoro_position_id=result.etoro_position_id,
            )
            s.add(row)
            s.flush()
            return row.id

    def update_execution(
        self,
        execution_id: uuid.UUID,
        status: ExecutionStatus,
        detail: str | None = None,
        execution_price: float | None = None,
        etoro_position_id: int | None = None,
    ) -> None:
        """Porta a esito una riga già scritta (write-ahead): nessuna riga nuova."""
        with self._sf.begin() as s:
            row = s.get(Execution, execution_id)
            if row is None:
                return
            row.status = status.value
            if detail is not None:
                row.detail = detail
            if execution_price is not None:
                row.execution_price = execution_price
            if etoro_position_id is not None:
                row.etoro_position_id = etoro_position_id

    def list_executions(self, limit: int = 50) -> list[Execution]:
        with self._sf() as s:
            return list(
                s.scalars(select(Execution).order_by(Execution.created_at.desc()).limit(limit))
            )

    def executions_by_status(
        self, statuses: tuple[str, ...], since: datetime
    ) -> list[Execution]:
        """Esecuzioni in quegli stati dalla data indicata, senza tetto di righe.

        Il reconcile deve vedere TUTTI i non-conclusi delle ultime ore: una
        finestra a numero di righe, in giornate attive, li taglierebbe fuori.
        """
        with self._sf() as s:
            return list(
                s.scalars(
                    select(Execution)
                    .where(
                        Execution.status.in_(list(statuses)),
                        Execution.created_at >= since,
                    )
                    .order_by(Execution.created_at)
                )
            )

    def cancel_pending_execution(self, execution_id: uuid.UUID) -> bool:
        with self._sf.begin() as s:
            row = s.get(Execution, execution_id)
            if row is None or row.status != ExecutionStatus.PENDING.value:
                return False
            row.status = ExecutionStatus.CANCELLED.value
            # il reference id resta nel dettaglio: annullare qui non annulla
            # nulla sul broker, e se l'ordine era passato il reconcile deve
            # poter ancora adottare la posizione.
            row.detail = f"annullato manualmente {row.detail or ''}".strip()
            return True

    # --- bot positions registry (§7) ---------------------------------------
    def register_open_position(
        self,
        etoro_position_id: int,
        run_id: str,
        symbol: str,
        instrument_id: int,
        amount_usd: float,
        entry_price: float,
        opened_at: datetime,
        sector: str = "unknown",
        direction: str = "long",
    ) -> None:
        with self._sf.begin() as s:
            s.merge(
                BotPosition(
                    etoro_position_id=etoro_position_id,
                    run_id=run_id,
                    symbol=symbol,
                    instrument_id=instrument_id,
                    direction=direction,
                    amount_usd=amount_usd,
                    entry_price=entry_price,
                    opened_at=opened_at,
                    sector=sector,
                )
            )

    def close_position(
        self,
        etoro_position_id: int,
        close_price: float | None,
        realized_pnl_usd: float | None,
        close_reason: str,
        closed_at: datetime | None = None,
        close_order_id: int | None = None,
        pnl_settled: bool = True,
    ) -> None:
        """Registra la chiusura. `pnl_settled=False` marca il PnL come STIMA:
        realized_pnl_usd verrà riscritto dal broker in una passata successiva."""
        with self._sf.begin() as s:
            pos = s.get(BotPosition, etoro_position_id)
            if pos is not None and pos.closed_at is None:
                pos.closed_at = closed_at or datetime.now(timezone.utc)
                pos.close_price = close_price
                pos.realized_pnl_usd = realized_pnl_usd
                pos.close_reason = close_reason
                pos.close_order_id = close_order_id
                pos.pnl_settled = pnl_settled

    def unsettled_closed_positions(self) -> list[BotPosition]:
        """Chiusure il cui PnL reale non è ancora arrivato dal broker."""
        with self._sf() as s:
            return list(
                s.scalars(
                    select(BotPosition)
                    .where(
                        BotPosition.closed_at.is_not(None),
                        BotPosition.pnl_settled.is_(False),
                    )
                    .order_by(BotPosition.closed_at)
                )
            )

    def settle_position_pnl(
        self,
        etoro_position_id: int,
        realized_pnl_usd: float,
        close_price: float | None = None,
    ) -> None:
        """Sostituisce la stima col PnL reale del broker e chiude la partita."""
        with self._sf.begin() as s:
            pos = s.get(BotPosition, etoro_position_id)
            if pos is None:
                return
            pos.realized_pnl_usd = realized_pnl_usd
            if close_price is not None:
                pos.close_price = close_price
            pos.pnl_settled = True

    def known_position_ids(self) -> set[int]:
        """Tutti i positionId mai registrati dal bot (aperti o chiusi)."""
        with self._sf() as s:
            return {int(pid) for pid in s.scalars(select(BotPosition.etoro_position_id))}

    def open_positions(self) -> list[BotPosition]:
        with self._sf() as s:
            return list(
                s.scalars(select(BotPosition).where(BotPosition.closed_at.is_(None)))
            )

    def get_open_position(self, position_id: int) -> BotPosition | None:
        with self._sf() as s:
            return s.scalar(
                select(BotPosition).where(
                    BotPosition.etoro_position_id == position_id,
                    BotPosition.closed_at.is_(None),
                )
            )

    def closed_positions(self, limit: int | None = 1000) -> list[BotPosition]:
        """Posizioni chiuse in ordine cronologico; `limit` tiene le PIÙ RECENTI.

        Le route in polling non hanno bisogno dello storico intero a ogni giro;
        il track record (`services/backtest.py`) sì, e passa `limit=None`.
        """
        with self._sf() as s:
            stmt = (
                select(BotPosition)
                .where(BotPosition.closed_at.is_not(None))
                .order_by(BotPosition.closed_at.desc())
            )
            if limit is not None:
                stmt = stmt.limit(limit)
            return list(reversed(list(s.scalars(stmt))))

    # --- equity -------------------------------------------------------------
    def record_equity_snapshot(
        self, day: date, equity_usd: float, cash_usd: float, exposure_usd: float
    ) -> None:
        with self._sf.begin() as s:
            s.merge(
                EquitySnapshot(
                    date=day, equity_usd=equity_usd, cash_usd=cash_usd, exposure_usd=exposure_usd
                )
            )

    def equity_series(self, limit: int | None = None) -> list[EquitySnapshot]:
        """Snapshot equity in ordine di data; `limit` tiene i PIÙ RECENTI.

        Uno snapshot al giorno: la serie intera resta piccola e il track record
        la vuole tutta. `limit` serve a chi guarda solo la coda (`/status`).
        """
        with self._sf() as s:
            if limit is None:
                return list(
                    s.scalars(select(EquitySnapshot).order_by(EquitySnapshot.date))
                )
            rows = list(
                s.scalars(
                    select(EquitySnapshot)
                    .order_by(EquitySnapshot.date.desc())
                    .limit(limit)
                )
            )
            return list(reversed(rows))

    # --- app settings (§10) -------------------------------------------------
    def get_setting(self, key: str) -> Any | None:
        with self._sf() as s:
            row = s.get(AppSetting, key)
            return None if row is None else row.value

    def set_setting(self, key: str, value: Any, source: str = "api") -> None:
        with self._sf.begin() as s:
            row = s.get(AppSetting, key)
            old = row.value if row is not None else None
            if row is None:
                s.add(AppSetting(key=key, value=value))
            else:
                row.value = value
                row.updated_at = datetime.now(timezone.utc)
            s.add(
                SettingsAudit(
                    key=key,
                    old_value={"value": old},
                    new_value={"value": value},
                    source=source,
                )
            )

    def settings_audit(self, limit: int = 100) -> list[SettingsAudit]:
        with self._sf() as s:
            return list(
                s.scalars(
                    select(SettingsAudit).order_by(SettingsAudit.changed_at.desc()).limit(limit)
                )
            )

    # --- arena evolutiva ----------------------------------------------------
    def create_agent(
        self,
        name: str,
        generation: int,
        dna: dict[str, Any],
        memory: str,
        month: str,
        starting_capital_usd: float,
        parent_id: uuid.UUID | None = None,
        is_champion: bool = False,
    ) -> uuid.UUID:
        with self._sf.begin() as s:
            agent = Agent(
                name=name,
                generation=generation,
                dna=dna,
                memory=memory,
                month=month,
                starting_capital_usd=starting_capital_usd,
                cash_usd=starting_capital_usd,
                parent_id=parent_id,
                is_champion=is_champion,
            )
            s.add(agent)
            s.flush()
            return agent.id

    def get_agent(self, agent_id: uuid.UUID) -> Agent | None:
        with self._sf() as s:
            return s.get(Agent, agent_id)

    def alive_agents(self) -> list[Agent]:
        with self._sf() as s:
            return list(
                s.scalars(
                    select(Agent).where(Agent.status == "alive").order_by(Agent.name)
                )
            )

    def all_agents(self) -> list[Agent]:
        with self._sf() as s:
            return list(
                s.scalars(select(Agent).order_by(Agent.generation, Agent.name))
            )

    def champion(self) -> Agent | None:
        with self._sf() as s:
            return s.scalars(
                select(Agent)
                .where(Agent.is_champion.is_(True))
                .order_by(Agent.born_at.desc())
                .limit(1)
            ).first()

    def set_champion(self, agent_id: uuid.UUID) -> None:
        with self._sf.begin() as s:
            for row in s.scalars(select(Agent).where(Agent.is_champion.is_(True))):
                row.is_champion = False
            agent = s.get(Agent, agent_id)
            if agent is not None:
                agent.is_champion = True

    def retire_agent(self, agent_id: uuid.UUID) -> None:
        """Il vincitore del mese non muore: evolve nella generazione successiva."""
        with self._sf.begin() as s:
            agent = s.get(Agent, agent_id)
            if agent is not None and agent.status == "alive":
                agent.status = "evolved"

    def kill_agent(self, agent_id: uuid.UUID, reason: str) -> None:
        with self._sf.begin() as s:
            agent = s.get(Agent, agent_id)
            if agent is not None and agent.status == "alive":
                agent.status = "dead"
                agent.died_at = datetime.now(timezone.utc)
                agent.death_reason = reason

    def update_agent_memory(self, agent_id: uuid.UUID, memory: str) -> None:
        with self._sf.begin() as s:
            agent = s.get(Agent, agent_id)
            if agent is not None:
                agent.memory = memory

    def open_sim_position(
        self,
        agent_id: uuid.UUID,
        symbol: str,
        instrument_id: int,
        amount_usd: float,
        entry_price: float,
        open_reason: str,
        opened_at: datetime | None = None,
        direction: str = "long",
        costs: SimCosts | None = None,
    ) -> bool:
        """Apre una posizione simulata scalando il cash; False se cash insufficiente.

        Con `costs` l'entry è peggiorata di mezzo spread e la fee riduce
        l'investito: nel libro mastro specchiato degli short il verso del
        peggioramento è lo stesso (entry più alta = meno units = PnL peggiore).
        """
        if amount_usd <= 0 or entry_price <= 0:
            return False
        costs = costs or SimCosts()
        invested = amount_usd - costs.fee_usd
        if invested <= 0:
            return False
        effective_entry = entry_price * (1 + costs.spread_pct / 200.0)
        with self._sf.begin() as s:
            # FOR UPDATE: il cash è read-modify-write, senza lock di riga due
            # operazioni concorrenti sullo stesso agente si sovrascrivono.
            agent = s.get(Agent, agent_id, with_for_update=True)
            if agent is None or agent.status != "alive" or agent.cash_usd < amount_usd:
                return False
            agent.cash_usd = agent.cash_usd - amount_usd
            s.add(
                SimPosition(
                    agent_id=agent_id,
                    symbol=symbol,
                    instrument_id=instrument_id,
                    direction=direction,
                    amount_usd=amount_usd,
                    units=invested / effective_entry,
                    entry_price=entry_price,
                    open_reason=open_reason,
                    opened_at=opened_at or datetime.now(timezone.utc),
                )
            )
            return True

    def close_sim_position(
        self,
        position_id: uuid.UUID,
        close_price: float,
        close_reason: str,
        costs: SimCosts | None = None,
        now: datetime | None = None,
    ) -> float | None:
        """Chiude una posizione simulata: accredita il ricavato, registra il trade.

        Ritorna il PnL realizzato, None se la posizione non esiste. Con `costs`
        il ricavato paga mezzo spread, la fee di chiusura e — per gli short
        sintetici — il costo overnight per notte di detenzione.
        """
        costs = costs or SimCosts()
        with self._sf.begin() as s:
            # FOR UPDATE sulla posizione (una sola chiusura vince: la seconda
            # non la trova più) e poi sull'agente, prima di toccarne il cash.
            pos = s.get(SimPosition, position_id, with_for_update=True)
            if pos is None:
                return None
            proceeds = pos.units * close_price * (1 - costs.spread_pct / 200.0)
            proceeds -= costs.fee_usd
            if pos.direction == "short" and costs.short_overnight_pct_per_day > 0:
                opened = pos.opened_at
                ref_now = now or datetime.now(timezone.utc)
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
                nights = max((ref_now - opened).days, 0)
                proceeds -= (
                    float(pos.amount_usd)
                    * costs.short_overnight_pct_per_day / 100.0
                    * nights
                )
            pnl = proceeds - pos.amount_usd
            agent = s.get(Agent, pos.agent_id, with_for_update=True)
            if agent is not None:
                agent.cash_usd = agent.cash_usd + proceeds
            s.add(
                SimTrade(
                    agent_id=pos.agent_id,
                    symbol=pos.symbol,
                    direction=pos.direction,
                    amount_usd=pos.amount_usd,
                    entry_price=pos.entry_price,
                    close_price=close_price,
                    pnl_usd=pnl,
                    opened_at=pos.opened_at,
                    open_reason=pos.open_reason,
                    close_reason=close_reason,
                )
            )
            s.delete(pos)
            return pnl

    def sim_positions(self, agent_id: uuid.UUID) -> list[SimPosition]:
        with self._sf() as s:
            return list(
                s.scalars(
                    select(SimPosition)
                    .where(SimPosition.agent_id == agent_id)
                    .order_by(SimPosition.opened_at)
                )
            )

    def sim_positions_by_agent(
        self, agent_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[SimPosition]]:
        """Posizioni aperte di più agenti in una query sola.

        La pagina arena serve agenti + campione + lineage: una query per agente
        era un N+1 servito in polling.
        """
        if not agent_ids:
            return {}
        out: dict[uuid.UUID, list[SimPosition]] = {}
        with self._sf() as s:
            rows = s.scalars(
                select(SimPosition)
                .where(SimPosition.agent_id.in_(agent_ids))
                .order_by(SimPosition.opened_at)
            )
            for row in rows:
                out.setdefault(row.agent_id, []).append(row)
        return out

    def sim_trades(
        self, agent_id: uuid.UUID, limit: int | None = 200
    ) -> list[SimTrade]:
        """Trade chiusi dal più recente; `limit=None` li restituisce tutti
        (serve alle metriche, che non possono guardare solo una finestra)."""
        with self._sf() as s:
            stmt = (
                select(SimTrade)
                .where(SimTrade.agent_id == agent_id)
                .order_by(SimTrade.closed_at.desc())
            )
            if limit is not None:
                stmt = stmt.limit(limit)
            return list(s.scalars(stmt))

    def record_sim_equity(
        self, agent_id: uuid.UUID, ts: datetime, equity_usd: float
    ) -> None:
        with self._sf.begin() as s:
            s.add(SimEquityPoint(agent_id=agent_id, ts=ts, equity_usd=equity_usd))

    def sim_equity_series(
        self, agent_id: uuid.UUID, limit: int | None = 3000
    ) -> list[SimEquityPoint]:
        """Serie equity simulata in ordine di tempo; `limit` tiene i PIÙ RECENTI.

        Un punto per ciclo: circa 26 al giorno di mercato, cioè ~570 al mese.
        3000 coprono l'intera vita di un agente (un mese) con margine, e
        mettono un tetto alla route servita in polling a 15s.
        """
        with self._sf() as s:
            stmt = (
                select(SimEquityPoint)
                .where(SimEquityPoint.agent_id == agent_id)
                .order_by(SimEquityPoint.ts.desc())
            )
            if limit is not None:
                stmt = stmt.limit(limit)
            return list(reversed(list(s.scalars(stmt))))

    def last_sim_equity(self, agent_ids: list[uuid.UUID]) -> dict[uuid.UUID, float]:
        """Ultimo punto equity per agente, in una query sola.

        Serve alle route arena, che mostrano l'equity di N agenti: il valore è
        già mark-to-market (lo scrive il ciclo di training), mentre ricalcolarlo
        lì richiederebbe i prezzi correnti di tutti i simboli.
        """
        if not agent_ids:
            return {}
        with self._sf() as s:
            latest = (
                select(
                    SimEquityPoint.agent_id.label("agent_id"),
                    func.max(SimEquityPoint.ts).label("ts"),
                )
                .where(SimEquityPoint.agent_id.in_(agent_ids))
                .group_by(SimEquityPoint.agent_id)
                .subquery()
            )
            rows = s.execute(
                select(SimEquityPoint.agent_id, SimEquityPoint.equity_usd).join(
                    latest,
                    (SimEquityPoint.agent_id == latest.c.agent_id)
                    & (SimEquityPoint.ts == latest.c.ts),
                )
            ).all()
        return {row[0]: float(row[1]) for row in rows}

    def add_arena_event(self, event: str, payload: dict[str, Any] | None = None) -> None:
        with self._sf.begin() as s:
            s.add(ArenaEvent(event=event, payload=payload or {}))

    def arena_events(self, limit: int = 100) -> list[ArenaEvent]:
        with self._sf() as s:
            return list(
                s.scalars(select(ArenaEvent).order_by(ArenaEvent.ts.desc()).limit(limit))
            )

    # --- credenziali per identità Authentik -------------------------------
    def get_user_credentials(self, user_id: str) -> UserCredential | None:
        with self._sf() as s:
            return s.get(UserCredential, user_id)

    def owner_user_id(self) -> str | None:
        """Utente che possiede le chiavi eToro: usato dalle run schedulate.

        L'app è mono-utente (una sola identità Authentik configura le chiavi);
        le run automatiche non hanno una richiesta HTTP da cui dedurre
        l'identità, quindi prendono l'unico account che ha entrambe le chiavi
        eToro salvate — il più recente se per qualche motivo ce n'è più d'uno.
        """
        with self._sf() as s:
            return s.scalars(
                select(UserCredential.user_id)
                .where(
                    UserCredential.etoro_api_key_encrypted.is_not(None),
                    UserCredential.etoro_user_key_encrypted.is_not(None),
                )
                .order_by(UserCredential.updated_at.desc())
                .limit(1)
            ).first()

    def set_user_credentials(
        self,
        user_id: str,
        *,
        email: str | None,
        display_name: str | None,
        etoro_api_key_encrypted: str | None,
        etoro_user_key_encrypted: str | None,
        openai_api_key_encrypted: str | None,
    ) -> None:
        with self._sf.begin() as s:
            row = s.get(UserCredential, user_id)
            if row is None:
                row = UserCredential(user_id=user_id)
                s.add(row)
            row.email = email
            row.display_name = display_name
            row.etoro_api_key_encrypted = etoro_api_key_encrypted
            row.etoro_user_key_encrypted = etoro_user_key_encrypted
            row.openai_api_key_encrypted = openai_api_key_encrypted
            row.updated_at = datetime.now(timezone.utc)
