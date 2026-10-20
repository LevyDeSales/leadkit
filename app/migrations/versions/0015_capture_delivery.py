"""Perfis e outbox transacional de capturas, sem alterar as entregas PDF."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0015_capture_delivery"
down_revision = "0014_capture"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "webhook_destinations",
        sa.Column(
            "events",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[\"plano.concluido\"]'::jsonb"),
        ),
    )
    op.add_column(
        "integrations",
        sa.Column(
            "capture_mapping", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
    )
    op.add_column("integrations", sa.Column("capture_key_field_id", sa.Integer(), nullable=True))
    op.create_table(
        "capture_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("integration_id", sa.Uuid()),
        sa.Column("webhook_destination_id", sa.Uuid()),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("dispatch_no", sa.Integer(), nullable=False),
        sa.Column("hatchet_run_id", sa.String(64)),
        sa.Column("last_error", sa.String(64)),
        sa.Column("response_status", sa.Integer()),
        sa.Column("row_id", sa.Integer()),
        sa.Column("outcome", sa.String(16)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_capture_deliveries"),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submissions.id"],
            ondelete="CASCADE",
            name="fk_capture_deliveries_submission_id_submissions",
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"],
            ["integrations.id"],
            ondelete="CASCADE",
            name="fk_capture_deliveries_integration_id_integrations",
        ),
        sa.ForeignKeyConstraint(
            ["webhook_destination_id"],
            ["webhook_destinations.id"],
            ondelete="CASCADE",
            name="fk_capture_deliveries_webhook",
        ),
        sa.CheckConstraint(
            "(integration_id IS NULL) <> (webhook_destination_id IS NULL)",
            name=op.f("ck_capture_deliveries_destination"),
        ),
        sa.CheckConstraint(
            "status in ('pending', 'succeeded', 'failed')",
            name=op.f("ck_capture_deliveries_status"),
        ),
        sa.UniqueConstraint(
            "submission_id", "event", "integration_id", name="uq_capture_deliveries_integration"
        ),
        sa.UniqueConstraint(
            "submission_id", "event", "webhook_destination_id", name="uq_capture_deliveries_webhook"
        ),
    )
    for column in ("submission_id", "integration_id", "webhook_destination_id"):
        op.create_index(f"ix_capture_deliveries_{column}", "capture_deliveries", [column])
    op.create_index(
        "ix_capture_deliveries_status_updated_at", "capture_deliveries", ["status", "updated_at"]
    )


def downgrade():
    raise RuntimeError("downgrade destrutivo não é suportado; preserve capturas e entregas")
