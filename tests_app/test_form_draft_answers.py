"""As respostas de um teste do rascunho como objeto (`drafts.assemble`, `drafts.score` e
`draft_tests.start`): o que a API de admin manda, no lugar do texto JSON do campo da página.

Os dois caminhos passam pelas mesmas conferências (o modelo das perguntas, e o que o banco JSONB não
guarda): o texto da página é analisado e cai no mesmo ponto em que o objeto da API entra. Os testes de
página (`test_admin_drafts.py`, `test_admin_scoring.py`) provam que o texto segue como antes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from conftest import DEMO_ANSWERS
from test_admin_forms import DEFINITION, SLUG
from test_admin_scoring import SLUG as SCORED
from test_admin_scoring import scored_definition

from app.deps import Deps
from app.forms import draft_tests, drafts, stored
from app.forms.definition import DefinitionError
from app.models import DraftTest

NOW = datetime(2027, 1, 15, 12, 0, tzinfo=UTC)
EXAMPLE = json.loads(DEFINITION)["examples"]["answers"]
OTHER = {**EXAMPLE, "escritorio": "Outra Contadora", "tamanho_equipe": "6_a_15"}


@pytest.fixture
def draft(deps: Deps) -> drafts.Draft:
    stored.import_definition(deps, DEFINITION, actor="testes")
    with deps.sessions() as session:
        found = drafts.load(session, SLUG)
    assert found is not None
    return found


def problems_of(error: pytest.ExceptionInfo[DefinitionError]) -> dict[str, str]:
    return {problem.path: problem.message for problem in error.value.problems}


def test_an_object_assembles_the_same_prompt_as_the_same_answers_written_as_text(
    draft: drafts.Draft,
) -> None:
    by_text = drafts.assemble(draft, answers=json.dumps(OTHER), now=NOW)
    by_object = drafts.assemble(draft, answers=OTHER, now=NOW)

    assert by_object.prompt == by_text.prompt and "Outra Contadora" in by_object.prompt
    assert by_object.answers.model_dump() == by_text.answers.model_dump()
    assert by_object.examples is False  # as respostas vieram de fora, não são as de exemplo
    assert json.loads(by_object.answers_text) == OTHER


def test_no_answers_still_means_the_examples_of_the_draft(draft: drafts.Draft) -> None:
    assembled = drafts.assemble(draft, answers=None, now=NOW)

    assert assembled.examples is True
    assert json.loads(assembled.answers_text) == EXAMPLE


def test_an_empty_object_is_an_answer_and_not_the_examples(draft: drafts.Draft) -> None:
    with pytest.raises(DefinitionError) as refused:
        drafts.assemble(draft, answers={}, now=NOW)

    assert problems_of(refused)["answers.nome"] == "campo obrigatório"


def test_an_object_is_checked_by_the_questions_as_the_text_is(draft: drafts.Draft) -> None:
    wrong = {**EXAMPLE, "email": "sem-arroba", "tamanho_equipe": "ninguem"}

    with pytest.raises(DefinitionError) as by_object:
        drafts.assemble(draft, answers=wrong, now=NOW)
    with pytest.raises(DefinitionError) as by_text:
        drafts.assemble(draft, answers=json.dumps(wrong), now=NOW)

    assert problems_of(by_object) == problems_of(by_text)
    assert "answers.email" in problems_of(by_object)
    assert "sem-arroba" not in json.dumps(problems_of(by_object))  # nunca o valor recebido


def test_an_object_with_an_answer_for_a_question_the_form_does_not_have_does_not_echo_the_key(
    draft: drafts.Draft,
) -> None:
    """A chave que a API recebe é texto de quem chama: a recusa diz que há resposta para uma pergunta
    que o formulário não tem, no campo `answers`, e não cita a chave. O texto da página segue
    apontando a chave (`answers.<chave>`), que é o que a operadora escreveu na tela dela."""
    sent = "chave_que_o_formulario_nao_tem"
    with pytest.raises(DefinitionError) as by_object:
        drafts.assemble(draft, answers={**EXAMPLE, sent: "x", "email": "sem-arroba"}, now=NOW)
    with pytest.raises(DefinitionError) as by_text:
        drafts.assemble(draft, answers=json.dumps({**EXAMPLE, sent: "x"}), now=NOW)

    assert problems_of(by_object) == {
        "answers.email": "e-mail inválido",  # os outros problemas saem juntos
        "answers": "resposta para uma pergunta que o formulário não tem",
    }
    assert sent not in json.dumps(problems_of(by_object))
    assert problems_of(by_text) == {f"answers.{sent}": "campo desconhecido"}


def test_several_unknown_keys_are_one_problem_and_scoring_and_the_test_refuse_them_too(
    deps: Deps, draft: drafts.Draft
) -> None:
    unknown = {**EXAMPLE, "uma": 1, "outra": 2}

    for refuse in (
        lambda: drafts.assemble(draft, answers=unknown, now=NOW),
        lambda: drafts.score(draft, answers=unknown, now=NOW),
        lambda: draft_tests.start(deps, SLUG, actor="chave:testes", answers=unknown, now=NOW),
    ):
        with pytest.raises(DefinitionError) as refused:
            refuse()

        assert problems_of(refused) == {
            "answers": "resposta para uma pergunta que o formulário não tem"
        }


def test_an_object_with_what_the_database_does_not_keep_is_refused_with_the_path(
    draft: drafts.Draft,
) -> None:
    with pytest.raises(DefinitionError) as refused:
        drafts.assemble(draft, answers={**EXAMPLE, "escritorio": "fim\x00do texto"}, now=NOW)

    assert "answers.escritorio" in problems_of(refused)


def test_an_object_with_a_number_the_database_does_not_keep_is_refused_with_the_path(
    draft: drafts.Draft,
) -> None:
    for number in (float("nan"), float("inf")):  # o JSON do Python os aceita; o JSONB, não
        with pytest.raises(DefinitionError) as refused:
            drafts.assemble(draft, answers={**EXAMPLE, "tamanho_equipe": number}, now=NOW)

        assert "answers.tamanho_equipe" in problems_of(refused)


def test_an_object_is_scored_as_the_same_answers_written_as_text(deps: Deps) -> None:
    stored.import_definition(deps, json.dumps(scored_definition()), actor="testes")
    with deps.sessions() as session:
        draft = drafts.load(session, SCORED)
    assert draft is not None
    answers = {**DEMO_ANSWERS, "tamanho_equipe": "mais_de_15", "canais": ["a", "b", "c", "d"]}

    by_text = drafts.score(draft, answers=json.dumps(answers), now=NOW)
    by_object = drafts.score(draft, answers=answers, now=NOW)

    assert by_object.run is not None and by_text.run is not None
    assert by_object.run == by_text.run and by_object.run.result.scores["alcance"] == 50
    assert by_object.examples is False and json.loads(by_object.answers_text) == answers


def test_a_test_started_with_an_object_keeps_the_validated_answers_and_audits_without_them(
    deps: Deps, draft: drafts.Draft
) -> None:
    test_id = draft_tests.start(deps, SLUG, actor="chave:testes", answers=OTHER, now=NOW)

    with deps.sessions() as session:
        row = session.execute(sa.select(DraftTest).where(DraftTest.id == test_id)).scalar_one()
        kept: dict[str, Any] = dict(row.answers)
    assert kept == drafts.assemble(draft, answers=OTHER, now=NOW).answers.model_dump(mode="json")
    assert kept["escritorio"] == "Outra Contadora"
    assert row.prompt == drafts.assemble(draft, answers=OTHER, now=NOW).prompt
