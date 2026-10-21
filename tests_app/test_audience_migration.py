"""A migração identifica legados sem inventar a autorização das novas consumidoras."""

from alembic import command
from test_baseline import config, fresh  # noqa: F401 - fixture do banco descartável da worker

from app.migrations.compatibility import schema_signature
from app.migrations.contracts import SIGNATURES


def test_migration_backfills_only_existing_destinations_and_preserves_revision(fresh):  # noqa: F811 - fixture importada
    command.upgrade(config(), "0015_capture_delivery")
    with fresh.begin() as connection:
        connection.exec_driver_sql("""
            INSERT INTO webhook_destinations
            (id, form_slug, url, secret_sealed, version, created_by, created_at, updated_by, updated_at, secret_created_at)
            VALUES ('00000000-0000-0000-0000-000000000001', NULL, 'https://example.test/hook', 'synthetic', 7, 'operadora', now(), 'operadora', now(), now())
        """)
    command.upgrade(config(), "0016_plan_audiences")
    with fresh.begin() as connection:
        assert schema_signature(connection) == SIGNATURES["0016_plan_audiences"]
        assert connection.exec_driver_sql(
            "SELECT purpose, purpose_origin, version FROM webhook_destinations"
        ).one() == ("internal", "legacy", 7)
        assert connection.exec_driver_sql(
            "SELECT minimum_contract, phase, epoch, attested_at FROM deployment_contract"
        ).one() == (1, "blocked", None, None)
        connection.exec_driver_sql("""
            INSERT INTO webhook_destinations
            (id, form_slug, url, secret_sealed, version, created_by, created_at, updated_by, updated_at, secret_created_at)
            VALUES ('00000000-0000-0000-0000-000000000002', 'new-test', 'https://example.test/new', 'synthetic', 1, 'operadora', now(), 'operadora', now(), now())
        """)
        assert connection.exec_driver_sql(
            "SELECT purpose, purpose_origin FROM webhook_destinations WHERE form_slug = 'new-test'"
        ).one() == ("participant", "default")
