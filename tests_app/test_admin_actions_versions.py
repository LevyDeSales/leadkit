"""A comparação de versões de um formulário (`app.admin.actions.versions`): quais duas versões, ou uma
e o rascunho, e o que mudou seção por seção. É o que a página `/admin/forms/{slug}/versions/compare`
e a API (`compare_form_versions`) dividem; as páginas têm os testes próprios."""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_admin_forms import DEFINITION, SLUG, store_version

from app.admin.actions import versions
from app.deps import Deps
from app.forms import UnknownForm, drafts, stored
from app.forms.definition import fingerprint
from app.models import Form, FormDraft, utcnow

NEWER = "A segunda versão."
OTHER = "Uma introdução só do rascunho."


@pytest.fixture
def two_versions(deps: Deps) -> None:
    """O formulário importado (versão 1) e uma versão 2 com outra introdução."""
    stored.import_definition(deps, DEFINITION, actor="testes")
    store_version(deps, 2, {**json.loads(DEFINITION), "intro": NEWER})


def compare(deps: Deps, slug: str = SLUG, **refs: str) -> versions.Comparison:
    with deps.sessions() as session:
        return versions.compare(session, slug, **refs)


def intro_change(result: versions.Comparison) -> versions.SectionChange:
    (section,) = [item for item in result.sections if item.key == "pagina"]
    return section


def save_draft(deps: Deps, **changes: Any) -> None:
    """Um rascunho pendente, gravado como o editor o grava."""
    with deps.sessions() as session:
        draft = drafts.load(session, SLUG)
    assert draft is not None
    texts = drafts.texts_of({**draft.document, **changes})
    drafts.save(deps, SLUG, texts, actor="testes", expected=draft.token)


def write_same_meaning_draft(deps: Deps) -> None:
    """Um rascunho guardado que só escreve por extenso um padrão do formato: a impressão digital
    difere da versão atual, mas ele diz o mesmo (`Draft.pending` é falso)."""
    document = json.loads(DEFINITION)
    document["intro"] = NEWER  # o mesmo texto da versão atual (a 2)
    question = document["steps"][0]["questions"][0]
    question["required"] = True  # o padrão do formato, escrito por extenso
    with deps.sessions.begin() as session:
        form_id = session.query(Form.id).filter(Form.slug == SLUG).scalar()
        session.add(
            FormDraft(
                form_id=form_id,
                definition=document,
                fingerprint=fingerprint(document),
                base_version=2,
                updated_by="testes",
                updated_at=utcnow(),
            )
        )


# --- sem escolha: o que a página faz ------------------------------------------------------------


def test_without_a_choice_the_current_version_is_compared_with_the_one_before(
    deps: Deps, two_versions: None
) -> None:
    result = compare(deps)

    assert (result.old, result.new) == ("1", "2")
    section = intro_change(result)
    assert section.changed and any(NEWER in line.text for line in section.lines)
    assert result.numbers == (2, 1)  # as versões publicadas, a mais recente primeiro
    assert result.draft.slug == SLUG and result.draft.pending is False


def test_without_a_choice_and_with_a_pending_draft_the_current_version_meets_the_draft(
    deps: Deps, two_versions: None
) -> None:
    save_draft(deps, intro=OTHER)

    result = compare(deps)

    assert (result.old, result.new) == ("2", versions.DRAFT_REF)
    assert any(OTHER in line.text for line in intro_change(result).lines)
    assert result.draft.pending is True


def test_a_draft_that_only_writes_out_the_defaults_is_not_pending_so_the_choice_ignores_it(
    deps: Deps, two_versions: None
) -> None:
    """A escolha padrão olha `Draft.pending` (o rascunho diz outra coisa), e não a impressão digital
    do rascunho: um rascunho com os padrões por extenso tem impressão digital diferente e diz o
    mesmo, e comparar a versão atual com ele não mostraria nada."""
    write_same_meaning_draft(deps)

    result = compare(deps)

    assert result.draft.saved and result.draft.token != result.draft.current.fingerprint  # type: ignore[union-attr]
    assert result.draft.pending is False
    assert (result.old, result.new) == ("1", "2")


def test_the_first_version_without_a_choice_is_compared_with_itself(deps: Deps) -> None:
    stored.import_definition(deps, DEFINITION, actor="testes")

    result = compare(deps)

    assert (result.old, result.new) == ("1", "1")
    assert not any(section.changed for section in result.sections)


# --- com escolha ---------------------------------------------------------------------------------


def test_two_versions_chosen_are_compared_in_that_order(deps: Deps, two_versions: None) -> None:
    forward = compare(deps, old="1", new="2")
    backward = compare(deps, old="2", new="1")

    assert (forward.old, forward.new) == ("1", "2")
    assert any(line.kind == "add" and NEWER in line.text for line in intro_change(forward).lines)
    assert any(line.kind == "del" and NEWER in line.text for line in intro_change(backward).lines)


def test_a_version_can_be_compared_with_the_draft(deps: Deps, two_versions: None) -> None:
    save_draft(deps, intro=OTHER)

    result = compare(deps, old="1", new=versions.DRAFT_REF)

    assert (result.old, result.new) == ("1", versions.DRAFT_REF)
    assert any(line.kind == "add" and OTHER in line.text for line in intro_change(result).lines)


def test_one_side_chosen_and_the_other_left_to_the_default(deps: Deps, two_versions: None) -> None:
    save_draft(deps, intro=OTHER)

    only_old = compare(deps, old="1")
    only_new = compare(deps, new="1")

    assert (only_old.old, only_old.new) == ("1", versions.DRAFT_REF)
    assert (only_new.old, only_new.new) == ("2", "1")


# --- o que não se compara ------------------------------------------------------------------------


def test_a_form_that_does_not_exist_cannot_be_compared(deps: Deps) -> None:
    with pytest.raises(UnknownForm):
        compare(deps, "nao-existe")


def test_a_form_without_a_published_version_has_nothing_to_compare(deps: Deps) -> None:
    drafts.create(deps, title="Gestão do escritório", slug="gestao", actor="testes")

    with pytest.raises(versions.NothingToCompare):
        compare(deps, "gestao")


@pytest.mark.parametrize("ref", ["3", "0", "um", "-1", "1.5", "draft", "1" * 10, "٣"])
def test_a_version_that_does_not_exist_cannot_be_compared(
    deps: Deps, two_versions: None, ref: str
) -> None:
    with pytest.raises(versions.UnknownVersion):
        compare(deps, old=ref)
    with pytest.raises(versions.UnknownVersion):
        compare(deps, new=ref)


def test_the_number_of_a_version_is_digits_and_at_most_nine_of_them() -> None:
    assert versions.version_number("12") == 12
    assert [versions.version_number(text) for text in ("", "a", "-1", "1.0", "١", "1" * 10)] == [
        None
    ] * 6
