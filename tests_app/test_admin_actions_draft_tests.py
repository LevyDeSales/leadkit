"""As ações do admin sobre o teste do rascunho (`app.admin.actions.draft_tests`): iniciar (o teste na
fila, o disparo da task `draft_test` com só o id e o `not_started` quando o engine não recebe),
a recusa que a página e a API traduzem cada uma no seu formato, o que a página do teste junta (o
prompt, a pontuação e os testes recentes) e a busca de um teste e do PDF dele.

A página (`test_admin_drafts.py`) e a API (`test_admin_api_draft_tests.py`) chamam estas funções;
os testes delas provam que o comportamento da página não mudou.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from test_admin_forms import DEFINITION, SLUG

from app.admin.actions import draft_tests as actions
from app.deps import Deps
from app.forms import UnknownForm, draft_tests, stored
from app.forms.definition import DefinitionError
from app.models import AuditLog, Base, DraftTest

NOW = datetime(2027, 1, 15, 12, 0, tzinfo=UTC)
ACTOR = "chave:testes"
EXAMPLE = json.loads(DEFINITION)["examples"]["answers"]
OTHER = {**EXAMPLE, "escritorio": "Outra Contadora"}


@pytest.fixture
def published(deps: Deps) -> None:
    stored.import_definition(deps, DEFINITION, actor="testes")


class Engine:
    """O disparador da task `draft_test` (o `app.state.test_trigger`): guarda o que recebe e, com
    `accepts` falso, faz de conta que o engine do Hatchet está fora do ar."""

    def __init__(self, *, accepts: bool = True) -> None:
        self.accepts = accepts
        self.received: list[uuid.UUID] = []

    async def __call__(self, test_id: uuid.UUID) -> str | None:
        self.received.append(test_id)
        return "run-fake" if self.accepts else None


def start(deps: Deps, engine: Engine, **options: Any) -> actions.Started:
    options = {"actor": ACTOR, "answers": None, "day": "", "now": NOW, **options}
    return asyncio.run(actions.start(deps, engine, SLUG, **options))


def row_of(deps: Deps, test_id: uuid.UUID) -> DraftTest:
    with deps.sessions() as session:
        return session.execute(sa.select(DraftTest).where(DraftTest.id == test_id)).scalar_one()


def rows_of_tests(deps: Deps) -> int:
    with deps.sessions() as session:
        return session.scalar(sa.select(sa.func.count()).select_from(DraftTest)) or 0


class NoLlm:
    """O LLM do teste: qualquer chamada é um erro. Montar o prompt, pontuar e listar não chamam o
    LLM (só o teste, no worker)."""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, spec: Any, answers: Any) -> Any:
        self.calls += 1
        raise AssertionError("o LLM não pode ser chamado fora do teste")


def counts(deps: Deps) -> dict[str, int]:
    """Quantas linhas cada tabela tem: montar o prompt e pontuar não gravam nada."""
    with deps.sessions() as session:
        return {
            table.name: session.scalar(sa.select(sa.func.count()).select_from(table)) or 0
            for table in Base.metadata.sorted_tables
        }


# --- a página do teste: o prompt, a pontuação e os testes recentes ------------------------------


def test_the_testing_state_assembles_the_prompt_with_the_example_answers_and_lists_the_tests(
    deps: Deps, published: None
) -> None:
    started = start(deps, Engine())

    state = actions.testing(deps, SLUG, now=NOW)

    assert state.problems == () and state.scored is None
    assert state.assembled is not None and state.assembled.examples is True
    assert "Nome do escritório: Escritório Exemplo" in state.assembled.prompt
    assert [run.id for run in state.recent] == [started.test_id]
    assert state.draft.slug == SLUG


def test_the_testing_state_takes_the_answers_as_text_or_as_an_object_and_the_date(
    deps: Deps, published: None
) -> None:
    by_text = actions.testing(deps, SLUG, answers=json.dumps(OTHER), day="2027-03-01", now=NOW)
    by_object = actions.testing(deps, SLUG, answers=OTHER, day="2027-03-01", now=NOW)

    assert by_text.assembled is not None and by_object.assembled is not None
    assert by_object.assembled.prompt == by_text.assembled.prompt
    assert "Outra Contadora" in by_object.assembled.prompt
    assert by_object.assembled.examples is False
    assert by_object.assembled.simulated is not None
    assert by_object.assembled.simulated.isoformat() == "2027-03-01"


def test_the_testing_state_scores_instead_of_assembling_when_asked(
    deps: Deps, published: None
) -> None:
    state = actions.testing(deps, SLUG, answers=OTHER, scoring=True, now=NOW)

    assert state.assembled is None and state.problems == ()
    assert state.scored is not None and state.scored.examples is False
    assert state.scored.run is None  # a definição de teste não tem pontuação


def test_answers_that_do_not_validate_come_back_as_problems_and_the_tests_still_list(
    deps: Deps, published: None
) -> None:
    started = start(deps, Engine())

    state = actions.testing(deps, SLUG, answers={**EXAMPLE, "email": "sem-arroba"}, now=NOW)

    assert state.assembled is None and state.scored is None
    assert [problem.path for problem in state.problems] == ["answers.email"]
    assert "sem-arroba" not in json.dumps([problem.message for problem in state.problems])
    assert [run.id for run in state.recent] == [started.test_id]


def test_a_date_that_is_not_a_date_comes_back_as_a_problem_of_the_date(
    deps: Deps, published: None
) -> None:
    state = actions.testing(deps, SLUG, day="2027-02-31", now=NOW)

    assert [problem.path for problem in state.problems] == ["date"]
    assert state.assembled is None


def test_the_testing_state_of_a_form_that_does_not_exist_is_unknown(deps: Deps) -> None:
    with pytest.raises(UnknownForm):
        actions.testing(deps, "nao-existe", now=NOW)


def test_the_testing_state_neither_writes_nor_calls_the_llm(deps: Deps, published: None) -> None:
    deps.llm = llm = NoLlm()
    before = counts(deps)

    actions.testing(deps, SLUG, now=NOW)
    actions.testing(deps, SLUG, answers=OTHER, day="2027-03-01", now=NOW)
    actions.testing(deps, SLUG, answers=OTHER, scoring=True, now=NOW)
    actions.testing(deps, SLUG, answers={}, now=NOW)  # recusada, e também não grava

    assert counts(deps) == before and llm.calls == 0


# --- achar um teste e o PDF dele ----------------------------------------------------------------


def found(deps: Deps, test_id: Any, *, slug: str = SLUG, now: datetime = NOW) -> actions.Found:
    return actions.find(deps, slug, str(test_id), now=now)


def test_a_test_in_the_queue_is_found_with_the_draft_it_was_started_from(
    deps: Deps, published: None
) -> None:
    started = start(deps, Engine(), answers=OTHER, day="2027-03-01")

    run = found(deps, started.test_id)

    assert run.draft.slug == SLUG and run.result.id == started.test_id
    assert (run.result.status, run.result.finished, run.result.has_pdf) == ("queued", False, False)
    assert run.result.started_by == ACTOR and run.result.plan is None
    assert run.result.simulated_date is not None
    assert run.result.simulated_date.isoformat() == "2027-03-01"
    assert run.result.answers["escritorio"] == "Outra Contadora"


def test_a_finished_test_brings_the_plan_the_times_and_the_pdf(deps: Deps, published: None) -> None:
    started = start(deps, Engine())
    assert draft_tests.run(deps, started.test_id, now=lambda: NOW) == "succeeded"

    run = found(deps, started.test_id).result

    assert (run.status, run.finished, run.has_pdf) == ("succeeded", True, True)
    assert run.plan is not None and run.llm_ms is not None and run.pdf_ms is not None
    assert run.model == "fake" and run.usage == {"fake": True}


def test_a_test_that_never_ended_is_found_as_interrupted_after_the_deadline(
    deps: Deps, published: None
) -> None:
    started = start(deps, Engine())

    run = found(deps, started.test_id, now=NOW + timedelta(minutes=7)).result

    assert (run.status, run.failure_code) == ("interrupted", draft_tests.INTERRUPTED_CODE)
    assert run.finished is True


@pytest.mark.parametrize(
    "wrong",
    ["nao-e-um-id", "", "123", "6f1d3c9e-0b7a-4c52-9d8e-2a4f5b6c7d80"],
    ids=["not_an_id", "empty", "number", "nobody_has_it"],
)
def test_an_id_that_is_not_a_test_of_the_form_is_an_unknown_test(
    deps: Deps, published: None, wrong: str
) -> None:
    start(deps, Engine())

    with pytest.raises(actions.UnknownTest):
        actions.find(deps, SLUG, wrong, now=NOW)


def test_a_test_of_another_form_is_not_found_through_this_one(deps: Deps, published: None) -> None:
    other = asyncio.run(
        actions.start(deps, Engine(), "demo", actor=ACTOR, answers=None, day="", now=NOW)
    )

    with pytest.raises(actions.UnknownTest):
        found(deps, other.test_id)  # SLUG não é o formulário do teste
    assert found(deps, other.test_id, slug="demo").result.id == other.test_id


def test_finding_a_test_of_a_form_that_does_not_exist_is_an_unknown_form(deps: Deps) -> None:
    with pytest.raises(UnknownForm):
        found(deps, uuid.uuid4(), slug="nao-existe")


def test_the_pdf_of_a_finished_test_comes_with_the_name_that_says_the_form_and_the_minute(
    deps: Deps, published: None
) -> None:
    started = start(deps, Engine())
    draft_tests.run(deps, started.test_id, now=lambda: NOW)

    pdf = actions.pdf(deps, SLUG, str(started.test_id), now=NOW)

    began = found(deps, started.test_id).result.started_at  # como o banco devolve o instante
    assert pdf.filename == f"teste-{SLUG}-{began:%Y%m%d-%H%M}.pdf"
    assert pdf.content.startswith(b"%PDF")


def test_a_test_that_did_not_get_to_the_pdf_has_none(deps: Deps, published: None) -> None:
    started = start(deps, Engine())  # na fila: ainda sem PDF

    with pytest.raises(actions.NoPdf):
        actions.pdf(deps, SLUG, str(started.test_id), now=NOW)


def test_the_pdf_of_a_test_that_is_not_there_is_an_unknown_test(
    deps: Deps, published: None
) -> None:
    with pytest.raises(actions.UnknownTest):
        actions.pdf(deps, SLUG, str(uuid.uuid4()), now=NOW)
    with pytest.raises(UnknownForm):
        actions.pdf(deps, "nao-existe", str(uuid.uuid4()), now=NOW)


# --- iniciar ------------------------------------------------------------------------------------


def test_a_started_test_is_queued_and_the_task_receives_only_its_id(
    deps: Deps, published: None
) -> None:
    engine = Engine()

    started = start(deps, engine, answers=OTHER, day="2027-03-01")

    assert started.launched is True and engine.received == [started.test_id]
    row = row_of(deps, started.test_id)
    assert (row.status, row.started_by, row.started_at) == ("queued", ACTOR, NOW)
    assert row.simulated_date.isoformat() == "2027-03-01"
    assert row.answers["escritorio"] == "Outra Contadora" and "Outra Contadora" in row.prompt


def test_a_started_test_is_audited_without_the_answers(deps: Deps, published: None) -> None:
    started = start(deps, Engine(), answers=OTHER, day="2027-03-01")

    with deps.sessions() as session:
        (entry,) = session.scalars(sa.select(AuditLog).where(AuditLog.action == "form.draft.test"))
    assert entry.actor == ACTOR and entry.details["test"] == str(started.test_id)
    assert "Outra Contadora" not in json.dumps(entry.details)


def test_an_engine_that_does_not_receive_the_task_ends_the_test_as_not_started_without_a_pause(
    deps: Deps, published: None
) -> None:
    started = start(deps, Engine(accepts=False))

    assert started.launched is False
    row = row_of(deps, started.test_id)
    assert (row.status, row.failure_code) == ("failed", draft_tests.NOT_STARTED_CODE)
    # o LLM não foi chamado, então não há pausa: o teste seguinte pode começar na hora
    assert start(deps, Engine()).launched is True


def test_a_test_in_progress_refuses_the_next_one_and_nothing_is_launched_or_queued(
    deps: Deps, published: None
) -> None:
    start(deps, Engine())
    engine = Engine()

    with pytest.raises(draft_tests.DraftTestRefused) as refused:
        start(deps, engine)

    assert refused.value.message == draft_tests.BUSY
    assert refused.value.retry_after is None and refused.value.paused is False
    assert engine.received == [] and rows_of_tests(deps) == 1


def test_the_pause_after_a_test_refuses_the_next_one_with_the_seconds_to_wait(
    deps: Deps, published: None
) -> None:
    first = start(deps, Engine())
    assert draft_tests.run(deps, first.test_id, now=lambda: NOW) == "succeeded"
    engine = Engine()

    with pytest.raises(draft_tests.DraftTestRefused) as refused:
        start(deps, engine, now=NOW + timedelta(seconds=10))

    assert refused.value.paused is True and refused.value.retry_after == 20
    assert "Espere 20 s" in refused.value.message
    assert engine.received == [] and rows_of_tests(deps) == 1
    assert start(deps, engine, now=NOW + timedelta(seconds=31)).launched is True


def test_answers_that_do_not_validate_start_nothing_and_never_repeat_the_value(
    deps: Deps, published: None
) -> None:
    engine = Engine()

    with pytest.raises(DefinitionError) as refused:
        start(deps, engine, answers={**EXAMPLE, "email": "sem-arroba"})

    assert [problem.path for problem in refused.value.problems] == ["answers.email"]
    assert "sem-arroba" not in json.dumps([problem.message for problem in refused.value.problems])
    assert engine.received == [] and rows_of_tests(deps) == 0


def test_a_date_that_is_not_a_date_starts_nothing(deps: Deps, published: None) -> None:
    engine = Engine()

    with pytest.raises(DefinitionError) as refused:
        start(deps, engine, day="amanhã")

    assert [problem.path for problem in refused.value.problems] == ["date"]
    assert engine.received == [] and rows_of_tests(deps) == 0


def test_a_form_that_does_not_exist_starts_nothing(deps: Deps) -> None:
    engine = Engine()

    with pytest.raises(UnknownForm):
        asyncio.run(
            actions.start(deps, engine, "nao-existe", actor=ACTOR, answers=None, day="", now=NOW)
        )

    assert engine.received == [] and rows_of_tests(deps) == 0


def test_starting_logs_only_ids_and_the_author(
    deps: Deps,
    published: None,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # O `fileConfig` do alembic, que a suíte roda na subida, desliga os loggers que já existiam:
    # religa o da ação, ou a ausência das respostas no log não provaria nada.
    monkeypatch.setattr(actions.logger, "disabled", False)
    with caplog.at_level(logging.DEBUG):
        started = start(deps, Engine(), answers=OTHER)

    assert str(started.test_id) in caplog.text and ACTOR in caplog.text
    assert "Outra Contadora" not in caplog.text
