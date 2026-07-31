"""Tabelle dell'arena evolutiva: agenti, conto simulato, eventi.

Revision ID: c7a91e02f3d4
Revises: b54d132da6a1
Create Date: 2026-07-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "c7a91e02f3d4"
down_revision = "b54d132da6a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuidv7()")),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("dna", JSONB(), nullable=False),
        sa.Column("memory", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="alive"),
        sa.Column("is_champion", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("parent_id", UUID(as_uuid=True), nullable=True),
        sa.Column("born_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("died_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("death_reason", sa.String(128), nullable=True),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("starting_capital_usd", sa.Float(), nullable=False),
        sa.Column("cash_usd", sa.Float(), nullable=False),
    )
    op.create_table(
        "sim_positions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuidv7()")),
        sa.Column("agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("amount_usd", sa.Float(), nullable=False),
        sa.Column("units", sa.Float(), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("open_reason", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_sim_positions_agent_id", "sim_positions", ["agent_id"])
    op.create_table(
        "sim_trades",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuidv7()")),
        sa.Column("agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("amount_usd", sa.Float(), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("close_price", sa.Float(), nullable=False),
        sa.Column("pnl_usd", sa.Float(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("open_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("close_reason", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_sim_trades_agent_id", "sim_trades", ["agent_id"])
    op.create_table(
        "sim_equity",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuidv7()")),
        sa.Column("agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("equity_usd", sa.Float(), nullable=False),
    )
    op.create_index("ix_sim_equity_agent_id", "sim_equity", ["agent_id"])
    op.create_table(
        "arena_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuidv7()")),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("payload", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_table("arena_events")
    op.drop_index("ix_sim_equity_agent_id", table_name="sim_equity")
    op.drop_table("sim_equity")
    op.drop_index("ix_sim_trades_agent_id", table_name="sim_trades")
    op.drop_table("sim_trades")
    op.drop_index("ix_sim_positions_agent_id", table_name="sim_positions")
    op.drop_table("sim_positions")
    op.drop_table("agents")
