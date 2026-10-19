"""As peças que toda operação da API de admin reutiliza (ADR 0015, "O contrato", "Listas" e
"Concorrência e repetição").

- **Corpos de entrada.** `ApiInput` recusa campo desconhecido (`422` com o nome do campo, em vez de
  ignorar um erro de digitação). `ConfirmedInput` acrescenta o `confirm` das operações marcadas;
  `VersionedInput`, o `expected_version` das escritas sobre algo versionado.
- **`confirm`.** A exigência em si é da declaração da operação (`app.admin.api.operations`); aqui
  estão o campo do corpo (`ConfirmedInput`) e o da query (`ConfirmQuery`, em `DELETE`).
- **`expected_version`.** `check_version` compara a versão que o cliente viu com a de agora e, se
  mudou, levanta `409 version_conflict` com a versão de agora (`current_version`); nada é gravado.
  Na query vai `ExpectedVersionQuery` (em `DELETE`). A versão é um texto opaco.
- **Listas.** Toda lista sai no envelope `{"items": [...], "next_cursor": "..."}` (`Page`), com
  `limit` (padrão 50, de 1 a 200) e `cursor`, opaco (`PageParams`). `next_cursor` é nulo no fim.
  `paginate` fatia uma lista que já cabe na memória (formulários, imagens, destinos, chaves) pela
  chave de cada item, e não por posição: entrar uma linha nova não pula nem repete nenhuma. A lista
  que cresce com o uso (os pedidos, a auditoria) pagina no banco, pela chave de ordenação (o instante e
  o `id`, em `encode_keyset` e `decode_keyset`), com o mesmo cursor.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Generic, TypeVar, overload

from fastapi import Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.admin.api.errors import ApiError, ErrorItem
from app.admin.auth import instant

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_CURSOR_LENGTH = 300

CONFIRM_TEXT = (
    "Precisa ser `true` nesta operação, que tem efeito difícil de desfazer ou fora do sistema: é o "
    "jeito de dizer que você sabe qual é. Sem ele, a resposta é `422 confirmation_required` e nada "
    "acontece."
)
EXPECTED_VERSION_TEXT = (
    "A versão que você viu (o campo `version` da leitura). Se mudou desde então, a resposta é "
    "`409 version_conflict`, com a versão de agora, e nada é gravado."
)

Item = TypeVar("Item")


class PdfResponse(Response):
    """A resposta de uma operação que entrega um PDF (`download_job_pdf`): os bytes, e não JSON. O
    OpenAPI a declara como `application/pdf` binário; as recusas seguem no formato de erro da API."""

    media_type = "application/pdf"


class HtmlResponse(Response):
    """A resposta de uma operação que entrega uma página HTML (as prévias): o texto da página, e não
    JSON. O OpenAPI a declara como `text/html`; as recusas seguem no formato de erro da API."""

    media_type = "text/html"


@overload
def utc(moment: datetime) -> datetime: ...


@overload
def utc(moment: None) -> None: ...


def utc(moment: datetime | None) -> datetime | None:
    """O instante em UTC (a API diz toda data em ISO 8601 UTC, com `Z`). Um instante sem fuso é UTC."""
    if moment is None:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def now(request: Request) -> datetime:
    """O instante pelo relógio do app (o dos testes é trocável), com fuso UTC."""
    return instant(request.app.state.clock)


class ApiInput(BaseModel):
    """O corpo de entrada de uma operação: campo desconhecido é `422`."""

    model_config = ConfigDict(extra="forbid")


class ConfirmedInput(ApiInput):
    """O corpo de uma operação marcada `x-destructive` ou `x-external-effect`."""

    confirm: bool = Field(default=False, description=CONFIRM_TEXT, examples=[True])


class VersionedInput(ApiInput):
    """O corpo de uma escrita sobre algo versionado."""

    expected_version: str = Field(min_length=1, description=EXPECTED_VERSION_TEXT)


ConfirmQuery = Annotated[bool, Query(description=CONFIRM_TEXT)]
ExpectedVersionQuery = Annotated[str, Query(min_length=1, description=EXPECTED_VERSION_TEXT)]


def check_version(current: str, expected: str) -> None:
    """`409 version_conflict`, com a versão de agora, se a que o cliente viu não é a atual."""
    if expected != current:
        raise ApiError(
            "version_conflict",
            "O recurso mudou desde que você o leu. Leia de novo e refaça a mudança.",
            current_version=current,
        )


# --- listas -------------------------------------------------------------------------------------


class Page(BaseModel, Generic[Item]):
    """O envelope de toda lista. Cada recurso o herda com o tipo do item, para o OpenAPI ter um
    modelo com nome."""

    items: list[Item] = Field(description="Os itens desta página.")
    next_cursor: str | None = Field(
        default=None,
        description="Passe em `cursor` para ler a página seguinte; nulo na última.",
    )


@dataclass(frozen=True)
class PageParams:
    limit: int
    cursor: str | None


def page_params(
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_LIMIT,
            description=f"Quantos itens por página (de 1 a {MAX_LIMIT}; padrão {DEFAULT_LIMIT}).",
        ),
    ] = DEFAULT_LIMIT,
    cursor: Annotated[
        str | None,
        Query(
            max_length=MAX_CURSOR_LENGTH,
            description="O `next_cursor` da página anterior. É opaco: não monte nem altere.",
        ),
    ] = None,
) -> PageParams:
    return PageParams(limit=limit, cursor=cursor)


def encode_cursor(position: str) -> str:
    """O cursor de uma lista: a posição (a chave do último item entregue), em base64 da URL."""
    raw = json.dumps({"after": position}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def invalid_cursor() -> ApiError:
    return ApiError(
        "validation_failed",
        "O cursor não vale. Comece de novo, sem `cursor`.",
        errors=[ErrorItem(field="cursor", message="O cursor não vale.")],
    )


def decode_cursor(cursor: str) -> str:
    """A posição que o cursor guarda, ou `422` (campo `cursor`) se ele não é um que demos."""
    try:
        data: Any = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (binascii.Error, ValueError):  # JSON e UTF-8 inválidos são ValueError
        raise invalid_cursor() from None
    after = data.get("after") if isinstance(data, dict) else None
    if not isinstance(after, str):
        raise invalid_cursor()
    return after


def encode_keyset(moment: datetime, row_id: uuid.UUID) -> str:
    """O cursor de uma lista que pagina no banco pela chave (`instante`, `id`): a posição do último
    item entregue, no mesmo envelope opaco de `encode_cursor`. O `id` desempata os itens do mesmo
    instante, então a ordem é total e nenhuma linha é pulada nem repetida. O instante tem fuso (o das
    colunas `timestamptz`), senão `decode_keyset` recusaria o cursor que ela mesma deu."""
    if moment.tzinfo is None:
        raise ValueError("o instante do cursor precisa ter fuso")
    return encode_cursor(f"{moment.isoformat()}|{row_id}")


def decode_keyset(cursor: str) -> tuple[datetime, uuid.UUID]:
    """O instante e o `id` que `encode_keyset` guardou. Uma posição malformada é `422` (campo `cursor`): o
    envelope não é o nosso, a posição não é `instante|id`, o `id` não é um UUID ou o instante não é um ISO
    8601 **com fuso** (sem fuso, o banco o leria no fuso da sessão, e a posição seria outra). **Só isso é
    garantido:** o cursor não é assinado, e uma posição bem formada que a API nunca entregou (de outra
    lista, escrita à mão) vale como qualquer outra, e só começa a leitura dali."""
    moment, _, row_id = decode_cursor(cursor).partition("|")
    try:
        found = datetime.fromisoformat(moment)
        parsed = uuid.UUID(row_id)
    except ValueError:
        raise invalid_cursor() from None
    if found.tzinfo is None:
        raise invalid_cursor()
    return found, parsed


def paginate(
    items: Sequence[Item], page: PageParams, key: Callable[[Item], str]
) -> tuple[list[Item], str | None]:
    """A página de uma lista inteira em memória: os `limit` itens depois do que o cursor aponta,
    e o cursor da página seguinte (nulo se esta é a última). `key` é única por item; um cursor cuja
    chave não está mais na lista é `422`."""
    start = 0
    if page.cursor is not None:
        after = decode_cursor(page.cursor)
        positions = [key(item) for item in items]
        if after not in positions:
            raise invalid_cursor()
        start = positions.index(after) + 1
    chunk = list(items[start : start + page.limit])
    more = start + page.limit < len(items)
    return chunk, encode_cursor(key(chunk[-1])) if more else None
