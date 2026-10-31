"""Vínculo aditivo do comando de teste, sem conteúdo nem FKs que prendam o legado."""

import sqlalchemy as sa
from alembic import op

revision = "0020_draft_test_commands"
down_revision = "0019_persisted_webhook_v2"
branch_labels = None
depends_on = None


def upgrade():
    columns = [sa.Column("command_id", sa.String(128), primary_key=True)]
    for name in ("namespace", "form_id", "test_id", "audit_id"):
        columns.append(sa.Column(name, sa.Uuid(), nullable=False))
    for name in ("form_slug", "expected_version", "principal_id"):
        columns.append(sa.Column(name, sa.Text(), nullable=False))
    for name in ("input_digest", "effective_digest"):
        columns.append(sa.Column(name, sa.String(64), nullable=False))
    for name in ("principal_kind", "channel", "dispatch_state"):
        columns.append(sa.Column(name, sa.String(16), nullable=False))
    for name in ("issued_at", "committed_at", "expires_at", "dispatch_at"):
        columns.append(sa.Column(name, sa.DateTime(timezone=True), nullable=name == "dispatch_at"))
    op.create_table(
        "draft_test_commands",
        *columns,
        sa.CheckConstraint(
            "principal_kind in ('admin', 'api_key')",
            name=op.f("ck_draft_test_commands_principal_kind"),
        ),
        sa.CheckConstraint(
            "dispatch_state in ('committed', 'dispatching', 'enqueued', 'not_started', 'uncertain')",
            name=op.f("ck_draft_test_commands_dispatch_state"),
        ),
    )
    op.create_index("ix_draft_test_commands_expires_at", "draft_test_commands", ["expires_at"])


def downgrade():
    raise RuntimeError("downgrade destrutivo não é suportado; preserve os vínculos de teste")
