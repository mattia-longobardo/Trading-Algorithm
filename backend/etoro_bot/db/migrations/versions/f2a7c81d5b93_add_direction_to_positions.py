"""Direzione persistita su posizioni live e simulate.

Prima la direzione non stava a registro: il live la rileggeva dal portafoglio
eToro a ogni ciclo (e su errore degradava in silenzio a long), la simulazione la
teneva in un prefisso "[SHORT]" dentro open_reason, testo libero scritto
dall'LLM.

Backfill: sim_positions e sim_trades sono ricostruibili dal prefisso.
bot_positions no — il portafoglio del broker è uno stato istantaneo, non uno
storico: le righe esistenti restano "long", che è il valore di gran lunga più
probabile e l'unico che lo storico non contraddice.

Revision ID: f2a7c81d5b93
Revises: e1b4a90c73df
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f2a7c81d5b93"
down_revision = "e1b4a90c73df"
branch_labels = None
depends_on = None

_TABLES = ("bot_positions", "sim_positions", "sim_trades")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "direction",
                sa.String(length=8),
                nullable=False,
                server_default=sa.text("'long'"),
            ),
        )
    # Le righe simulate portano la direzione nel testo: si recupera da lì.
    for table in ("sim_positions", "sim_trades"):
        op.execute(
            f"UPDATE {table} SET direction = 'short' "
            "WHERE open_reason LIKE '[SHORT]%'"
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "direction")
