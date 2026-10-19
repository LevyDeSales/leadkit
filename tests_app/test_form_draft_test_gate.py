"""O que um teste com sucesso diz à publicação (`draft_tests.gate_of`): o teste libera a publicação se
testou o rascunho como está, ou se depois dele o rascunho só mudou em partes que não vão ao LLM. A
página do teste (`draft_pages`) traduz o código em texto; a API o devolve como código."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from test_admin_forms import DEFINITION, SLUG

from app.deps import Deps
from app.forms import draft_tests, drafts, stored

NOW = datetime(2027, 1, 15, 12, 0, tzinfo=UTC)
ACTOR = "chave:testes"


def draft_of(deps: Deps) -> drafts.Draft:
    with deps.sessions() as session:
        found = drafts.load(session, SLUG)
    assert found is not None
    return found


def result_of(deps: Deps, test_id: Any) -> draft_tests.DraftTestResult:
    with deps.sessions() as session:
        found = draft_tests.find(session, draft_of(deps), test_id, now=NOW)
    assert found is not None
    return found


def edit(deps: Deps, **changes: Any) -> None:
    """Salva o rascunho com as mudanças, como a API salva."""
    document = {**draft_of(deps).document, **changes}
    drafts.replace_document(deps, SLUG, document, actor=ACTOR, expected=draft_of(deps).token)


@pytest.fixture
def tested(deps: Deps) -> Any:
    """O id de um teste com sucesso do rascunho como ele está."""
    stored.import_definition(deps, DEFINITION, actor="testes")
    test_id = draft_tests.start(deps, SLUG, actor=ACTOR, now=NOW)
    assert draft_tests.run(deps, test_id, now=lambda: NOW) == "succeeded"
    return test_id


def test_a_test_of_the_draft_as_it_is_now_unlocks_the_publication(deps: Deps, tested: Any) -> None:
    assert draft_tests.gate_of(draft_of(deps), result_of(deps, tested)) == "current"


def test_a_change_to_a_part_that_does_not_go_to_the_llm_keeps_the_test_valid(
    deps: Deps, tested: Any
) -> None:
    edit(deps, intro="Uma introdução nova, que o LLM não lê.")

    assert draft_tests.gate_of(draft_of(deps), result_of(deps, tested)) == "unlocks"


def test_a_change_to_what_goes_to_the_llm_makes_the_test_stale(deps: Deps, tested: Any) -> None:
    prompt = json.loads(DEFINITION)["prompt"] + "\nConsidere também o mercado."
    edit(deps, prompt=prompt)

    assert draft_tests.gate_of(draft_of(deps), result_of(deps, tested)) == "stale"


def test_a_test_that_did_not_succeed_says_nothing_to_the_publication(deps: Deps) -> None:
    stored.import_definition(deps, DEFINITION, actor="testes")
    queued = draft_tests.start(deps, SLUG, actor=ACTOR, now=NOW)

    assert draft_tests.gate_of(draft_of(deps), result_of(deps, queued)) is None
    draft_tests.not_started(deps, queued)
    assert draft_tests.gate_of(draft_of(deps), result_of(deps, queued)) is None
