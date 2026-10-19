"""Perguntas e opções condicionais (`show_when`) de ponta a ponta: a gravação das respostas, o
webhook, o envio pela página e o salvar do editor do rascunho. A definição, o avaliador e o modelo
das respostas estão em `test_form_conditions.py`; o editor das condições, em
`test_admin_condition_editor.py`."""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from conftest import publish, record_webhook
from fastapi.testclient import TestClient
from test_admin import FakeClock, login
from test_admin_destinations import ADMIN_ENV
from test_admin_drafts import TestTrigger, save
from test_admin_form_editor import stored
from test_admin_forms import import_form
from test_form_conditions import (
    CONSENT,
    DEFINITION,
    DEFINITION_TEXT,
    EMPLOYEE,
    SELF_EMPLOYED,
    SLUG,
    compiled,
)
from test_intake import submit
from test_public import text

from app import jobs, pipeline
from app.api.main import create_app
from app.deps import Deps
from app.forms import FormSpec
from app.models import Submission
from app.settings import Settings

# --- a gravação e o webhook -------------------------------------------------------------------


@pytest.fixture
def conditional(deps: Deps) -> FormSpec:
    spec = compiled()
    publish(deps, spec)
    return spec


def saved_answers(deps: Deps) -> dict[str, Any]:
    with deps.sessions() as session:
        return dict(session.scalars(sa.select(Submission.answers)).one())


def test_a_hidden_question_is_absent_from_the_stored_answers_and_an_unanswered_one_is_null(
    deps: Deps, conditional: FormSpec
) -> None:
    submit(
        deps,
        answers={**EMPLOYEE, "clientes_proprios": "nao", "carteira": "um_a_dez"},
        form=conditional,
        consent=CONSENT,
    )

    assert saved_answers(deps) == {
        "nome": "Contadora Exemplo",
        "email": "contadora@example.test",
        "whatsapp": None,  # apareceu e ficou sem resposta
        "situacao": "clt_escritorio",
        "clientes_proprios": "nao",
        # carteira, nome_escritorio, equipe e pausa_7_dias estão escondidas: ausentes
        "dor_principal": "retrabalho",
        "dores_adicionais": None,
        "resultado": "rotina",
        "pergunta": None,
    }


def test_the_stored_answers_of_a_shown_branch_keep_every_answer(
    deps: Deps, conditional: FormSpec
) -> None:
    submit(deps, answers={**EMPLOYEE, "pausa_7_dias": "trava"}, form=conditional, consent=CONSENT)

    answers = saved_answers(deps)
    assert answers["carteira"] == "um_a_dez" and answers["equipe"] == "so_eu"
    assert answers["pausa_7_dias"] == "trava" and answers["nome_escritorio"] is None
    assert "outra_dor" not in answers  # a dor principal não é "outra": escondida


def test_a_replay_with_other_answers_in_a_hidden_question_is_still_a_replay(
    deps: Deps, conditional: FormSpec
) -> None:
    first = submit(deps, key="mesma", answers=SELF_EMPLOYED, form=conditional, consent=CONSENT)
    again = submit(
        deps,
        key="mesma",
        answers={**SELF_EMPLOYED, "clientes_proprios": "sim"},
        form=conditional,
        consent=CONSENT,
    )

    assert not first.replay and again.replay  # a pergunta escondida não entra na comparação


def test_the_webhook_carries_the_answers_without_the_hidden_questions(
    deps: Deps, conditional: FormSpec
) -> None:
    webhook = record_webhook(deps)
    job = submit(
        deps, answers={**EMPLOYEE, "clientes_proprios": "nao"}, form=conditional, consent=CONSENT
    ).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "run-1")
    plan_id, _ = pipeline.generate_plan(deps, job.id, 1)
    artifact_id, _ = pipeline.render_pdf(deps, job.id, 1, plan_id)
    (delivery,) = pipeline.finalize(deps, job.id, 1, artifact_id)

    assert pipeline.deliver(deps, delivery) == "succeeded"

    (_, _, payload) = webhook.calls[0]
    assert payload["answers"]["clientes_proprios"] == "nao"
    assert not {"carteira", "equipe", "nome_escritorio", "pausa_7_dias"} & payload["answers"].keys()
    assert payload["form"]["slug"] == SLUG


def test_a_form_without_conditions_stores_its_answers_exactly_as_before(deps: Deps) -> None:
    answers = {
        "email": "contadora@example.test",
        "nome": "Contadora",
        "tamanho_equipe": "2_a_5",
        "principal_desafio": "captacao",
        "objetivo_90_dias": "Organizar a captação de clientes.",
    }
    submit(deps, answers=answers)

    assert saved_answers(deps) == {
        "email": "contadora@example.test",
        "nome": "Contadora",
        "escritorio": None,  # opcional sem resposta: segue gravada como null
        "tamanho_equipe": "2_a_5",
        "principal_desafio": "captacao",
        "objetivo_90_dias": "Organizar a captação de clientes.",
    }


# --- o envio pela página ----------------------------------------------------------------------


@pytest.fixture
def client(deps: Deps, conditional: FormSpec) -> TestClient:
    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return "run-fake"

    return TestClient(create_app(trigger=fake_trigger))


def form_post(client: TestClient, **answers: str | list[str] | None) -> Any:
    data: dict[str, Any] = {
        **EMPLOYEE,
        "_privacy": "1",
        "_consent_version": CONSENT["text_version"],
        "_idem": "chave-da-renderizacao",
        **answers,
    }
    return client.post(
        f"/p/{SLUG}", data={k: v for k, v in data.items() if v is not None}, follow_redirects=False
    )


def test_the_public_form_sends_a_conditional_path_and_drops_the_hidden_answers(
    client: TestClient, deps: Deps
) -> None:
    response = form_post(client, clientes_proprios="nao", carteira="um_a_dez", equipe="so_eu")

    assert response.status_code == 303
    answers = saved_answers(deps)
    assert (
        answers["clientes_proprios"] == "nao"
        and "carteira" not in answers
        and "equipe" not in answers
    )


def test_the_public_form_names_the_field_of_a_question_that_appeared_and_was_left_blank(
    client: TestClient,
) -> None:
    response = form_post(client, clientes_proprios=None)

    assert response.status_code == 422
    assert "Escolha uma opção para continuar." in text(response.text)


def test_the_public_form_refuses_a_hidden_option_with_a_message_in_the_field(
    client: TestClient, deps: Deps
) -> None:
    response = form_post(client, carteira="zero")

    assert response.status_code == 422
    error = re.search(r'<p class="error" id="q-carteira-erro">(.*?)</p>', response.text, re.DOTALL)
    assert error is not None
    assert "Esta opção não está disponível para as respostas anteriores" in text(error.group(1))
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Submission)) == 0


def test_the_values_typed_in_a_hidden_question_do_not_come_back_to_the_page(
    client: TestClient,
) -> None:
    # `nao` esconde a carteira e o nome do escritório; o e-mail inválido faz a página voltar.
    response = form_post(
        client, clientes_proprios="nao", nome_escritorio="Escritório Escondido", email="sem-arroba"
    )

    assert response.status_code == 422
    assert "Escritório Escondido" not in response.text


# --- o editor do rascunho ---------------------------------------------------------------------


@pytest.fixture
def admin(deps: Deps) -> TestClient:
    deps.settings = Settings(_env_file=None, **ADMIN_ENV)  # type: ignore[call-arg]
    clock = FakeClock()

    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return "run-fake"

    current = TestClient(
        create_app(trigger=fake_trigger, test_trigger=TestTrigger(deps, clock), clock=clock),
        follow_redirects=False,
    )
    assert login(current).status_code == 303
    assert import_form(current, DEFINITION_TEXT).status_code == 303
    return current


def test_saving_the_draft_in_the_editor_keeps_the_conditions_of_questions_and_options(
    admin: TestClient, deps: Deps
) -> None:
    response = save(admin, draft=f"/admin/forms/{SLUG}/draft")

    assert response.status_code == 303
    steps = stored(deps, SLUG)["steps"]
    assert steps == DEFINITION["steps"]
    assert steps[0]["questions"][5]["show_when"]["mode"] == "any"
    assert steps[0]["questions"][5]["options"][0]["show_when"]["rules"][0]["field"] == "situacao"
