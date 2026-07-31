"""Elimina la tabella risk_scores, mai alimentata.

Era nata per una pagina /risk e per GET /risk/score/history che non esistono
più: nessun writer, nessun reader, zero righe utili. Il modello
RiskScoreSnapshot è stato rimosso insieme alla tabella.

Il downgrade la ricrea identica allo schema iniziale (af8e44648542): resta
vuota, perché non c'è nulla da ripopolare.

Revision ID: e1b4a90c73df
Revises: d3f1c7b25a90
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e1b4a90c73df"
down_revision = "d3f1c7b25a90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("risk_scores")


def downgrade() -> None:
    op.create_table(
        "risk_scores",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column(
            "breakdown",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("date"),
    )
