"""O formato de erro único da API de admin (ADR 0015, "Erros"), pela costura HTTP.

Cada status da tabela da ADR sai como `{"code", "detail"}` (com `errors` só no `422`), com os
cabeçalhos de privacidade, e **a resposta nunca traz o valor recebido**. O formato do app principal
(as páginas do admin e a API pública) continua o que era.

As rotas de prova (`/probe/...`) são acrescentadas à sub-aplicação só nestes testes: elas levantam
cada erro da tabela, que as operações reais ainda não levantam todos (o `410` e o `502`, por
exemplo, chegam com as fases dos pedidos e das imagens).
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
import uuid
from collections.abc import Iterator
from typing import Annotated, Any, Literal

import pytest
import sqlalchemy as sa
from admin_api_support import API, add_routes, make_api
from fastapi import Form, HTTPException
from fastapi.testclient import TestClient
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from test_admin import FakeClock

from app.admin import _HIDE_ADMIN_QUERY
from app.admin.api import errors
from app.admin.api.errors import STATUS_OF, ApiError, ErrorItem
from app.admin.api.operations import OpenRouter
from app.api.main import MAX_BODY_BYTES, MAX_UPLOAD_BODY_BYTES
from app.deps import Deps
from app.forms.definition.document import Problem

PROBE = f"{API}/probe"
PRIVATE = {"cache-control": "no-store", "x-content-type-options": "nosniff"}
# Um valor que nenhuma resposta pode devolver. Sorteado na execução, como as chaves.
SENTINEL = secrets.token_hex(16)


class Item(BaseModel):
    label: str = Field(min_length=3)


class Probe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=3, max_length=10)
    count: int = Field(ge=1)
    items: list[Item]
    role: Literal["leitura", "total"] = "leitura"
    when: AwareDatetime | None = None
    slug: str | None = None

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str | None) -> str | None:
        if value is not None and not value.islower():
            raise ValueError("Use só letras minúsculas.")
        return value


class Add(BaseModel):
    op: Literal["add"]
    value: Any


class Remove(BaseModel):
    op: Literal["remove"]


class Edits(BaseModel):
    """Uma lista de operações pela etiqueta `op`: o jeito do JSON Patch do rascunho."""

    edits: list[Annotated[Add | Remove, Field(discriminator="op")]]


class Boom(Exception):
    pass


# O que cada código da tabela leva além de `code` e `detail`.
EXTRAS: dict[str, dict[str, Any]] = {
    "version_conflict": {"current_version": "abc123"},
    "rate_limited": {"retry_after": 7},
    "test_paused": {"retry_after": 30},
    "unavailable": {"retry_after": 5},
}


def probes() -> OpenRouter:
    """As rotas de prova são abertas de propósito (`OpenRouter`): provam o formato de erro, não a chave."""
    router = OpenRouter(prefix="/probe")

    @router.get("/raise/{code}")
    def raise_code(code: str) -> None:
        raise ApiError(code, f"Detalhe de {code}.", **EXTRAS.get(code, {}))

    @router.get("/ok")
    def ok() -> dict[str, str]:
        return {"ok": "sim"}

    @router.post("/body")
    def body(payload: Probe) -> dict[str, str]:
        return {"name": payload.name}

    @router.post("/union")
    def union(payload: Edits) -> dict[str, int]:
        return {"edits": len(payload.edits)}

    @router.post("/form")
    def form(title: Annotated[str, Form()]) -> dict[str, str]:
        return {"title": title}

    @router.get("/uuid/{value}")
    def by_uuid(value: uuid.UUID) -> dict[str, str]:
        return {"value": str(value)}

    @router.get("/time")
    def by_time(at: dt.time) -> dict[str, str]:
        return {"at": str(at)}

    @router.get("/boom")
    def boom() -> None:
        raise Boom(f"explodiu com {SENTINEL}")

    @router.get("/database")
    def database() -> None:
        raise sa.exc.OperationalError("SELECT 1", {}, Exception(f"sem rota para {SENTINEL}"))

    @router.get("/problems")
    def problems() -> None:
        raise ApiError.invalid(
            "A definição não compila.",
            [
                Problem("steps.1.questions.0.options", "Faltam opções."),
                Problem("pdf.template", "Chave desconhecida.", line=4),
            ],
        )

    @router.get("/teapot")
    def teapot() -> None:
        raise HTTPException(status_code=418, detail=f"sou um bule {SENTINEL}")

    return router


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def api(deps: Deps, clock: FakeClock) -> TestClient:
    client = make_api(deps, clock)
    add_routes(client, probes())
    return client


def assert_private(response: Any) -> None:
    for name, value in PRIVATE.items():
        assert response.headers.get(name) == value, name


def assert_error_format(response: Any, code: str) -> dict[str, Any]:
    """O corpo é `{"code", "detail"}` (mais `errors` no 422, `current_version` no 409 e `reasons` na
    recusa da publicação), o status é o do código e os cabeçalhos de privacidade estão lá."""
    body = response.json()
    assert body["code"] == code and response.status_code == STATUS_OF[code]
    assert isinstance(body["detail"], str) and body["detail"]
    assert set(body) <= {"code", "detail", "errors", "current_version", "reasons"}
    assert_private(response)
    return body  # type: ignore[no-any-return]


# --- a tabela da ADR ----------------------------------------------------------------------------


def test_the_error_table_has_every_status_of_the_adr() -> None:
    assert sorted(set(STATUS_OF.values())) == [
        401,
        403,
        404,
        405,
        409,
        410,
        413,
        422,
        429,
        500,
        502,
        503,
    ]
    assert STATUS_OF["version_conflict"] == STATUS_OF["state_conflict"] == 409
    assert STATUS_OF["rate_limited"] == STATUS_OF["test_paused"] == 429


@pytest.mark.parametrize("code", sorted(STATUS_OF))
def test_each_code_of_the_table_answers_with_its_status_and_one_format(
    api: TestClient, code: str
) -> None:
    response = api.get(f"{PROBE}/raise/{code}")

    body = assert_error_format(response, code)
    assert body["detail"] == f"Detalhe de {code}."
    assert "errors" not in body  # fora do 422 com problemas, `errors` não vem
    assert "reasons" not in body  # só a recusa da publicação o leva
    assert ("current_version" in body) == (code == "version_conflict")
    if code == "version_conflict":
        assert body["current_version"] == "abc123"
    if code in ("rate_limited", "test_paused", "unavailable"):
        assert response.headers["Retry-After"] == str(EXTRAS[code]["retry_after"])
    else:
        assert "retry-after" not in response.headers


def test_an_unknown_code_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="desconhecido"):
        ApiError("coisa_nova", "x")


def test_the_status_comes_from_the_code_so_a_caller_cannot_mix_them_up() -> None:
    assert ApiError("gone", "O PDF foi apagado.").status_code == 410
    assert ApiError("upstream_failed", "O Baserow não respondeu.").status_code == 502


def test_a_retry_after_is_at_least_one_second() -> None:
    assert ApiError("rate_limited", "x", retry_after=0).headers["Retry-After"] == "1"


def test_the_responses_of_an_operation_are_declared_with_the_error_model_and_its_meaning() -> None:
    declared = errors.error_responses(401, 403, 422)

    assert set(declared) == {401, 403, 422}
    assert all(entry["model"] is errors.ErrorResponse for entry in declared.values())
    assert "WWW-Authenticate" in declared[401]["description"]
    assert "confirmation_required" in declared[422]["description"]


# --- o 422 e o valor recebido -------------------------------------------------------------------


def test_a_validation_error_names_each_field_by_its_path_with_indexes_from_zero(
    api: TestClient,
) -> None:
    sent = {
        "name": SENTINEL,  # longo demais
        "count": f"x{SENTINEL}",  # não é número
        "items": [{"label": "bom"}, {"label": SENTINEL[:2]}],  # a segunda é curta
        "role": f"z{SENTINEL}",  # fora da lista
        "when": "2027-01-31T12:00:00",  # sem fuso
        "slug": f"A{SENTINEL}".upper(),  # maiúscula: o validador recusa
        "intruso": SENTINEL,  # campo que não existe
    }

    response = api.post(f"{PROBE}/body", json=sent)

    body = assert_error_format(response, "validation_failed")
    found = {item["field"]: item["message"] for item in body["errors"]}
    assert found == {
        "name": "Use no máximo 10 caracteres.",
        "count": "Informe um número inteiro.",
        "items.1.label": "Use no mínimo 3 caracteres.",
        "role": "Use um destes valores: 'leitura' ou 'total'.",
        "when": "Informe o fuso horário, por exemplo 2027-01-31T12:00:00Z.",
        "slug": "Use só letras minúsculas.",
        "intruso": "Campo desconhecido.",
    }
    assert all(set(item) == {"field", "message", "line"} for item in body["errors"])
    assert all(item["line"] is None for item in body["errors"])


def test_a_validation_error_never_carries_the_value_received(api: TestClient) -> None:
    sent = {
        "name": SENTINEL,
        "count": SENTINEL,
        "items": [{"label": ""}],
        "role": SENTINEL,
        "slug": SENTINEL.upper(),
    }

    response = api.post(f"{PROBE}/body", json=sent)

    assert response.status_code == 422 and len(response.json()["errors"]) == 5
    assert SENTINEL not in response.text and SENTINEL.upper() not in response.text
    for item in response.json()["errors"]:
        assert "input" not in item and "ctx" not in item and "url" not in item


def test_the_name_of_an_unknown_field_is_the_only_thing_echoed_from_the_request(
    api: TestClient,
) -> None:
    """O nome do campo que sobrou é o caminho do problema (`field`); o valor dele não volta."""
    response = api.post(
        f"{PROBE}/body",
        json={"name": "bom", "count": 1, "items": [], "intruso": SENTINEL},
    )

    assert response.json()["errors"] == [
        {"field": "intruso", "message": "Campo desconhecido.", "line": None}
    ]
    assert SENTINEL not in response.text


def test_a_path_parameter_that_does_not_parse_does_not_echo_its_characters(api: TestClient) -> None:
    """O Pydantic cita o caractere errado de um UUID na mensagem dele; a nossa é fixa."""
    response = api.get(f"{PROBE}/uuid/zz{SENTINEL}")

    body = assert_error_format(response, "validation_failed")
    assert body["errors"] == [
        {"field": "value", "message": "Informe um identificador válido.", "line": None}
    ]
    assert SENTINEL not in response.text and "found" not in response.text


def test_an_error_type_the_table_does_not_know_becomes_a_generic_message(api: TestClient) -> None:
    response = api.get(f"{PROBE}/time", params={"at": f"no-{SENTINEL}"})

    body = assert_error_format(response, "validation_failed")
    assert body["errors"] == [{"field": "at", "message": "Valor inválido.", "line": None}]
    assert SENTINEL not in response.text


def test_a_type_the_union_does_not_accept_names_the_accepted_ones_and_not_the_one_received(
    api: TestClient,
) -> None:
    """O Pydantic cita a etiqueta recebida (`Input tag 'x' found using 'op'...`); a nossa mensagem diz
    só as aceitas, e o campo é o que diz o tipo (`edits.1.op`), e não a união inteira."""
    response = api.post(
        f"{PROBE}/union",
        json={"edits": [{"op": "add", "value": 1}, {"op": f"x{SENTINEL}"}, {"value": SENTINEL}]},
    )

    body = assert_error_format(response, "validation_failed")
    assert body["errors"] == [
        {
            "field": "edits.1.op",
            "message": "Use um destes valores: 'add' ou 'remove'.",
            "line": None,
        },
        {"field": "edits.2.op", "message": "Campo obrigatório.", "line": None},
    ]
    assert SENTINEL not in response.text


def test_a_query_parameter_is_named_without_the_source(api: TestClient) -> None:
    response = api.get(f"{PROBE}/time")  # falta o `at`

    assert response.json()["errors"][0] == {
        "field": "at",
        "message": "Campo obrigatório.",
        "line": None,
    }


def test_a_body_that_is_not_json_is_a_validation_error_on_the_whole_body(api: TestClient) -> None:
    response = api.post(
        f"{PROBE}/body",
        content=f'{{"name": "{SENTINEL}',
        headers={"Content-Type": "application/json"},
    )

    body = assert_error_format(response, "validation_failed")
    assert body["errors"] == [
        {"field": "", "message": "O corpo não é um JSON válido.", "line": None}
    ]
    assert SENTINEL not in response.text


def test_a_body_that_is_not_even_text_is_also_not_a_valid_json(api: TestClient) -> None:
    response = api.post(
        f"{PROBE}/body", content=b"\xff\xfe\x00\xff", headers={"Content-Type": "application/json"}
    )

    body = assert_error_format(response, "validation_failed")
    assert body["errors"] == [
        {"field": "", "message": "O corpo não é um JSON válido.", "line": None}
    ]


def test_a_form_the_server_cannot_read_is_a_validation_error_not_a_400(api: TestClient) -> None:
    """O Starlette recusa o multipart quebrado com 400; a API só tem os status da tabela."""
    response = api.post(
        f"{PROBE}/form",
        content=f"--x\r\nContent-Disposition: nada {SENTINEL}".encode(),
        headers={"Content-Type": "multipart/form-data"},  # sem o boundary
    )

    body = assert_error_format(response, "validation_failed")
    assert body["errors"] == [
        {"field": "", "message": "Não deu para ler o corpo da requisição.", "line": None}
    ]
    assert SENTINEL not in response.text


def test_the_problems_of_a_definition_become_errors_with_their_path_and_line(
    api: TestClient,
) -> None:
    response = api.get(f"{PROBE}/problems")

    body = assert_error_format(response, "validation_failed")
    assert body["detail"] == "A definição não compila."
    assert body["errors"] == [
        {"field": "steps.1.questions.0.options", "message": "Faltam opções.", "line": None},
        {"field": "pdf.template", "message": "Chave desconhecida.", "line": 4},
    ]


def test_the_error_items_are_a_named_model_for_the_openapi() -> None:
    item = ErrorItem(field="a.0", message="x")

    assert item.model_dump() == {"field": "a.0", "message": "x", "line": None}
    assert set(errors.ErrorResponse.model_fields) == {
        "code",
        "detail",
        "errors",
        "current_version",
        "reasons",
    }


def test_the_reasons_of_a_refusal_come_with_the_errors_and_only_when_there_are_some() -> None:
    refused = ApiError(
        "validation_failed",
        "A publicação não aconteceu.",
        errors=[ErrorItem(field="pdf", message="o PDF não saiu", line=3)],
        reasons=["Escreva uma nota do que mudou.", "Teste o rascunho."],
    )

    assert refused.body() == {
        "code": "validation_failed",
        "detail": "A publicação não aconteceu.",
        "errors": [{"field": "pdf", "message": "o PDF não saiu", "line": 3}],
        "reasons": ["Escreva uma nota do que mudou.", "Teste o rascunho."],
    }
    assert "reasons" not in ApiError("validation_failed", "x").body()
    assert "reasons" not in ApiError("validation_failed", "x", reasons=[]).body()


# --- o que o roteador e o servidor respondem ----------------------------------------------------


def test_a_path_without_an_operation_is_a_404_in_the_same_format(api: TestClient) -> None:
    for path in (f"{API}/nao-existe", f"{API}/", f"{API}/probe/raise"):
        response = api.get(path)

        body = assert_error_format(response, "not_found")
        assert body["detail"] == "Não há operação neste endereço."


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
def test_the_prefix_without_the_trailing_slash_answers_in_the_error_format_not_with_a_redirect(
    api: TestClient, method: str
) -> None:
    """O `Mount` só casa `/admin/api/v1/...`; sem a rota do prefixo o app principal responderia 307."""
    response = api.request(method, API)

    body = assert_error_format(response, "not_found")
    assert body["detail"] == "Não há operação neste endereço."
    assert "location" not in response.headers


def test_a_trailing_slash_is_another_path_and_not_a_redirect(api: TestClient) -> None:
    """Com `redirect_slashes` o Starlette responderia 307 a `/me/`, no formato dele e antes da chave."""
    for path in (f"{PROBE}/ok/", f"{API}/me/", f"{API}/api-keys/"):
        response = api.get(path)

        assert_error_format(response, "not_found")
        assert "location" not in response.headers
    assert api.get(f"{PROBE}/ok").status_code == 200


def test_a_method_the_path_does_not_accept_is_a_405_with_the_allowed_methods(
    api: TestClient,
) -> None:
    response = api.delete(f"{PROBE}/ok")

    body = assert_error_format(response, "method_not_allowed")
    assert body["detail"] == "Este método não vale neste endereço."
    assert response.headers["Allow"] == "GET"


def test_an_unexpected_failure_is_a_500_without_detail(api: TestClient) -> None:
    response = api.get(f"{PROBE}/boom")

    body = assert_error_format(response, "internal_error")
    assert body == {"code": "internal_error", "detail": "Erro interno. Tente de novo mais tarde."}
    assert SENTINEL not in response.text and "Boom" not in response.text


def test_an_unexpected_http_exception_is_also_a_500_without_its_text(api: TestClient) -> None:
    response = api.get(f"{PROBE}/teapot")

    assert_error_format(response, "internal_error")
    assert SENTINEL not in response.text and "bule" not in response.text


def test_the_database_down_is_a_503_that_asks_to_retry(api: TestClient) -> None:
    response = api.get(f"{PROBE}/database")

    body = assert_error_format(response, "unavailable")
    assert response.headers["Retry-After"] == str(errors.UNAVAILABLE_RETRY_AFTER)
    assert SENTINEL not in response.text
    assert body["detail"].startswith("O banco de dados não respondeu")


# --- os cabeçalhos de privacidade e o CORS ------------------------------------------------------


def test_every_response_is_private_the_successes_and_each_kind_of_refusal(api: TestClient) -> None:
    responses = [
        api.get(f"{PROBE}/ok"),
        api.get(f"{PROBE}/raise/state_conflict"),
        api.get(f"{PROBE}/raise/rate_limited"),
        api.get(f"{PROBE}/nao-existe"),
        api.delete(f"{PROBE}/ok"),
        api.post(f"{PROBE}/body", json={}),
        api.get(f"{PROBE}/boom"),
        api.post(f"{PROBE}/body", content=b"x" * (MAX_BODY_BYTES + 1)),
    ]

    assert [r.status_code for r in responses] == [200, 409, 429, 404, 405, 422, 500, 413]
    for response in responses:
        assert_private(response)


def test_there_is_no_cors(api: TestClient) -> None:
    """A API não é para páginas de outra origem: nem o preflight nem a resposta liberam uma origem."""
    other = {"Origin": "https://outra-origem.example.test"}
    preflight = api.options(
        f"{PROBE}/ok", headers={**other, "Access-Control-Request-Method": "GET"}
    )
    simple = api.get(f"{PROBE}/ok", headers=other)

    assert preflight.status_code == 405
    for response in (preflight, simple):
        assert not [name for name in response.headers if name.startswith("access-control-")]


# --- o limite de corpo --------------------------------------------------------------------------


def test_a_body_over_the_limit_is_a_413_in_the_error_format(api: TestClient) -> None:
    declared = api.post(
        f"{PROBE}/body",
        content=b"{" + b" " * (MAX_BODY_BYTES + 10) + b"}",
        headers={"Content-Type": "application/json"},
    )

    body = assert_error_format(declared, "payload_too_large")
    assert body["detail"] == "O corpo da requisição passa do limite."


def test_a_body_over_the_limit_without_content_length_is_also_a_413(api: TestClient) -> None:
    def chunks() -> Iterator[bytes]:  # sem Content-Length (transfer-encoding chunked)
        yield b"{"
        for _ in range(40):
            yield b" " * 8192
        yield b"}"

    response = api.post(
        f"{PROBE}/body", content=chunks(), headers={"Content-Type": "application/json"}
    )

    assert_error_format(response, "payload_too_large")


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/assets"), ("PUT", "/assets/0b0a8f3e-6f0b-4c2e-9d57-3f1d1c1f2a10/content")],
)
def test_a_body_over_the_image_limit_on_the_api_is_a_413_in_post_and_put(
    api: TestClient, method: str, path: str
) -> None:
    """O envio (`POST /assets`) e a troca (`PUT /assets/<id>/content`) têm o limite de imagem; passar
    dele é `413` no formato da API. O limite de cada caminho está em `test_body_size_limit.py`, e o
    envio de verdade, em `test_admin_api_assets.py`."""
    response = api.request(method, f"{API}{path}", content=b"0" * (MAX_UPLOAD_BODY_BYTES + 1))

    assert_error_format(response, "payload_too_large")


def test_another_path_of_the_api_does_not_get_the_image_limit(api: TestClient) -> None:
    for method in ("POST", "PUT"):
        response = api.request(method, f"{API}/api-keys", content=b"0" * (MAX_BODY_BYTES + 1))

        assert_error_format(response, "payload_too_large")


# --- o resto do app não mudou -------------------------------------------------------------------


def test_the_public_api_and_the_admin_pages_keep_their_own_error_formats(api: TestClient) -> None:
    public = api.post("/v1/forms/nao-existe/submissions", json={})
    big = api.post(
        "/v1/forms/demo/submissions",
        content=b"{" + b" " * (MAX_BODY_BYTES + 10) + b"}",
        headers={"Content-Type": "application/json"},
    )
    page = api.get("/admin/forms")

    assert public.status_code == 404 and public.json() == {"detail": "formulário não encontrado"}
    assert big.status_code == 413 and big.json() == {"detail": "corpo muito grande"}
    assert page.status_code == 303 and page.headers["location"].startswith("/admin/login")


def test_the_api_does_not_show_up_in_the_openapi_of_the_main_app(api: TestClient) -> None:
    """O `openapi.json` do app principal é o das páginas e da API pública; o da API de admin é só
    das operações dela."""
    paths = api.get("/openapi.json").json()["paths"]

    assert not [path for path in paths if path.startswith(f"{API}")]
    assert "/v1/forms/{slug}/submissions" in paths


def test_the_sub_application_serves_none_of_the_automatic_docs_of_fastapi(api: TestClient) -> None:
    """O `/docs` e o `/redoc` do FastAPI são públicos e ficam desligados. O `/openapi.json` é da
    referência (`app.admin.api.reference`, com a regra de acesso dela): ver
    `test_admin_api_reference.py`."""
    for path in ("/docs", "/redoc", "/docs/oauth2-redirect"):
        assert_error_format(api.get(f"{API}{path}"), "not_found")


# --- o log de acesso ----------------------------------------------------------------------------


def test_the_query_of_the_api_never_reaches_the_access_log() -> None:
    """A busca (`?q=`) pode ter nome ou e-mail de lead: o filtro que já esconde a query de `/admin`
    cobre `/admin/api`."""
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        (
            "127.0.0.1:50000",
            "GET",
            f"{API}/jobs?q=contadora%40example.test&limit=5",
            "1.1",
            200,
        ),
        None,
    )

    assert _HIDE_ADMIN_QUERY.filter(record)

    shown = record.getMessage()
    assert f"{API}/jobs?[removido]" in shown
    assert "contadora" not in shown and "limit=5" not in shown
