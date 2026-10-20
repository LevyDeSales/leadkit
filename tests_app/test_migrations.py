"""Constraints e compatibilidade dos dados na baseline pública.

Instalação vazia, guard e ausência de seeds são provados em test_baseline.py. Os testes da
cadeia histórica original ficam nos gates privados da ponte, fora da distribuição.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from conftest import DEMO_ANSWERS, LEGACY_SLUGS, legacy_spec, publish

from app import jobs, pipeline
from app.deps import Deps
from app.forms import FormSpec
from app.models import (
    ApiKey,
    Form,
    FormAsset,
    FormVersion,
    FormVersionAsset,
    Integration,
    IntegrationSync,
    Job,
    Lead,
    Plan,
    Submission,
    utcnow,
)
from app.tokens import new_public_id

MIGRATIONS = Path(__file__).resolve().parents[1] / "app" / "migrations"


def alembic() -> Config:
    """As migrations sem o `alembic.ini`, cujo `fileConfig` refaria o logging no meio da suíte."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    return config


def send_like_the_previous_code(deps: Deps) -> uuid.UUID:
    """Um envio do demo gravado como o código anterior à migration 0003 grava: sem a versão do
    formulário. Devolve o id do pedido."""
    answers = legacy_spec("demo").answers_model.model_validate(DEMO_ANSWERS)
    with deps.sessions.begin() as session:
        lead_id = session.execute(
            sa.insert(Lead.__table__)
            .values(email="contadora@example.test", name="Contadora Exemplo")
            .returning(Lead.__table__.c.id)
        ).scalar_one()
        submission_id = session.execute(
            sa.insert(Submission.__table__)
            .values(
                lead_id=lead_id,
                form_slug="demo",
                form_version="demo-2026.10",
                idempotency_key="envio-antigo",
                answers=answers.model_dump(mode="json"),
                consent={"privacy_at": utcnow().isoformat(), "text_version": "demo-v1"},
            )
            .returning(Submission.__table__.c.id)
        ).scalar_one()
        return session.execute(
            sa.insert(Job.__table__)
            .values(
                submission_id=submission_id,
                public_id=new_public_id(),
                expires_at=utcnow() + timedelta(days=30),
            )
            .returning(Job.__table__.c.id)
        ).scalar_one()


def prompt_version_of_the_plan(deps: Deps, job_id: uuid.UUID) -> str:
    """Roda o passo do plano, como o worker, e devolve a versão do prompt que o escreveu."""
    with deps.sessions.begin() as session:
        jobs.claim(session, job_id, 1, "run-1")
    plan_id, _ = pipeline.generate_plan(deps, job_id, 1)
    with deps.sessions() as session:
        return session.get_one(Plan, plan_id).prompt_version


def test_omitted_version_number_is_one_and_upgrade_keeps_its_plan_version(
    deps: Deps, newer_demo: FormSpec
) -> None:
    job_id = send_like_the_previous_code(deps)
    with deps.sessions() as session:
        job = session.get_one(Job, job_id)
        assert session.get_one(Submission, job.submission_id).form_version_number == 1
    command.upgrade(alembic(), "head")
    publish(deps, newer_demo)

    assert prompt_version_of_the_plan(deps, job_id) == "demo-v1"


def test_an_older_submission_ignores_a_newly_published_current_version(
    deps: Deps, newer_demo: FormSpec
) -> None:
    job_id = send_like_the_previous_code(deps)  # o banco já migrou; os containers, ainda não
    publish(deps, newer_demo)

    assert prompt_version_of_the_plan(deps, job_id) == "demo-v1"


# --- versões publicadas -----------------------------------------------------------------------


def test_a_published_version_never_changes(deps: Deps) -> None:
    """O banco mantém imutáveis as versões publicadas, inclusive quando há upgrade no-op."""
    with (
        pytest.raises(sa.exc.DBAPIError, match="não muda nem sai"),
        deps.sessions.begin() as session,
    ):
        session.execute(sa.update(FormVersion).values(note="mudei"))


# --- prazos de retenção -----------------------------------------------------------------------


def periods_of(deps: Deps) -> dict[str, tuple[int | None, int | None]]:
    with deps.sessions() as session:
        rows = session.execute(sa.select(Form.slug, Form.retention_pdf_days, Form.retention_days))
        return {slug: (pdf, request) for slug, pdf, request in rows}


def test_forms_without_retention_overrides_follow_the_environment(
    deps: Deps,
) -> None:
    """Sem prazo próprio, as versões existentes seguem o ambiente mesmo após upgrade no-op."""
    command.upgrade(alembic(), "head")

    assert periods_of(deps) == {slug: (None, None) for slug in LEGACY_SLUGS}


def test_the_database_refuses_a_retention_period_below_one_day(deps: Deps) -> None:
    for column in ("retention_pdf_days", "retention_days"):
        with (
            pytest.raises(sa.exc.IntegrityError, match="ck_forms_retention"),
            deps.sessions.begin() as session,
        ):
            session.execute(sa.update(Form).where(Form.slug == "demo").values({column: 0}))


# --- integrações e sincronizações -------------------------------------------------------------


def new_integration(**changes: object) -> Integration:
    now = utcnow()
    values: dict[str, object] = {
        "form_slug": "demo",
        "kind": "baserow",
        "base_url": "https://baserow.example.test",
        "table_id": 7,
        "token_sealed": "cifrado",
        "created_by": "testes",
        "updated_by": "testes",
        "created_at": now,
        "updated_at": now,
        "token_changed_at": now,
        **changes,
    }
    return Integration(**values, purpose="internal")


def completed_like_the_previous_code(deps: Deps) -> tuple[uuid.UUID, uuid.UUID]:
    """Um pedido do demo concluído até o fim do pipeline; devolve o pedido e o PDF dele."""
    job_id = send_like_the_previous_code(deps)
    with deps.sessions.begin() as session:
        jobs.claim(session, job_id, 1, "run-1")
    plan_id, _ = pipeline.generate_plan(deps, job_id, 1)
    artifact_id, _ = pipeline.render_pdf(deps, job_id, 1, plan_id)
    return job_id, artifact_id


def test_default_version_submission_completes_without_an_integration(
    deps: Deps,
) -> None:
    """Sem integração configurada, concluir um pedido não cria sincronização."""
    command.upgrade(alembic(), "head")
    job_id, artifact_id = completed_like_the_previous_code(deps)

    pipeline.finalize(deps, job_id, 1, artifact_id)

    with deps.sessions() as session:
        assert session.get_one(Job, job_id).status == "completed"
        assert session.scalar(sa.select(sa.func.count()).select_from(IntegrationSync)) == 0


def test_the_database_allows_one_integration_of_each_kind_per_form(deps: Deps) -> None:
    with deps.sessions.begin() as session:
        session.add(new_integration())
        session.add(new_integration(form_slug="mcc"))
    with (
        pytest.raises(sa.exc.IntegrityError, match="uq_integrations_form_slug_kind"),
        deps.sessions.begin() as session,
    ):
        session.add(new_integration())


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [({"kind": "planilha"}, "ck_integrations_kind"), ({"table_id": 0}, "table_id")],
)
def test_the_database_refuses_an_unknown_kind_and_a_table_below_one(
    deps: Deps, changes: dict[str, object], constraint: str
) -> None:
    with (
        pytest.raises(sa.exc.IntegrityError, match=constraint),
        deps.sessions.begin() as session,
    ):
        session.add(new_integration(**changes))


def test_there_is_one_sync_per_pdf_and_the_syncs_follow_the_integration(deps: Deps) -> None:
    job_id, artifact_id = completed_like_the_previous_code(deps)
    with deps.sessions.begin() as session:
        integration = new_integration()
        session.add(integration)
        session.flush()
        integration_id = integration.id
        session.add(
            IntegrationSync(integration_id=integration_id, job_id=job_id, artifact_id=artifact_id)
        )
    with (
        pytest.raises(sa.exc.IntegrityError, match="uq_integration_syncs_artifact"),
        deps.sessions.begin() as session,
    ):
        session.add(
            IntegrationSync(integration_id=integration_id, job_id=job_id, artifact_id=artifact_id)
        )
    with (
        pytest.raises(sa.exc.IntegrityError, match="ck_integration_syncs_status"),
        deps.sessions.begin() as session,
    ):
        session.execute(sa.update(IntegrationSync).values(status="sincronizando"))

    with deps.sessions.begin() as session:  # apagar a integração leva o estado das sincronizações
        session.execute(sa.delete(Integration))
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(IntegrationSync)) == 0
        assert session.get(Job, job_id) is not None


# --- imagens dos formulários -----------------------------------------------------------------


def test_upgrade_does_not_invent_images_or_replace_existing_versions(deps: Deps) -> None:
    command.upgrade(alembic(), "head")

    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(FormAsset)) == 0
        assert session.scalar(sa.select(sa.func.count()).select_from(FormVersionAsset)) == 0
        assert session.scalar(sa.select(sa.func.count()).select_from(FormVersion)) == 3


def add_asset(session: sa.orm.Session, name: str, form_id: uuid.UUID | None = None) -> None:
    now = utcnow()
    session.add(
        FormAsset(
            form_id=form_id,
            name=name,
            content_type="image/png",
            byte_size=10,
            source_size=20,
            width=1,
            height=1,
            sha256="0" * 64,
            storage_key="assets/x.png",
            created_by="testes",
            created_at=now,
            updated_by="testes",
            updated_at=now,
        )
    )
    session.flush()


def test_the_library_does_not_accept_two_images_with_the_same_name(deps: Deps) -> None:
    with deps.sessions.begin() as session:
        add_asset(session, "logo")
    with (
        pytest.raises(sa.exc.IntegrityError, match="form_assets_library_name"),
        deps.sessions.begin() as session,
    ):
        add_asset(session, "logo")


def test_a_form_has_its_own_names_apart_from_the_library_and_from_other_forms(deps: Deps) -> None:
    with deps.sessions.begin() as session:
        demo, mcc = (
            session.scalar(sa.select(Form.id).where(Form.slug == slug)) for slug in ("demo", "mcc")
        )
        add_asset(session, "logo")  # a biblioteca
        add_asset(session, "logo", demo)
        add_asset(session, "logo", mcc)
    with (
        pytest.raises(sa.exc.IntegrityError, match="uq_form_assets_form_id_name"),
        deps.sessions.begin() as session,
    ):
        add_asset(session, "logo", demo)


def test_the_database_refuses_an_image_type_the_admin_does_not_accept(deps: Deps) -> None:
    with (
        pytest.raises(sa.exc.IntegrityError, match="ck_form_assets_content_type"),
        deps.sessions.begin() as session,
    ):
        add_asset(session, "logo")
        session.execute(sa.update(FormAsset).values(content_type="image/svg+xml"))


def test_an_image_a_published_version_pins_cannot_be_deleted(deps: Deps) -> None:
    with deps.sessions.begin() as session:
        add_asset(session, "logo")
        asset_id = session.scalar(sa.select(FormAsset.id))
        version_id = session.scalar(sa.select(FormVersion.id).limit(1))
        session.add(
            FormVersionAsset(
                form_version_id=version_id,
                name="logo",
                asset_id=asset_id,
                sha256="0" * 64,
                content_type="image/png",
            )
        )
    with (
        pytest.raises(sa.exc.IntegrityError, match="fk_form_version_assets_asset_id"),
        deps.sessions.begin() as session,
    ):
        session.execute(sa.delete(FormAsset))


# --- chaves de API ----------------------------------------------------------------------------


def add_api_key(session: sa.orm.Session, name: str, key_id: str = "0a1b2c3d") -> None:
    session.add(
        ApiKey(
            key_id=key_id,
            name=name,
            role="leitura",
            secret_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
            created_by="testes",
        )
    )
    session.flush()


def test_the_database_does_not_accept_two_keys_with_the_same_name_or_the_same_id(
    deps: Deps,
) -> None:
    with deps.sessions.begin() as session:
        add_api_key(session, "backup-noturno")
    with (
        pytest.raises(sa.exc.IntegrityError, match="uq_api_keys_name"),
        deps.sessions.begin() as session,
    ):
        add_api_key(session, "backup-noturno", key_id="ffffffff")
    with (
        pytest.raises(sa.exc.IntegrityError, match="uq_api_keys_key_id"),
        deps.sessions.begin() as session,
    ):
        add_api_key(session, "outra-chave")


def test_the_database_refuses_a_role_the_api_does_not_know(deps: Deps) -> None:
    with (
        pytest.raises(sa.exc.IntegrityError, match="ck_api_keys_role"),
        deps.sessions.begin() as session,
    ):
        add_api_key(session, "backup-noturno")
        session.execute(sa.update(ApiKey).values(role="administrador"))
