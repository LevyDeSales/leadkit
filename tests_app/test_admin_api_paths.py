"""Um caractere de controle no caminho ou na query (o byte NUL, uma quebra de linha...) é recusado antes
do banco.

O Postgres recusa o NUL num texto, então um `slug`, um `scope`, um código de pedido ou o `q` da busca
com `%00` chegava ao banco e saía como `500`. Nenhum endereço, código ou id tem caractere de controle:
um caminho que o tem é `404`, sem repetir o caminho, e um parâmetro de query que o tem é `422` no campo
dele na API de admin (`400` nas páginas e na API pública), sem repetir o valor. O teste enumera os
parâmetros de caminho e de query das operações do OpenAPI, então uma operação nova entra sozinha, e
percorre as páginas do admin e algumas rotas públicas.
"""

from __future__ import annotations

import functools
import re
import secrets
from typing import Any

import httpx
import pytest
from admin_api_matrix import operations_of
from admin_api_support import API, ApiClient, admin_routes, built_openapi, make_api
from fastapi.routing import iter_route_contexts
from test_admin import FakeClock

from app.admin.api.application import create_admin_api
from app.admin.api.operations import OperationRoute
from app.admin.api.operations import plain_query as query_check
from app.deps import Deps

# Um valor que nenhuma resposta pode devolver. Sorteado na execução, como as chaves.
SENTINEL = "ZZ" + secrets.token_hex(8)
# O NUL, uma quebra de linha, o último caractere de controle de baixo (US) e o DEL, escritos como o
# cliente os escreve no caminho.
CONTROL = {"nul": "%00", "newline": "%0a", "unit-separator": "%1f", "del": "%7f"}
PLAIN = {"integer": "1", "uuid": "00000000-0000-0000-0000-000000000000", "string": "x"}


def filled(path: str, parameters: list[dict[str, Any]], bad: str, value: str) -> str:
    """O caminho com `bad` no lugar do valor e um valor comum nos outros parâmetros."""
    values = {}
    for parameter in parameters:
        if parameter["in"] != "path":
            continue
        schema = parameter["schema"]
        kind = "uuid" if schema.get("format") == "uuid" else schema.get("type", "string")
        values[parameter["name"]] = value if parameter["name"] == bad else PLAIN[kind]
    return path.format(**values)


def with_path_parameters() -> list[tuple[str, str, str]]:
    """(operation_id, método, parâmetro) de cada parâmetro de caminho de cada operação da API."""
    found = []
    for method, path, operation in operations_of(built_openapi()):
        for parameter in operation.get("parameters", []):
            if parameter["in"] == "path":
                found.append((operation["operationId"], method, parameter["name"]))
    return found


CASES = with_path_parameters()


@functools.cache
def operations_by_id() -> dict[str, tuple[str, list[dict[str, Any]]]]:
    """(caminho, parâmetros) de cada operação, do documento montado uma vez: os testes abaixo são
    mais de seiscentos, e cada um procurava a sua operação num documento montado de novo."""
    return {
        operation["operationId"]: (path, operation.get("parameters", []))
        for _, path, operation in operations_of(built_openapi())
    }


def operation_of(operation_id: str) -> tuple[str, str, list[dict[str, Any]]]:
    path, parameters = operations_by_id()[operation_id]
    return path, operation_id, parameters


def test_the_enumeration_finds_the_slug_the_scope_and_the_other_path_parameters() -> None:
    names = {name for _, _, name in CASES}

    assert {"slug", "scope", "public_id", "key_id", "asset_id"} <= names
    assert len({operation_id for operation_id, _, _ in CASES}) >= 25  # a enumeração não encolheu


@pytest.mark.parametrize("control", CONTROL.values(), ids=CONTROL.keys())
@pytest.mark.parametrize(
    ("operation_id", "method", "name"), CASES, ids=[f"{o}-{n}" for o, _, n in CASES]
)
def test_a_control_character_in_a_path_parameter_is_a_404_in_every_operation(
    api_client: Any, operation_id: str, method: str, name: str, control: str
) -> None:
    client: ApiClient = api_client("total")
    path, _, parameters = operation_of(operation_id)
    address = API + filled(path, parameters, name, f"{SENTINEL}{control}{SENTINEL}")

    response = client.request(method, address, json={"confirm": True, "expected_version": "1"})

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert SENTINEL not in response.text and SENTINEL not in str(response.headers)
    assert response.headers["cache-control"] == "no-store"


def test_a_control_character_gets_the_same_404_without_a_key(deps: Deps) -> None:
    """Um caminho assim não corresponde a nenhuma operação: a resposta é a de qualquer endereço sem
    operação, antes da chave."""
    anonymous = make_api(deps, FakeClock())

    response = anonymous.get(f"{API}/destinations/a%00b")
    unknown = anonymous.get(f"{API}/nao-existe")

    assert response.status_code == unknown.status_code == 404
    assert response.json()["code"] == unknown.json()["code"] == "not_found"


@pytest.mark.parametrize("control", CONTROL.values(), ids=CONTROL.keys())
def test_a_control_character_in_the_path_of_an_admin_page_is_a_404(
    api_client: Any, control: str
) -> None:
    client: ApiClient = api_client("total")
    pages = sorted((method, path) for method, path in admin_routes(client.app) if "{" in path)
    assert len(pages) > 20  # as páginas com parâmetro no caminho foram achadas
    refused: list[tuple[str, str, int]] = []

    for method, path in pages:
        address = re.sub(r"\{[^}]+\}", f"a{control}b", path)
        response = client.request(method, address, follow_redirects=False)
        if response.status_code != 404:
            refused.append((method, path, response.status_code))

    assert refused == []


@pytest.mark.parametrize(
    ("method", "address"),
    [
        ("GET", "/p/a%00b"),
        ("GET", "/v1/jobs/a%00b"),
        ("GET", "/v1/jobs/a%00b/pdf"),
        ("POST", "/v1/forms/a%00b/submissions"),
        ("GET", "/admin/destinations/forms/a%00b"),
        ("GET", "/admin/forms/a%00b"),
        ("GET", "/admin/jobs/a%00b"),
    ],
)
def test_a_control_character_in_a_public_or_admin_path_is_a_404_and_not_a_server_error(
    api_client: Any, method: str, address: str
) -> None:
    client: ApiClient = api_client("total")

    response = client.request(method, address, json={}, follow_redirects=False)

    assert response.status_code == 404
    assert "a\x00b" not in response.text and "a%00b" not in response.text


def test_a_clean_path_still_reaches_its_operation(api_client: Any) -> None:
    """O guarda só recusa o que tem caractere de controle: o resto do caminho segue como era."""
    client: ApiClient = api_client("leitura")

    ok = client.get(f"{API}/destinations/default")
    absent: httpx.Response = client.get(f"{API}/destinations/nao-existe")

    assert ok.status_code == 200
    assert absent.status_code == 404 and "Escopo não encontrado" in absent.json()["detail"]


# --- a query string -----------------------------------------------------------------------------
# O mesmo vale para o valor de um parâmetro de query: o `q` da busca de pedidos ia ao banco com o NUL
# (um `500`). Na API de admin é `422 validation_failed` no campo do parâmetro, depois da chave, do papel
# e da confirmação (a ordem das recusas); nas páginas e na API pública, `400`. Nunca repete o valor.
CONTROL_VALUES = {"nul": "\x00", "newline": "\n", "unit-separator": "\x1f", "del": "\x7f"}
PLAIN_QUERY = {"integer": "1", "boolean": "true", "string": "x"}


def with_query_parameters() -> list[tuple[str, str, str]]:
    """(operation_id, método, parâmetro) de cada parâmetro de query de cada operação da API."""
    found = []
    for method, _, operation in operations_of(built_openapi()):
        for parameter in operation.get("parameters", []):
            if parameter["in"] == "query":
                found.append((operation["operationId"], method, parameter["name"]))
    return found


QUERY_CASES = with_query_parameters()


def plain_query(parameters: list[dict[str, Any]], bad: str, value: str) -> dict[str, str]:
    """Os parâmetros de query, com `value` em `bad` e um valor comum nos outros."""
    found = {}
    for parameter in parameters:
        if parameter["in"] != "query":
            continue
        kind = parameter["schema"].get("type", "string")
        found[parameter["name"]] = value if parameter["name"] == bad else PLAIN_QUERY.get(kind, "x")
    return found


def test_the_enumeration_finds_the_search_and_the_paging_parameters() -> None:
    names = {name for _, _, name in QUERY_CASES}

    assert {"q", "status", "cursor", "limit", "confirm", "expected_version"} <= names
    assert len({operation_id for operation_id, _, _ in QUERY_CASES}) >= 10


@pytest.mark.parametrize("control", CONTROL_VALUES.values(), ids=CONTROL_VALUES.keys())
@pytest.mark.parametrize(
    ("operation_id", "method", "name"), QUERY_CASES, ids=[f"{o}-{n}" for o, _, n in QUERY_CASES]
)
def test_a_control_character_in_a_query_parameter_is_a_422_on_that_field_in_every_operation(
    api_client: Any, operation_id: str, method: str, name: str, control: str
) -> None:
    client: ApiClient = api_client("total")
    path, _, parameters = operation_of(operation_id)
    plain_path = filled(path, parameters, "", "")
    query = plain_query(parameters, name, f"{SENTINEL}{control}{SENTINEL}")
    if name == "confirm":  # a confirmação é a primeira recusa: sem ela, nada mais é lido
        query["confirm"] = f"true{control}"

    response = client.request(method, API + plain_path, params=query, json={"confirm": True})

    assert response.status_code == 422
    body = response.json()
    if name == "confirm":
        assert body["code"] in ("confirmation_required", "validation_failed")
    else:
        assert body["code"] == "validation_failed"
        assert [item["field"] for item in body["errors"]] == [name]
    assert SENTINEL not in response.text and SENTINEL not in str(response.headers)


def test_the_search_of_the_orders_with_a_nul_is_a_422_and_not_a_server_error(
    api_client: Any,
) -> None:
    response = api_client("leitura").get(f"{API}/jobs", params={"q": "a\x00b"})

    assert response.status_code == 422
    assert response.json()["errors"] == [
        {"field": "q", "message": "Não use caracteres de controle.", "line": None}
    ]


def test_the_key_and_the_role_come_before_the_query_check(api_client: Any, deps: Deps) -> None:
    anonymous = make_api(deps, FakeClock()).get(f"{API}/jobs", params={"q": "a\x00b"})
    reader: ApiClient = api_client("leitura")

    forbidden = reader.delete(
        f"{API}/destinations/default", params={"expected_version": "1\x00", "confirm": "true"}
    )

    assert anonymous.status_code == 401
    assert forbidden.status_code == 403  # `leitura` não pode remover: o papel vem antes da query


def test_a_control_character_in_the_name_of_an_undeclared_parameter_is_ignored_as_any_other(
    api_client: Any,
) -> None:
    response = api_client("leitura").get(f"{API}/destinations", params={"a\x00b": "1"})

    assert response.status_code == 200


@pytest.mark.parametrize("control", CONTROL.values(), ids=CONTROL.keys())
def test_a_control_character_in_the_query_of_an_admin_page_is_a_400_and_not_a_server_error(
    api_client: Any, control: str
) -> None:
    client: ApiClient = api_client("total")
    pages = sorted((method, path) for method, path in admin_routes(client.app) if "{" not in path)
    assert len(pages) > 10  # as páginas sem parâmetro no caminho foram achadas
    refused: list[tuple[str, str, int]] = []

    for method, path in pages:
        for query in (f"q=a{control}b", f"a{control}b=1"):
            response = client.request(method, f"{path}?{query}", follow_redirects=False)
            if response.status_code != 400:
                refused.append((method, f"{path}?{query}", response.status_code))

    assert refused == []


def test_the_search_of_the_orders_page_with_a_nul_is_a_400(api_client: Any) -> None:
    client: ApiClient = api_client("total")

    response = client.get("/admin?q=%00")

    assert response.status_code == 400
    assert "a\x00b" not in response.text


@pytest.mark.parametrize(
    ("method", "address"),
    [
        ("GET", "/p/demo?utm=a%00b"),
        ("GET", "/v1/jobs/abc?token=a%00b"),
        ("GET", "/v1/jobs/abc/pdf?token=a%0ab"),
        ("POST", "/v1/forms/demo/submissions?x=%7f"),
    ],
)
def test_a_control_character_in_the_query_of_a_public_route_is_a_400(
    api_client: Any, method: str, address: str
) -> None:
    client: ApiClient = api_client("total")

    response = client.request(method, address, json={}, follow_redirects=False)

    assert response.status_code == 400


def test_a_clean_query_still_reaches_its_operation(api_client: Any) -> None:
    client: ApiClient = api_client("leitura")

    ok = client.get(f"{API}/jobs", params={"q": "abc", "limit": "5"})
    page = client.get("/admin/destinations", params={"x": "1"}, follow_redirects=False)

    assert ok.status_code == 200
    assert page.status_code in (200, 303)


def test_every_operation_of_the_api_has_the_query_check_as_a_dependency() -> None:
    """A recusa do caractere de controle na query é uma dependência de `OperationRouter.operation`: toda
    operação a tem, as que já existem e as das próximas fases, sem que cada uma lembre de nada."""
    routes = [
        context.route
        for context in iter_route_contexts(create_admin_api().routes)
        if isinstance(context.route, OperationRoute)
    ]

    assert len(routes) >= 48
    assert [
        route.path
        for route in routes
        if not any(dep.call is query_check for dep in route.dependant.dependencies)
    ] == []
