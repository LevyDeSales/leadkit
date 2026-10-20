"""Finalidade de receptores e autorização fixada das novas entregas PDF."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0016_plan_audiences"
down_revision = "0015_capture_delivery"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "deployment_contract",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("minimum_contract", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("phase", sa.String(16), nullable=False, server_default="blocked"),
        sa.Column("epoch", sa.Uuid()),
        sa.Column("evidence", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("attested_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("id = 1", name=op.f("ck_deployment_contract_single_row")),
        sa.CheckConstraint(
            "minimum_contract >= 0", name=op.f("ck_deployment_contract_minimum_contract")
        ),
        sa.CheckConstraint(
            "phase in ('blocked', 'verified')", name=op.f("ck_deployment_contract_phase")
        ),
        sa.CheckConstraint(
            "phase <> 'verified' OR (minimum_contract >= 1 AND epoch IS NOT NULL AND attested_at IS NOT NULL)",
            name=op.f("ck_deployment_contract_verified"),
        ),
    )
    op.execute(sa.text("INSERT INTO deployment_contract (id) VALUES (1)"))
    for table in ("webhook_destinations", "integrations"):
        op.add_column(
            table, sa.Column("purpose", sa.String(16), nullable=False, server_default="participant")
        )
        op.add_column(
            table,
            sa.Column("purpose_origin", sa.String(16), nullable=False, server_default="default"),
        )
        # Somente as linhas anteriores à migração conservam o envelope interno legado.
        op.execute(sa.text(f"UPDATE {table} SET purpose = 'internal', purpose_origin = 'legacy'"))
        op.create_check_constraint(
            op.f(f"ck_{table}_purpose"), table, "purpose in ('internal', 'participant')"
        )
        op.create_check_constraint(
            op.f(f"ck_{table}_purpose_origin"),
            table,
            "purpose_origin in ('legacy', 'explicit', 'default')",
        )
    for column in (
        sa.Column("webhook_destination_id", sa.Uuid()),
        sa.Column("config_version", sa.Integer()),
        sa.Column("audience", sa.String(16), nullable=False, server_default="internal"),
        sa.Column("authorization_origin", sa.String(16), nullable=False, server_default="legacy"),
        sa.Column("authorized_attempt", sa.Integer()),
        sa.Column("dispatch_no", sa.Integer(), nullable=False, server_default="1"),
    ):
        op.add_column("deliveries", column)
    # O identificador é histórico: excluir o destino não autoriza fallback nem apaga o fato.
    op.create_check_constraint(
        op.f("ck_deliveries_audience"), "deliveries", "audience in ('internal', 'participant')"
    )
    op.create_check_constraint(
        op.f("ck_deliveries_authorization_origin"),
        "deliveries",
        "authorization_origin in ('legacy', 'explicit')",
    )
    op.create_check_constraint(
        op.f("ck_deliveries_authorization"),
        "deliveries",
        "authorization_origin = 'legacy' AND audience = 'internal' OR authorization_origin = 'explicit' AND webhook_destination_id IS NOT NULL AND config_version IS NOT NULL AND authorized_attempt IS NOT NULL",
    )


def downgrade():
    raise RuntimeError("downgrade destrutivo não é suportado; preserve finalidades e autorizações")
