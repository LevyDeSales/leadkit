"""Evidência do PDF validado; históricos continuam sem evidência presumida."""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("artifacts", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("artifacts", sa.Column("storage_revision", sa.Text(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("downgrade bloqueado: migrations de produto são aditivas")
