"""Cenários sintéticos compartilhados do contrato de respostas persistidas."""

import json
from uuid import uuid4

import pytest
import sqlalchemy as sa
from test_capture import capture_document
from test_deployment_contract import evidence

from app import deployment_contract, destinations
from app.forms.starter import definition
from app.forms.stored import import_definition
from app.intake import SubmissionIn, accept_submission
from app.models import DeploymentContract


@pytest.fixture
def v2_ready(deps):
    table = DeploymentContract.__table__
    with deps.sessions() as session:
        previous = dict(session.execute(sa.select(table)).one()._mapping)
    with deps.sessions.begin() as session:
        session.get_one(DeploymentContract, 1).minimum_contract = 3
        epoch = uuid4()
        deployment_contract.begin_rollout(session, epoch=epoch)
        deployment_contract.attest_rollout(session, epoch=epoch, evidence=evidence(contract=3))
    yield deps
    with deps.sessions.begin() as session:
        session.execute(sa.update(table).where(table.c.id == 1).values(**previous))


def configure_v2(deps, generation="none"):
    document = (
        capture_document()
        if generation == "none"
        else definition(slug="persisted-pdf", title="Diagnóstico sintético")
    )
    import_definition(deps, json.dumps(document), actor="operadora")
    with deps.sessions.begin() as session:
        result = destinations.save(
            session,
            deps.cipher,
            form_slug=document["slug"],
            url="https://example.test/persisted",
            actor="operadora",
            expected_version=0,
            production=False,
            http_confirmed=False,
            purpose="internal",
            events=["submissao.persistida.v2"],
        )
    return document, result.destination.id


def accept_v2(deps, document, key="persisted-test"):
    spec = deps.forms.current(document["slug"])
    with deps.sessions.begin() as session:
        return accept_submission(
            session,
            spec,
            SubmissionIn(
                answers=document["examples"]["answers"],
                consent={"privacy": True, "text_version": spec.consent_text_version},
            ),
            idempotency_key=key,
            signer=deps.signer,
            link_ttl=deps.link_ttl,
            limits=deps.submission_limits,
            forms=deps.forms,
            cipher=deps.cipher,
        )
