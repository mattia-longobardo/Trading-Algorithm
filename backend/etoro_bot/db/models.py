"""Modelli SQLAlchemy 2 del journal (PostgreSQL 18).

Le PK surrogate delle righe append-only usano uuidv7() nativo di PG18:
ordinabili temporalmente, ideali per decisions/executions.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    environment: Mapped[str] = mapped_column(String(16))     # demo | real
    summary_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class Decision(Base):
    __tablename__ = "decisions"
    __table_args__ = (Index("ix_decisions_run_id", "run_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    symbol: Mapped[str] = mapped_column(String(32))
    stage: Mapped[str] = mapped_column(String(32))  # analyst|debate|portfolio|risk|reconcile_anomaly
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class Execution(Base):
    __tablename__ = "executions"
    __table_args__ = (
        Index("ix_executions_run_id", "run_id"),
        Index("ix_executions_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))             # buy | sell
    amount_usd: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16))          # filled|failed|skipped|rejected
    detail: Mapped[str] = mapped_column(Text, default="")
    execution_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    etoro_position_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class BotPosition(Base):
    """Registry delle SOLE posizioni aperte dal bot (§7): l'unica fonte per
    dashboard, backtest e risk score. Le posizioni manuali non entrano mai."""

    __tablename__ = "bot_positions"
    __table_args__ = (Index("ix_bot_positions_closed_at", "closed_at"),)

    etoro_position_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    symbol: Mapped[str] = mapped_column(String(32))
    instrument_id: Mapped[int] = mapped_column(Integer)
    amount_usd: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sector: Mapped[str] = mapped_column(String(64), default="unknown")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Ordine di chiusura inviato al broker: il PnL reale arriva in trade history
    # con ritardo, quindi la chiusura resta "non liquidata" (pnl_settled=False)
    # finché una passata successiva non la riconcilia. Fino ad allora
    # realized_pnl_usd contiene la STIMA mark-to-market.
    close_order_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pnl_settled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    equity_usd: Mapped[float] = mapped_column(Float)
    cash_usd: Mapped[float] = mapped_column(Float)
    exposure_usd: Mapped[float] = mapped_column(Float)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )


class SettingsAudit(Base):
    __tablename__ = "settings_audit"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    key: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="api")


class Agent(Base):
    """Agente evolutivo dell'arena: DNA (parametri), memoria e conto simulato.

    status: alive | dead. is_champion marca il vincitore dell'ultimo mese
    completato (resta True anche da vivo mentre continua ad allenarsi);
    è il DNA usato dal trading live.
    """

    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    name: Mapped[str] = mapped_column(String(32))
    generation: Mapped[int] = mapped_column(Integer)
    dna: Mapped[dict] = mapped_column(JSONB)
    memory: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="alive")   # alive | dead
    is_champion: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    born_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    died_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    death_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    month: Mapped[str] = mapped_column(String(7))            # "YYYY-MM" del ciclo di vita
    starting_capital_usd: Mapped[float] = mapped_column(Float)
    cash_usd: Mapped[float] = mapped_column(Float)


class SimPosition(Base):
    """Posizione aperta del conto simulato di un agente."""

    __tablename__ = "sim_positions"
    __table_args__ = (Index("ix_sim_positions_agent_id", "agent_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"))
    symbol: Mapped[str] = mapped_column(String(32))
    instrument_id: Mapped[int] = mapped_column(Integer)
    amount_usd: Mapped[float] = mapped_column(Float)
    units: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    open_reason: Mapped[str] = mapped_column(Text, default="")


class SimTrade(Base):
    """Trade simulato chiuso (round-trip) di un agente."""

    __tablename__ = "sim_trades"
    __table_args__ = (Index("ix_sim_trades_agent_id", "agent_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"))
    symbol: Mapped[str] = mapped_column(String(32))
    amount_usd: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    close_price: Mapped[float] = mapped_column(Float)
    pnl_usd: Mapped[float] = mapped_column(Float)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    open_reason: Mapped[str] = mapped_column(Text, default="")
    close_reason: Mapped[str] = mapped_column(Text, default="")


class SimEquityPoint(Base):
    """Serie equity intraday/giornaliera del conto simulato."""

    __tablename__ = "sim_equity"
    __table_args__ = (Index("ix_sim_equity_agent_id", "agent_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    equity_usd: Mapped[float] = mapped_column(Float)


class ArenaEvent(Base):
    """Log evolutivo dell'arena: nascite, morti, valutazioni, pause."""

    __tablename__ = "arena_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    event: Mapped[str] = mapped_column(String(32))  # birth|death|evolution|pause|resume|live_on|live_off
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class UserCredential(Base):
    """Credenziali broker/LLM cifrate e isolate per identità Authentik."""

    __tablename__ = "user_credentials"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    etoro_api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    etoro_user_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    openai_api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )
