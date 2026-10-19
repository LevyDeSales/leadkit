"""O editor do modelo do plano e dos dados do formulário, pela costura HTTP (TestClient), como o
navegador o usa: a árvore do plano (texto, listas, grupos e opções fixas, com os limites, os campos
internos e as validações declarativas) e os blocos de dados com tipo (listas, janelas e edição), em
campos, sem JSON à mão. Incluir, remover e reordenar voltam a página inteira sem JavaScript e só o
trecho do editor com o HTMX."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

import httpx
import pytest
from conftest import legacy_definition
from fastapi.testclient import TestClient
from test_admin import ORIGIN, FakeClock, csrf_token, login
from test_admin_destinations import ADMIN_ENV
from test_admin_drafts import (
    Fields,
    TestTrigger,
    editor,
    invalid_fields,
    post,
    problems_shown,
    save,
)
from test_admin_form_editor import HTMX, autofocused, form_of, pending, stored
from test_admin_forms import DEFINITION, SLUG, import_form, section_of
from test_public import form_fields, tags, text

from app.api.main import create_app
from app.deps import Deps
from app.settings import Settings

LESSONS = ["MÓDULO 1 — A1: Boas Vindas", "MÓDULO 2 — A1: Proposta de Valor", "MÓDULO 3 — A1: Venda"]
RICH_SLUG = "demo-arvore"
RICH_PLAN: dict[str, Any] = {
    "description": "Plano de 90 dias em fases.",
    "fields": [
        {
            "name": "titulo",
            "kind": "text",
            "min_length": 3,
            "max_length": 120,
            "forbidden_pattern": {"pattern": r"\d", "message": "Sem números no título."},
        },
        {
            "name": "mensagem",
            "kind": "text",
            "description": "A mensagem final.",
            "max_length": 2000,
            "min_paragraphs": 2,
        },
        {"name": "decisao", "kind": "options", "internal": True, "values": ["abrir", "fechar"]},
        {"name": "aula", "kind": "options", "values_from": "aulas"},
        {
            "name": "fases",
            "kind": "list",
            "min_items": 1,
            "max_items": 3,
            "items": {
                "kind": "group",
                "model": "Fase",
                "fields": [
                    {"name": "nome", "kind": "text", "max_length": 60},
                    {
                        "name": "acoes",
                        "kind": "list",
                        "items": {"kind": "text", "max_length": 220},
                    },
                ],
            },
        },
    ],
    "forbidden": [
        {
            "expressions": ["economia garantida", "sem juros"],
            "scope": "except",
            "fields": ["titulo"],
        },
        {"expressions": ["única IA"]},
    ],
}
RICH_EXAMPLE: dict[str, Any] = {
    "titulo": "Plano organizado",
    "mensagem": "Primeiro parágrafo.\n\nSegundo parágrafo.",
    "decisao": "abrir",
    "aula": LESSONS[0],
    "fases": [{"nome": "Base", "acoes": ["Listar os clientes ativos."]}],
}
RICH_DATA: dict[str, Any] = {
    "aulas": {"kind": "list", "items": LESSONS},
    "janelas": {
        "kind": "windows",
        "windows": [
            {"name": "opcao", "label": "Opção", "start": "2026-09-25", "end": "2026-10-30"},
            {"name": "desistencia", "start": "2026-11-03", "end": "2026-12-20"},
        ],
    },
    "edicao": {"kind": "edition", "name": "Ciclo demonstrativo 2027", "ends": "2027-05-31"},
    "nota": {"kind": "value", "value": {"fonte": "Resolução de exemplo"}},
}


def rich_definition(**changes: Any) -> dict[str, Any]:
    """A definição do demo com o plano em árvore, as regras e os blocos de dados dos testes, com
    outro endereço."""
    document = json.loads(DEFINITION)
    document |= {
        "slug": RICH_SLUG,
        "plan": copy.deepcopy(RICH_PLAN),
        "data": copy.deepcopy(RICH_DATA),
    }
    document["consent"]["version"] = f"{RICH_SLUG}-v1"
    document["examples"]["plan"] = copy.deepcopy(RICH_EXAMPLE)
    document["pdf"] = {**document["pdf"], "html": "<h1>{{ plan.titulo }}</h1>"}
    document["preview"] = "<h2>{{ plan.titulo }}</h2>"
    return document | changes


RICH = f"/admin/forms/{RICH_SLUG}/draft"

MANY_SLUG = "demo-listas"
MANY = f"/admin/forms/{MANY_SLUG}/draft"
GROUPS: dict[str, Any] = {
    "combo": {"kind": "list", "items": ["Plano A", "Plano B"]},
    "ecossistema": {"kind": "list", "items": ["Grupo A", "Grupo B"]},
    "materiais": {"kind": "list", "items": ["Guia"]},
}
CHOICE = "plan.fields.3.values_from"  # o campo `aula`, que tira as opções das listas


def many_definition(lists: str | list[str]) -> dict[str, Any]:
    """O formulário do plano em árvore, com três listas nos dados e o campo `aula` tirando as
    opções de `lists` (um nome ou uma lista de nomes), com outro endereço."""
    document = rich_definition(slug=MANY_SLUG, data=copy.deepcopy(GROUPS))
    document["consent"]["version"] = f"{MANY_SLUG}-v1"
    document["plan"]["fields"][3] = {"name": "aula", "kind": "options", "values_from": lists}
    document["examples"]["plan"]["aula"] = "Plano B"
    return document


def positions_of(page: str, at: str) -> dict[str, str]:
    """O número da posição de cada lista no campo `at` (`plan.fields.3`), como a página o põe: o
    das marcadas, 1, 2, 3..., e em branco o das outras."""
    boxes = tags(page, "input", name=f"{at}.values_from", type="checkbox")
    return {
        str(box["value"]): str(
            tags(page, "input", name=f"{at}.values_order.{box['value']}")[0]["value"]
        )
        for box in boxes
    }


def boxes_of(page: str, name: str) -> list[tuple[str | None, bool]]:
    """As caixas de marcar com este nome, na ordem da página: o valor e se está marcada."""
    return [
        (box.get("value"), "checked" in box)
        for box in tags(page, "input", name=name, type="checkbox")
    ]


def form_of_refused(response: httpx.Response) -> Fields:
    """O formulário que a página devolve quando o salvar recusou o rascunho."""
    assert response.status_code == 422, response.text
    return form_fields(response.text, "editor-form")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def worker(deps: Deps, clock: FakeClock) -> TestTrigger:
    return TestTrigger(deps, clock)


@pytest.fixture
def client(deps: Deps, clock: FakeClock, worker: TestTrigger) -> TestClient:
    """A operadora no admin, com o demo importado (versão 1)."""
    deps.settings = Settings(_env_file=None, **ADMIN_ENV)  # type: ignore[call-arg]

    async def fake_trigger(job_id: Any, attempt: int) -> str | None:
        return "run-fake"

    current = TestClient(
        create_app(trigger=fake_trigger, test_trigger=worker, clock=clock),
        follow_redirects=False,
    )
    assert login(current).status_code == 303
    assert import_form(current, DEFINITION).status_code == 303
    return current


@pytest.fixture
def rich(client: TestClient) -> TestClient:
    """A operadora com o formulário do plano em árvore importado, ao lado do demo."""
    assert import_form(client, json.dumps(rich_definition())).status_code == 303
    return client


@pytest.fixture
def many(client: TestClient) -> TestClient:
    """A operadora com o formulário das três listas importado (o campo `aula` usa `materiais` e
    `combo`, nessa ordem), ao lado do demo."""
    assert (
        import_form(client, json.dumps(many_definition(["materiais", "combo"]))).status_code == 303
    )
    return client


def many_stored(deps: Deps) -> dict[str, Any]:
    return stored(deps, MANY_SLUG)


def rich_stored(deps: Deps) -> dict[str, Any]:
    return stored(deps, RICH_SLUG)


def operate(
    client: TestClient, fields: Fields, op: str, *, htmx: bool = False, draft: str = RICH
) -> httpx.Response:
    headers = {**ORIGIN, **(HTMX if htmx else {})}
    data = {**fields, "csrf_token": csrf_token(client), "op": op}
    return client.post(f"{draft}/edit", data=data, headers=headers)


def fields_of(client: TestClient, draft: str = RICH) -> Fields:
    return editor(client, draft)


def step(client: TestClient, fields: Fields, op: str) -> Fields:
    """Uma operação do editor, e o formulário que a página devolve."""
    return form_of(operate(client, fields, op))


# --- a árvore do plano em campos -------------------------------------------------------------


def test_the_editor_shows_the_plan_tree_as_fields_and_no_longer_as_json(client: TestClient) -> None:
    fields = editor(client)

    assert fields["plan.description"] == ""
    assert (fields["plan.fields.0.name"], fields["plan.fields.0.kind"]) == ("titulo", "text")
    assert (fields["plan.fields.0.min_length"], fields["plan.fields.0.max_length"]) == ("3", "120")
    assert fields["plan.fields.2.name"] == "acoes"
    assert fields["plan.fields.2.kind"] == "list"
    assert (fields["plan.fields.2.min_items"], fields["plan.fields.2.max_items"]) == ("3", "5")
    assert fields["plan.fields.2.items.kind"] == "text"
    assert "plan.fields.2.items.name" not in fields  # os itens de uma lista não têm nome
    assert "plan.fields.0.internal" not in fields
    rest = json.loads(fields["definition"])
    assert "plan" not in rest and "data" not in rest
    assert "plan" in rest["examples"]  # o plano de exemplo segue no JSON


def test_the_editor_shows_groups_internal_fields_options_rules_and_data(rich: TestClient) -> None:
    fields = fields_of(rich)

    assert fields["plan.description"] == "Plano de 90 dias em fases."
    assert fields["plan.fields.0.forbidden_pattern.pattern"] == r"\d"
    assert fields["plan.fields.0.forbidden_pattern.message"] == "Sem números no título."
    assert fields["plan.fields.1.min_paragraphs"] == "2"
    assert fields["plan.fields.1.description"] == "A mensagem final."
    assert fields["plan.fields.2.internal"] == "on"
    assert fields["plan.fields.2.values"] == "abrir\nfechar"
    assert fields["plan.fields.3.values_from"] == "aulas"
    assert fields["plan.fields.4.items.kind"] == "group"
    assert fields["plan.fields.4.items.model"] == "Fase"
    assert fields["plan.fields.4.items.fields.0.name"] == "nome"
    assert fields["plan.fields.4.items.fields.1.items.kind"] == "text"
    assert fields["plan.fields.4.items.fields.1.items.max_length"] == "220"
    assert fields["plan.forbidden.0.expressions"] == "economia garantida\nsem juros"
    assert fields["plan.forbidden.0.scope"] == "except"
    assert fields["plan.forbidden.0.fields"] == "titulo"
    assert fields["plan.forbidden.1.scope"] == "all"
    assert fields["data.0.name"] == "aulas" and fields["data.0.kind"] == "list"
    assert fields["data.0.items"] == "\n".join(LESSONS)
    assert fields["data.2.kind"] == "windows" and fields["data.2.sequential"] == "yes"
    assert fields["data.2.windows.0.start"] == "2026-09-25"
    assert fields["data.2.windows.1.label"] == ""
    assert fields["data.1.edition.name"] == "Ciclo demonstrativo 2027"
    assert (fields["data.1.edition.starts"], fields["data.1.edition.ends"]) == ("", "2027-05-31")
    assert json.loads(fields["data.3.value"]) == {"fonte": "Resolução de exemplo"}


def test_saving_the_rich_plan_unchanged_leaves_the_published_version_as_it_is(
    rich: TestClient, deps: Deps
) -> None:
    response = save(rich, draft=RICH)

    assert response.status_code == 303
    assert not pending(rich, RICH_SLUG)
    assert rich_stored(deps) == rich_definition()


def test_every_new_field_of_the_editor_has_a_label(rich: TestClient) -> None:
    page = rich.get(RICH).text
    labelled = {str(label["for"]) for label in tags(page, "label") if label.get("for")}
    controls = [*tags(page, "input"), *tags(page, "select"), *tags(page, "textarea")]
    named = [
        control
        for control in controls
        if str(control.get("name", "")).startswith(("plan.", "data."))
        and control.get("type") not in ("hidden", "radio", "checkbox")
    ]
    assert len(named) > 40
    for control in named:
        assert control.get("id") in labelled or control.get("aria-label"), control["name"]


# --- incluir, remover, reordenar e trocar o tipo ----------------------------------------------


def test_adding_a_field_to_the_plan_brings_the_page_back_with_it_and_saves_nothing(
    rich: TestClient, deps: Deps
) -> None:
    fields = fields_of(rich)

    response = operate(rich, fields, "plan-add-field:plan")

    after = form_of(response)
    assert "<html" in response.text  # a página inteira, sem JavaScript
    assert (after["plan.fields.5.name"], after["plan.fields.5.kind"]) == ("", "text")
    assert autofocused(response) == ["plan.fields.5.name"]
    assert after["expected"] == fields["expected"]
    assert not pending(rich, RICH_SLUG) and rich_stored(deps) == rich_definition()


def test_with_htmx_only_the_plan_editor_comes_back(rich: TestClient) -> None:
    response = operate(rich, fields_of(rich), "plan-add-field:plan", htmx=True)

    assert response.status_code == 200 and "<html" not in response.text
    assert tags(response.text, "div", id="plano-editor")
    assert tags(response.text, "input", name="plan.fields.5.name")
    assert autofocused(response) == ["plan.fields.5.name"]
    assert not tags(response.text, "textarea", name="prompt")  # o resto da página fica
    assert not tags(response.text, "input", name="data.0.name")  # e o editor dos dados também


def test_a_new_field_is_saved_once_the_example_plan_has_it_too(
    rich: TestClient, deps: Deps
) -> None:
    fields = step(rich, fields_of(rich), "plan-add-field:plan")
    fields["plan.fields.5.name"] = "proximo_passo"
    fields["plan.fields.5.description"] = "O que fazer esta semana."
    fields["plan.fields.5.max_length"] = "400"

    refused = post(rich, RICH, **fields)

    # O plano de exemplo é validado pelo modelo da própria versão: ele ainda não tem o campo novo.
    assert refused.status_code == 422
    assert "examples.plan.proximo_passo: campo obrigatório" in problems_shown(refused)
    assert invalid_fields(refused) == ["definition"]
    example = json.loads(fields["definition"])
    example["examples"]["plan"]["proximo_passo"] = "Montar a planilha de contatos."
    fields["definition"] = json.dumps(example, ensure_ascii=False)

    response = post(rich, RICH, **fields)

    assert response.status_code == 303, text(response.text)
    saved = rich_stored(deps)["plan"]["fields"][5]
    assert saved == {
        "name": "proximo_passo",
        "kind": "text",
        "description": "O que fazer esta semana.",
        "max_length": 400,
    }


def test_changing_the_kind_of_a_field_shows_the_fields_of_the_new_kind(rich: TestClient) -> None:
    fields = fields_of(rich)
    fields["plan.fields.0.kind"] = "list"

    after = step(rich, fields, "plan-refresh:plan.fields.0")

    assert after["plan.fields.0.items.kind"] == "text"  # a lista nasce com itens de texto
    assert "plan.fields.0.items.name" not in after
    assert (after["plan.fields.0.name"], after["plan.fields.0.kind"]) == ("titulo", "list")
    fields = dict(after)
    fields["plan.fields.0.items.kind"] = "group"

    grouped = step(rich, fields, "plan-refresh:plan.fields.0.items")

    assert grouped["plan.fields.0.items.fields.0.kind"] == "text"  # o grupo nasce com um campo
    assert grouped["plan.fields.0.items.fields.0.name"] == ""


def test_fields_move_inside_their_group_and_are_removed(rich: TestClient, deps: Deps) -> None:
    fields = step(rich, fields_of(rich), "plan-down:plan.fields.4.items.fields.0")
    assert fields["plan.fields.4.items.fields.0.name"] == "acoes"
    assert fields["plan.fields.4.items.fields.1.name"] == "nome"

    fields = step(rich, fields, "plan-up:plan.fields.1")
    assert (fields["plan.fields.0.name"], fields["plan.fields.1.name"]) == ("mensagem", "titulo")
    assert fields["plan.fields.1.forbidden_pattern.pattern"] == r"\d"  # o campo leva o que tem

    fields = step(rich, fields, "plan-remove:plan.fields.2")  # `decisao`
    names = [fields[f"plan.fields.{i}.name"] for i in range(4)]
    assert names == ["mensagem", "titulo", "aula", "fases"] and "plan.fields.4.name" not in fields

    fields = step(rich, fields, "plan-add-field:plan.fields.3.items")
    assert fields["plan.fields.3.items.fields.2.name"] == ""
    assert autofocused(operate(rich, fields, "plan-add-field:plan")) == ["plan.fields.4.name"]


def test_moving_the_first_field_up_or_the_last_down_changes_nothing(rich: TestClient) -> None:
    fields = fields_of(rich)

    up = step(rich, fields, "plan-up:plan.fields.0")
    down = step(rich, fields, "plan-down:plan.fields.4")

    assert up["plan.fields.0.name"] == "titulo" and down["plan.fields.4.name"] == "fases"


def test_a_reordered_and_trimmed_plan_is_saved_in_the_new_order(
    rich: TestClient, deps: Deps
) -> None:
    fields = step(rich, fields_of(rich), "plan-up:plan.fields.1")  # `mensagem` passa para o início

    response = post(rich, RICH, **fields)

    assert response.status_code == 303, text(response.text)
    names = [field["name"] for field in rich_stored(deps)["plan"]["fields"]]
    assert names == ["mensagem", "titulo", "decisao", "aula", "fases"]  # a ordem do LLM


def test_an_operation_pointing_at_nothing_changes_nothing(rich: TestClient) -> None:
    fields = fields_of(rich)

    for op in (
        "plan-remove:plan.fields.9",
        "plan-up:plan.fields.9.items.fields.2",
        "plan-add-field:plan.fields.0",  # um campo de texto não tem campos
        "plan-remove:plan.items",
        "plan-refresh:plan.fields.7",
        "plan-add-rule:x",
        "plan-remove-rule:5",
        "plan-remove:../etc",
        "data-remove:9",
        "data-add-window:9",
        "data-remove-window:0:9",
        "data-refresh:9",
    ):
        after = step(rich, fields, op)
        assert after == fields, op


# --- tipos de campo: texto, lista, grupo e opções fixas ---------------------------------------


def build_phase_list(client: TestClient, fields: Fields) -> Fields:
    """Acrescenta ao plano uma lista de grupos, `etapas`, com um texto e uma lista de textos."""
    fields = step(client, fields, "plan-add-field:plan")
    fields["plan.fields.5.name"] = "etapas"
    fields["plan.fields.5.kind"] = "list"
    fields = step(client, fields, "plan-refresh:plan.fields.5")
    fields["plan.fields.5.items.kind"] = "group"
    fields["plan.fields.5.items.model"] = "Etapa"
    fields = step(client, fields, "plan-refresh:plan.fields.5.items")
    fields["plan.fields.5.min_items"] = "1"
    fields["plan.fields.5.items.fields.0.name"] = "titulo"
    fields["plan.fields.5.items.fields.0.max_length"] = "80"
    fields = step(client, fields, "plan-add-field:plan.fields.5.items")
    fields["plan.fields.5.items.fields.1.name"] = "tipo"
    fields["plan.fields.5.items.fields.1.kind"] = "options"
    fields = step(client, fields, "plan-refresh:plan.fields.5.items.fields.1")
    fields["plan.fields.5.items.fields.1.values"] = "decidir\n  esperar \n\nrevisar\n"
    fields["plan.fields.5.items.fields.1.internal"] = "on"
    return fields


def test_a_list_of_groups_with_text_and_fixed_options_is_built_and_saved(
    rich: TestClient, deps: Deps
) -> None:
    fields = build_phase_list(rich, fields_of(rich))
    example = json.loads(fields["definition"])
    example["examples"]["plan"]["etapas"] = [{"titulo": "Primeira", "tipo": "decidir"}]
    fields["definition"] = json.dumps(example, ensure_ascii=False)

    response = post(rich, RICH, **fields)

    assert response.status_code == 303, text(response.text)
    assert rich_stored(deps)["plan"]["fields"][5] == {
        "name": "etapas",
        "kind": "list",
        "min_items": 1,
        "items": {
            "kind": "group",
            "model": "Etapa",
            "fields": [
                {"name": "titulo", "kind": "text", "max_length": 80},
                {
                    "name": "tipo",
                    "kind": "options",
                    "internal": True,
                    "values": ["decidir", "esperar", "revisar"],
                },
            ],
        },
    }
    again = fields_of(rich)
    assert again["plan.fields.5.items.fields.1.values"] == "decidir\nesperar\nrevisar"


def test_a_field_gets_a_label_for_the_admin_and_the_items_of_a_list_do_not(
    rich: TestClient, deps: Deps
) -> None:
    fields = fields_of(rich)
    assert fields["plan.fields.0.label"] == ""
    assert "plan.fields.4.items.label" not in fields  # os itens de uma lista não têm rótulo
    fields["plan.fields.0.label"] = "  Título do plano  "
    fields["plan.fields.4.items.fields.0.label"] = "Nome da fase"

    response = post(rich, RICH, **fields)

    assert response.status_code == 303, text(response.text)
    plan = rich_stored(deps)["plan"]["fields"]
    assert plan[0]["label"] == "Título do plano"
    assert plan[4]["items"]["fields"][0]["label"] == "Nome da fase"
    assert "label" not in plan[1]  # sem rótulo, a definição não ganha a chave
    again = fields_of(rich)
    assert again["plan.fields.0.label"] == "Título do plano"
    assert again["plan.fields.4.items.fields.0.label"] == "Nome da fase"


def test_kind_specific_values_of_another_kind_are_not_saved(rich: TestClient, deps: Deps) -> None:
    """Quem troca o tipo de um campo não leva para a definição o que era do tipo antigo."""
    fields = fields_of(rich)
    fields["plan.fields.1.kind"] = "options"  # era texto, com o máximo e o mínimo de parágrafos
    fields["plan.fields.1.values"] = "um\ndois"
    example = json.loads(fields["definition"])
    example["examples"]["plan"]["mensagem"] = "um"
    fields["definition"] = json.dumps(example, ensure_ascii=False)

    response = post(rich, RICH, **fields)

    assert response.status_code == 303, text(response.text)
    assert rich_stored(deps)["plan"]["fields"][1] == {
        "name": "mensagem",
        "kind": "options",
        "description": "A mensagem final.",
        "values": ["um", "dois"],
    }


def test_options_take_their_values_from_a_list_of_the_data_and_the_page_offers_the_lists(
    rich: TestClient, deps: Deps
) -> None:
    page = rich.get(RICH).text
    # Uma caixa por lista: só as listas dos dados servem, e as outras não aparecem.
    assert boxes_of(page, "plan.fields.3.values_from") == [("aulas", True)]
    assert boxes_of(page, "plan.fields.2.values_from") == [("aulas", False)]

    fields = fields_of(rich)
    assert fields["plan.fields.3.values_from"] == "aulas"
    del fields["plan.fields.3.values_from"]  # nenhuma caixa marcada: valem os valores fixos
    fields["plan.fields.3.values"] = "basica\navancada"
    example = json.loads(fields["definition"])
    example["examples"]["plan"]["aula"] = "basica"
    fields["definition"] = json.dumps(example, ensure_ascii=False)
    assert post(rich, RICH, **fields).status_code == 303

    assert rich_stored(deps)["plan"]["fields"][3] == {
        "name": "aula",
        "kind": "options",
        "values": ["basica", "avancada"],
    }


# --- opções tiradas de uma ou de várias listas ------------------------------------------------


def test_the_page_shows_the_chosen_lists_first_in_their_order_and_then_the_others(
    many: TestClient,
) -> None:
    page = many.get(MANY).text

    assert boxes_of(page, CHOICE) == [("materiais", True), ("combo", True), ("ecossistema", False)]
    # A posição de cada marcada: é a ordem das opções, e a operadora a muda pelo número.
    assert positions_of(page, "plan.fields.3") == {
        "materiais": "1",
        "combo": "2",
        "ecossistema": "",
    }


def test_saving_a_list_of_names_unchanged_leaves_the_draft_as_it_is(
    many: TestClient, deps: Deps
) -> None:
    response = save(many, draft=MANY)

    assert response.status_code == 303
    assert not pending(many, MANY_SLUG)
    assert many_stored(deps) == many_definition(["materiais", "combo"])


def test_the_chosen_lists_are_saved_as_a_list_of_names_in_the_order_of_the_page(
    many: TestClient, deps: Deps
) -> None:
    fields = fields_of(many, MANY)
    assert fields[CHOICE] == ["materiais", "combo"]  # as marcadas, na ordem em que a página as põe
    fields[CHOICE] = ["materiais", "combo", "ecossistema"]  # a nova entra no fim

    response = post(many, MANY, **fields)

    assert response.status_code == 303, text(response.text)
    assert many_stored(deps)["plan"]["fields"][3] == {
        "name": "aula",
        "kind": "options",
        "values_from": ["materiais", "combo", "ecossistema"],
    }


def test_the_numbers_of_the_lists_set_the_order_whatever_the_order_of_the_boxes(
    many: TestClient, deps: Deps
) -> None:
    fields = fields_of(many, MANY)
    fields[CHOICE] = ["materiais", "combo", "ecossistema"]
    fields["plan.fields.3.values_order.combo"] = "1"
    fields["plan.fields.3.values_order.ecossistema"] = "2"
    fields["plan.fields.3.values_order.materiais"] = "3"

    response = post(many, MANY, **fields)

    assert response.status_code == 303, text(response.text)
    assert many_stored(deps)["plan"]["fields"][3]["values_from"] == [
        "combo",
        "ecossistema",
        "materiais",
    ]


def test_a_list_without_a_number_goes_after_the_numbered_ones_and_the_page_renumbers_them(
    many: TestClient,
) -> None:
    fields = fields_of(many, MANY)
    fields[CHOICE] = ["materiais", "combo", "ecossistema"]
    fields["plan.fields.3.values_order.materiais"] = "7"
    fields["plan.fields.3.values_order.combo"] = "2"
    fields["plan.fields.3.values_order.ecossistema"] = ""

    page = operate(many, fields, "plan-refresh:plan.fields.3", draft=MANY).text

    assert boxes_of(page, CHOICE) == [("combo", True), ("materiais", True), ("ecossistema", True)]
    assert positions_of(page, "plan.fields.3") == {
        "combo": "1",
        "materiais": "2",
        "ecossistema": "3",
    }


def test_a_single_chosen_list_is_saved_as_its_name_alone(many: TestClient, deps: Deps) -> None:
    fields = fields_of(many, MANY)
    fields[CHOICE] = "combo"

    assert post(many, MANY, **fields).status_code == 303

    assert many_stored(deps)["plan"]["fields"][3]["values_from"] == "combo"


def test_a_choice_without_any_list_or_fixed_value_is_refused_with_the_message_of_the_compiler(
    many: TestClient,
) -> None:
    fields = fields_of(many, MANY)
    del fields[CHOICE]
    fields["plan.fields.3.values"] = ""

    response = post(many, MANY, **fields)

    assert response.status_code == 422
    assert "aula · valores: precisa de pelo menos 1 item" in problems_shown(response)


def test_a_list_the_data_no_longer_has_stays_marked_with_a_warning_and_the_save_refuses_it(
    many: TestClient,
) -> None:
    fields = fields_of(many, MANY)
    fields[CHOICE] = ["combo", "inventada"]

    page = operate(many, fields, "plan-refresh:plan.fields.3", draft=MANY).text
    assert boxes_of(page, CHOICE) == [
        ("combo", True),
        ("inventada", True),
        ("ecossistema", False),
        ("materiais", False),
    ]
    assert "inventada (não existe mais nos dados)" in text(page)

    response = post(many, MANY, **fields)
    assert response.status_code == 422
    assert "não há uma lista chamada inventada nos dados do formulário" in problems_shown(response)
    assert 'href="#f-plan-fields-3-values_from"' in response.text


def test_the_mcc_v2_is_made_in_the_editor_with_the_three_lists_and_without_the_repeating_block(
    client: TestClient, deps: Deps
) -> None:
    """O passo da operadora para a versão 2 do MCC (issue 49): no rascunho, o campo dos entregáveis
    marca as três listas que o prompt mostra e o bloco que as repetia sai dos dados."""
    draft = "/admin/forms/mcc/draft"
    field = "plan.fields.3.items.fields.5.items.values_from"
    fields = fields_of(client, draft)
    assert fields[field] == "entregaveis_irpf"
    block = next(i for i in range(20) if fields[f"data.{i}.name"] == "entregaveis_irpf")

    # As caixas voltam na ordem dos dados, que não é a das opções: os números dizem a ordem.
    at = "plan.fields.3.items.fields.5.items"
    fields[field] = ["combo", "materiais", "ecossistema"]
    fields[f"{at}.values_order.combo"] = "1"
    fields[f"{at}.values_order.ecossistema"] = "2"
    fields[f"{at}.values_order.materiais"] = "3"
    fields = form_of(operate(client, fields, f"data-remove:{block}", draft=draft))
    response = post(client, draft, **fields)

    assert response.status_code == 303, text(response.text)
    v2 = legacy_definition("mcc")
    v2["plan"]["fields"][3]["items"]["fields"][5]["items"]["values_from"] = [
        "combo",
        "ecossistema",
        "materiais",
    ]
    del v2["data"]["entregaveis_irpf"]
    assert stored(deps, "mcc") == v2


# --- validações declarativas ------------------------------------------------------------------


def test_forbidden_expressions_are_rules_with_a_scope_and_the_fields_to_choose_from(
    rich: TestClient, deps: Deps
) -> None:
    page = rich.get(RICH).text
    boxes = re.findall(r'<input[^>]*name="plan.forbidden.0.fields"[^>]*value="([^"]+)"', page)
    assert boxes == ["titulo", "mensagem", "fases.nome", "fases.acoes"]  # os textos, em árvore

    fields = fields_of(rich)
    fields = step(rich, fields, "plan-add-rule")
    fields["plan.forbidden.2.expressions"] = "calculadora definitiva\n\n  Garantia de resultado  "
    fields["plan.forbidden.2.scope"] = "only"
    fields["plan.forbidden.2.fields"] = ["mensagem", "fases.nome"]
    fields["plan.forbidden.1.scope"] = "all"
    fields["plan.forbidden.1.fields"] = ["titulo"]  # com o plano inteiro, os campos não contam

    response = post(rich, RICH, **fields)

    assert response.status_code == 303, text(response.text)
    assert rich_stored(deps)["plan"]["forbidden"] == [
        RICH_PLAN["forbidden"][0],
        RICH_PLAN["forbidden"][1],
        {
            "expressions": ["calculadora definitiva", "Garantia de resultado"],
            "scope": "only",
            "fields": ["mensagem", "fases.nome"],
        },
    ]


def test_a_rule_is_removed_with_its_button(rich: TestClient, deps: Deps) -> None:
    fields = step(rich, fields_of(rich), "plan-remove-rule:0")

    assert fields["plan.forbidden.0.expressions"] == "única IA"
    assert "plan.forbidden.1.expressions" not in fields
    assert post(rich, RICH, **fields).status_code == 303
    assert rich_stored(deps)["plan"]["forbidden"] == [RICH_PLAN["forbidden"][1]]


def test_a_plan_without_rules_has_no_forbidden_key(rich: TestClient, deps: Deps) -> None:
    fields = step(rich, fields_of(rich), "plan-remove-rule:0")
    fields = step(rich, fields, "plan-remove-rule:0")

    assert post(rich, RICH, **fields).status_code == 303
    assert "forbidden" not in rich_stored(deps)["plan"]


def test_a_rule_pointing_at_a_field_that_was_renamed_is_refused_at_the_rule(
    rich: TestClient,
) -> None:
    fields = fields_of(rich)
    fields["plan.fields.0.name"] = "chamada"  # a regra 0 fala do campo `titulo`

    response = post(rich, RICH, **fields)

    assert response.status_code == 422
    shown = problems_shown(response)
    assert "não é um campo de texto do plano" in shown
    assert 'href="#f-plan-forbidden-0-fields"' in response.text


def test_a_pattern_the_example_plan_breaks_is_refused_with_the_rule_of_the_field(
    rich: TestClient,
) -> None:
    fields = fields_of(rich)
    example = json.loads(fields["definition"])
    example["examples"]["plan"]["titulo"] = "Plano de 90 dias"  # o título não leva números
    fields["definition"] = json.dumps(example, ensure_ascii=False)

    response = post(rich, RICH, **fields)

    assert response.status_code == 422
    assert "examples.plan.titulo: Sem números no título." in problems_shown(response)


@pytest.mark.parametrize(
    ("name", "value", "message", "target"),
    [
        (
            "plan.fields.0.min_length",
            "abc",
            "precisa ser um número inteiro",
            "f-plan-fields-0-min_length",
        ),
        (
            "plan.fields.0.min_length",
            "500",
            "o mínimo passa do máximo (120)",
            "f-plan-fields-0-min_length",
        ),
        (
            "plan.fields.0.forbidden_pattern.pattern",
            "(aberto",
            "expressão regular inválida (posição 7)",
            "f-plan-fields-0-forbidden_pattern-pattern",
        ),
        (
            "plan.fields.2.values",
            "abrir\nabrir",
            "valor repetido",
            "f-plan-fields-2-values",
        ),
        (
            "plan.fields.4.items.fields.0.name",
            "acoes",
            "outro campo do grupo já usa este nome",
            "f-plan-fields-4-items-fields-1-name",
        ),
        (
            "plan.fields.0.name",
            "Título",
            "use letras minúsculas sem acento, números e _, começando por uma letra",
            "f-plan-fields-0-name",
        ),
        ("plan.fields.0.name", "", "campo obrigatório", "f-plan-fields-0-name"),
        (
            "plan.fields.4.items.model",
            "minúscula",
            "comece por letra maiúscula e use só letras e números",
            "f-plan-fields-4-items-model",
        ),
        (
            "plan.fields.4.min_items",
            "5",
            "o mínimo passa do máximo (3)",
            "f-plan-fields-4-min_items",
        ),
    ],
    ids=[
        "not-a-number",
        "min-over-max",
        "broken-regex",
        "repeated-value",
        "repeated-name",
        "bad-name",
        "no-name",
        "bad-model",
        "items-min-over-max",
    ],
)
def test_a_problem_in_the_plan_is_shown_on_its_own_field_and_opens_its_box(
    rich: TestClient, name: str, value: str, message: str, target: str
) -> None:
    fields = fields_of(rich)
    fields[name] = value

    response = post(rich, RICH, **fields)

    assert response.status_code == 422
    assert message in problems_shown(response)
    assert f'href="#{target}"' in response.text
    assert re.search(rf'<[^>]*id="{target}-erro"', response.text), target
    assert tags(response.text, "input", id=target) or tags(response.text, "textarea", id=target)
    assert form_of_refused(response)[name] == value  # o que ela escreveu volta para corrigir


def test_a_box_with_a_problem_is_open_and_marked(rich: TestClient) -> None:
    fields = fields_of(rich)
    fields["plan.fields.4.items.fields.1.items.max_length"] = "-1"

    response = post(rich, RICH, **fields)

    assert response.status_code == 422
    opened = [str(box["id"]) for box in tags(response.text, "details") if "open" in box]
    assert "f-plan-fields-4" in opened and "f-plan-fields-4-items-fields-1" in opened
    assert "f-plan-fields-0" not in opened


def test_changing_a_rule_of_the_plan_asks_for_a_test_before_publishing(
    rich: TestClient,
) -> None:
    fields = fields_of(rich)
    fields["plan.forbidden.1.expressions"] = "única IA\ncalculadora definitiva"
    assert post(rich, RICH, **fields).status_code == 303

    page = rich.get(f"{RICH}/publish")

    assert page.status_code == 200
    assert "Falta um teste com sucesso: mudou o modelo do plano" in text(page.text)


# --- dados do formulário -----------------------------------------------------------------------


def test_a_list_of_names_is_one_name_per_line_and_blank_lines_do_not_count(
    rich: TestClient, deps: Deps
) -> None:
    fields = fields_of(rich)
    fields["data.0.items"] = "\n".join([*LESSONS, "", "  MÓDULO 4 — A1: Entrega  ", ""])
    example = json.loads(fields["definition"])
    fields["definition"] = json.dumps(example, ensure_ascii=False)

    response = post(rich, RICH, **fields)

    assert response.status_code == 303, text(response.text)
    assert rich_stored(deps)["data"]["aulas"]["items"] == [*LESSONS, "MÓDULO 4 — A1: Entrega"]


def test_a_new_windows_block_is_built_with_its_windows_and_saved(
    rich: TestClient, deps: Deps
) -> None:
    fields = step(rich, fields_of(rich), "data-add")
    assert (fields["data.4.name"], fields["data.4.kind"]) == ("", "list")
    fields["data.4.name"] = "prazos"
    fields["data.4.kind"] = "windows"
    fields = step(rich, fields, "data-refresh:4")
    assert fields["data.4.sequential"] == "yes"  # em sequência é o padrão
    assert fields["data.4.windows.0.name"] == ""  # nasce com uma janela
    fields["data.4.windows.0.name"] = "fase_um"
    fields["data.4.windows.0.label"] = "Fase um"
    fields["data.4.windows.0.start"] = "2026-10-01"
    fields["data.4.windows.0.end"] = "2026-10-31"
    fields = step(rich, fields, "data-add-window:4")
    fields["data.4.windows.1.name"] = "fase_dois"
    fields["data.4.windows.1.start"] = "2026-11-01"
    fields["data.4.windows.1.end"] = "2026-11-30"

    response = post(rich, RICH, **fields)

    assert response.status_code == 303, text(response.text)
    assert rich_stored(deps)["data"]["prazos"] == {
        "kind": "windows",
        "windows": [
            {"name": "fase_um", "label": "Fase um", "start": "2026-10-01", "end": "2026-10-31"},
            {"name": "fase_dois", "start": "2026-11-01", "end": "2026-11-30"},
        ],
    }


def test_windows_can_be_marked_as_not_a_sequence(rich: TestClient, deps: Deps) -> None:
    fields = fields_of(rich)
    fields["data.2.sequential"] = "no"
    fields["data.2.windows.1.start"] = "2026-10-01"  # começa antes do fim da janela anterior

    response = post(rich, RICH, **fields)

    assert response.status_code == 303, text(response.text)
    assert rich_stored(deps)["data"]["janelas"]["sequential"] is False


def test_windows_move_and_are_removed_inside_their_block(rich: TestClient) -> None:
    fields = step(rich, fields_of(rich), "data-down-window:2:0")
    assert (fields["data.2.windows.0.name"], fields["data.2.windows.1.name"]) == (
        "desistencia",
        "opcao",
    )
    fields = step(rich, fields, "data-remove-window:2:0")
    assert fields["data.2.windows.0.name"] == "opcao"
    assert "data.2.windows.1.name" not in fields
    fields = step(rich, fields, "data-up-window:2:0")
    assert fields["data.2.windows.0.name"] == "opcao"  # a primeira não sobe


def test_blocks_come_in_name_order_and_are_removed(rich: TestClient, deps: Deps) -> None:
    fields = fields_of(rich)
    names = [fields[f"data.{i}.name"] for i in range(4)]
    assert names == ["aulas", "edicao", "janelas", "nota"]  # o banco não guarda a ordem dos blocos
    assert not [name for name in fields if re.fullmatch(r"data\.\d+\.(up|down)", name)]

    fields = step(rich, fields, "data-remove:3")  # `nota`
    assert "data.3.name" not in fields and fields["data.2.name"] == "janelas"

    # O prompt do demo não lê nada dos dados, então o bloco pode sair sem outra mudança.
    assert post(rich, RICH, **fields).status_code == 303
    assert set(rich_stored(deps)["data"]) == {"aulas", "janelas", "edicao"}


@pytest.mark.parametrize(
    ("change", "message", "target"),
    [
        (
            {"data.2.windows.0.start": "2026-11-01"},
            "termina antes de começar",
            "f-data-2-windows-0-end",
        ),
        (
            {"data.2.windows.1.start": "2026-10-30"},
            "precisa começar depois do fim da janela anterior (opcao)",
            "f-data-2-windows-1-start",
        ),
        (
            {"data.2.windows.1.name": "opcao"},
            "outra janela do bloco já usa este nome",
            "f-data-2-windows-1-name",
        ),
        (
            {"data.2.windows.0.start": "30/10/2026"},
            "use uma data no formato AAAA-MM-DD, como 2026-10-30",
            "f-data-2-windows-0-start",
        ),
        (
            {"data.2.windows.0.name": "Opção"},
            "use letras minúsculas sem acento, números e _, começando por uma letra",
            "f-data-2-windows-0-name",
        ),
        (
            {"data.1.edition.starts": "2027-06-01"},
            "termina antes de começar",
            "f-data-1-edition-ends",
        ),
        (
            {"data.0.items": "\n".join([*LESSONS, LESSONS[0].upper()])},
            "outro item da lista tem o mesmo nome",
            "f-data-0-items",
        ),
        ({"data.0.items": "\n \n"}, "precisa de pelo menos 1 item", "f-data-0-items"),
        (
            {"data.3.value": '{"fonte": '},
            "JSON inválido",
            "f-data-3-value",
        ),
        (
            {"data.3.name": "aulas"},
            "outro bloco dos dados já usa este nome",
            "f-data-3-name",
        ),
        (
            {"data.3.name": ""},
            "dê um nome ao bloco",
            "f-data-3-name",
        ),
        (
            {"data.3.name": "Nota Fiscal"},
            "use letras minúsculas sem acento, números e _, começando por uma letra",
            "f-data-3-name",
        ),
        (
            {"data.1.edition.name": ""},
            "não pode ficar vazio",
            "f-data-1-edition-name",
        ),
    ],
    ids=[
        "backwards",
        "overlapping",
        "repeated-window",
        "slashes",
        "bad-window-name",
        "edition-backwards",
        "repeated-item",
        "empty-list",
        "broken-json",
        "repeated-block",
        "unnamed-block",
        "bad-block-name",
        "edition-without-name",
    ],
)
def test_the_data_editor_refuses_dates_out_of_order_and_repeated_names_on_the_field(
    rich: TestClient, change: dict[str, Any], message: str, target: str
) -> None:
    fields = fields_of(rich)
    fields |= change

    response = post(rich, RICH, **fields)

    assert response.status_code == 422
    assert message in problems_shown(response)
    assert f'href="#{target}"' in response.text
    assert re.search(rf'<[^>]*id="{target}-erro"', response.text), target
    for name, value in change.items():
        assert form_of_refused(response)[name] == value
    assert not pending(rich, RICH_SLUG)


def test_the_value_block_keeps_free_json_and_the_prompt_can_read_it(
    rich: TestClient, deps: Deps
) -> None:
    fields = fields_of(rich)
    fields["data.3.value"] = json.dumps({"fonte": "Outra resolução", "numeros": [1, 2.5]})
    fields["prompt"] += "Fonte: {{ data.nota.fonte }}\n"

    response = post(rich, RICH, **fields)

    assert response.status_code == 303, text(response.text)
    assert rich_stored(deps)["data"]["nota"]["value"] == {
        "fonte": "Outra resolução",
        "numeros": [1, 2.5],
    }


def test_the_json_that_the_database_cannot_keep_is_refused_in_a_value_block(
    rich: TestClient,
) -> None:
    fields = fields_of(rich)
    fields["data.3.value"] = '{"fonte": NaN}'

    response = post(rich, RICH, **fields)

    assert response.status_code == 422
    assert "NaN e Infinity não são números em JSON" in problems_shown(response)


def test_with_htmx_only_the_data_editor_comes_back(rich: TestClient) -> None:
    response = operate(rich, fields_of(rich), "data-add", htmx=True)

    assert response.status_code == 200 and "<html" not in response.text
    assert tags(response.text, "div", id="dados-editor")
    assert tags(response.text, "input", name="data.4.name")
    assert autofocused(response) == ["data.4.name"]
    assert not tags(response.text, "input", name="plan.fields.0.name")


def test_the_variables_panel_lists_the_blocks_of_the_data(rich: TestClient) -> None:
    page = text(rich.get(RICH).text)

    for name in ("data.aulas", "data.janelas", "data.edicao", "data.nota"):
        assert name in page


def test_the_prompt_reads_the_typed_blocks_in_the_saved_draft(rich: TestClient) -> None:
    fields = fields_of(rich)
    fields["prompt"] += (
        "Janela: {{ data.janelas.opcao.end|long_date }}; edição {{ data.edicao.name }}; "
        "{{ data.aulas|length }} aulas.\n"
    )
    assert post(rich, RICH, **fields).status_code == 303

    page = post(rich, f"{RICH}/prompt", answers="", date="")

    assert page.status_code == 200, page.text
    assert "Janela: 30 de outubro de 2026; edição Ciclo demonstrativo 2027; 3 aulas." in text(
        page.text
    )


# --- a leitura da versão publicada ------------------------------------------------------------


def test_the_reading_of_the_published_version_lists_what_the_server_checks_in_the_plan(
    rich: TestClient,
) -> None:
    page = rich.get(f"/admin/forms/{RICH_SLUG}")

    plan = text(section_of(page, "modelo-do-plano"))
    assert "Conferência do plano" in plan
    assert "Em todos os textos menos em titulo: sem «economia garantida», «sem juros»." in plan
    assert "Em todos os textos do plano: sem «única IA»." in plan
    assert r"titulo: o texto não pode ter o padrão \d (Sem números no título.)" in plan
    assert "Pelo menos 2 parágrafos, separados por linha em branco." in plan  # a descrição do campo
    data = text(section_of(page, "dados"))
    assert (
        "janelas" in data
        and "2026-09-25" in data
        and "Ciclo demonstrativo 2027" in data
        and "MÓDULO 1" in data
    )


def test_a_plan_without_checks_has_no_checks_section(client: TestClient) -> None:
    plan = text(section_of(client.get(f"/admin/forms/{SLUG}"), "modelo-do-plano"))

    assert "Conferência do plano" not in plan


def test_the_variables_panel_says_what_each_kind_of_block_gives_the_template(
    rich: TestClient,
) -> None:
    page = text(rich.get(RICH).text)

    assert "data.aulas lista de nomes" in page
    assert "data.janelas janelas pelo nome, cada uma com name, label, start e end" in page
    assert "data.edicao edição, com name, starts e ends" in page
    assert "data.nota dados do formulário, como estão escritos" in page
