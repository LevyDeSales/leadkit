"""O cliente HTTP da API de admin e a requisição de cada ferramenta.

`prepare` monta a requisição de uma chamada de ferramenta (o caminho preenchido, a query, o corpo JSON
ou o formulário `multipart`); `ApiClient` a envia com a chave no cabeçalho `Authorization: Bearer`. A
chave só existe aqui, dentro do cliente httpx: nenhuma outra parte do servidor a recebe, e toda
mensagem de erro daqui sai sem ela.

O cliente não segue redirecionamento (um `3xx` levaria o cabeçalho para onde a API não mandou), não
guarda cookie e só fala com a base configurada.

**O argumento de caminho** (`slug`, código do pedido) só pode ter letras, números, `.`, `_`, `~` e `-`
(`SAFE_SEGMENT`, até 200 caracteres), e `.` e `..` sozinhos não valem: o resto é recusado como erro da ferramenta, antes de
qualquer pergunta ou requisição. Escapar o `/` não bastaria: o servidor da API (uvicorn) decodifica o
`%2F` antes de rotear, e `get_job(public_id="<código>/pdf")` chegaria a `download_job_pdf`, com o PDF de
uma lead, mesmo sem a opção do dado da lead; `get_form(slug="x/draft")` chegaria a `get_form_draft`.

**O arquivo enviado** (`upload_asset`, `replace_asset`) só vale se estiver dentro de
`GERADOR_MCP_UPLOAD_DIR` (depois de resolver os atalhos), porque o caminho vem do modelo e a imagem
enviada é servida sem login. A conferência da assinatura (PNG, JPEG, WebP) só barra o que não é imagem; ela
não diz que a imagem é a certa: quem decide é a pessoa, que vê o caminho e o tamanho antes.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote

import httpx

from app.admin_mcp.catalog import ToolSpec
from app.admin_mcp.config import Config
from app.admin_mcp.redaction import Redactor

# O maior arquivo que o servidor lê para enviar. A API recusa mais de 8 MB de imagem; aqui é só um teto
# para não ler um arquivo enorme do disco por engano.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
TIMEOUT = httpx.Timeout(60.0, connect=10.0)
ACCEPT = "application/json, application/pdf, text/html;q=0.9"
_PATH_ARGUMENT = re.compile(r"\{([^}]+)\}")
# O que um argumento de caminho pode ter. Sem `/`, `%`, `?`, `#`, espaço nem controle: nada que o
# servidor da API decodifique ou interprete como outro caminho.
MAX_SEGMENT = 200  # nenhum código, `slug` ou id da API passa disto
SAFE_SEGMENT = re.compile(rf"[A-Za-z0-9._~-]{{1,{MAX_SEGMENT}}}")
# Falhas em que a requisição nem saiu (não houve conexão): repetir é seguro mesmo numa escrita.
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout)


class InputError(Exception):
    """O argumento da chamada não vale (caminho perigoso, arquivo que não é imagem...). A mensagem é
    em português, para quem chamou, e não leva a chave."""


class ApiUnavailable(Exception):
    """Não houve resposta da API (rede, DNS, tempo esgotado). A mensagem é em português e não leva a
    chave nem o texto da exceção da biblioteca."""


@dataclass(frozen=True)
class Upload:
    """Uma imagem lida do disco para o envio: onde está (já sem atalhos), o conteúdo, o tipo e o nome
    que vai à API. O que a pessoa confirma é este arquivo, e são estes bytes que vão."""

    path: Path
    data: bytes
    media_type: str
    filename: str

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class PreparedRequest:
    """Uma requisição pronta, sem a base e sem os cabeçalhos (que são do cliente)."""

    method: str
    path: str
    query: Mapping[str, Any] = field(default_factory=dict)
    json: Any = None
    has_json: bool = False
    form: Mapping[str, str] = field(default_factory=dict)
    files: Mapping[str, tuple[str, bytes, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class ApiResponse:
    """O que a API respondeu: o status, o tipo (sem os parâmetros), os bytes e o `Retry-After`."""

    status: int
    media_type: str
    content: bytes
    retry_after: str | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def check_path_arguments(spec: ToolSpec, arguments: Mapping[str, Any]) -> None:
    """Recusa, antes de qualquer pergunta ou requisição, um argumento de caminho que não vale."""
    for name, value in arguments.items():
        if spec.locations.get(name) == "path":
            _segment(name, value)


def read_uploads(
    spec: ToolSpec, arguments: Mapping[str, Any], upload_dir: Path | None
) -> dict[str, Upload]:
    """Lê as imagens que a chamada manda (os argumentos de `file`), todas de dentro de `upload_dir`."""
    return {
        name: _read_image(name, value, upload_dir)
        for name, value in arguments.items()
        if spec.locations.get(name) == "file" and value is not None
    }


def prepare(
    spec: ToolSpec,
    arguments: Mapping[str, Any],
    flags: Mapping[str, bool] | None = None,
    *,
    uploads: Mapping[str, Upload] | None = None,
) -> PreparedRequest:
    """A requisição de `spec` com os `arguments` da chamada.

    `flags` são as confirmações que o servidor conseguiu da pessoa (`confirm`, `confirm_contact`): só
    elas viajam, o que o modelo mandou por conta própria não (o `ToolRunner` as tira antes), e o
    `confirm` de uma ferramenta de arquivo (`local`) nem sai do MCP. Os argumentos que o servidor fixa
    (`spec.fixed`) vão por último e mandam. `uploads` são as imagens que `read_uploads` já leu.
    """
    located: list[tuple[str, str, Any]] = []
    for name, value in arguments.items():
        if name not in spec.locations:
            raise InputError(f"O argumento `{name}` não existe nesta ferramenta.")
        located.append((spec.locations[name], name, value))
    for name, value in (flags or {}).items():
        located.append((spec.locations[name], name, value))
    for name, fixed in spec.fixed.items():
        located.append((fixed.where, name, fixed.value))

    path_values = {name: value for where, name, value in located if where == "path"}
    query = {
        name: _query_value(value)
        for where, name, value in located
        if where == "query" and value is not None
    }
    body = {name: value for where, name, value in located if where == "body"}
    files = {}
    for where, name, value in located:
        if where == "file" and value is not None:
            if uploads is None or name not in uploads:
                raise InputError(f"O arquivo de `{name}` não foi lido.")
            sent = uploads[name]
            files[name] = (sent.filename, sent.data, sent.media_type)

    request = {"method": spec.method, "path": _fill_path(spec.path, path_values), "query": query}
    if spec.body == "multipart":
        form = {name: _form_value(value) for name, value in body.items() if value is not None}
        return PreparedRequest(**request, form=form, files=files)
    if spec.body == "json" and (body or spec.body_required):
        return PreparedRequest(**request, json=body, has_json=True)
    return PreparedRequest(**request)


def _fill_path(template: str, values: Mapping[str, Any]) -> str:
    def fill(match: re.Match[str]) -> str:
        return quote(_segment(match.group(1), values.get(match.group(1))), safe="")

    return _PATH_ARGUMENT.sub(fill, template)


def _segment(name: str, raw: Any) -> str:
    """O texto de um argumento de caminho, ou `InputError`. Só `SAFE_SEGMENT`, e nem `.` nem `..`."""
    text = "" if raw is None or isinstance(raw, bool | dict | list) else str(raw)
    if SAFE_SEGMENT.fullmatch(text) is None or text in (".", ".."):
        raise InputError(
            f"O argumento `{name}` só pode ter letras, números, `.`, `_`, `~` e `-`, até {MAX_SEGMENT} "
            "caracteres (sem `/`, espaço nem outro símbolo): não enviei nada."
        )
    return text


def _query_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return [_query_value(item) for item in value]
    if isinstance(value, dict):
        raise InputError("Um objeto não vale como parâmetro de query.")
    return value


def _form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# As imagens que a API aceita (PNG, JPEG, WebP). A assinatura só barra o que **não** é imagem (um
# `id_rsa` ou uma planilha, num caminho que o modelo escolheu); ela não diz que a imagem é a certa, nem
# que é segura de publicar. Quem garante isso é a pasta `GERADOR_MCP_UPLOAD_DIR` (só o que está nela vale)
# e a pessoa, que vê o caminho e o tamanho antes. A validação de verdade é da API.
def _image_type(head: bytes) -> tuple[str, str] | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def _upload_root(upload_dir: Path | None) -> Path:
    """A pasta de envio, já resolvida, ou um `InputError` que diz o que fazer."""
    if upload_dir is None:
        raise InputError(
            "Para enviar uma imagem, defina GERADOR_MCP_UPLOAD_DIR com a pasta de onde o servidor pode "
            "ler arquivos (no bloco `env` da configuração do MCP); só o que estiver dentro dela vale, "
            "porque a imagem enviada é servida sem login."
        )
    try:
        root = upload_dir.expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise InputError(
            "A pasta GERADOR_MCP_UPLOAD_DIR não existe: crie-a ou corrija a variável no bloco `env`."
        ) from None
    if not root.is_dir():
        raise InputError(
            "GERADOR_MCP_UPLOAD_DIR não é uma pasta: corrija a variável no bloco `env`."
        )
    return root


def _inside(name: str, root: Path, raw: Any) -> Path:
    """O destino de `raw` com os atalhos e os `..` resolvidos, **se estiver dentro de `root`**.

    A conferência vem **antes** de qualquer pergunta sobre o arquivo (se existe, se é um arquivo): para um
    caminho de fora, a resposta é a mesma exista ele ou não, então o modelo não usa a ferramenta para
    descobrir o que há no disco. A resolução não exige que o destino exista (`strict=False`) para que um
    caminho de fora que não existe e um que existe caiam na mesma recusa.
    """
    outside = InputError(
        f"O arquivo de `{name}` está fora da pasta de envio (GERADOR_MCP_UPLOAD_DIR): não o li. "
        "Só vale o que está dentro dela."
    )
    wanted = Path(str(raw)).expanduser()
    # Um caminho sem a raiz vale a partir da pasta de envio; com ela, vale como está.
    candidate = wanted if wanted.is_absolute() else root / wanted
    try:
        path = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        # Um laço de atalhos ou um byte nulo: sem destino para seguir, vale o caminho já normalizado.
        path = Path(os.path.normpath(candidate))
    if not path.is_relative_to(root):
        raise outside
    return path


def _read_regular(path: Path, ceiling: int) -> bytes:
    """Os bytes de um arquivo comum, abrindo **sem seguir atalho no último nome** (`O_NOFOLLOW`) e lendo
    pelo descritor: o arquivo conferido é o arquivo lido, mesmo que alguém troque o nome por um atalho
    entre a conferência e a leitura. `O_NONBLOCK` impede que um FIFO na pasta prenda o servidor."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError):
        raise InputError(
            "Não consegui ler o arquivo (é um atalho, ou não há permissão para lê-lo)."
        ) from None
    too_big = InputError("O arquivo passa de 10 MB; a API aceita imagens de até 8 MB.")
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise InputError("O caminho não é um arquivo comum.")
        if status.st_size > ceiling:
            raise too_big
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(ceiling + 1)
    except OSError:
        raise InputError("Não consegui ler o arquivo.") from None
    finally:
        os.close(descriptor)
    if len(data) > ceiling:  # o arquivo cresceu depois do fstat
        raise too_big
    return data


def _read_image(name: str, raw: Any, upload_dir: Path | None) -> Upload:
    root = _upload_root(upload_dir)
    path = _inside(name, root, raw)
    # Só aqui, dentro da pasta, a existência pode ser dita.
    if not path.exists():
        raise InputError(
            f"O arquivo de `{name}` não existe dentro da pasta de envio (confira o caminho)."
        )
    data = _read_regular(path, MAX_UPLOAD_BYTES)
    found = _image_type(data[:16])
    if found is None:
        raise InputError("O arquivo não é uma imagem PNG, JPEG ou WebP; não o enviei.")
    media_type, extension = found
    # Só a extensão: o nome do arquivo da pessoa não vai para a API.
    return Upload(path=path, data=data, media_type=media_type, filename=f"upload{extension}")


class ApiClient:
    """Fala com a API de admin em nome da chave. Use dentro de `async with`, ou chame `aclose`."""

    def __init__(
        self,
        config: Config,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        redact: Redactor | None = None,
    ) -> None:
        self._base = config.base_url
        self.redact = redact or Redactor(config.api_key)
        self._http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {config.api_key}", "Accept": ACCEPT},
            follow_redirects=False,
            timeout=TIMEOUT,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, path: str) -> ApiResponse:
        """Um `GET` simples (o `openapi.json` e o `/me` da subida)."""
        return await self.send(PreparedRequest(method="GET", path=path))

    async def send(self, request: PreparedRequest) -> ApiResponse:
        try:
            response = await self._http.request(
                request.method,
                self._base + request.path,
                params=dict(request.query) or None,
                json=request.json if request.has_json else None,
                data=dict(request.form) or None,
                files=dict(request.files) or None,
            )
        except httpx.InvalidURL:
            # O caminho tem teto (200 caracteres), mas o erro também pode vir da query ou do endereço:
            # o texto não culpa só `GERADOR_API_URL`.
            raise ApiUnavailable(
                "A URL montada para a API não vale (confira GERADOR_API_URL e o tamanho dos argumentos): "
                "nada foi enviado."
            ) from None
        except httpx.HTTPError as problem:
            raise ApiUnavailable(_failure(request.method, problem)) from None
        return ApiResponse(
            status=response.status_code,
            media_type=response.headers.get("content-type", "").split(";")[0].strip().lower(),
            content=response.content,
            retry_after=response.headers.get("retry-after"),
        )


def _failure(method: str, problem: httpx.HTTPError) -> str:
    """O texto de uma falha de rede. Uma **escrita** que estourou o tempo (ou caiu depois de sair) pode
    ter sido executada: o texto manda conferir o estado antes de repetir, e não repetir às cegas."""
    kind = type(problem).__name__
    if method == "GET" or isinstance(problem, NOT_SENT):
        if isinstance(problem, httpx.TimeoutException):
            return (
                "A API não respondeu a tempo. Tente de novo; se repetir, confira GERADOR_API_URL "
                "e a rede."
            )
        return f"Não consegui falar com a API ({kind}). Confira GERADOR_API_URL e a rede."
    return (
        f"A API não confirmou a operação ({kind}), e ela PODE ter sido executada. Não repita ainda: "
        "confira o estado antes, com a ferramenta de leitura do recurso (`get_...` ou `list_...`)."
    )
