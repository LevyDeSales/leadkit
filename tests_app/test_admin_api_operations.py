"""Como uma operação da API de admin se declara: o papel, as marcas e o `confirm` que saem juntos, os
erros declarados, o `expected_version` e as listas em `{items, next_cursor}`.

As operações de prova (`/probe/...`) são acrescentadas à sub-aplicação só nestes testes: as fases de
recurso trazem as reais, e esta é a prova de que o esqueleto que elas vão usar faz o que a ADR 0015
diz.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import pytest
from admin_api_support import API, add_routes, bearer, make_api, new_key
from fastapi import Body, Depends, File, Path, Request, UploadFile
from fastapi.testclient import TestClient
from pydantic import BaseModel
from test_admin import FakeClock

from app.admin.api import errors, security
from app.admin.api.common import (
    ApiInput,
    ConfirmedInput,
    ConfirmQuery,
    ExpectedVersionQuery,
    HtmlResponse,
    Page,
    PageParams,
    PdfResponse,
    VersionedInput,
    check_version,
    decode_keyset,
    encode_cursor,
    encode_keyset,
    page_params,
    paginate,
)
from app.admin.api.errors import ApiError
from app.admin.api.operations import Confirmation, OperationRouter, tags_of
from app.admin.api.security import CurrentKey, ensure_role
from app.deps import Deps

PROBE = f"{API}/probe"
TAG = "Prova"
TAG_TEXT = "As operações de prova destes testes."
VERSION_NOW = "v2"
STATE_TEXT = "O recurso não está num estado que aceite a ação (`state_conflict`)."
VERSION_TEXT = "A versão esperada não é a de agora (`version_conflict`)."
PART_TEXT = "A chave `leitura` pediu `more=true`, que é só do papel `total` (`forbidden_role`)."
PART_ASKED = "Pedir o resto (`more=true`)"
CUSTOM_TEXT = "Esta prova apaga a coisa e a lead não a vê mais."


class Done(BaseModel):
    done: bool


class Person(BaseModel):
    name: str


class PeoplePage(Page[Person]):
    pass


class Destroy(ConfirmedInput):
    reason: str = "teste"


class Rename(VersionedInput):
    name: str


class Simulate(ConfirmedInput):
    dry_run: bool = False
    reason: str = "teste"


def declare(effects: list[str], people: list[Person]) -> OperationRouter:
    router = OperationRouter(tag=TAG, description=TAG_TEXT)
    text = {"summary": "Prova", "description": "Uma operação de prova.", "returns": "Feito."}

    @router.operation(
        "POST",
        "/probe/destroy/{thing}",
        operation_id="probe_destroy",
        role="total",
        destructive=True,
        errors={409: STATE_TEXT},
        response_model=Done,
        **text,
    )
    def destroy(thing: str, body: Destroy) -> Done:
        effects.append(f"destroy:{thing}")
        return Done(done=True)

    @router.operation(
        "POST",
        "/probe/send",
        operation_id="probe_send",
        role="total",
        external_effect=True,
        response_model=Done,
        **text,
    )
    def send(body: Destroy) -> Done:
        effects.append("send")
        return Done(done=True)

    @router.operation(
        "POST",
        "/probe/both",
        operation_id="probe_both",
        role="total",
        destructive=True,
        external_effect=True,
        response_model=Done,
        **text,
    )
    def both(body: Destroy) -> Done:
        effects.append("both")
        return Done(done=True)

    @router.operation(
        "POST",
        "/probe/custom",
        operation_id="probe_custom",
        role="total",
        destructive=True,
        confirmation_text=CUSTOM_TEXT,
        response_model=Done,
        **text,
    )
    def custom(body: Destroy) -> Done:
        effects.append("custom")
        return Done(done=True)

    @router.operation(
        "POST",
        "/probe/simulate",
        operation_id="probe_simulate",
        role="total",
        external_effect=True,
        confirm_unless="dry_run",
        confirmation_text=CUSTOM_TEXT,
        response_model=Done,
        **text,
    )
    def simulate(body: Simulate) -> Done:
        effects.append("simulate:dry" if body.dry_run else "simulate")
        return Done(done=True)

    @router.operation(
        "DELETE",
        "/probe/remove/{thing}",
        operation_id="probe_remove",
        role="total",
        destructive=True,
        errors={409: VERSION_TEXT},
        response_model=Done,
        **text,
    )
    def remove(
        thing: Annotated[str, Path(description="A coisa.")],
        expected_version: ExpectedVersionQuery,
        confirm: ConfirmQuery = False,
    ) -> Done:
        check_version(VERSION_NOW, expected_version)
        effects.append(f"remove:{thing}")
        return Done(done=True)

    @router.operation(
        "PUT",
        "/probe/rename/{thing}",
        operation_id="probe_rename",
        role="total",
        errors={409: VERSION_TEXT},
        response_model=Done,
        **text,
    )
    def rename(thing: str, body: Rename) -> Done:
        check_version(VERSION_NOW, body.expected_version)
        effects.append(f"rename:{thing}")
        return Done(done=True)

    @router.operation(
        "GET",
        "/probe/lead",
        operation_id="probe_lead",
        role="leitura",
        lead_data=True,
        response_model=Done,
        **text,
    )
    def lead() -> Done:
        return Done(done=True)

    @router.operation(
        "GET",
        "/probe/part",
        operation_id="probe_part",
        role="leitura",
        errors={403: PART_TEXT},
        response_model=Done,
        **text,
    )
    def part(key: CurrentKey, more: bool = False) -> Done:
        if more:  # uma parte da operação pede mais papel que o resto
            ensure_role(key, "total", what=PART_ASKED)
        effects.append(f"part:{more}")
        return Done(done=True)

    @router.operation(
        "GET",
        "/probe/file/{thing}",
        operation_id="probe_file",
        role="total",
        heavy=True,
        response_model=None,
        response_class=PdfResponse,
        **text,
    )
    def file(thing: str) -> PdfResponse:
        return PdfResponse(
            b"%PDF-1.4 " + thing.encode(),
            headers={"Content-Disposition": f'attachment; filename="{thing}.pdf"'},
        )

    @router.operation(
        "GET",
        "/probe/page/{thing}",
        operation_id="probe_page",
        role="leitura",
        heavy=True,
        response_model=None,
        response_class=HtmlResponse,
        **text,
    )
    def page(thing: str) -> HtmlResponse:
        return HtmlResponse(f"<!doctype html><p>{thing}</p>")

    @router.operation(
        "GET",
        "/probe/peek/{thing}",
        operation_id="probe_peek",
        role="leitura",
        response_model=Done,
        **text,
    )
    def peek(thing: str) -> Done:
        return Done(done=True)

    @router.operation(
        "GET",
        "/probe/people",
        operation_id="probe_people",
        role="leitura",
        response_model=PeoplePage,
        **text,
    )
    def listing(page: Annotated[PageParams, Depends(page_params)]) -> PeoplePage:
        chunk, next_cursor = paginate(people, page, key=lambda person: person.name)
        return PeoplePage(items=chunk, next_cursor=next_cursor)

    return router


@pytest.fixture
def effects() -> list[str]:
    return []


@pytest.fixture
def people() -> list[Person]:
    return [Person(name=f"pessoa-{number:03d}") for number in range(120)]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def api(deps: Deps, clock: FakeClock, effects: list[str], people: list[Person]) -> TestClient:
    client = make_api(deps, clock)
    add_routes(client, declare(effects, people))
    return client


@pytest.fixture
def total(deps: Deps, clock: FakeClock) -> dict[str, str]:
    return bearer(new_key(deps, clock, "total").key)


@pytest.fixture
def reader(deps: Deps, clock: FakeClock) -> dict[str, str]:
    return bearer(new_key(deps, clock, "leitura").key)


def document(api: TestClient) -> dict[str, Any]:
    found: dict[str, Any] = api.app.state.admin_api.openapi()  # type: ignore[attr-defined]
    return found


def operation(api: TestClient, operation_id: str) -> dict[str, Any]:
    for methods in document(api)["paths"].values():
        for found in methods.values():
            if found["operationId"] == operation_id:
                return found  # type: ignore[no-any-return]
    raise AssertionError(operation_id)


# --- as marcas e o confirm saem da mesma declaração ---------------------------------------------

# (operação, caminho, o efeito que ela registra)
MARKED = [
    ("probe_destroy", "/probe/destroy/a", "destroy:a"),
    ("probe_send", "/probe/send", "send"),
    ("probe_both", "/probe/both", "both"),
]


@pytest.mark.parametrize(("operation_id", "path", "effect"), MARKED)
def test_a_marked_operation_does_nothing_without_the_confirm(
    api: TestClient,
    total: dict[str, str],
    effects: list[str],
    operation_id: str,
    path: str,
    effect: str,
) -> None:
    body = {"reason": "teste"}
    for sent in (
        body,
        {**body, "confirm": False},
        {**body, "confirm": "true"},
        {**body, "confirm": 1},
    ):
        response = api.post(f"{API}{path}", json=sent, headers=total)

        assert response.status_code == 422, sent
        assert response.json()["code"] == "confirmation_required"
        assert "Nada foi feito" in response.json()["detail"]
    assert effects == []

    done = api.post(f"{API}{path}", json={**body, "confirm": True}, headers=total)

    assert done.status_code == 200 and effects == [effect]


@pytest.mark.parametrize(("operation_id", "path", "effect"), MARKED)
def test_the_mark_and_the_confirm_requirement_come_together(
    api: TestClient, operation_id: str, path: str, effect: str
) -> None:
    found = operation(api, operation_id)

    assert found["x-required-role"] == "total"
    assert found.get("x-destructive") or found.get("x-external-effect")
    schema = document(api)["components"]["schemas"]["Destroy"]
    assert schema["properties"]["confirm"]["type"] == "boolean"  # o campo está no contrato
    assert found["responses"]["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }


def test_each_declaration_gives_exactly_its_marks(api: TestClient) -> None:
    marks = {
        operation_id: {
            name
            for name in ("x-destructive", "x-external-effect", "x-lead-data")
            if operation(api, operation_id).get(name)
        }
        for operation_id in (
            "probe_destroy",
            "probe_send",
            "probe_both",
            "probe_remove",
            "probe_rename",
            "probe_lead",
            "probe_people",
        )
    }

    assert marks == {
        "probe_destroy": {"x-destructive"},
        "probe_send": {"x-external-effect"},
        "probe_both": {"x-destructive", "x-external-effect"},
        "probe_remove": {"x-destructive"},
        "probe_rename": set(),
        "probe_lead": {"x-lead-data"},
        "probe_people": set(),
    }


def test_an_unmarked_operation_does_not_ask_for_the_confirm(
    api: TestClient, total: dict[str, str], effects: list[str]
) -> None:
    renamed = api.put(
        f"{PROBE}/rename/a", json={"name": "b", "expected_version": VERSION_NOW}, headers=total
    )

    assert renamed.status_code == 200 and effects == ["rename:a"]


@pytest.mark.parametrize(
    "sent", [{"reason": "teste"}, {"reason": "teste", "confirm": None}, [], "true", 7]
)
def test_only_the_boolean_true_confirms_and_the_body_may_be_anything_else(
    api: TestClient, total: dict[str, str], effects: list[str], sent: Any
) -> None:
    response = api.post(f"{PROBE}/send", json=sent, headers=total)

    assert response.json()["code"] == "confirmation_required" and effects == []


def test_no_body_at_all_is_also_a_missing_confirmation(
    api: TestClient, total: dict[str, str], effects: list[str]
) -> None:
    response = api.post(f"{PROBE}/send", headers=total)

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert effects == []


def test_the_confirmation_comes_before_the_body_validation_and_after_the_role(
    api: TestClient, total: dict[str, str], reader: dict[str, str], effects: list[str]
) -> None:
    invalid = {"reason": 7, "intruso": 1}

    unconfirmed = api.post(f"{PROBE}/send", json=invalid, headers=total)
    confirmed = api.post(f"{PROBE}/send", json={**invalid, "confirm": True}, headers=total)
    forbidden = api.post(f"{PROBE}/send", json=invalid, headers=reader)

    assert unconfirmed.json()["code"] == "confirmation_required"
    assert confirmed.json()["code"] == "validation_failed"
    assert forbidden.json()["code"] == "forbidden_role"
    assert effects == []


def test_in_a_delete_the_confirmation_goes_in_the_query(
    api: TestClient, total: dict[str, str], effects: list[str]
) -> None:
    url = f"{PROBE}/remove/a"
    version = {"expected_version": VERSION_NOW}

    for params in (version, {**version, "confirm": "false"}, {**version, "confirm": "talvez"}):
        response = api.delete(url, params=params, headers=total)
        assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
        assert "na query" in response.json()["detail"]
    in_body = api.request("DELETE", url, params=version, json={"confirm": True}, headers=total)
    assert in_body.json()["code"] == "confirmation_required"  # no DELETE, só a query vale
    assert effects == []

    done = api.delete(url, params={**version, "confirm": "true"}, headers=total)

    assert done.status_code == 200 and effects == ["remove:a"]


def test_the_query_confirm_and_expected_version_are_in_the_contract_of_a_delete(
    api: TestClient,
) -> None:
    parameters = {p["name"]: p for p in operation(api, "probe_remove")["parameters"]}

    assert parameters["confirm"]["in"] == "query" and parameters["expected_version"]["required"]
    assert parameters["confirm"]["description"] and parameters["expected_version"]["description"]


def marked_router() -> OperationRouter:
    return OperationRouter(tag=TAG, description=TAG_TEXT)


MARKED_TEXT = {
    "summary": "Prova",
    "description": "Prova.",
    "returns": "Feito.",
    "response_model": Done,
}


def test_a_marked_operation_must_declare_the_confirm_field_in_its_body() -> None:
    """A marca e a exigência saem juntas; o campo no contrato é o que o OpenAPI e o MCP mostram."""
    router = marked_router()

    class NoConfirm(ApiInput):
        reason: str = "x"

    with pytest.raises(RuntimeError, match=r"não declara o campo `confirm` no corpo"):

        @router.operation(
            "POST", "/probe/a", operation_id="a", role="total", external_effect=True, **MARKED_TEXT
        )
        def without_field(body: NoConfirm) -> Done:
            return Done(done=True)

    with pytest.raises(RuntimeError, match=r"não declara o campo `confirm` no corpo"):

        @router.operation(
            "POST", "/probe/b", operation_id="b", role="total", destructive=True, **MARKED_TEXT
        )
        def without_body() -> Done:
            return Done(done=True)


def test_a_marked_delete_must_declare_the_confirm_field_in_its_query() -> None:
    router = marked_router()

    with pytest.raises(RuntimeError, match=r"não declara o campo `confirm` na query"):

        @router.operation(
            "DELETE",
            "/probe/c/{thing}",
            operation_id="c",
            role="total",
            destructive=True,
            **MARKED_TEXT,
        )
        def without_query(thing: str) -> Done:
            return Done(done=True)

    @router.operation(
        "DELETE",
        "/probe/d/{thing}",
        operation_id="d",
        role="total",
        destructive=True,
        **MARKED_TEXT,
    )
    def with_query(thing: str, confirm: ConfirmQuery = False) -> Done:
        return Done(done=True)


def test_a_marked_operation_with_an_embedded_body_declares_the_confirm_beside_the_others() -> None:
    router = marked_router()

    @router.operation(
        "POST", "/probe/e", operation_id="e", role="total", external_effect=True, **MARKED_TEXT
    )
    def embedded(reason: Annotated[str, Body()], confirm: Annotated[bool, Body()] = False) -> Done:
        return Done(done=True)

    with pytest.raises(RuntimeError, match=r"não declara o campo `confirm` no corpo"):

        @router.operation(
            "POST", "/probe/f", operation_id="f", role="total", external_effect=True, **MARKED_TEXT
        )
        def embedded_without(
            reason: Annotated[str, Body()], other: Annotated[str, Body()] = ""
        ) -> Done:
            return Done(done=True)


def test_an_unmarked_operation_does_not_need_the_confirm_field() -> None:
    router = marked_router()

    @router.operation("POST", "/probe/g", operation_id="g", role="total", **MARKED_TEXT)
    def plain(body: Rename) -> Done:
        return Done(done=True)


def test_a_confirmation_needs_a_mark_to_confirm() -> None:
    with pytest.raises(ValueError, match="sem marca"):
        Confirmation(destructive=False, external_effect=False)


def test_an_operation_can_say_in_its_own_words_what_the_confirmation_is_for(
    api: TestClient, total: dict[str, str], effects: list[str]
) -> None:
    refused = api.post(f"{PROBE}/custom", json={"reason": "teste"}, headers=total)

    assert refused.status_code == 422 and refused.json()["code"] == "confirmation_required"
    assert refused.json()["detail"] == (
        f"{CUSTOM_TEXT} Mande `confirm: true` no corpo para confirmar. Nada foi feito."
    )
    assert effects == []
    assert (
        api.post(f"{PROBE}/send", json={}, headers=total)
        .json()["detail"]
        .startswith("Esta operação tem ")
    )  # sem texto próprio, o efeito em geral
    done = api.post(f"{PROBE}/custom", json={"confirm": True}, headers=total)
    assert done.status_code == 200 and effects == ["custom"]


def test_the_own_words_of_a_confirmation_cannot_be_empty_nor_without_a_mark() -> None:
    with pytest.raises(ValueError, match="vazio"):
        Confirmation(destructive=True, external_effect=False, what="  ")
    router = marked_router()
    with pytest.raises(ValueError, match="confirmation_text só vale numa operação marcada"):

        @router.operation(
            "POST",
            "/probe/h",
            operation_id="h",
            role="total",
            confirmation_text="Texto.",
            **MARKED_TEXT,
        )
        def plain(body: Rename) -> Done:
            return Done(done=True)


# --- a confirmação que uma simulação dispensa ---------------------------------------------------


def test_a_simulation_does_not_need_the_confirm_but_the_real_call_does(
    api: TestClient, total: dict[str, str], effects: list[str]
) -> None:
    for sent in ({}, {"dry_run": False}, {"dry_run": None}, {"dry_run": "true"}, {"dry_run": 1}):
        refused = api.post(f"{PROBE}/simulate", json=sent, headers=total)

        assert refused.status_code == 422, sent
        assert refused.json()["code"] == "confirmation_required"
    assert effects == []

    simulated = api.post(f"{PROBE}/simulate", json={"dry_run": True}, headers=total)
    confirmed = api.post(f"{PROBE}/simulate", json={"confirm": True}, headers=total)

    assert simulated.status_code == 200 and confirmed.status_code == 200
    assert effects == ["simulate:dry", "simulate"]


def test_the_refusal_of_an_operation_with_a_simulation_says_how_to_only_simulate(
    api: TestClient, total: dict[str, str]
) -> None:
    refused = api.post(f"{PROBE}/simulate", json={}, headers=total)

    assert refused.json()["detail"] == (
        f"{CUSTOM_TEXT} Mande `confirm: true` no corpo para confirmar, ou `dry_run: true` para só "
        "simular, que não pede confirmação. Nada foi feito."
    )


def test_the_simulation_exemption_is_declared_in_the_contract_beside_the_mark(
    api: TestClient,
) -> None:
    found = operation(api, "probe_simulate")
    schema = document(api)["components"]["schemas"]["Simulate"]["properties"]

    assert found["x-external-effect"] is True
    assert found["x-confirm-unless"] == "dry_run"
    assert schema["dry_run"]["type"] == "boolean" and schema["dry_run"]["default"] is False
    assert schema["confirm"]["type"] == "boolean"  # o `confirm` segue no contrato
    others = {operation(api, name).get("x-confirm-unless") for name in ("probe_send", "probe_both")}
    assert others == {None}  # só a operação que declarou dispensa


def test_the_exemption_is_not_a_way_around_the_mark_of_another_operation(
    api: TestClient, total: dict[str, str], effects: list[str]
) -> None:
    response = api.post(f"{PROBE}/send", json={"dry_run": True}, headers=total)

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert effects == []


def test_the_exemption_comes_after_the_role_and_before_the_body_validation(
    api: TestClient, total: dict[str, str], reader: dict[str, str], effects: list[str]
) -> None:
    invalid = {"dry_run": True, "reason": 7}

    forbidden = api.post(f"{PROBE}/simulate", json=invalid, headers=reader)
    unvalidated = api.post(f"{PROBE}/simulate", json=invalid, headers=total)

    assert forbidden.json()["code"] == "forbidden_role"
    assert (
        unvalidated.json()["code"] == "validation_failed"
    )  # passou da confirmação e parou no corpo
    assert effects == []


def test_an_operation_cannot_dispense_the_confirmation_without_a_mark_to_confirm() -> None:
    router = marked_router()

    with pytest.raises(ValueError, match="confirm_unless só vale numa operação marcada"):

        @router.operation(
            "POST",
            "/probe/i",
            operation_id="i",
            role="total",
            confirm_unless="dry_run",
            **MARKED_TEXT,
        )
        def plain(body: Simulate) -> Done:
            return Done(done=True)

    with pytest.raises(ValueError, match="vazio"):
        Confirmation(destructive=True, external_effect=False, unless=" ")


class TextDryRun(ConfirmedInput):
    dry_run: str = "não"


class TrueDryRun(ConfirmedInput):
    dry_run: bool = True


def dispensing(router: OperationRouter, number: int) -> Any:
    return router.operation(
        "POST",
        f"/probe/j{number}",
        operation_id=f"j{number}",
        role="total",
        external_effect=True,
        confirm_unless="dry_run",
        **MARKED_TEXT,
    )


def test_the_field_that_dispenses_the_confirmation_must_be_a_boolean_of_the_body() -> None:
    """A exigência e a dispensa saem da mesma declaração, e o campo da dispensa está no contrato:
    um nome que o corpo não tem, ou que não é um booleano que começa falso, seria uma dispensa que
    ninguém vê (ou que vale sem ninguém pedir)."""
    router = marked_router()

    @dispensing(router, 0)
    def fine(body: Simulate) -> Done:
        return Done(done=True)

    with pytest.raises(RuntimeError, match="não é um booleano que começa falso"):

        @dispensing(router, 1)
        def text(body: TextDryRun) -> Done:
            return Done(done=True)

    with pytest.raises(RuntimeError, match="não é um booleano que começa falso"):

        @dispensing(router, 2)
        def true_by_default(body: TrueDryRun) -> Done:
            return Done(done=True)

    with pytest.raises(RuntimeError, match="não declara o campo `dry_run`"):

        @dispensing(router, 3)
        def without_field(body: Destroy) -> Done:
            return Done(done=True)


def test_a_delete_cannot_dispense_the_confirmation_because_it_has_no_body_to_say_so() -> None:
    router = marked_router()

    with pytest.raises(RuntimeError, match="corpo JSON"):

        @router.operation(
            "DELETE",
            "/probe/k/{thing}",
            operation_id="k",
            role="total",
            destructive=True,
            confirm_unless="dry_run",
            **MARKED_TEXT,
        )
        def remove(thing: str, confirm: ConfirmQuery = False, dry_run: bool = False) -> Done:
            return Done(done=True)


# --- os erros que a declaração diz que a operação tem -------------------------------------------


def test_the_declared_errors_follow_what_the_route_is(api: TestClient) -> None:
    def declared(operation_id: str) -> set[str]:
        return {code for code in operation(api, operation_id)["responses"] if code[0] == "4"}

    assert declared("probe_people") == {"401", "422", "429"}  # leitura, sem recurso no caminho
    assert declared("probe_lead") == {"401", "429"}  # nada a validar: sem 422
    assert declared("probe_peek") == {"401", "404", "422", "429"}  # um parâmetro no caminho
    assert declared("probe_send") == {"401", "403", "422", "429"}  # total
    assert declared("probe_destroy") == {
        "401",
        "403",
        "404",
        "409",
        "422",
        "429",
    }  # total + id + 409


def test_a_422_is_declared_only_where_the_request_has_something_to_validate(
    api: TestClient,
) -> None:
    """Sem corpo nem parâmetro, nada reprova: o 422 que o FastAPI poria sozinho também não vem."""
    assert "422" not in operation(api, "probe_lead")["responses"]
    for operation_id in ("probe_peek", "probe_people", "probe_send", "probe_remove"):
        assert "422" in operation(api, operation_id)["responses"], operation_id


def test_the_text_of_a_409_is_the_one_of_its_operation(api: TestClient) -> None:
    def conflict(operation_id: str) -> str:
        return str(operation(api, operation_id)["responses"]["409"]["description"])

    assert conflict("probe_destroy") == STATE_TEXT  # sem versão: só o estado
    assert conflict("probe_rename") == VERSION_TEXT
    assert conflict("probe_remove") == VERSION_TEXT


@pytest.mark.parametrize("text", ["", "   "])
def test_a_409_with_a_blank_text_is_refused_with_a_clear_message(text: str) -> None:
    router = OperationRouter(tag=TAG, description=TAG_TEXT)

    with pytest.raises(ValueError, match=r"409 exige o texto da operação"):
        router.operation(
            "POST",
            "/probe/texto-vazio",
            operation_id="probe_blank_text",
            role="total",
            errors={409: text},
            summary="Prova",
            description="Prova.",
            returns="Feito.",
            response_model=Done,
        )


def test_an_empty_text_for_any_other_declared_error_is_refused_too() -> None:
    with pytest.raises(ValueError, match="o texto da resposta desta operação está vazio"):
        errors.error_responses(404, texts={404: " "})


def test_a_tag_in_two_routers_stops_the_assembly() -> None:
    first = OperationRouter(tag=TAG, description=TAG_TEXT)
    again = OperationRouter(tag=TAG, description="Outro recurso, a mesma tag.")
    other = OperationRouter(tag="Outra", description="Outra tag.")

    with pytest.raises(RuntimeError, match=r"a tag «Prova» é de mais de um roteador"):
        tags_of([first, other, again])

    assert tags_of([first, other]) == [
        {"name": TAG, "description": TAG_TEXT},
        {"name": "Outra", "description": "Outra tag."},
    ]


def test_a_409_declared_without_its_own_text_is_refused_when_it_is_registered() -> None:
    router = OperationRouter(tag=TAG, description=TAG_TEXT)

    with pytest.raises(ValueError, match="409.*texto"):
        router.operation(
            "POST",
            "/probe/sem-texto",
            operation_id="probe_without_text",
            role="total",
            errors=(409,),
            summary="Prova",
            description="Prova.",
            returns="Feito.",
            response_model=Done,
        )


def test_the_statuses_that_carry_a_header_declare_it(api: TestClient) -> None:
    for methods in document(api)["paths"].values():
        for found in methods.values():
            responses = found["responses"]
            assert "WWW-Authenticate" in responses["401"]["headers"]
            assert "Retry-After" in responses["429"]["headers"]
            assert set(responses["401"]["headers"]) == {"WWW-Authenticate"}


def test_the_headers_of_the_other_statuses_are_declared_where_they_are_asked_for() -> None:
    declared = errors.error_responses(405, 503)

    assert set(declared[405]["headers"]) == {"Allow"}
    assert set(declared[503]["headers"]) == {"Retry-After"}
    assert "headers" not in errors.error_responses(403)[403]


def test_the_tags_come_with_their_router_and_carry_its_description(api: TestClient) -> None:
    tags = {tag["name"]: tag["description"] for tag in document(api)["tags"]}

    assert tags["Chaves de API"]
    assert OperationRouter(tag=TAG, description=TAG_TEXT).description == TAG_TEXT


def test_a_tag_without_a_description_does_not_exist() -> None:
    with pytest.raises(ValueError, match="descrição"):
        OperationRouter(tag=TAG, description="  ")


def test_every_declared_error_response_is_the_one_error_model(api: TestClient) -> None:
    for methods in document(api)["paths"].values():
        for found in methods.values():
            for code, response in found["responses"].items():
                if code[0] == "4":
                    content = response["content"]["application/json"]["schema"]
                    assert content == {"$ref": "#/components/schemas/ErrorResponse"}, code
                    assert response["description"]
    assert "HTTPValidationError" not in document(api)["components"]["schemas"]


def test_the_success_response_has_a_description_in_portuguese(api: TestClient) -> None:
    assert operation(api, "probe_send")["responses"]["200"]["description"] == "Feito."


# --- o papel dentro da operação -----------------------------------------------------------------


def test_a_part_of_an_operation_can_ask_for_a_bigger_role_with_the_same_403(
    api: TestClient, total: dict[str, str], reader: dict[str, str], effects: list[str]
) -> None:
    """`leitura` passa pela operação, mas `more=true` é só de `total`: o 403 é o mesmo da operação
    inteira, e a parte recusada não roda."""
    plain = api.get(f"{PROBE}/part", headers=reader)
    refused = api.get(f"{PROBE}/part", params={"more": "true"}, headers=reader)
    allowed = api.get(f"{PROBE}/part", params={"more": "true"}, headers=total)
    whole = api.post(f"{PROBE}/send", json={"confirm": True}, headers=reader)

    assert plain.status_code == allowed.status_code == 200
    assert effects == ["part:False", "part:True"]  # a chamada recusada não chegou ao fim
    assert refused.status_code == 403 and refused.json()["code"] == "forbidden_role"
    assert set(refused.json()) == set(whole.json()) == {"code", "detail"}
    assert refused.json()["detail"] == (
        f"{PART_ASKED} pede uma chave com papel «total»; a sua tem papel «leitura»."
    )
    assert whole.json()["detail"] == (
        "Esta operação pede uma chave com papel «total»; a sua tem papel «leitura»."
    )


def test_the_error_of_a_part_is_declared_with_the_text_of_the_operation(api: TestClient) -> None:
    responses = operation(api, "probe_part")["responses"]

    assert responses["403"]["description"] == PART_TEXT
    assert operation(api, "probe_part")["x-required-role"] == "leitura"


# --- a resposta que não é JSON ------------------------------------------------------------------


def test_a_page_operation_declares_the_text_response_and_answers_with_the_html(
    api: TestClient, reader: dict[str, str]
) -> None:
    success = operation(api, "probe_page")["responses"]["200"]

    assert success["description"] == "Feito."
    assert success["content"] == {"text/html": {"schema": {"type": "string"}}}
    response = api.get(f"{PROBE}/page/ola", headers=reader)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.text == "<!doctype html><p>ola</p>"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_a_page_operation_refuses_in_the_one_error_format_and_declares_the_usual_errors(
    api: TestClient,
) -> None:
    anonymous = api.get(f"{PROBE}/page/ola")

    assert anonymous.status_code == 401 and anonymous.json()["code"] == "invalid_credentials"
    declared = operation(api, "probe_page")["responses"]
    assert set(declared) >= {"200", "401", "404", "422", "429"}
    assert set(declared["200"]["content"]) == {"text/html"}
    assert "application/json" in declared["401"]["content"]


def test_a_file_operation_declares_the_binary_response_and_answers_with_the_bytes(
    api: TestClient, total: dict[str, str]
) -> None:
    success = operation(api, "probe_file")["responses"]["200"]

    assert success["description"] == "Feito."
    assert success["content"] == {
        "application/pdf": {"schema": {"type": "string", "format": "binary"}}
    }
    response = api.get(f"{PROBE}/file/plano", headers=total)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content == b"%PDF-1.4 plano"
    assert response.headers["content-disposition"] == 'attachment; filename="plano.pdf"'
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_a_file_operation_refuses_in_the_one_error_format_and_declares_the_usual_errors(
    api: TestClient, reader: dict[str, str]
) -> None:
    refused = api.get(f"{PROBE}/file/plano", headers=reader)
    anonymous = api.get(f"{PROBE}/file/plano")

    assert refused.status_code == 403 and refused.json()["code"] == "forbidden_role"
    assert anonymous.status_code == 401 and anonymous.json()["code"] == "invalid_credentials"
    declared = set(operation(api, "probe_file")["responses"])
    assert declared >= {"200", "401", "403", "404", "422", "429"}
    assert "application/json" not in operation(api, "probe_file")["responses"]["200"]["content"]


def test_a_file_operation_cannot_also_declare_a_response_model() -> None:
    router = OperationRouter(tag=TAG, description=TAG_TEXT)

    with pytest.raises(ValueError, match="response_model"):

        @router.operation(
            "GET",
            "/probe/both-ways",
            operation_id="probe_both_ways",
            role="total",
            summary="Prova",
            description="Uma operação de prova.",
            returns="Feito.",
            response_model=Done,
            response_class=PdfResponse,
        )
        def both_ways() -> PdfResponse:
            return PdfResponse(b"")


# --- expected_version ---------------------------------------------------------------------------


def test_a_write_on_a_stale_version_is_a_409_with_the_current_one_and_nothing_is_written(
    api: TestClient, total: dict[str, str], effects: list[str]
) -> None:
    response = api.put(
        f"{PROBE}/rename/a", json={"name": "b", "expected_version": "v1"}, headers=total
    )

    assert response.status_code == 409
    assert response.json()["code"] == "version_conflict"
    assert response.json()["current_version"] == VERSION_NOW
    assert effects == []


def test_the_expected_version_is_required_in_a_versioned_body(
    api: TestClient, total: dict[str, str]
) -> None:
    response = api.put(f"{PROBE}/rename/a", json={"name": "b"}, headers=total)

    assert response.status_code == 422
    assert response.json()["errors"] == [
        {"field": "expected_version", "message": "Campo obrigatório.", "line": None}
    ]
    blank = api.put(f"{PROBE}/rename/a", json={"name": "b", "expected_version": ""}, headers=total)
    assert blank.json()["errors"][0]["field"] == "expected_version"


def test_in_a_delete_the_expected_version_goes_in_the_query(
    api: TestClient, total: dict[str, str], effects: list[str]
) -> None:
    stale = api.delete(
        f"{PROBE}/remove/a", params={"expected_version": "v1", "confirm": "true"}, headers=total
    )

    assert stale.status_code == 409 and stale.json()["current_version"] == VERSION_NOW
    assert effects == []


def test_check_version_passes_the_current_one_and_refuses_any_other() -> None:
    check_version("42", "42")
    with pytest.raises(Exception) as refused:
        check_version("42", "41")
    assert refused.value.code == "version_conflict"  # type: ignore[attr-defined]
    assert refused.value.current_version == "42"  # type: ignore[attr-defined]


# --- listas -------------------------------------------------------------------------------------


def read_all(api: TestClient, headers: dict[str, str], **params: Any) -> list[dict[str, Any]]:
    pages = []
    cursor = None
    while True:
        response = api.get(
            f"{PROBE}/people",
            params={**params, **({"cursor": cursor} if cursor else {})},
            headers=headers,
        )
        assert response.status_code == 200
        pages.append(response.json())
        cursor = pages[-1]["next_cursor"]
        if cursor is None:
            return pages


def test_a_list_is_an_envelope_of_items_and_a_cursor_for_the_next_page(
    api: TestClient, reader: dict[str, str]
) -> None:
    first = api.get(f"{PROBE}/people", headers=reader).json()

    assert set(first) == {"items", "next_cursor"}
    assert len(first["items"]) == 50  # o padrão
    assert first["items"][0] == {"name": "pessoa-000"}
    assert isinstance(first["next_cursor"], str)


def test_the_pages_cover_the_list_once_and_the_last_one_has_no_cursor(
    api: TestClient, reader: dict[str, str], people: list[Person]
) -> None:
    pages = read_all(api, reader)

    assert [len(page["items"]) for page in pages] == [50, 50, 20]
    assert [p["next_cursor"] is None for p in pages] == [False, False, True]
    names = [item["name"] for page in pages for item in page["items"]]
    assert names == [person.name for person in people]


def test_the_limit_sets_the_page_size_from_1_to_200(
    api: TestClient, reader: dict[str, str]
) -> None:
    assert [len(p["items"]) for p in read_all(api, reader, limit=70)] == [70, 50]
    assert [len(p["items"]) for p in read_all(api, reader, limit=60)] == [60, 60]
    assert [len(p["items"]) for p in read_all(api, reader, limit=200)] == [120]


@pytest.mark.parametrize("limit", ["0", "201", "-1", "muitos"])
def test_a_limit_out_of_range_is_a_422_on_the_limit(
    api: TestClient, reader: dict[str, str], limit: str
) -> None:
    response = api.get(f"{PROBE}/people", params={"limit": limit}, headers=reader)

    assert response.status_code == 422
    assert response.json()["errors"][0]["field"] == "limit"


def test_a_new_item_at_the_front_neither_skips_nor_repeats_a_row(
    api: TestClient, reader: dict[str, str], people: list[Person]
) -> None:
    """O cursor aponta para o último item entregue, não para uma posição."""
    first = api.get(f"{PROBE}/people", params={"limit": 50}, headers=reader).json()
    people.insert(0, Person(name="pessoa-nova"))

    second = api.get(
        f"{PROBE}/people", params={"limit": 50, "cursor": first["next_cursor"]}, headers=reader
    ).json()

    assert [i["name"] for i in second["items"]][:2] == ["pessoa-050", "pessoa-051"]


def raw_cursor(value: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "cursor",
    [
        "lixo",
        "!!!",
        raw_cursor([1, 2]),
        raw_cursor({"after": 7}),
        raw_cursor({"outro": "pessoa-001"}),
        raw_cursor({"after": "pessoa-que-sumiu"}),
        encode_cursor("pessoa-que-sumiu"),
    ],
)
def test_a_cursor_we_did_not_give_is_a_422_on_the_cursor(
    api: TestClient, reader: dict[str, str], cursor: str
) -> None:
    response = api.get(f"{PROBE}/people", params={"cursor": cursor}, headers=reader)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert response.json()["errors"] == [
        {"field": "cursor", "message": "O cursor não vale.", "line": None}
    ]


def test_a_cursor_longer_than_it_can_be_is_a_422(api: TestClient, reader: dict[str, str]) -> None:
    response = api.get(f"{PROBE}/people", params={"cursor": "a" * 301}, headers=reader)

    assert response.status_code == 422 and response.json()["errors"][0]["field"] == "cursor"


def test_the_list_parameters_are_in_the_contract(api: TestClient) -> None:
    parameters = {p["name"]: p for p in operation(api, "probe_people")["parameters"]}

    assert parameters["limit"]["schema"]["default"] == 50
    assert parameters["limit"]["schema"]["minimum"] == 1
    assert parameters["limit"]["schema"]["maximum"] == 200
    assert parameters["cursor"]["description"]
    schema = document(api)["components"]["schemas"]["PeoplePage"]
    assert set(schema["properties"]) == {"items", "next_cursor"}


def test_an_empty_list_is_one_empty_page() -> None:
    chunk, cursor = paginate([], PageParams(limit=50, cursor=None), key=str)

    assert chunk == [] and cursor is None


def test_a_keyset_cursor_gives_back_the_instant_with_its_microseconds_and_the_id() -> None:
    moment = datetime(2026, 10, 16, 12, 30, 45, 123456, tzinfo=UTC)
    row_id = uuid.uuid4()

    assert decode_keyset(encode_keyset(moment, row_id)) == (moment, row_id)


def test_a_keyset_cursor_is_not_written_for_an_instant_without_timezone() -> None:
    with pytest.raises(ValueError, match="fuso"):
        encode_keyset(datetime(2026, 10, 16, 12, 0), uuid.uuid4())


# Um id fixo: o nome de um teste parametrizado não pode mudar de um worker do xdist para outro.
ROW_ID = uuid.UUID(int=7)


@pytest.mark.parametrize(
    "cursor",
    [
        "lixo",
        encode_cursor("sem-separador"),
        encode_cursor("2026-10-16T12:00:00+00:00|nao-e-um-uuid"),
        encode_cursor(f"nao-e-uma-data|{ROW_ID}"),
        encode_cursor(f"|{ROW_ID}"),
        # Um instante sem fuso: o banco o leria no fuso da sessão, e a posição seria outra.
        encode_cursor(f"2026-10-16T12:00:00|{ROW_ID}"),
        encode_cursor(f"2026-10-16T12:00:00.123456|{ROW_ID}"),
        encode_cursor(f"2026-10-16|{ROW_ID}"),
        raw_cursor({"after": 7}),
    ],
)
def test_a_malformed_keyset_cursor_is_a_422_on_the_cursor(cursor: str) -> None:
    with pytest.raises(ApiError) as refused:
        decode_keyset(cursor)

    assert refused.value.code == "validation_failed"
    assert [item.field for item in refused.value.errors or []] == ["cursor"]


@pytest.mark.parametrize("offset", ["+00:00", "Z", "-03:00", "+05:30"])
def test_a_well_formed_keyset_cursor_is_just_a_position_whoever_wrote_it(offset: str) -> None:
    """O cursor não é assinado: uma posição bem formada (instante com fuso e um UUID) vale como qualquer
    outra, tenha ou não saído da API. O que se garante é que uma posição malformada é `422`."""
    moment, row_id = decode_keyset(encode_cursor(f"2026-10-16T12:00:00{offset}|{ROW_ID}"))

    assert moment.tzinfo is not None and moment.utcoffset() is not None
    assert row_id == ROW_ID


# --- o corpo de formulário (multipart): a chave vem antes dele, e o confirm vai no formulário -----

FORM_TAG = "Prova de formulário"


class UploadForm(ApiInput):
    note: str = "sem nota"
    file: UploadFile


class MarkedUploadForm(ConfirmedInput):
    file: UploadFile


def declare_forms(effects: list[str]) -> OperationRouter:
    router = OperationRouter(tag=FORM_TAG, description="As provas dos corpos de formulário.")
    text = {"summary": "Prova", "description": "Uma operação de prova.", "returns": "Feito."}

    @router.operation(
        "POST",
        "/probe/upload",
        operation_id="probe_upload",
        role="total",
        response_model=Done,
        **text,
    )
    def upload(body: Annotated[UploadForm, File()]) -> Done:
        effects.append(f"upload:{body.note}:{len(body.file.file.read())}")
        return Done(done=True)

    @router.operation(
        "PUT",
        "/probe/upload-marked",
        operation_id="probe_upload_marked",
        role="total",
        external_effect=True,
        response_model=Done,
        **text,
    )
    def upload_marked(body: Annotated[MarkedUploadForm, File()]) -> Done:
        effects.append(f"marked:{len(body.file.file.read())}")
        return Done(done=True)

    return router


@pytest.fixture
def forms_api(deps: Deps, clock: FakeClock, effects: list[str]) -> TestClient:
    client = make_api(deps, clock)
    add_routes(client, declare_forms(effects))
    return client


GARBAGE = {"Content-Type": "multipart/form-data; boundary=fronteira"}


def test_a_form_body_waits_for_the_key_and_the_role_before_it_is_read(
    forms_api: TestClient, total: dict[str, str], reader: dict[str, str], effects: list[str]
) -> None:
    """Um corpo que nem se lê (aqui, lixo no lugar do `multipart`) só é recusado por ser corpo depois
    de a chave e o papel passarem: quem não tem chave recebe `401`, e quem tem papel `leitura`, `403`
    (o servidor não gasta memória nem disco com o arquivo de quem não pode enviá-lo)."""
    anonymous = forms_api.post(f"{PROBE}/upload", content=b"lixo", headers=GARBAGE)
    wrong = forms_api.post(
        f"{PROBE}/upload", content=b"lixo", headers={**GARBAGE, "Authorization": "Bearer gp_x"}
    )
    forbidden = forms_api.post(f"{PROBE}/upload", content=b"lixo", headers={**GARBAGE, **reader})
    allowed = forms_api.post(f"{PROBE}/upload", content=b"lixo", headers={**GARBAGE, **total})

    assert anonymous.status_code == 401 and anonymous.json()["code"] == "invalid_credentials"
    assert anonymous.headers["WWW-Authenticate"] == "Bearer"
    assert wrong.status_code == 401
    assert forbidden.status_code == 403 and forbidden.json()["code"] == "forbidden_role"
    assert allowed.status_code == 422 and allowed.json()["code"] == "validation_failed"
    assert [item["field"] for item in allowed.json()["errors"]] == ["file"]  # o corpo não se leu
    assert effects == []


def test_a_form_body_with_no_boundary_is_a_422_on_the_file_field_of_the_model(
    forms_api: TestClient, total: dict[str, str], effects: list[str]
) -> None:
    """O campo é o do arquivo do modelo do corpo (`UploadFile`), seja qual for o nome dele."""
    plain = forms_api.post(
        f"{PROBE}/upload", content=b"x", headers={**total, "Content-Type": "multipart/form-data"}
    )
    marked = forms_api.put(
        f"{PROBE}/upload-marked",
        content=b"x",
        headers={**total, "Content-Type": "multipart/form-data"},
    )

    for response in (plain, marked):
        assert response.status_code == 422 and response.json()["code"] == "validation_failed"
        (item,) = response.json()["errors"]
        assert item["field"] == "file" and "multipart" in item["message"]
    assert effects == []


def test_a_form_body_is_not_read_for_a_caller_who_cannot_send_it(
    forms_api: TestClient, reader: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[int] = []
    original = Request.form

    def counting(self: Request, **kwargs: Any) -> Any:
        reads.append(1)
        return original(self, **kwargs)

    monkeypatch.setattr(Request, "form", counting)
    files = {"file": ("a.bin", b"abc")}

    forms_api.post(f"{PROBE}/upload", files=files)
    forms_api.post(f"{PROBE}/upload", files=files, headers=reader)
    forms_api.post(f"{PROBE}/upload", files=files, headers={"Authorization": "Bearer gp_x"})

    assert reads == []


def test_a_form_body_asks_the_key_once(
    forms_api: TestClient, total: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[int] = []
    original = security._authenticate

    def counting(authorization: str | None, now: Any) -> Any:
        asked.append(1)
        return original(authorization, now)

    monkeypatch.setattr(security, "_authenticate", counting)

    done = forms_api.post(
        f"{PROBE}/upload", files={"file": ("a.bin", b"abc")}, data={"note": "n"}, headers=total
    )

    assert done.status_code == 200 and asked == [1]


def test_a_form_body_spends_one_token_of_the_bucket_of_the_key(
    deps: Deps, clock: FakeClock, effects: list[str]
) -> None:
    """O balde com 5 fichas e o relógio parado: a sexta chamada é a primeira sem ficha. Se o corpo
    autenticasse duas vezes (uma antes de ler, outra na dependência), a terceira já seria recusada."""
    client = make_api(deps, clock, API_RATE_LIMIT_PER_MINUTE="5")
    add_routes(client, declare_forms(effects))
    key = bearer(new_key(deps, clock, "total").key)

    answers = [
        client.post(f"{PROBE}/upload", files={"file": ("a.bin", b"abc")}, headers=key).status_code
        for _ in range(6)
    ]

    assert answers == [200, 200, 200, 200, 200, 429]


def test_a_form_operation_works_as_any_other_with_the_key(
    forms_api: TestClient, total: dict[str, str], effects: list[str]
) -> None:
    done = forms_api.post(
        f"{PROBE}/upload", files={"file": ("a.bin", b"abcd")}, data={"note": "n"}, headers=total
    )

    assert done.status_code == 200 and done.json() == {"done": True}
    assert effects == ["upload:n:4"]


def test_a_form_operation_is_declared_as_multipart_with_a_named_model(
    forms_api: TestClient,
) -> None:
    found = operation(forms_api, "probe_upload")

    content = found["requestBody"]["content"]
    assert list(content) == ["multipart/form-data"]
    assert content["multipart/form-data"]["schema"] == {"$ref": "#/components/schemas/UploadForm"}


@pytest.mark.parametrize("sent", [{}, {"confirm": "false"}, {"confirm": "talvez"}, {"confirm": ""}])
def test_a_marked_form_operation_does_nothing_without_the_confirm_in_the_form(
    forms_api: TestClient, total: dict[str, str], effects: list[str], sent: dict[str, str]
) -> None:
    response = forms_api.put(
        f"{PROBE}/upload-marked", files={"file": ("a.bin", b"abc")}, data=sent, headers=total
    )

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert "no corpo" in response.json()["detail"] and effects == []


@pytest.mark.parametrize("sent", ["true", "True", "TRUE", "1"])
def test_a_marked_form_operation_goes_ahead_with_the_confirm_in_the_form(
    forms_api: TestClient, total: dict[str, str], effects: list[str], sent: str
) -> None:
    response = forms_api.put(
        f"{PROBE}/upload-marked",
        files={"file": ("a.bin", b"abc")},
        data={"confirm": sent},
        headers=total,
    )

    assert response.status_code == 200 and effects == ["marked:3"]


def test_a_marked_form_operation_also_takes_the_confirm_in_a_urlencoded_form(
    forms_api: TestClient, total: dict[str, str], effects: list[str]
) -> None:
    """Sem arquivo o formulário não é `multipart`: o `confirm` vale igual, e o campo que falta é um
    `422` do corpo (e não uma exceção por ler o corpo duas vezes)."""
    response = forms_api.put(f"{PROBE}/upload-marked", data={"confirm": "true"}, headers=total)

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    assert [item["field"] for item in response.json()["errors"]] == ["file"]
    assert effects == []


def test_the_form_confirmation_comes_before_the_validation_of_the_form_and_after_the_role(
    forms_api: TestClient, total: dict[str, str], reader: dict[str, str], effects: list[str]
) -> None:
    files = {"file": ("a.bin", b"abc")}

    unconfirmed = forms_api.put(f"{PROBE}/upload-marked", data={"intruso": "1"}, headers=total)
    confirmed = forms_api.put(
        f"{PROBE}/upload-marked", data={"confirm": "true", "intruso": "1"}, headers=total
    )
    forbidden = forms_api.put(f"{PROBE}/upload-marked", files=files, headers=reader)

    assert unconfirmed.json()["code"] == "confirmation_required"
    assert confirmed.json()["code"] == "validation_failed"
    assert forbidden.json()["code"] == "forbidden_role"
    assert effects == []


def test_a_marked_form_operation_declares_the_confirm_field_in_the_form(
    forms_api: TestClient,
) -> None:
    found = operation(forms_api, "probe_upload_marked")

    assert found["x-external-effect"] is True
    schema = document(forms_api)["components"]["schemas"]["MarkedUploadForm"]
    assert schema["properties"]["confirm"]["type"] == "boolean"


def test_a_text_where_the_file_goes_is_a_422_in_portuguese_that_does_not_repeat_it(
    forms_api: TestClient, total: dict[str, str]
) -> None:
    response = forms_api.post(
        f"{PROBE}/upload", data={"file": "um texto no lugar do arquivo"}, headers=total
    )

    assert response.status_code == 422
    (item,) = response.json()["errors"]
    assert item["field"] == "file" and "multipart" in item["message"]
    assert "um texto no lugar" not in response.text
