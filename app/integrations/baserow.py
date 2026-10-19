"""Cliente da API do Baserow: ler os campos de uma tabela e achar, criar e atualizar uma linha.

Autentica com o token de banco de dados do Baserow (`Authorization: Token ...`), que precisa de
permissão de leitura, criação e atualização na tabela. A API usa os ids dos campos (`field_<id>`),
não os nomes.

Falhas viram `RetryableError` ou `TerminalError` com um `FailureCode` e uma mensagem só com a etapa,
o status e o código de erro do Baserow, nunca o corpo da resposta nem o que foi enviado: a mensagem
vai para o estado da sincronização, para a tela e para o Hatchet, e o corpo pode trazer dado da lead.
Redirecionamentos não são seguidos. O token nunca entra em mensagem nem em `repr`.

- 408, 425, 429, 5xx e falha de conexão: tenta de novo (`INTEGRATION_UNAVAILABLE`);
- 401, 403, 404 e os outros 4xx: definitivo (`INTEGRATION_REJECTED`), até alguém corrigir o token, a
  tabela ou o mapeamento e reenviar. A exceção é o 404 ao atualizar uma linha, que sumiu entre
  achá-la e atualizá-la: a próxima tentativa a cria de novo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.adapters.webhook import outcome
from app.errors import FailureCode, RetryableError, TerminalError

TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_ERROR_CODE = re.compile(r"^[A-Z][A-Z_]{2,80}$")
_FIELD_KEY = re.compile(r"^field_\d+$")


@dataclass(frozen=True)
class BaserowOption:
    """Uma opção de um campo de seleção: o id que a API recebe e o texto que a pessoa vê."""

    id: int
    value: str


@dataclass(frozen=True)
class BaserowField:
    """Um campo da tabela, como a API o descreve. `options` só vem nos campos de seleção."""

    id: int
    name: str
    type: str
    primary: bool = False
    read_only: bool = False
    options: tuple[BaserowOption, ...] = ()


@dataclass(frozen=True)
class Connection:
    """Onde e como falar com uma tabela. O `repr` não mostra o token (um log ou um traceback não o
    levam)."""

    base_url: str
    table_id: int
    token: str = field(repr=False)


def new_client() -> httpx.Client:
    """O cliente HTTP do Baserow: os timeouts acima e sem seguir redirecionamentos."""
    return httpx.Client(timeout=TIMEOUT, follow_redirects=False)


class BaserowTransport:
    """Fala com a API do Baserow com um cliente HTTP só. É a costura que os testes trocam por um
    cliente sobre `httpx.MockTransport`."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or new_client()

    def fields(self, connection: Connection) -> list[BaserowField]:
        """Os campos da tabela, na ordem da API."""
        body = self._call(
            "GET", connection, f"fields/table/{connection.table_id}/", step="ler os campos"
        )
        if not isinstance(body, list):
            raise _unexpected("ler os campos")
        return [_field_of(item) for item in body]

    def find_rows(self, connection: Connection, key_field_id: int, value: str) -> list[int]:
        """Os ids das linhas cujo campo-chave é exatamente `value` (no máximo dois: mais de uma
        é duplicata, e a sincronização atualiza a primeira)."""
        body = self._call(
            "GET",
            connection,
            f"rows/table/{connection.table_id}/",
            step="procurar a linha",
            params={f"filter__field_{key_field_id}__equal": value, "size": 2},
        )
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            raise _unexpected("procurar a linha")
        return [_id_of(item, "procurar a linha") for item in results]

    def create_row(self, connection: Connection, row: dict[str, Any]) -> int:
        body = self._call(
            "POST", connection, f"rows/table/{connection.table_id}/", step="criar a linha", json=row
        )
        return _id_of(body, "criar a linha")

    def update_row(self, connection: Connection, row_id: int, row: dict[str, Any]) -> int:
        body = self._call(
            "PATCH",
            connection,
            f"rows/table/{connection.table_id}/{row_id}/",
            step="atualizar a linha",
            json=row,
            missing_is_retryable=True,
        )
        return _id_of(body, "atualizar a linha")

    def _call(
        self,
        method: str,
        connection: Connection,
        path: str,
        *,
        step: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        missing_is_retryable: bool = False,
    ) -> Any:
        url = f"{connection.base_url.rstrip('/')}/api/database/{path}"
        headers = {"Authorization": f"Token {connection.token}"}
        try:
            response = self.client.request(method, url, params=params, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise RetryableError(
                FailureCode.INTEGRATION_UNAVAILABLE, f"{step}: {type(exc).__name__}"
            ) from None
        status = response.status_code
        verdict = outcome(status)
        if verdict == "delivered":
            try:
                return response.json()
            except ValueError:
                raise _unexpected(step) from None
        if verdict == "retry":
            raise RetryableError(FailureCode.INTEGRATION_UNAVAILABLE, f"{step}: HTTP {status}")
        if status == 404 and missing_is_retryable:
            raise RetryableError(FailureCode.INTEGRATION_UNAVAILABLE, f"{step}: HTTP 404")
        raise TerminalError(
            FailureCode.INTEGRATION_REJECTED, f"{step}: HTTP {status}{_explain(response)}"
        )


def _explain(response: httpx.Response) -> str:
    """O que dizer de uma recusa sem repetir o corpo: o código de erro do Baserow e quais campos
    ele apontou (os ids, não os valores)."""
    status = response.status_code
    if status in (401, 403):
        return ", o Baserow recusou o token (confira o token e a permissão na tabela)"
    if status == 404:
        return ", tabela não encontrada (confira a URL e o id da tabela)"
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    parts = []
    code = body.get("error")
    if isinstance(code, str) and _ERROR_CODE.match(code):
        parts.append(code)
    detail = body.get("detail")
    if isinstance(detail, dict):
        keys = sorted(key for key in detail if isinstance(key, str) and _FIELD_KEY.match(key))
        if keys:
            parts.append("campos " + ", ".join(keys))
    return f" ({'; '.join(parts)})" if parts else ""


def _unexpected(step: str) -> RetryableError:
    return RetryableError(FailureCode.INTEGRATION_UNAVAILABLE, f"{step}: resposta inesperada")


def _id_of(item: Any, step: str) -> int:
    row_id = item.get("id") if isinstance(item, dict) else None
    if not isinstance(row_id, int) or isinstance(row_id, bool):
        raise _unexpected(step)
    return row_id


def _field_of(item: Any) -> BaserowField:
    if not isinstance(item, dict):
        raise _unexpected("ler os campos")
    field_id, name, kind = item.get("id"), item.get("name"), item.get("type")
    if not (isinstance(field_id, int) and isinstance(name, str) and isinstance(kind, str)):
        raise _unexpected("ler os campos")
    options = tuple(
        BaserowOption(option["id"], option["value"])
        for option in item.get("select_options") or ()
        if isinstance(option, dict)
        and isinstance(option.get("id"), int)
        and isinstance(option.get("value"), str)
    )
    return BaserowField(
        id=field_id,
        name=name,
        type=kind,
        primary=bool(item.get("primary")),
        read_only=bool(item.get("read_only")),
        options=options,
    )
