"""O formato de erro único da API de admin (ADR 0015, "Erros").

Toda recusa da sub-aplicação sai como `{"code", "detail"}`, com `errors` (e a linha de cada um) só no
`422`:

    {"code": "validation_failed", "detail": "...", "errors": [{"field", "message", "line"}]}

A recusa da publicação (`publish_form_draft`) ainda leva `reasons`, a lista do que falta, em português,
tudo de uma vez: são as duas listas de `PublishRefused` (`reasons` e `problems`), e os problemas, com o
caminho do campo e a linha, vão em `errors` (a nota que falta, também, no campo `note`).

- `code` é um dos da tabela da ADR (`STATUS_OF`), e o status HTTP sai dele: quem levanta um
  `ApiError` diz o código, nunca o status.
- `field` é o caminho do campo no documento, com os índices a partir de 0 (`steps.1.questions.0`),
  e `line`, a linha do template ou do texto (`Problem.line`).
- **A resposta nunca traz o valor recebido.** As mensagens de validação são textos fixos em português
  por tipo de erro do Pydantic, montados só com o que a regra exige (o mínimo, o máximo, os valores
  aceitos), nunca com o que a pessoa mandou: as respostas da lead e os segredos não voltam em
  mensagem de erro. O que não se reconhece vira "Valor inválido.".
- `internal_error` não traz detalhe: o log do servidor tem a exceção.
- Toda resposta, de erro ou não, leva `Cache-Control: no-store` e `X-Content-Type-Options: nosniff`
  (`PRIVATE_HEADERS`). O `500` é montado pelo `ServerErrorMiddleware`, que fica fora dos
  middlewares da sub-aplicação, então o cabeçalho vai no próprio `error_response`.

Os erros do app principal (as páginas do admin e a API pública) não mudam: estes tratadores são da
sub-aplicação.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

if TYPE_CHECKING:
    from app.forms.definition.document import Problem

logger = logging.getLogger(__name__)

PREFIX = "/admin/api/v1"
PRIVATE_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
UNAVAILABLE_RETRY_AFTER = 5  # segundos, no 503 do banco fora do ar
NO_OPERATION = "Não há operação neste endereço."

# A tabela de erros da ADR 0015 ("Erros"): o código e o status dele. O `method_not_allowed` (405) é
# do roteador, não de uma operação.
STATUS_OF: dict[str, int] = {
    "command_not_found": 404,
    "command_forbidden": 403,
    "command_conflict": 409,
    "command_expired": 410,
    "command_in_future": 422,
    "invalid_credentials": 401,
    "key_expired": 401,
    "key_revoked": 401,
    "forbidden_role": 403,
    "not_found": 404,
    "method_not_allowed": 405,
    "version_conflict": 409,
    "state_conflict": 409,
    "gone": 410,
    "payload_too_large": 413,
    "validation_failed": 422,
    "confirmation_required": 422,
    "rate_limited": 429,
    "test_paused": 429,
    "upstream_failed": 502,
    "unavailable": 503,
    "internal_error": 500,
}

# O que cada status quer dizer, para a descrição da resposta no OpenAPI (é o texto que o MCP e a
# referência mostram a quem nunca viu o sistema).
MEANING: dict[int, str] = {
    401: (
        "Sem chave, chave desconhecida ou de segredo errado (`invalid_credentials`: a mesma resposta "
        "nos três casos), ou chave vencida (`key_expired`) ou revogada (`key_revoked`). Leva "
        "`WWW-Authenticate: Bearer`."
    ),
    403: "A chave tem papel `leitura` e a operação pede `total` (`forbidden_role`).",
    404: "O recurso do caminho não existe (`not_found`).",
    405: "O método não vale neste caminho (`method_not_allowed`); `Allow` lista os que valem.",
    410: "O PDF já foi apagado pela retenção (`gone`).",
    413: "O corpo passa do limite (`payload_too_large`).",
    422: (
        "O corpo ou a definição não valem (`validation_failed`; `errors` traz o caminho de cada "
        "campo) ou falta `confirm: true` numa operação marcada (`confirmation_required`; nada "
        "acontece)."
    ),
    429: (
        "Limite de uso da chave ou bloqueio por chave errada (`rate_limited`), ou pausa entre os "
        "testes pagos do rascunho (`test_paused`). Leva `Retry-After`."
    ),
    500: "Erro nosso, sem detalhe na resposta (`internal_error`); o log do servidor tem a causa.",
    502: (
        "O storage ou o Baserow não respondeu ou recusou, e nada foi gravado (`upstream_failed`)."
    ),
    503: "Banco fora do ar (`unavailable`). Leva `Retry-After`.",
}

# O `409` não tem texto geral: cada operação diz o que o conflito dela é (a versão esperada que
# mudou, `version_conflict`, ou o estado que não aceita a ação, `state_conflict`). Uma operação sem
# versão, como a revogação de uma chave, não deve falar de versão.
OWN_TEXT = frozenset({409})

_RETRY_AFTER = {
    "Retry-After": {
        "description": "Quantos segundos esperar antes de tentar de novo.",
        "schema": {"type": "integer", "minimum": 1},
    }
}

# Os cabeçalhos que a resposta de cada status leva (`WWW-Authenticate` no 401, `Allow` no 405,
# `Retry-After` nos que pedem espera): o OpenAPI os declara na resposta, para quem lê o contrato
# saber o que esperar.
HEADERS: dict[int, dict[str, dict[str, Any]]] = {
    401: {
        "WWW-Authenticate": {
            "description": "Sempre `Bearer`: o único esquema que a API aceita.",
            "schema": {"type": "string", "examples": ["Bearer"]},
        }
    },
    405: {
        "Allow": {
            "description": "Os métodos que este caminho aceita.",
            "schema": {"type": "string", "examples": ["GET"]},
        }
    },
    429: _RETRY_AFTER,
    503: _RETRY_AFTER,
}


class ErrorItem(BaseModel):
    """Um problema do `422`: o campo, o motivo e, se couber, a linha."""

    field: str = Field(
        description="Caminho do campo no documento, com os índices a partir de 0. Vazio quando o "
        "problema é do corpo inteiro.",
        examples=["steps.1.questions.0.options"],
    )
    message: str = Field(description="O motivo, em português. Nunca traz o valor recebido.")
    line: int | None = Field(
        default=None, description="A linha do template ou do texto, quando houver."
    )


class ErrorResponse(BaseModel):
    """O corpo de toda resposta de erro da API de admin."""

    code: str = Field(
        description="O código do erro (a tabela da ADR 0015). O status HTTP sai dele.",
        examples=["validation_failed"],
    )
    detail: str = Field(description="A explicação, em português.")
    errors: list[ErrorItem] | None = Field(
        default=None, description="Só no `422 validation_failed`: um item por campo com problema."
    )
    current_version: str | None = Field(
        default=None, description="Só no `409 version_conflict`: a versão de agora."
    )
    reasons: list[str] | None = Field(
        default=None,
        description="Só na recusa de `publish_form_draft` (`422 validation_failed`): tudo o que falta "
        "para publicar, em português e de uma vez (a nota, o teste do que vai ao LLM, a confirmação "
        "dos dados de contato, o PDF de exemplo, nada para publicar). Os problemas da definição ou do "
        "PDF, com o caminho do campo e a linha, vêm em `errors`, e a nota que falta, no campo `note`.",
    )


def error_responses(
    *statuses: int, texts: Mapping[int, str] | None = None
) -> dict[int | str, dict[str, Any]]:
    """O `responses` do OpenAPI de uma operação: cada status com o modelo de erro, o que ele quer
    dizer (`texts` troca o texto geral pelo da operação) e os cabeçalhos que a resposta leva. O `409`
    só entra com o texto da operação (`OWN_TEXT`)."""
    texts = texts or {}
    declared: dict[int | str, dict[str, Any]] = {}
    for status in statuses:
        text = (texts.get(status) or "").strip()
        if status in OWN_TEXT and not text:
            raise ValueError(
                f'{status} exige o texto da operação (errors={{{status}: "..."}}): o conflito de '
                "cada operação é um, e não há texto geral"
            )
        if status in texts and not text:
            raise ValueError(f"{status}: o texto da resposta desta operação está vazio")
        declared[status] = {"model": ErrorResponse, "description": text or MEANING[status]}
        if status in HEADERS:
            declared[status]["headers"] = copy.deepcopy(HEADERS[status])
    return declared


class ApiError(Exception):
    """Uma recusa da API: `code` (da tabela `STATUS_OF`), `detail` (em português, sem o valor
    recebido) e, no `422`, os `errors`. `retry_after` (segundos) vira o cabeçalho `Retry-After`;
    `current_version`, o campo do `409 version_conflict`; `reasons`, a lista do que falta na recusa da
    publicação."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        errors: Sequence[ErrorItem] = (),
        headers: Mapping[str, str] | None = None,
        retry_after: int | None = None,
        current_version: str | None = None,
        reasons: Sequence[str] = (),
    ) -> None:
        if code not in STATUS_OF:
            raise ValueError(f"código de erro desconhecido: {code!r}")
        super().__init__(detail)
        self.code = code
        self.status_code = STATUS_OF[code]
        self.detail = detail
        self.errors = tuple(errors)
        self.headers = dict(headers or {})
        if retry_after is not None:
            self.headers["Retry-After"] = str(max(1, retry_after))
        self.current_version = current_version
        self.reasons = tuple(reasons)

    @classmethod
    def invalid(cls, detail: str, problems: Iterable[Problem]) -> ApiError:
        """O `422 validation_failed` dos problemas de uma definição (`Problem.path` é o `field`)."""
        items = [ErrorItem(field=p.path, message=p.message, line=p.line) for p in problems]
        return cls("validation_failed", detail, errors=items)

    def body(self) -> dict[str, Any]:
        """O JSON da resposta. `errors` só no `422` (cada item com `line`), `current_version` só no
        `409 version_conflict` e `reasons` só na recusa da publicação."""
        body: dict[str, Any] = {"code": self.code, "detail": self.detail}
        if self.errors:
            body["errors"] = [item.model_dump() for item in self.errors]
        if self.current_version is not None:
            body["current_version"] = self.current_version
        if self.reasons:
            body["reasons"] = list(self.reasons)
        return body


def error_response(error: ApiError) -> JSONResponse:
    headers = {**PRIVATE_HEADERS, **error.headers}
    return JSONResponse(error.body(), status_code=error.status_code, headers=headers)


class BarePrefix:
    """O caminho `/admin/api/v1` sem a barra final. O `Mount` só casa `/admin/api/v1/...`, e sem esta
    rota o app principal responderia com um 307 para `/admin/api/v1/`: aqui ele responde o 404 da
    API, no formato dela, como responde o resto do que não é uma operação."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await error_response(ApiError("not_found", NO_OPERATION))(scope, receive, send)


def too_large_response() -> JSONResponse:
    """O `413` do `BodySizeLimit` para um caminho da API (o limite é do app principal, que conta
    os bytes antes da sub-aplicação)."""
    return error_response(ApiError("payload_too_large", "O corpo da requisição passa do limite."))


# --- a validação do FastAPI e do Pydantic ------------------------------------------------------

_SOURCES = {"body", "query", "path", "header", "cookie"}


def _field_of(location: Sequence[Any]) -> str:
    """`loc` do Pydantic sem a origem (`body`, `query`...): `("body", "steps", 1)` vira `steps.1`."""
    parts = list(location)
    if parts and parts[0] in _SOURCES:
        parts = parts[1:]
    return ".".join(str(part) for part in parts)


def _limit(context: Mapping[str, Any], name: str) -> str:
    return str(context.get(name, ""))


# Um texto fixo por tipo de erro do Pydantic. Só entram nele os limites da regra (`ctx`), nunca o
# `input`. Os tipos que não estão aqui caem em "Valor inválido.": é o que garante que nenhuma
# mensagem do Pydantic, que às vezes cita um pedaço do valor (um UUID com o caractere errado, por
# exemplo), chegue à resposta.
_MESSAGES: dict[str, str] = {
    "missing": "Campo obrigatório.",
    "string_type": "Informe um texto.",
    "string_too_short": "Use no mínimo {min_length} caracteres.",
    "string_too_long": "Use no máximo {max_length} caracteres.",
    "string_pattern_mismatch": "O formato não é o esperado.",
    "int_type": "Informe um número inteiro.",
    "int_parsing": "Informe um número inteiro.",
    "int_from_float": "Informe um número inteiro.",
    "float_type": "Informe um número.",
    "float_parsing": "Informe um número.",
    "bool_type": "Informe true ou false.",
    "bool_parsing": "Informe true ou false.",
    "greater_than": "Use um valor maior que {gt}.",
    "greater_than_equal": "Use um valor maior ou igual a {ge}.",
    "less_than": "Use um valor menor que {lt}.",
    "less_than_equal": "Use um valor menor ou igual a {le}.",
    "literal_error": "Use um destes valores: {expected}.",
    "enum": "Use um destes valores: {expected}.",
    # Uma união discriminada (as operações de um JSON Patch, pelo `op`): o tipo que veio não está
    # entre os aceitos. A mensagem do Pydantic cita o tipo recebido; a nossa, só os aceitos.
    "union_tag_invalid": "Use um destes valores: {expected_tags}.",
    "union_tag_not_found": "Campo obrigatório.",
    "datetime_type": "Informe a data e a hora em ISO 8601, por exemplo 2027-01-31T12:00:00Z.",
    "datetime_parsing": "Informe a data e a hora em ISO 8601, por exemplo 2027-01-31T12:00:00Z.",
    "datetime_from_date_parsing": (
        "Informe a data e a hora em ISO 8601, por exemplo 2027-01-31T12:00:00Z."
    ),
    "timezone_aware": "Informe o fuso horário, por exemplo 2027-01-31T12:00:00Z.",
    "uuid_type": "Informe um identificador válido.",
    "uuid_parsing": "Informe um identificador válido.",
    "list_type": "Informe uma lista.",
    "too_short": "Use no mínimo {min_length} itens.",
    "too_long": "Use no máximo {max_length} itens.",
    "too_short_one": "Informe pelo menos um item.",  # `too_short` com `min_length` 1
    "dict_type": "Informe um objeto.",
    "model_type": "Informe um objeto.",
    "model_attributes_type": "Informe um objeto.",
    "extra_forbidden": "Campo desconhecido.",
    "json_invalid": "O corpo não é um JSON válido.",
}


# O que o FastAPI diz (em inglês, citando o tipo do que recebeu) quando um campo de arquivo recebe um
# texto do formulário; a mensagem da API é a nossa.
NOT_A_FILE = "Expected UploadFile"
FILE_EXPECTED = "Mande o arquivo como uma parte do formulário (multipart), e não como texto."


class _Blank(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


def message_of(error: Mapping[str, Any]) -> str:
    """O texto em português de um erro do Pydantic (um item de `RequestValidationError.errors()`),
    sem o valor recebido."""
    kind = error.get("type", "")
    context: Mapping[str, Any] = error.get("ctx") or {}
    if kind in ("value_error", "assertion_error"):
        # A mensagem é de um validador nosso (`raise ValueError("...")`), escrita em português e
        # sem o valor: a regra de quem escreve o validador.
        reason = str(context.get("error", "")).strip()
        if reason.startswith(NOT_A_FILE):  # o campo do arquivo recebeu um texto
            return FILE_EXPECTED
        return reason or "Valor inválido."
    if kind == "too_short" and context.get("min_length") == 1:
        kind = "too_short_one"  # «1 itens» não é português
    template = _MESSAGES.get(kind)
    if template is None:
        return "Valor inválido."
    values = {key: str(value) for key, value in context.items()}
    if "expected" in values:  # "'leitura' or 'total'", como o Pydantic lista os valores aceitos
        values["expected"] = values["expected"].replace(" or ", " ou ")
    if "expected_tags" in values:  # "'add', 'remove'", os tipos que a união aceita
        tags = values["expected_tags"].split(", ")
        values["expected_tags"] = (
            tags[0] if len(tags) == 1 else f"{', '.join(tags[:-1])} ou {tags[-1]}"
        )
    return template.format_map(_Blank(values))


def _tag_field(error: Mapping[str, Any]) -> str:
    """O campo de um erro de união discriminada: o Pydantic aponta a união (`patch.2`), e o problema é
    o campo que diz o tipo (`patch.2.op`), que o `ctx` nomeia entre aspas (`'op'`)."""
    field = _field_of(error.get("loc", ()))
    name = str((error.get("ctx") or {}).get("discriminator", "")).strip("'\"")
    return f"{field}.{name}" if field and name.isidentifier() else field


def validation_items(errors: Iterable[Mapping[str, Any]]) -> list[ErrorItem]:
    items = []
    for error in errors:
        kind = error.get("type")
        if kind == "json_invalid":  # o `loc` é a posição no texto
            field = ""
        elif kind in ("union_tag_invalid", "union_tag_not_found"):
            field = _tag_field(error)
        else:
            field = _field_of(error.get("loc", ()))
        items.append(ErrorItem(field=field, message=message_of(error)))
    return items


# --- os tratadores da sub-aplicação ------------------------------------------------------------


def _api_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return error_response(exc)


def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return error_response(
        ApiError(
            "validation_failed",
            "A requisição não vale; veja o motivo de cada campo em `errors`.",
            errors=validation_items(exc.errors()),
        )
    )


def _http_error(request: Request, exc: Exception) -> JSONResponse:
    """O que o Starlette e o FastAPI levantam por conta própria: caminho sem operação (404), método
    que o caminho não aceita (405), corpo grande demais durante a leitura (413) e corpo que
    não se lê (400, tratado como o `422`). Nossas operações levantam `ApiError`."""
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code == 404:
        return error_response(ApiError("not_found", NO_OPERATION))
    if exc.status_code == 405:
        allow = (exc.headers or {}).get("Allow")
        return error_response(
            ApiError(
                "method_not_allowed",
                "Este método não vale neste endereço.",
                headers={"Allow": allow} if allow else None,
            )
        )
    if exc.status_code == 413:
        return too_large_response()
    if exc.status_code == 400:
        item = ErrorItem(field="", message="Não deu para ler o corpo da requisição.")
        return error_response(
            ApiError(
                "validation_failed",
                "A requisição não vale; veja o motivo de cada campo em `errors`.",
                errors=[item],
            )
        )
    logger.warning("API de admin: HTTPException %s sem tratamento próprio", exc.status_code)
    return _internal_error(request, exc)


def _database_down(request: Request, exc: Exception) -> JSONResponse:
    logger.warning("API de admin: o banco não respondeu (%s)", type(exc).__name__, exc_info=exc)
    return error_response(
        ApiError(
            "unavailable",
            "O banco de dados não respondeu. Tente de novo em instantes.",
            retry_after=UNAVAILABLE_RETRY_AFTER,
        )
    )


def _internal_error(request: Request, exc: Exception) -> JSONResponse:
    """Sem detalhe na resposta. O Starlette repropaga a exceção depois de enviar a resposta, e o
    servidor a registra com o traceback."""
    return error_response(ApiError("internal_error", "Erro interno. Tente de novo mais tarde."))


async def _generation_unavailable(request: Request, exc: Exception) -> JSONResponse:
    return error_response(
        ApiError("state_conflict", "Operação de geração inaplicável a este formulário.")
    )


def _contract_unavailable(request: Request, exc: Exception) -> JSONResponse:
    return error_response(
        ApiError(
            "unavailable",
            "A atualização ainda não habilitou esta operação.",
            retry_after=UNAVAILABLE_RETRY_AFTER,
        )
    )


def install(app: FastAPI) -> None:
    """Os tratadores da sub-aplicação: o formato único para cada recusa."""
    from app.deployment_contract import DeploymentContractError
    from app.forms import GenerationUnavailable

    app.add_exception_handler(DeploymentContractError, _contract_unavailable)
    app.add_exception_handler(GenerationUnavailable, _generation_unavailable)
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(sa.exc.OperationalError, _database_down)
    app.add_exception_handler(sa.exc.TimeoutError, _database_down)
    app.add_exception_handler(Exception, _internal_error)
