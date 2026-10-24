"""Seleção explícita e compatibilidade local do envio de respostas."""

import pytest
from persisted_webhook_support import configure_v2
from persisted_webhook_support import v2_ready as v2_ready  # noqa: PLC0414

from app import destinations
from app.integration_readiness import problems


@pytest.mark.parametrize("generation", ["none", "pdf"])
def test_v2_only_is_ready_for_capture_and_pdf(v2_ready, generation):
    document, _ = configure_v2(v2_ready, generation)
    with v2_ready.sessions() as session:
        assert problems(session, v2_ready.forms.current(document["slug"])) == []


@pytest.mark.parametrize(
    "scope,purpose,events",
    [
        (None, "internal", ["submissao.persistida.v2"]),
        ("demo", "participant", ["submissao.persistida.v2"]),
        ("demo", "internal", ["submissao.persistida", "submissao.persistida.v2"]),
    ],
)
def test_v2_refuses_default_participant_and_duplicate_version(v2_ready, scope, purpose, events):
    with (
        v2_ready.sessions.begin() as session,
        pytest.raises((destinations.InvalidEvents, destinations.InvalidPurpose)),
    ):
        destinations.save(
            session,
            v2_ready.cipher,
            form_slug=scope,
            url="https://example.test/hook",
            actor="operadora",
            expected_version=0,
            production=False,
            http_confirmed=False,
            purpose=purpose,
            events=events,
        )


def test_v2_configuration_waits_for_verified_runtime(v2_ready):
    from uuid import uuid4

    import sqlalchemy as sa

    from app.deployment_contract import begin_rollout
    from app.models import WebhookDestination

    with v2_ready.sessions.begin() as session:
        begin_rollout(session, epoch=uuid4())
    with pytest.raises(destinations.InvalidEvents, match="atualização"):
        configure_v2(v2_ready, "pdf")
    with v2_ready.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(WebhookDestination)) == 0
