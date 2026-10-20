"""Recibos de capturas sem geração; preserva as tabelas e os contratos PDF."""

import sqlalchemy as sa
from alembic import op

revision = "0014_capture"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "capture_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("public_id", sa.String(32), nullable=False),
        sa.Column("token_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submissions.id"],
            ondelete="CASCADE",
            name="fk_capture_receipts_submission_id_submissions",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_capture_receipts"),
        sa.UniqueConstraint("submission_id", name="uq_capture_receipts_submission_id"),
        sa.UniqueConstraint("public_id", name="uq_capture_receipts_public_id"),
    )


def downgrade():
    raise RuntimeError(
        "downgrade destrutivo não é suportado; preserve capturas no rollback de runtime"
    )
