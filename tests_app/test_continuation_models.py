"""Contrato compartilhado de persistência, exercitado no PostgreSQL real."""

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app import models
from app.db import make_engine


@pytest.fixture
def contract_session(database_url):
    engine = make_engine(database_url)
    with engine.connect() as connection:
        transaction = connection.begin()
        schema = "continuation_contract"
        connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
        isolated = connection.execution_options(schema_translate_map={None: schema})
        models.Base.metadata.create_all(isolated)
        with Session(isolated) as session:
            yield session
        transaction.rollback()
    engine.dispose()


def test_receiver_scope_and_credential_expiry_are_enforced(contract_session):
    session = contract_session
    form = models.Form(slug="synthetic", title="Sintético", created_by="teste")
    session.add(form)
    session.flush()
    receiver = models.HandoffReceiver(
        form_slug=form.slug,
        name="Atendimento",
        entry_url="https://example.test/continuar",
        reference_ttl_seconds=3600,
        created_by="teste",
        updated_by="teste",
    )
    session.add(receiver)
    session.flush()
    credential = models.IntegrationCredential(
        key_id=uuid.uuid4().hex,
        name="Serviço",
        secret_hash="a" * 64,
        receiver_id=receiver.id,
        receiver_revision=1,
        form_slug=form.slug,
        created_by="teste",
        expires_at=None,
        operation="create",
        idempotency_hash="b" * 64,
        command_fingerprint="c" * 64,
    )
    session.add(credential)
    session.flush()
    assert credential.expires_at is None
    with pytest.raises(sa.exc.IntegrityError), session.begin_nested():
        credential.form_slug = "another-form"
        session.flush()
