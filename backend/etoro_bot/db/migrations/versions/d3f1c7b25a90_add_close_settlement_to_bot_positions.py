"""Liquidazione differita del PnL di chiusura sulle posizioni del bot.

L'ordine di chiusura non è ancora in trade history quando lo inviamo: la
posizione resta marcata non liquidata (pnl_settled=False, realized_pnl_usd =
stima mark-to-market) finché una passata successiva non recupera il netProfit
reale. Le righe già chiuse sono considerate liquidate.

Revision ID: d3f1c7b25a90
Revises: c7a91e02f3d4
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d3f1c7b25a90"
down_revision = "c7a91e02f3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bot_positions",
        sa.Column("close_order_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "bot_positions",
        sa.Column(
            "pnl_settled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Lo storico già chiuso non ha nulla da riconciliare.
    op.execute(
        "UPDATE bot_positions SET pnl_settled = true WHERE closed_at IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("bot_positions", "pnl_settled")
    op.drop_column("bot_positions", "close_order_id")
