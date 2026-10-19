"""O que o `Review` da publicação diz do rascunho: se o que vai ao LLM pede um teste e se já tem um
(`test_status`), o que falta antes de a pessoa escolher a nota e confirmar o contato (`blockers`) e os
valores de opção que a versão nova deixa de gravar (`dropped_values`). A página da publicação e a API
leem as três: a regra é uma só, e o `test_status` concorda com `draft_tests.gate_of`, a regra do que
um teste vale para a publicação."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_admin_forms import DEFINITION, SLUG

from app.deps import Deps
from app.forms import draft_tests, drafts, stored

ACTOR = "chave:testes"
START = datetime(2027, 1, 15, 12, 0, tzinfo=UTC)
NEW_LINE = "\nConsidere também o momento do mercado contábil."


@pytest.fixture
def form(deps: Deps) -> None:
    stored.import_definition(deps, DEFINITION, actor="testes")


@pytest.fixture
def tick() -> list[int]:
    """Quantos testes o teste já rodou: cada um começa 1 minuto depois do anterior (a pausa entre eles
    é de 30 s)."""
    return [0]


def draft_of(deps: Deps) -> drafts.Draft:
    with deps.sessions() as session:
        found = drafts.load(session, SLUG)
    assert found is not None
    return found


def edit(deps: Deps, **changes: Any) -> None:
    """Salva o rascunho com as mudanças, como a API salva."""
    document = {**draft_of(deps).document, **changes}
    drafts.replace_document(deps, SLUG, document, actor=ACTOR, expected=draft_of(deps).token)


def edit_prompt(deps: Deps) -> None:
    edit(deps, prompt=draft_of(deps).document["prompt"] + NEW_LINE)


def run_test(deps: Deps, tick: list[int]) -> Any:
    """Um teste do rascunho com sucesso, como agora."""
    tick[0] += 1
    moment = START + timedelta(minutes=tick[0])
    test_id = draft_tests.start(deps, SLUG, actor=ACTOR, now=moment)
    assert draft_tests.run(deps, test_id, now=lambda: moment) == "succeeded"
    return test_id


def review_of(deps: Deps) -> drafts.Review:
    with deps.sessions() as session:
        return drafts.review(session, draft_of(deps))


def test_a_draft_with_nothing_pending_needs_no_test_and_has_nothing_to_publish(
    deps: Deps, form: None
) -> None:
    found = review_of(deps)

    assert found.test_status == drafts.TEST_NOT_NEEDED
    assert found.blockers() == [drafts.NOTHING_TO_PUBLISH]


def test_a_change_to_what_goes_to_the_llm_is_missing_a_test_until_one_succeeds(
    deps: Deps, form: None, tick: list[int]
) -> None:
    edit_prompt(deps)

    assert review_of(deps).test_status == drafts.TEST_MISSING
    assert review_of(deps).blockers() == [drafts.TEST_REQUIRED.format(changes="mudou o prompt")]
    run_test(deps, tick)

    assert review_of(deps).test_status == drafts.TEST_COVERED
    assert review_of(deps).blockers() == []


def test_a_change_to_a_part_the_llm_does_not_read_needs_no_test(deps: Deps, form: None) -> None:
    edit(deps, intro="Uma introdução nova, que o LLM não lê.")

    assert review_of(deps).test_status == drafts.TEST_NOT_NEEDED
    assert review_of(deps).blockers() == []


def test_the_review_agrees_with_what_a_test_is_worth_to_the_publication(
    deps: Deps, form: None, tick: list[int]
) -> None:
    """`gate_of` diz o que o teste vale (`current`, `unlocks`, `stale`); o `Review` tem de concordar: só
    os dois primeiros liberam a publicação."""
    edit_prompt(deps)
    test_id = run_test(deps, tick)

    def worth() -> str | None:
        with deps.sessions() as session:
            result = draft_tests.find(session, draft_of(deps), test_id, now=START)
        assert result is not None
        return draft_tests.gate_of(draft_of(deps), result)

    assert worth() == "current" and review_of(deps).test_status == drafts.TEST_COVERED
    edit(deps, intro="Uma introdução nova, que o LLM não lê.")
    assert worth() == "unlocks" and review_of(deps).test_status == drafts.TEST_COVERED
    edit(deps, instructions=draft_of(deps).document["instructions"] + "\nUse frases curtas.")
    assert worth() == "stale" and review_of(deps).test_status == drafts.TEST_MISSING


def test_the_note_is_not_a_blocker_but_the_refusal_of_the_publication_lists_it(
    deps: Deps, form: None
) -> None:
    """A nota é de quem publica: a revisão (que não tem nota) não a diz como o que falta, mas as
    razões da recusa a listam."""
    edit(deps, intro="Uma introdução nova.")

    found = review_of(deps)

    assert found.blockers() == []
    assert found.reasons(note="", contact_confirmed=False) == [drafts.NOTE_REQUIRED]


def test_the_review_lists_the_option_values_the_new_version_no_longer_records(
    deps: Deps, form: None
) -> None:
    steps = draft_of(deps).document["steps"]
    team = next(q for step in steps for q in step["questions"] if q["name"] == "tamanho_equipe")
    team["options"] = [option for option in team["options"] if option["value"] != "mais_de_15"]
    edit(deps, steps=steps)

    dropped = review_of(deps).dropped_values()

    assert [(item.question, item.value) for item in dropped] == [("tamanho_equipe", "mais_de_15")]


def test_the_first_publication_has_no_dropped_values(deps: Deps) -> None:
    created = drafts.create(deps, title="Formulário novo", slug="formulario-novo", actor=ACTOR)
    with deps.sessions() as session:
        draft = drafts.load(session, created)
        assert draft is not None
        found = drafts.review(session, draft)

    assert found.first and found.dropped_values() == ()
