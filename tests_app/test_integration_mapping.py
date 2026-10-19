"""O mapeamento de uma integração, em funções puras: as fontes, o casamento das opções de seleção
pelo rótulo, o relatório de antes de ativar e a montagem da linha."""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import baserow_field, demo_baserow_fields, legacy_definition, legacy_spec

from app.forms import FormSpec, Option
from app.forms.definition import compile_definition
from app.integrations import mapping
from app.integrations.baserow import BaserowField, BaserowOption
from app.integrations.mapping import Entry, RowData

SOURCES_QUESTION = {
    "name": "fontes",
    "kind": "choices",
    "label": "De onde vêm seus clientes?",
    "required": False,
    "options": [
        {"value": "indicacao", "label": "Indicação"},
        {"value": "instagram", "label": "Instagram"},
        {"value": "outro", "label": "Outro"},
    ],
}


def demo_with_sources() -> FormSpec:
    """O demo com uma pergunta de várias opções a mais, opcional."""
    document = legacy_definition("demo")
    document["steps"][1]["questions"].append(dict(SOURCES_QUESTION))
    return compile_definition(document, version_number=2)


def live_fields(raw: list[dict[str, Any]] | None = None) -> list[BaserowField]:
    from app.integrations.baserow import _field_of

    return [_field_of(item) for item in (raw if raw is not None else demo_baserow_fields())]


def entry(field_id: int, name: str, kind: str, source: str) -> Entry:
    return Entry(field_id, name, kind, source)


FULL_MAPPING = [
    entry(101, "Nome", "text", "lead.name"),
    entry(102, "E-mail", "email", "lead.email"),
    entry(103, "Telefone", "phone_number", "lead.phone"),
    entry(104, "Código do pedido", "text", "job.public_id"),
    entry(105, "Respostas", "long_text", "job.answers_json"),
    entry(106, "PDF", "url", "job.pdf_url"),
    entry(107, "Consentiu", "boolean", "consent.privacy"),
    entry(108, "Equipe", "single_select", "answer:tamanho_equipe"),
    entry(109, "Desafio", "single_select", "answer:principal_desafio"),
]


def row_data(spec: FormSpec, **overrides: Any) -> RowData:
    base = mapping.sample_data(spec, "pedido-1")
    values = {**base.__dict__, **overrides}
    return RowData(**values)


# --- o texto das opções ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("form_label", "baserow_label"),
    [
        ("Só eu", "so eu"),
        ("De 2 a 5 pessoas", "  DE 2 A 5   PESSOAS "),
        ("1–10", "1-10"),
        ("Atrair novos clientes", "Atrair novos clientes"),
        ("Organizar a operação", "Organizar a operacao"),
    ],
)
def test_an_option_matches_by_its_label_ignoring_case_accents_spaces_and_dashes(
    form_label: str, baserow_label: str
) -> None:
    (pair,) = mapping.pair_options(
        [Option("v", form_label)], [BaserowOption(1, "Outra"), BaserowOption(2, baserow_label)]
    )

    assert pair.baserow == BaserowOption(2, baserow_label)


def test_an_option_without_a_baserow_pair_is_reported_and_the_value_is_not_used_to_guess() -> None:
    pairs = mapping.pair_options(
        [Option("so_eu", "Só eu"), Option("equipe", "Tenho equipe")],
        [BaserowOption(1, "Só eu"), BaserowOption(2, "so_eu")],  # o valor interno não casa
    )

    assert [pair.baserow for pair in pairs] == [BaserowOption(1, "Só eu"), None]


def test_the_exact_label_wins_over_one_that_only_matches_loosely() -> None:
    (pair,) = mapping.pair_options(
        [Option("a", "Café")], [BaserowOption(1, "cafe"), BaserowOption(2, "Café")]
    )

    assert pair.baserow == BaserowOption(2, "Café")


# --- as fontes ------------------------------------------------------------------------------


def test_the_catalog_has_the_lead_and_submission_data_and_one_source_per_answer() -> None:
    sources = mapping.catalog(demo_with_sources())

    assert {"lead.name", "lead.email", "lead.phone", "job.public_id", "job.pdf_url"} <= set(sources)
    assert sources["answer:tamanho_equipe"].kind == "choice"
    assert sources["answer:fontes"].kind == "choices"
    assert sources["answer:objetivo_90_dias"].kind == "text"
    groups = [group for group, _ in mapping.grouped(sources)]
    assert groups == ["Dados da lead", "Dados do envio", "Respostas do formulário"]


@pytest.mark.parametrize(
    ("source", "field_type", "fits"),
    [
        ("lead.name", "text", True),
        ("lead.name", "single_select", False),
        ("lead.name", "boolean", False),
        ("consent.privacy", "boolean", True),
        ("consent.privacy", "text", True),
        ("answer:tamanho_equipe", "single_select", True),
        ("answer:tamanho_equipe", "multiple_select", True),
        ("answer:fontes", "multiple_select", True),
        ("answer:fontes", "single_select", False),
        ("answer:objetivo_90_dias", "single_select", False),
        ("answer:fontes", "created_on", False),
    ],
)
def test_a_source_fits_the_field_types_that_can_hold_it(
    source: str, field_type: str, fits: bool
) -> None:
    assert mapping.compatible(mapping.catalog(demo_with_sources())[source], field_type) is fits


def test_the_field_name_suggests_the_source_when_the_reading_is_clear() -> None:
    sources = mapping.catalog(demo_with_sources())
    fields = {item.name: item for item in live_fields()}

    assert mapping.suggest(fields["Nome"], sources) == "lead.name"
    assert mapping.suggest(fields["E-mail"], sources) == "lead.email"
    assert mapping.suggest(fields["Telefone"], sources) == "lead.phone"
    assert mapping.suggest(fields["Código do pedido"], sources) == "job.public_id"
    assert mapping.suggest(fields["Respostas"], sources) == "job.answers_json"
    assert mapping.suggest(fields["Equipe"], sources) == ""  # nenhuma pergunta com este nome
    assert mapping.suggest(fields["Criado em"], sources) == ""  # somente leitura


def test_only_the_field_types_the_integration_fills_are_offered() -> None:
    fields = {item.name: item for item in live_fields()}
    other = BaserowField(900, "Valor", "number")

    assert mapping.unsupported_reason(fields["Nome"]) is None
    assert "somente leitura" in (mapping.unsupported_reason(fields["Criado em"]) or "")
    assert "«number»" in (mapping.unsupported_reason(other) or "")


# --- guardar um mapeamento ------------------------------------------------------------------


def test_a_mapping_needs_exactly_one_field_for_the_order_code_and_a_text_one() -> None:
    sources = mapping.catalog(demo_with_sources())
    live = live_fields()
    without_key = [item for item in FULL_MAPPING if item.source != "job.public_id"]
    twice = [*FULL_MAPPING, entry(105, "Respostas", "long_text", "job.public_id")]
    on_boolean = [
        entry(107, "Consentiu", "boolean", "job.public_id") if item.field_id == 104 else item
        for item in FULL_MAPPING
    ]

    mapping.check_entries(FULL_MAPPING, live, sources)  # não levanta
    for bad in (without_key, twice):
        with pytest.raises(mapping.InvalidMapping, match="código do pedido"):
            mapping.check_entries(bad, live, sources)
    with pytest.raises(mapping.InvalidMapping, match="é de .* e não recebe|não recebe"):
        mapping.check_entries(on_boolean, live, sources)


def test_a_mapping_refuses_a_field_that_is_gone_read_only_or_given_a_wrong_source() -> None:
    sources = mapping.catalog(demo_with_sources())
    live = live_fields()
    bad = [
        *FULL_MAPPING,
        entry(999, "Sumiu", "text", "lead.name"),
        entry(110, "Criado em", "created_on", "lead.name"),
        entry(105, "Respostas", "long_text", "answer:nao_existe"),
    ]

    with pytest.raises(mapping.InvalidMapping) as raised:
        mapping.check_entries(bad, live, sources)

    text = " ".join(raised.value.problems)
    assert "«Sumiu» não existe" in text
    assert "«Criado em» não pode ser preenchido" in text
    assert "recebe uma fonte que o formulário não tem" in text


# --- o relatório ----------------------------------------------------------------------------


def test_the_report_is_clean_when_every_option_has_a_pair() -> None:
    report = mapping.build_report(FULL_MAPPING, live_fields(), demo_with_sources())

    assert report.ok and report.problems == () and report.unmatched == 0
    team = next(item for item in report.fields if item.entry.field_id == 108)
    assert [pair.baserow.value for pair in team.pairs if pair.baserow] == [
        "Só eu",
        "De 2 a 5 pessoas",
        "De 6 a 15 pessoas",
        "Mais de 15 pessoas",
    ]


def test_the_report_lists_the_options_that_have_no_pair_and_does_not_pass() -> None:
    fields = demo_baserow_fields()
    fields[7] = baserow_field(108, "Equipe", "single_select", options=("Só eu", "De 2 a 5 pessoas"))

    report = mapping.build_report(FULL_MAPPING, live_fields(fields), demo_with_sources())

    assert not report.ok and report.unmatched == 2
    team = next(item for item in report.fields if item.entry.field_id == 108)
    assert [pair.label for pair in team.unmatched] == [
        "De 6 a 15 pessoas",
        "Mais de 15 pessoas",
    ]


def test_the_report_flags_a_field_that_left_the_table_changed_type_or_lost_its_question() -> None:
    fields = demo_baserow_fields()
    del fields[0]  # o campo do nome saiu da tabela
    fields[4] = baserow_field(106, "PDF", "number")  # o link virou número
    fields.append(baserow_field(111, "Fontes", "multiple_select", options=("Indicação",)))
    mapped = [*FULL_MAPPING, entry(111, "Fontes", "multiple_select", "answer:fontes")]

    # A versão atual do formulário (a 1) não tem a pergunta `fontes`, que só a versão 2 trouxe.
    report = mapping.build_report(mapped, live_fields(fields), legacy_spec("demo"))

    text = " ".join(report.problems)
    assert not report.ok
    assert "«Nome» não existe mais" in text
    assert "«PDF» mudou de tipo" in text
    assert "«Fontes» recebe uma fonte que a versão atual do formulário não tem" in text


def test_the_report_asks_for_the_key_and_for_something_to_fill() -> None:
    empty = mapping.build_report([], live_fields(), demo_with_sources())
    no_key = mapping.build_report(
        [item for item in FULL_MAPPING if item.source != "job.public_id"],
        live_fields(),
        demo_with_sources(),
    )

    assert not empty.ok and "Nenhum campo está mapeado" in empty.problems[0]
    assert not no_key.ok and "código do pedido não está mapeado" in no_key.problems[0]


# --- a linha --------------------------------------------------------------------------------


def test_the_row_has_each_value_where_the_mapping_puts_it_with_the_option_ids() -> None:
    spec = demo_with_sources()
    data = row_data(
        spec,
        lead_name="Ana Souza",
        answers={
            "tamanho_equipe": "2_a_5",
            "principal_desafio": "captacao",
            "objetivo_90_dias": "Crescer",
        },
    )

    row = mapping.build_row(FULL_MAPPING, live_fields(), data)

    assert row["field_101"] == "Ana Souza"
    assert row["field_102"] == "teste@example.test"
    assert row["field_104"] == "pedido-1"
    assert json.loads(row["field_105"])["objetivo_90_dias"] == "Crescer"
    assert row["field_107"] is True
    assert row["field_108"] == 10802  # "De 2 a 5 pessoas"
    assert row["field_109"] == 10901  # "Atrair novos clientes"
    assert set(row) == {f"field_{item.field_id}" for item in FULL_MAPPING}


def test_a_multiple_select_gets_the_ids_of_every_chosen_option() -> None:
    fields = [
        *demo_baserow_fields(),
        baserow_field(
            111, "Fontes", "multiple_select", options=("indicação", "INSTAGRAM", "Outro")
        ),
    ]
    spec = demo_with_sources()
    data = row_data(spec, answers={"fontes": ["indicacao", "outro"]})

    row = mapping.build_row(
        [entry(111, "Fontes", "multiple_select", "answer:fontes")], live_fields(fields), data
    )
    nothing = mapping.build_row(
        [entry(111, "Fontes", "multiple_select", "answer:fontes")],
        live_fields(fields),
        row_data(spec, answers={}),
    )

    assert row == {"field_111": [11101, 11103]}
    assert nothing == {"field_111": []}


def test_a_single_select_without_an_answer_clears_the_field() -> None:
    row = mapping.build_row(
        [entry(108, "Equipe", "single_select", "answer:tamanho_equipe")],
        live_fields(),
        row_data(demo_with_sources(), answers={}),
    )

    assert row == {"field_108": None}


def test_an_option_the_table_does_not_have_stops_the_row_naming_the_question_not_the_answer() -> (
    None
):
    fields = demo_baserow_fields()
    fields[7] = baserow_field(108, "Equipe", "single_select", options=("Só eu",))
    data = row_data(demo_with_sources(), answers={"tamanho_equipe": "6_a_15"})

    with pytest.raises(mapping.UnmatchedOption) as raised:
        mapping.build_row(
            [entry(108, "Equipe", "single_select", "answer:tamanho_equipe")],
            live_fields(fields),
            data,
        )

    assert "«Equipe»" in str(raised.value) and "Quantas pessoas" in str(raised.value)
    assert "6_a_15" not in str(raised.value) and "De 6 a 15" not in str(raised.value)


def test_text_fields_get_the_label_the_lead_saw_for_a_choice_and_a_list_for_several() -> None:
    fields = [
        baserow_field(120, "Equipe em texto", "text"),
        baserow_field(121, "Fontes em texto", "long_text"),
        baserow_field(122, "Consentiu em texto", "text"),
    ]
    data = row_data(
        demo_with_sources(), answers={"tamanho_equipe": "2_a_5", "fontes": ["indicacao", "outro"]}
    )

    row = mapping.build_row(
        [
            entry(120, "Equipe em texto", "text", "answer:tamanho_equipe"),
            entry(121, "Fontes em texto", "long_text", "answer:fontes"),
            entry(122, "Consentiu em texto", "text", "consent.privacy"),
        ],
        live_fields(fields),
        data,
    )

    assert row == {
        "field_120": "De 2 a 5 pessoas",
        "field_121": "Indicação, Outro",
        "field_122": "Sim",
    }


def test_the_marketing_flags_follow_the_consent_the_lead_gave() -> None:
    fields = [
        baserow_field(130, "Aceitou e-mail", "boolean"),
        baserow_field(131, "Aceitou zap", "boolean"),
    ]
    data = row_data(
        demo_with_sources(),
        consent={"privacy_at": "2026-10-15T10:00:00+00:00", "marketing_email": True},
    )

    row = mapping.build_row(
        [
            entry(130, "Aceitou e-mail", "boolean", "consent.marketing_email"),
            entry(131, "Aceitou zap", "boolean", "consent.marketing_whatsapp"),
        ],
        live_fields(fields),
        data,
    )

    assert row == {"field_130": True, "field_131": False}


def test_a_field_that_changed_type_or_left_the_table_stops_the_row() -> None:
    data = row_data(demo_with_sources())

    with pytest.raises(mapping.MappingError, match="mudou de tipo"):
        mapping.build_row(
            [entry(106, "PDF", "url", "job.pdf_url")],
            live_fields([baserow_field(106, "PDF", "boolean")]),
            data,
        )
    with pytest.raises(mapping.MappingError, match="não existe mais"):
        mapping.build_row([entry(106, "PDF", "url", "job.pdf_url")], live_fields([]), data)


def test_an_answer_from_an_older_version_uses_the_labels_of_that_version() -> None:
    older = legacy_definition("demo")
    for step in older["steps"]:
        for question in step["questions"]:
            for option in question.get("options", ()):
                if option["value"] == "2_a_5":
                    option["label"] = "Entre 2 e 5 pessoas"
    fields = demo_baserow_fields()
    fields[7] = baserow_field(
        108, "Equipe", "single_select", options=("Entre 2 e 5 pessoas", "Só eu")
    )
    data = row_data(
        compile_definition(older, version_number=1), answers={"tamanho_equipe": "2_a_5"}
    )

    row = mapping.build_row(
        [entry(108, "Equipe", "single_select", "answer:tamanho_equipe")], live_fields(fields), data
    )

    assert row == {"field_108": 10801}


# --- o envio de teste -----------------------------------------------------------------------


def test_the_sample_data_is_synthetic_and_fills_every_question() -> None:
    spec = demo_with_sources()

    data = mapping.sample_data(spec, "teste-demo")

    assert data.public_id == "teste-demo"
    assert data.lead_email.endswith("@example.test") and data.lead_name == "Contadora Exemplo"
    assert data.answers["tamanho_equipe"] == "so_eu"  # a primeira opção
    assert data.answers["fontes"] == ["indicacao", "instagram"]  # as duas primeiras
    assert set(data.answers) == {question.name for question in mapping.questions_of(spec)}
    row = mapping.build_row(FULL_MAPPING, live_fields(), data)  # e a linha de teste monta
    assert row["field_108"] == 10801
