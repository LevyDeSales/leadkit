"""Baseline estrutural Leadkit; sem importação ou publicação de formulários.

A raiz pública tem o mesmo marcador do schema compatível, sem reescrever a cadeia privada.
O guard online compara o catálogo completo antes de qualquer migration, inclusive no-op.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = None
branch_labels = None
depends_on = None


IMMUTABLE_FUNCTION = """
CREATE FUNCTION form_versions_are_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'form_versions: uma versão publicada não muda nem sai (%)', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$
"""

IMMUTABLE_TRIGGER = """
CREATE TRIGGER form_versions_are_immutable
    BEFORE UPDATE OR DELETE ON form_versions
    FOR EACH ROW EXECUTE FUNCTION form_versions_are_immutable()
"""


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index(op.f("ix_audit_log_at"), "audit_log", ["at"], unique=False)
    op.create_index(op.f("ix_audit_log_target_id"), "audit_log", ["target_id"], unique=False)
    op.create_table(
        "leads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("first_touch", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leads")),
        sa.UniqueConstraint("email", name=op.f("uq_leads_email")),
    )
    op.create_table(
        "submissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("form_slug", sa.String(length=64), nullable=False),
        sa.Column("form_version", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("answers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("consent", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tracking", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scores", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=True),
        sa.Column("persona", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name=op.f("fk_submissions_lead_id_leads")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_submissions")),
        sa.UniqueConstraint("form_slug", "idempotency_key", name="uq_submissions_form_idempotency"),
    )
    op.create_index(op.f("ix_submissions_lead_id"), "submissions", ["lead_id"], unique=False)
    op.create_index(op.f("ix_submissions_priority"), "submissions", ["priority"], unique=False)
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("stage", sa.String(length=16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("token_version", sa.Integer(), nullable=False),
        sa.Column("hatchet_run_id", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=40), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "stage in ('queued', 'generating', 'rendering', 'completed', 'failed')",
            name=op.f("ck_jobs_stage"),
        ),
        sa.CheckConstraint(
            "status in ('queued', 'running', 'completed', 'failed')", name=op.f("ck_jobs_status")
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"], ["submissions.id"], name=op.f("fk_jobs_submission_id_submissions")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint("public_id", name=op.f("uq_jobs_public_id")),
        sa.UniqueConstraint("submission_id", name=op.f("uq_jobs_submission_id")),
    )
    op.create_index("ix_jobs_status_created_at", "jobs", ["status", "created_at"], unique=False)
    op.create_table(
        "plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name=op.f("fk_plans_job_id_jobs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plans")),
        sa.UniqueConstraint("job_id", "version", name="uq_plans_job_version"),
    )
    op.create_index(op.f("ix_plans_job_id"), "plans", ["job_id"], unique=False)
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("renderer", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name=op.f("fk_artifacts_job_id_jobs")),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"], name=op.f("fk_artifacts_plan_id_plans")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifacts")),
        sa.UniqueConstraint("job_id", "kind", "version", name="uq_artifacts_job_kind_version"),
        sa.UniqueConstraint("storage_key", name=op.f("uq_artifacts_storage_key")),
    )
    op.create_index(op.f("ix_artifacts_job_id"), "artifacts", ["job_id"], unique=False)
    op.create_table(
        "deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("destination", sa.String(length=64), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("hatchet_run_id", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status in ('pending', 'succeeded', 'failed')", name=op.f("ck_deliveries_status")
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["artifacts.id"], name=op.f("fk_deliveries_artifact_id_artifacts")
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name=op.f("fk_deliveries_job_id_jobs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deliveries")),
        sa.UniqueConstraint(
            "artifact_id", "destination", name="uq_deliveries_artifact_destination"
        ),
    )
    op.create_index(op.f("ix_deliveries_job_id"), "deliveries", ["job_id"], unique=False)
    op.create_index(
        "ix_deliveries_status_created_at", "deliveries", ["status", "created_at"], unique=False
    )
    op.add_column("artifacts", sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "submissions",
        sa.Column("form_version_number", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.create_table(
        "webhook_destinations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("form_slug", sa.String(length=64), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("secret_sealed", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("secret_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_destinations")),
        sa.UniqueConstraint("form_slug", name="uq_webhook_destinations_form_slug"),
    )
    # No máximo um destino padrão (form_slug nulo); o Postgres 14 ainda não tem NULLS NOT DISTINCT.
    op.create_index(
        "uq_webhook_destinations_default",
        "webhook_destinations",
        [sa.text("(form_slug IS NULL)")],
        unique=True,
        postgresql_where=sa.text("form_slug IS NULL"),
    )
    op.create_table(
        "webhook_env_seed",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_webhook_env_seed_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_env_seed")),
    )
    # Nulas e sem default: instantâneas no Postgres e inofensivas para o código anterior.
    op.add_column("deliveries", sa.Column("destination_scope", sa.String(16), nullable=True))
    op.add_column("deliveries", sa.Column("destination_host", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_deliveries_destination_scope"),
        "deliveries",
        "destination_scope in ('form', 'default')",
    )
    op.create_table(
        "forms",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_forms")),
        sa.UniqueConstraint("slug", name=op.f("uq_forms_slug")),
    )
    op.create_table(
        "form_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("form_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("published_by", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("number >= 1", name=op.f("ck_form_versions_number")),
        sa.ForeignKeyConstraint(
            ["form_id"], ["forms.id"], name=op.f("fk_form_versions_form_id_forms")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_form_versions")),
        sa.UniqueConstraint("form_id", "number", name="uq_form_versions_form_number"),
    )
    op.execute(IMMUTABLE_FUNCTION)
    op.execute(IMMUTABLE_TRIGGER)
    op.create_table(
        "form_drafts",
        sa.Column("form_id", sa.Uuid(), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tested_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("base_version >= 1", name=op.f("ck_form_drafts_base_version")),
        sa.ForeignKeyConstraint(
            ["form_id"], ["forms.id"], name=op.f("fk_form_drafts_form_id_forms")
        ),
        sa.PrimaryKeyConstraint("form_id", name=op.f("pk_form_drafts")),
    )
    op.create_table(
        "draft_tests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("form_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_by", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("simulated_date", sa.Date(), nullable=True),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("definition_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("llm_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("answers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("llm_ms", sa.Integer(), nullable=True),
        sa.Column("pdf_ms", sa.Integer(), nullable=True),
        sa.Column("pdf", sa.LargeBinary(), nullable=True),
        sa.Column("pdf_pages", sa.Integer(), nullable=True),
        sa.Column("failure_code", sa.String(length=40), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status in ('queued', 'running', 'succeeded', 'failed')",
            name=op.f("ck_draft_tests_status"),
        ),
        sa.ForeignKeyConstraint(
            ["form_id"], ["forms.id"], name=op.f("fk_draft_tests_form_id_forms")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_draft_tests")),
    )
    op.create_index("ix_draft_tests_form_id_started_at", "draft_tests", ["form_id", "started_at"])
    # Um teste por vez em cada formulário, também entre processos: o segundo clique bate aqui.
    op.create_index(
        "uq_draft_tests_in_progress",
        "draft_tests",
        ["form_id"],
        unique=True,
        postgresql_where=sa.text("status in ('queued', 'running')"),
    )
    op.alter_column("form_drafts", "base_version", existing_type=sa.Integer(), nullable=True)
    op.add_column("forms", sa.Column("retention_pdf_days", sa.Integer(), nullable=True))
    op.add_column("forms", sa.Column("retention_days", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_forms_retention_pdf_days"),
        "forms",
        "retention_pdf_days IS NULL OR retention_pdf_days >= 1",
    )
    op.create_check_constraint(
        op.f("ck_forms_retention_days"),
        "forms",
        "retention_days IS NULL OR retention_days >= 1",
    )
    op.create_table(
        "integrations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("form_slug", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("table_id", sa.Integer(), nullable=False),
        sa.Column("token_sealed", sa.Text(), nullable=False),
        sa.Column(
            "mapping",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("key_field_id", sa.Integer(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("token_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind in ('baserow')", name=op.f("ck_integrations_kind")),
        sa.CheckConstraint("table_id >= 1", name=op.f("ck_integrations_table_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integrations")),
        sa.UniqueConstraint("form_slug", "kind", name="uq_integrations_form_slug_kind"),
    )
    op.create_table(
        "integration_syncs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("hatchet_run_id", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.Column("row_id", sa.Integer(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status in ('pending', 'succeeded', 'failed')",
            name=op.f("ck_integration_syncs_status"),
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.id"],
            name=op.f("fk_integration_syncs_artifact_id_artifacts"),
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"],
            ["integrations.id"],
            name=op.f("fk_integration_syncs_integration_id_integrations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_integration_syncs_job_id_jobs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_syncs")),
        sa.UniqueConstraint("integration_id", "artifact_id", name="uq_integration_syncs_artifact"),
    )
    op.create_index(
        op.f("ix_integration_syncs_integration_id"),
        "integration_syncs",
        ["integration_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_integration_syncs_job_id"), "integration_syncs", ["job_id"], unique=False
    )
    op.create_index(
        "ix_integration_syncs_status_created_at",
        "integration_syncs",
        ["status", "created_at"],
        unique=False,
    )
    op.create_table(
        "form_assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("form_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=48), nullable=False),
        sa.Column("content_type", sa.String(length=32), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("source_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "content_type in ('image/png', 'image/jpeg', 'image/webp')",
            name=op.f("ck_form_assets_content_type"),
        ),
        sa.CheckConstraint("byte_size >= 1", name=op.f("ck_form_assets_byte_size")),
        sa.ForeignKeyConstraint(
            ["form_id"], ["forms.id"], name=op.f("fk_form_assets_form_id_forms")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_form_assets")),
        sa.UniqueConstraint("form_id", "name", name="uq_form_assets_form_id_name"),
    )
    op.create_index(
        "uq_form_assets_library_name",
        "form_assets",
        ["name"],
        unique=True,
        postgresql_where=sa.text("form_id IS NULL"),
    )
    op.create_index("ix_form_assets_sha256", "form_assets", ["sha256"])
    op.create_table(
        "form_version_assets",
        sa.Column("form_version_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=48), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["form_assets.id"],
            name=op.f("fk_form_version_assets_asset_id_form_assets"),
        ),
        sa.ForeignKeyConstraint(
            ["form_version_id"],
            ["form_versions.id"],
            name=op.f("fk_form_version_assets_form_version_id_form_versions"),
        ),
        sa.PrimaryKeyConstraint("form_version_id", "name", name=op.f("pk_form_version_assets")),
    )
    op.create_index(op.f("ix_form_version_assets_asset_id"), "form_version_assets", ["asset_id"])
    op.create_index("ix_form_version_assets_sha256", "form_version_assets", ["sha256"])
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key_id", sa.String(length=8), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.Text(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role in ('leitura', 'total')", name=op.f("ck_api_keys_role")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("key_id", name=op.f("uq_api_keys_key_id")),
        sa.UniqueConstraint("name", name=op.f("uq_api_keys_name")),
    )


def downgrade() -> None:
    raise RuntimeError("rollback operacional exige voltar a imagem, sem downgrade da baseline")
