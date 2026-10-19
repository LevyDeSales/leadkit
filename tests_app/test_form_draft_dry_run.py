"""A simulação da publicação (`drafts.publish(..., dry_run=True)`): roda as mesmas conferências da
publicação, inclusive o PDF de exemplo no adaptador, e devolve o que falta em vez de levantar a
recusa. Não grava, não audita e não mexe no rascunho; a publicação de verdade, sem `dry_run`, é a de
sempre."""

from __future__ import annotations

import json
from typing import Any

import pytest
import sqlalchemy as sa
from admin_api_support import snapshot
from test_admin_forms import CONTACT_PROMPT, DEFINITION, SLUG
from test_form_draft_review import (
    ACTOR,
    draft_of,
    edit,
    edit_prompt,
    run_test,
)

from app.deps import Deps
from app.errors import FailureCode, TerminalError
from app.forms import UnknownForm, drafts, stored
from app.models import AuditLog

NOTE = "Prompt pede o momento do mercado."


class SpyPdf:
    """O adaptador do PDF, trocado: conta as renderizações e, se `refuses`, recusa o exemplo."""

    def __init__(self, inner: Any, *, refuses: bool = False) -> None:
        self.inner = inner
        self.refuses = refuses
        self.calls = 0

    def render(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.refuses:
            raise TerminalError(FailureCode.RENDER_FAILED, "o exemplo não renderiza")
        return self.inner.render(*args, **kwargs)


@pytest.fixture
def form(deps: Deps) -> None:
    stored.import_definition(deps, DEFINITION, actor="testes")


@pytest.fixture
def tick() -> list[int]:
    """Quantos testes o teste já rodou (ver `run_test`)."""
    return [0]


@pytest.fixture
def pdf(deps: Deps) -> SpyPdf:
    spy = SpyPdf(deps.pdf)
    deps.pdf = spy
    return spy


def simulate(deps: Deps, *, note: str = NOTE, confirmed: bool = False) -> drafts.Simulation:
    simulation = drafts.publish(
        deps,
        SLUG,
        actor=ACTOR,
        note=note,
        expected=draft_of(deps).token,
        contact_confirmed=confirmed,
        dry_run=True,
    )
    assert isinstance(simulation, drafts.Simulation)
    return simulation


def test_a_dry_run_says_what_the_publication_would_do_and_writes_nothing(
    deps: Deps, form: None, pdf: SpyPdf, tick: list[int]
) -> None:
    edit_prompt(deps)
    run_test(deps, tick)
    before = snapshot()

    simulation = simulate(deps)

    assert simulation.ready
    assert simulation.reasons == () and simulation.problems == ()
    assert simulation.review.number == 2
    assert [change.key for change in simulation.review.changes if change.changed] == ["prompt"]
    assert simulation.review.llm_changes == ("o prompt",)
    assert snapshot() == before  # nem versão, nem rascunho, nem auditoria
    with deps.sessions() as session:
        assert stored.stored_version(session, SLUG, 2) is None  # a versão 2 não existe
        published = session.scalars(sa.select(AuditLog).where(AuditLog.action == "form.publish"))
        assert list(published) == []
    # A publicação de verdade, depois, cria a versão que a simulação disse.
    published = drafts.publish(deps, SLUG, actor=ACTOR, note=NOTE, expected=draft_of(deps).token)
    assert published.number == simulation.review.number


def test_a_dry_run_renders_the_example_pdf_in_the_adapter(
    deps: Deps, form: None, pdf: SpyPdf, tick: list[int]
) -> None:
    edit_prompt(deps)
    run_test(deps, tick)
    calls = pdf.calls

    simulate(deps)

    assert pdf.calls == calls + 1


def test_a_dry_run_lists_everything_that_is_missing_at_once_instead_of_refusing(
    deps: Deps, form: None, pdf: SpyPdf
) -> None:
    edit_prompt(deps)  # sem teste e, aqui, sem nota

    simulation = simulate(deps, note="  ")

    assert not simulation.ready
    assert simulation.reasons == (
        drafts.TEST_REQUIRED.format(changes="mudou o prompt"),
        drafts.NOTE_REQUIRED,
    )
    assert pdf.calls == 1  # roda tudo, para a lista ser completa


def test_a_dry_run_reports_a_pdf_the_adapter_refuses_with_the_same_problems_as_the_publication(
    deps: Deps, form: None, tick: list[int]
) -> None:
    edit_prompt(deps)
    run_test(deps, tick)
    deps.pdf = SpyPdf(deps.pdf, refuses=True)

    simulation = simulate(deps)
    with pytest.raises(drafts.PublishRefused) as refused:
        drafts.publish(deps, SLUG, actor=ACTOR, note=NOTE, expected=draft_of(deps).token)

    assert not simulation.ready
    assert simulation.reasons == (drafts.PDF_FAILED,) == refused.value.reasons
    assert simulation.problems == refused.value.problems != ()
    assert [problem.path for problem in simulation.problems] == ["pdf"]


def test_a_dry_run_of_a_draft_with_nothing_to_publish_does_not_render_the_pdf(
    deps: Deps, form: None, pdf: SpyPdf
) -> None:
    simulation = simulate(deps)

    assert simulation.reasons == (drafts.NOTHING_TO_PUBLISH,)
    assert not simulation.ready and pdf.calls == 0


def test_a_dry_run_asks_for_the_contact_confirmation_only_in_the_list_of_what_is_missing(
    deps: Deps, tick: list[int]
) -> None:
    stored.import_definition(deps, DEFINITION, actor="testes")
    edit(deps, prompt=json.loads(CONTACT_PROMPT)["prompt"])
    run_test(deps, tick)

    unconfirmed = simulate(deps)
    confirmed = simulate(deps, confirmed=True)

    assert unconfirmed.review.contact == ("name", "email")  # a resposta traz os dados citados
    assert unconfirmed.reasons == (drafts.CONTACT_UNCONFIRMED,) and not unconfirmed.ready
    assert confirmed.reasons == () and confirmed.ready
    with pytest.raises(drafts.PublishRefused) as refused:
        drafts.publish(deps, SLUG, actor=ACTOR, note=NOTE, expected=draft_of(deps).token)
    assert refused.value.only_contact_missing


def test_a_refusal_with_other_reasons_besides_the_contact_is_not_only_the_contact(
    deps: Deps, tick: list[int]
) -> None:
    stored.import_definition(deps, DEFINITION, actor="testes")
    edit(deps, prompt=json.loads(CONTACT_PROMPT)["prompt"])
    run_test(deps, tick)

    with pytest.raises(drafts.PublishRefused) as refused:
        drafts.publish(deps, SLUG, actor=ACTOR, note="", expected=draft_of(deps).token)

    assert refused.value.reasons == (drafts.CONTACT_UNCONFIRMED, drafts.NOTE_REQUIRED)
    assert not refused.value.only_contact_missing


def test_a_dry_run_of_a_draft_that_changed_is_a_conflict_like_the_publication(
    deps: Deps, form: None
) -> None:
    edit_prompt(deps)
    stale = draft_of(deps).token
    edit(deps, intro="Outra mudança, salva depois.")

    with pytest.raises(drafts.DraftChanged):
        drafts.publish(deps, SLUG, actor=ACTOR, note=NOTE, expected=stale, dry_run=True)


def test_a_dry_run_of_a_form_that_does_not_exist_is_unknown(deps: Deps) -> None:
    with pytest.raises(UnknownForm):
        drafts.publish(deps, "nao-existe", actor=ACTOR, note=NOTE, expected="x", dry_run=True)


def test_the_publication_without_a_dry_run_is_the_one_it_always_was(
    deps: Deps, form: None, tick: list[int]
) -> None:
    edit_prompt(deps)
    run_test(deps, tick)

    published = drafts.publish(deps, SLUG, actor=ACTOR, note=NOTE, expected=draft_of(deps).token)

    assert isinstance(published, drafts.Published) and published.number == 2
    assert published.review.number == 2 and published.review.llm_changes == ("o prompt",)
    with deps.sessions() as session:
        (entry,) = session.scalars(sa.select(AuditLog).where(AuditLog.action == "form.publish"))
    assert entry.details["version"] == 2 and entry.actor == ACTOR
