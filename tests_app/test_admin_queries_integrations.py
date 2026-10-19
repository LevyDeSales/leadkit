"""A consulta da integração com o Baserow que a página e a API dividem
(`app.admin.queries.integrations`): uma linha por campo da tabela, com o que o preenche e as fontes que
servem a ele, sem rótulo de tela.

`field_choices` é função pura: a tabela vem pronta (`live_fields`) e o catálogo de fontes, de um
formulário do repositório. O que a página faz dessas escolhas (o rótulo do tipo, os grupos) está nos
testes da página (`test_admin_integrations.py`).
"""

from __future__ import annotations

import pytest
from conftest import baserow_field, legacy_spec
from test_integration_mapping import FULL_MAPPING, live_fields

from app.admin.queries import integrations as queries
from app.integrations import mapping


def choices(
    entries: list[mapping.Entry] | None = None,
    typed: dict[int, str] | None = None,
    raw: list[dict[str, object]] | None = None,
) -> dict[int, queries.FieldChoice]:
    catalog = mapping.catalog(legacy_spec("demo"))
    found = queries.field_choices(live_fields(raw), catalog, entries or [], typed)  # type: ignore[arg-type]
    return {item.id: item for item in found}


def test_with_nothing_mapped_yet_the_name_of_each_field_suggests_its_source() -> None:
    found = choices()

    assert (found[101].selected, found[101].suggested) == ("lead.name", True)
    assert (found[102].selected, found[104].selected) == ("lead.email", "job.public_id")
    assert found[108].selected == ""  # "Equipe" não é o nome de nada: nenhuma sugestão
    assert all(item.suggested for item in found.values() if item.selected)


def test_a_saved_mapping_wins_over_the_suggestion_and_leaves_the_rest_unmapped() -> None:
    saved = [mapping.Entry(101, "Nome", "text", "lead.phone")]

    found = choices(entries=saved)

    assert (found[101].selected, found[101].suggested) == ("lead.phone", False)
    assert (found[102].selected, found[102].suggested) == (
        "",
        False,
    )  # sem sugestão depois de salvo


def test_what_the_person_just_sent_wins_over_the_saved_mapping_entirely() -> None:
    found = choices(entries=FULL_MAPPING, typed={101: "lead.email"})

    assert found[101].selected == "lead.email"
    assert found[102].selected == ""  # o que não veio é "nada", e não o que estava guardado
    assert not any(item.suggested for item in found.values())


def test_a_field_the_integration_cannot_fill_says_why_and_offers_no_source() -> None:
    found = choices(
        raw=[
            baserow_field(1, "Criado em", "created_on", read_only=True),
            baserow_field(2, "Nota", "rating"),
            baserow_field(3, "Nome", "text"),
        ]
    )

    assert (found[1].unsupported, found[1].sources, found[1].selected) == ("read_only", (), "")
    assert (found[2].unsupported, found[2].sources) == ("unsupported_type", ())
    assert found[3].unsupported is None and "lead.name" in found[3].sources


def test_each_field_offers_only_the_sources_that_fit_its_type() -> None:
    found = choices()

    assert "lead.name" in found[101].sources and "consent.privacy" in found[101].sources
    assert found[107].sources == (
        "consent.privacy",
        "consent.marketing_email",
        "consent.marketing_whatsapp",
    )  # um sim ou não só vem de um consentimento
    assert found[108].sources == ("answer:tamanho_equipe", "answer:principal_desafio")
    assert found[101].sources[0] == "lead.name"  # a ordem do catálogo


def test_the_primary_field_and_the_type_are_the_ones_baserow_described() -> None:
    found = choices()

    assert found[101].primary and not found[102].primary
    assert (found[102].type, found[108].type, found[110].type) == (
        "email",
        "single_select",
        "created_on",
    )


@pytest.mark.parametrize(
    ("kind", "field_type", "text"),
    [
        ("read_only", "created_on", "campo de somente leitura"),
        ("unsupported_type", "rating", "tipo «rating» sem suporte"),
        (None, "text", None),
    ],
)
def test_the_reason_text_of_a_field_is_built_from_its_kind(
    kind: str | None, field_type: str, text: str | None
) -> None:
    assert mapping.reason_text(kind, field_type) == text


@pytest.mark.parametrize(
    ("last_error", "code"),
    [
        ("[integration_unavailable] criar a linha: HTTP 503", "integration_unavailable"),
        (
            "[integration_rejected] atualizar a linha: HTTP 400 (ERROR_REQUEST_BODY_VALIDATION)",
            "integration_rejected",
        ),
        ("integration_mapping", "integration_mapping"),  # só o código, quando o erro não ficou
        ("[codigo_que_nao_existe] algo", None),
        ("um texto qualquer, com o nome de alguém", None),
        ("", None),
        (None, None),
    ],
)
def test_the_failure_code_of_a_sync_is_the_known_code_and_never_the_free_text(
    last_error: str | None, code: str | None
) -> None:
    assert queries.failure_code_of(last_error) == code
