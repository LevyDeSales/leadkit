"""`drafts.save_document`: o miolo comum dos salvares do rascunho (o do editor das perguntas, o do PDF
e o da API). Guarda um documento inteiro, com as conferências das duas páginas, numa transação.

Os testes de página (`test_admin_drafts.py`, `test_admin_pdf_editor.py`, `test_admin_new_forms.py`)
provam que as duas páginas seguem se comportando como antes; aqui está o que o miolo tem de seu.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import sqlalchemy as sa
from test_admin_forms import DEFINITION, SLUG, store_version

from app.deps import Deps
from app.forms import UnknownForm, drafts, stored
from app.forms.definition import DefinitionError, fingerprint
from app.models import AuditLog, Form, WebhookDestination

ACTOR = "chave:testes"
SCRATCH = "gestao"  # criado do zero, sem versão publicada


def document(**changes: Any) -> dict[str, Any]:
    return {**json.loads(DEFINITION), **changes}


def draft_of(deps: Deps, slug: str) -> drafts.Draft:
    with deps.sessions() as session:
        found = drafts.load(session, slug)
    assert found is not None
    return found


@pytest.fixture
def published(deps: Deps) -> drafts.Draft:
    """Um formulário com a versão 1 publicada e sem rascunho."""
    stored.import_definition(deps, DEFINITION, actor="testes")
    return draft_of(deps, SLUG)


@pytest.fixture
def scratch(deps: Deps) -> drafts.Draft:
    """Um formulário criado do zero: só o rascunho, antes da versão 1."""
    drafts.create(deps, title="Gestão do escritório", slug=SCRATCH, actor="testes")
    return draft_of(deps, SCRATCH)


def test_it_stores_the_whole_document_audits_it_and_answers_with_the_draft(
    deps: Deps, published: drafts.Draft
) -> None:
    changed = document(intro="Uma introdução nova.")

    saved = drafts.save_document(
        deps, SLUG, changed, actor=ACTOR, expected=published.token, resources=True
    )

    assert saved.document["intro"] == "Uma introdução nova." and saved.pending
    assert saved.updated_by == ACTOR and saved.token == fingerprint(saved.document)
    with deps.sessions() as session:
        (entry,) = session.scalars(
            sa.select(AuditLog).where(AuditLog.action == "form.draft.save")
        ).all()
    assert entry.actor == ACTOR and entry.details["form_slug"] == SLUG
    assert entry.details["fingerprint"] == saved.token


def test_a_draft_that_changed_since_it_was_read_is_refused_and_nothing_is_stored(
    deps: Deps, published: drafts.Draft
) -> None:
    with pytest.raises(drafts.DraftChanged):
        drafts.save_document(deps, SLUG, document(intro="X"), actor=ACTOR, expected="velho")

    assert draft_of(deps, SLUG).saved is False


def test_an_unknown_form_is_refused(deps: Deps) -> None:
    with pytest.raises(UnknownForm):
        drafts.save_document(deps, "nao-existe", document(), actor=ACTOR, expected="x")


def test_a_document_that_does_not_compile_is_refused_with_the_path_and_nothing_is_stored(
    deps: Deps, published: drafts.Draft
) -> None:
    with pytest.raises(DefinitionError) as refused:
        drafts.save_document(deps, SLUG, document(minutes=0), actor=ACTOR, expected=published.token)

    assert [problem.path for problem in refused.value.problems] == ["minutes"]
    assert draft_of(deps, SLUG).saved is False


def test_a_document_that_is_not_an_object_is_refused(deps: Deps, published: drafts.Draft) -> None:
    with pytest.raises(DefinitionError) as refused:
        drafts.save_document(deps, SLUG, [], actor=ACTOR, expected=published.token)  # type: ignore[arg-type]

    assert [problem.path for problem in refused.value.problems] == [""]


def test_what_the_database_does_not_keep_is_refused_with_the_path(
    deps: Deps, published: drafts.Draft
) -> None:
    with pytest.raises(DefinitionError) as refused:
        drafts.save_document(
            deps, SLUG, document(intro="com\x00nulo"), actor=ACTOR, expected=published.token
        )

    assert [problem.path for problem in refused.value.problems] == ["intro"]


def test_the_slug_of_the_document_has_to_be_the_forms(deps: Deps, published: drafts.Draft) -> None:
    with pytest.raises(DefinitionError) as refused:
        drafts.save_document(
            deps, SLUG, document(slug="outro"), actor=ACTOR, expected=published.token
        )

    (problem,) = refused.value.problems
    assert problem.path == "slug" and "tem de ser o do formulário" in problem.message


def test_the_address_changes_before_the_first_publication_and_takes_the_destination_along(
    deps: Deps, scratch: drafts.Draft
) -> None:
    with deps.sessions.begin() as session:
        session.add(
            WebhookDestination(
                form_slug=SCRATCH,
                url="https://receptor.example.test/gestao",
                secret_sealed="lacrado",
                created_by="testes",
                updated_by="testes",
            )
        )
    moved = {**scratch.document, "slug": "gestao-2027"}

    saved = drafts.save_document(
        deps, SCRATCH, moved, actor=ACTOR, expected=scratch.token, new_slug="gestao-2027"
    )

    assert saved.slug == "gestao-2027"
    with deps.sessions() as session:
        assert session.scalar(sa.select(Form.id).where(Form.slug == SCRATCH)) is None
        assert session.scalar(
            sa.select(WebhookDestination.id).where(WebhookDestination.form_slug == "gestao-2027")
        )
        (entry,) = session.scalars(sa.select(AuditLog).where(AuditLog.action == "form.rename"))
    assert entry.details == {"form_slug": "gestao-2027", "previous_slug": SCRATCH}


def test_after_the_first_publication_the_address_does_not_change(
    deps: Deps, published: drafts.Draft
) -> None:
    with pytest.raises(DefinitionError) as refused:
        drafts.save_document(
            deps,
            SLUG,
            document(slug="outro"),
            actor=ACTOR,
            expected=published.token,
            new_slug="outro",
        )

    (problem,) = refused.value.problems
    assert problem.path == "slug" and "não muda depois da primeira publicação" in problem.message


def test_a_reserved_or_taken_address_is_refused(deps: Deps, scratch: drafts.Draft) -> None:
    for wanted in ("import", "demo"):
        with pytest.raises(DefinitionError) as refused:
            drafts.save_document(
                deps,
                SCRATCH,
                {**scratch.document, "slug": wanted},
                actor=ACTOR,
                expected=scratch.token,
                new_slug=wanted,
            )
        assert [problem.path for problem in refused.value.problems] == ["slug"], wanted


OUTSIDE_THE_APP = {"pdf": {"html": '<img src="https://example.test/logo.png" alt="">'}}


def with_outside_resource(base: dict[str, Any]) -> dict[str, Any]:
    return {**base, "pdf": {**base["pdf"], **OUTSIDE_THE_APP["pdf"]}}


def test_the_pdf_resources_are_checked_by_default(deps: Deps, published: drafts.Draft) -> None:
    with pytest.raises(DefinitionError) as refused:
        drafts.save_document(
            deps,
            SLUG,
            with_outside_resource(published.document),
            actor=ACTOR,
            expected=published.token,
        )

    assert all(problem.path.startswith("pdf") for problem in refused.value.problems)
    assert draft_of(deps, SLUG).saved is False


def test_the_fields_page_does_not_ask_for_the_resources_it_never_touches(
    deps: Deps, published: drafts.Draft
) -> None:
    saved = drafts.save_document(
        deps,
        SLUG,
        with_outside_resource(published.document),
        actor=ACTOR,
        expected=published.token,
        resources=False,
    )

    assert "https://example.test" in saved.document["pdf"]["html"]


def test_both_pages_save_through_it(
    deps: Deps, published: drafts.Draft, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O editor das perguntas (`save`) e o do PDF (`save_templates`) montam o documento e o entregam
    ao miolo comum: a conferência é uma só."""
    calls: list[bool] = []
    original = drafts.save_document

    def spy(*args: Any, **kwargs: Any) -> drafts.Draft:
        calls.append(kwargs.get("resources", True))
        return original(*args, **kwargs)

    monkeypatch.setattr(drafts, "save_document", spy)
    saved = drafts.save(
        deps,
        SLUG,
        drafts.texts_of({**published.document, "intro": "Pelas perguntas."}),
        actor="testes",
        expected=published.token,
    )
    drafts.save_templates(
        deps,
        SLUG,
        drafts.TemplateTexts(
            pdf_html=saved.document["pdf"]["html"] + "<!-- ok -->",
            pdf_css="",
            preview=saved.document["preview"],
            scores=(),
        ),
        actor="testes",
        expected=saved.token,
    )

    assert calls == [False, True]  # as perguntas não pedem o PDF; o editor do PDF, sim


def test_a_draft_back_to_the_published_text_leaves_no_pending_draft(
    deps: Deps, published: drafts.Draft
) -> None:
    store_version(deps, 2, document(intro="Outra."))
    current = draft_of(deps, SLUG)

    saved = drafts.save_document(
        deps, SLUG, document(intro="Outra."), actor=ACTOR, expected=current.token
    )

    assert saved.pending is False and saved.saved is False


# --- a versão do texto do consentimento é do sistema -------------------------------------------


def test_the_version_of_the_consent_text_is_the_systems_not_the_documents(
    deps: Deps, published: drafts.Draft
) -> None:
    consent = {**published.document["consent"], "version": "inventada-v9", "notice": "Novo aviso."}

    saved = drafts.save_document(
        deps, SLUG, document(consent=consent), actor=ACTOR, expected=published.token
    )

    assert saved.document["consent"]["notice"] == "Novo aviso."
    assert saved.document["consent"]["version"] == published.document["consent"]["version"]


def test_a_new_address_gets_the_first_consent_version_of_it(
    deps: Deps, scratch: drafts.Draft
) -> None:
    saved = drafts.save_document(
        deps,
        SCRATCH,
        {**scratch.document, "slug": "gestao-2027"},
        actor=ACTOR,
        expected=scratch.token,
        new_slug="gestao-2027",
    )

    assert saved.document["consent"]["version"] == "gestao-2027-v1"


# --- replace_document e patch_document: a API troca o documento ou o remenda ---------------


def test_replace_document_takes_the_address_of_the_document_as_the_wish_to_rename(
    deps: Deps, scratch: drafts.Draft
) -> None:
    saved = drafts.replace_document(
        deps,
        SCRATCH,
        {**scratch.document, "slug": "gestao-2027"},
        actor=ACTOR,
        expected=scratch.token,
    )

    assert saved.slug == "gestao-2027"


def test_patch_document_edits_a_copy_and_stores_the_checked_result(
    deps: Deps, published: drafts.Draft
) -> None:
    saved = drafts.patch_document(
        deps,
        SLUG,
        [{"op": "replace", "path": "/intro", "value": "Pelo patch."}],
        actor=ACTOR,
        expected=published.token,
    )

    assert saved.document["intro"] == "Pelo patch." and saved.pending
    assert saved.document["title"] == published.document["title"]


def test_patch_document_is_all_or_nothing_even_when_the_valid_operations_come_first(
    deps: Deps, published: drafts.Draft
) -> None:
    with pytest.raises(DefinitionError) as refused:
        drafts.patch_document(
            deps,
            SLUG,
            [
                {"op": "replace", "path": "/intro", "value": "Vale."},
                {"op": "remove", "path": "/nao-existe"},
            ],
            actor=ACTOR,
            expected=published.token,
        )

    assert [problem.path for problem in refused.value.problems] == ["patch.1"]
    assert draft_of(deps, SLUG).saved is False


def test_patch_document_checks_the_whole_result_like_a_save(
    deps: Deps, published: drafts.Draft
) -> None:
    with pytest.raises(DefinitionError) as refused:
        drafts.patch_document(
            deps,
            SLUG,
            [{"op": "replace", "path": "/minutes", "value": 0}],
            actor=ACTOR,
            expected=published.token,
        )

    assert [problem.path for problem in refused.value.problems] == ["minutes"]
    assert draft_of(deps, SLUG).saved is False


def test_patch_document_on_a_draft_that_changed_or_a_form_that_does_not_exist_is_refused(
    deps: Deps, published: drafts.Draft
) -> None:
    with pytest.raises(drafts.DraftChanged):
        drafts.patch_document(
            deps,
            SLUG,
            [{"op": "replace", "path": "/intro", "value": "X"}],
            actor=ACTOR,
            expected="velho",
        )

    with pytest.raises(UnknownForm):
        drafts.patch_document(deps, "nao-existe", [], actor=ACTOR, expected="x")
