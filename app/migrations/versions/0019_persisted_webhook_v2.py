"""Snapshot de respostas e emissor estável; adoção drenada do piso3."""

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0019_persisted_webhook_v2"
down_revision = "0018_journey_events"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "webhook_issuer",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("namespace", sa.Uuid(), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_webhook_issuer_single_row")),
    )
    op.execute(
        sa.text("INSERT INTO webhook_issuer VALUES (1, :namespace)").bindparams(
            namespace=uuid.uuid4()
        )
    )
    op.add_column(
        "capture_deliveries",
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column("capture_deliveries", sa.Column("payload", JSONB(), nullable=True))
    op.create_check_constraint(
        op.f("ck_capture_deliveries_envelope"),
        "capture_deliveries",
        "(schema_version = 1 AND payload IS NULL) OR "
        "(schema_version = 2 AND integration_id IS NULL AND payload IS NOT NULL "
        "AND payload @> jsonb_build_object('schema_version', 2, 'event', event, "
        "'event_id', id::text, 'submission', jsonb_build_object('id', submission_id::text), "
        "'delivery_authorization', jsonb_build_object('destination_id', "
        "webhook_destination_id::text, 'revision', config_version, 'purpose', 'internal')))",
    )
    op.execute("""
        CREATE FUNCTION preserve_webhook_issuer() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'webhook issuer is immutable'; END $$;
        CREATE TRIGGER webhook_issuer_is_immutable BEFORE UPDATE OR DELETE ON webhook_issuer
        FOR EACH ROW EXECUTE FUNCTION preserve_webhook_issuer();
        CREATE FUNCTION preserve_persisted_webhook() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'UPDATE' THEN
            IF NEW.schema_version IS DISTINCT FROM OLD.schema_version OR
              (OLD.schema_version = 2 AND
               ROW(NEW.id, NEW.submission_id, NEW.event, NEW.integration_id,
                   NEW.webhook_destination_id, NEW.config_version, NEW.payload)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.submission_id, OLD.event, OLD.integration_id,
                   OLD.webhook_destination_id, OLD.config_version, OLD.payload)) THEN
              RAISE EXCEPTION 'persisted webhook snapshot is immutable';
            END IF;
          ELSIF NEW.schema_version = 2 AND
            (NEW.payload->>'issuer') IS DISTINCT FROM
            ('leadkit:' || (SELECT namespace::text FROM webhook_issuer WHERE id = 1)) THEN
            RAISE EXCEPTION 'persisted webhook issuer is invalid';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER persisted_webhook_is_immutable BEFORE INSERT OR UPDATE ON capture_deliveries
        FOR EACH ROW EXECUTE FUNCTION preserve_persisted_webhook();
    """)
    op.execute("""
        UPDATE deployment_contract SET minimum_contract = GREATEST(minimum_contract, 3),
        phase = 'blocked', epoch = NULL, evidence = '{}'::jsonb, attested_at = NULL
        WHERE id = 1
    """)


def downgrade():
    raise RuntimeError("downgrade destrutivo não é suportado; preserve respostas e identidade")
