"""Como uma operação da API de admin se declara (ADR 0015, "O contrato").

    router = OperationRouter(tag="Chaves de API", description="...")

    @router.operation(
        "POST", "/api-keys/{key_id}/revoke",
        operation_id="revoke_api_key", role="total", destructive=True,
        summary="...", description="...", returns="...", response_model=ApiKey,
        errors={409: "A chave já estava revogada (`state_conflict`)."},
    )
    def revoke_api_key(...): ...

A declaração é a fonte de tudo o que o contrato exige, e cada par sai junto, de modo que um não
existe sem o outro:

- **O papel.** `role` põe `requires(role)` na rota (ver `app.admin.api.security`) e o
  `x-required-role` no OpenAPI. **Negado por padrão**, em duas camadas: `OperationRoute` recusa, ao
  registrar, uma rota sua sem `requires`; e `refuse_unguarded_routes`, que fecha a montagem da
  sub-aplicação, recusa toda rota que não seja `OperationRoute` nem `OpenRoute`. Esquecer o papel, ou
  registrar a rota por um roteador comum, derruba a subida e não deixa uma operação aberta.
- **A rota aberta é uma exceção explícita.** `OpenRouter` (rotas `OpenRoute`) é a única porta sem chave,
  para o que a ADR manda servir com a sessão do admin (a referência e o `openapi.json`, de
  `app.admin.api.reference`). Quem a usa responde pela autenticação da rota.
- **As marcas e o `confirm`.** `destructive=True` (apaga ou sobrescreve algo difícil de desfazer) e
  `external_effect=True` (muda o que a lead vê, para onde os dados dela vão, chama um serviço de
  fora ou cria uma chave de API) põem, juntos, `x-destructive` e/ou `x-external-effect` no OpenAPI e a
  exigência de `"confirm": true` em toda chamada (na query, em `DELETE`). Sem ele, `422
  confirmation_required` e nada acontece. A marca é derivada da própria dependência de confirmação
  (`Confirmation`), e não escrita à mão. O corpo (`ConfirmedInput`) ou a query (`ConfirmQuery`) tem de
  declarar o campo `confirm`, para o OpenAPI e o MCP o mostrarem: `OperationRoute` recusa, ao
  registrar, uma operação marcada cujo corpo (ou, em `DELETE`, cuja query) não o declara. A recusa diz
  o efeito em geral; `confirmation_text` a troca por uma frase **da operação** (desativar um
  formulário o tira do ar; mudar a retenção pode apagar dado da lead), que é o que quem confirma lê.
- **A simulação dispensa o `confirm`.** Uma operação marcada que também sabe **só simular** (a
  publicação, com `dry_run`) declara `confirm_unless="dry_run"`: com esse campo booleano `true` no
  corpo, a chamada não tem efeito e passa sem `confirm`. É a **mesma declaração** que gera a marca e a
  exigência: a dispensa mora na dependência de confirmação (`Confirmation.unless`), não pode existir
  numa operação sem marca, e vai ao OpenAPI como `x-confirm-unless` (o nome do campo), para o MCP não
  pedir confirmação de uma simulação. `OperationRoute` recusa, ao registrar, uma dispensa cujo campo
  não está no corpo JSON, não é um booleano ou começa verdadeiro (uma dispensa que vale sem ninguém
  pedir). Só o booleano `true` dispensa, como só ele confirma; a dispensa vale **depois** do papel e
  **antes** do corpo, na mesma ordem do `confirm`, e é só da operação que a declarou.
- **`x-lead-data`** (`lead_data=True`): a operação pode trazer dado pessoal da lead, só para `total`.
- **`heavy=True`**: a chamada custa 5 fichas do balde de uso, e não 1.
- **O papel dentro da operação.** O papel de `role` vale para a operação inteira. Uma operação de
  `leitura` que tem uma parte só de `total` (o `lead_data` de `get_job`) chama `ensure_role(chave,
  "total")` (de `app.admin.api.security`) nessa parte: é o mesmo `403 forbidden_role`. Ela declara esse
  `403` em `errors`, porque o papel `leitura` não o traz sozinho.
- **A resposta que não é JSON.** Uma operação que entrega um arquivo (`download_job_pdf`) passa
  `response_class=PdfResponse` e `response_model=None`: o sucesso é o arquivo, declarado no OpenAPI
  como `application/pdf` binário, e as recusas seguem em JSON, no formato de erro da API. Uma que
  entrega uma página (as prévias do rascunho) passa `response_class=HtmlResponse`: o sucesso é o
  HTML, declarado como `text/html` em texto (`{"type": "string"}`), e as recusas também seguem em
  JSON.
- **A tag** vem com o roteador: `OperationRouter(tag=..., description=...)` guarda o texto dela, e
  `create_admin_api` monta a lista de tags do OpenAPI a partir dos roteadores que inclui (`tags_of`).
  Uma tag sem descrição não existe, e uma tag em dois roteadores derruba a montagem.
- **Os erros declarados** saem do que a rota é: `401` e `429` em toda operação (com `WWW-Authenticate`
  e `Retry-After` na resposta), `403` nas `total`, `404` nas que têm um recurso no caminho, `422` nas
  que têm corpo ou parâmetro (sem nada a validar, como `GET /me`, não há 422), e os de `errors`.
  `errors` é uma lista de status ou um mapa de status para o texto da operação; o `409` só entra com
  texto, porque o conflito de cada operação é um (a versão esperada que mudou, `version_conflict`, ou
  o estado que não aceita a ação, `state_conflict`), e uma operação sem versão não fala de versão.

- **Caractere de controle num parâmetro de query.** O NUL num `q` chegava ao Postgres, que o recusa num
  texto (um `500`). `plain_query` é a dependência de toda operação que recusa, com `422
  validation_failed` no campo do parâmetro e sem repetir o valor, o parâmetro de query declarado que
  traz um caractere de controle. No caminho, a recusa é um `404` do app (`ControlCharacters`, em
  `app.api.main`), antes da rota.

A ordem em que as dependências correm é a das recusas: autenticação (`401`, `429`), papel (`403`),
confirmação (`422 confirmation_required`), os caracteres de controle da query (`422 validation_failed`)
e só então o corpo e os parâmetros (`422 validation_failed`). Um corpo que não é JSON é a única recusa que o FastAPI faz antes de tudo, **com
uma exceção que é regra**: a rota de um corpo de formulário (`multipart`, o envio de uma imagem)
autentica e confere o papel antes de ler o corpo (`OperationRoute.get_route_handler`), porque o arquivo
é o único corpo grande da API. Nela, o `confirm` é o campo do formulário.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Coroutine, Iterable, Mapping
from typing import Any

from fastapi import Depends, FastAPI, Request, Response, params
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIRouter, iter_route_contexts
from pydantic import BaseModel
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.admin.api.errors import ApiError, ErrorItem, error_responses
from app.admin.api.limits import HEAVY_COST
from app.admin.api.security import RoleGate, requires

# Os métodos sem corpo: a confirmação vai na query.
QUERY_METHODS = ("GET", "HEAD", "DELETE")
# Os corpos de formulário (o envio de uma imagem é `multipart`): lá tudo é texto, e a confirmação é o
# campo `confirm` do formulário.
FORM_TYPES = ("multipart/form-data", "application/x-www-form-urlencoded")
NOT_A_FORM = (
    "O corpo não é um formulário `multipart/form-data` que se possa ler (falta o `boundary` do "
    "cabeçalho `Content-Type`, ou o corpo está cortado): mande o arquivo numa parte do formulário."
)


async def confirmed(request: Request) -> bool:
    """Se a chamada trouxe `"confirm": true` no corpo JSON (na query `confirm=true`, em `DELETE`; num
    corpo de formulário, o campo `confirm` com `true` ou `1`). Num corpo JSON só o booleano `true`
    vale: `"true"`, `1` e `"sim"` não."""
    if request.method in QUERY_METHODS:
        return request.query_params.get("confirm", "").lower() in ("true", "1")
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() in FORM_TYPES:
        # O FastAPI já leu o formulário (a rota de um corpo de formulário o lê antes das
        # dependências), e o Starlette guarda o que leu: ler de novo devolve o mesmo formulário. Ler
        # o corpo como JSON, aqui, falharia por já ter sido consumido.
        try:
            form = await request.form()
        except StarletteHTTPException:  # o corpo não é o formulário que o cabeçalho diz
            return False
        value = form.get("confirm")
        return isinstance(value, str) and value.strip().lower() in ("true", "1")
    try:
        body = await request.json()
    except ValueError:  # JSON ou UTF-8 inválido; sem corpo, `json.loads(b"")` também cai aqui
        return False
    return isinstance(body, dict) and body.get("confirm") is True


async def exempted(request: Request, field: str) -> bool:
    """Se o corpo JSON da chamada traz `field: true` (a simulação que dispensa o `confirm`). Só o
    booleano `true` vale, como no `confirm`: `"true"`, `1` e `"sim"` não."""
    try:
        body = await request.json()
    except ValueError:  # JSON ou UTF-8 inválido; sem corpo, `json.loads(b"")` também cai aqui
        return False
    return isinstance(body, dict) and body.get(field) is True


class Confirmation:
    """A dependência das operações marcadas: exige o `confirm`. Guarda as marcas, e é dela que a
    `OperationRoute` tira o `x-destructive` e o `x-external-effect`.

    A mensagem da recusa diz o efeito em geral. `what` a troca por uma frase da operação, que diz o
    que **esta** operação faz (desativar um formulário o tira do ar para as leads; mudar a retenção
    pode apagar dado), e é o que o MCP mostra à pessoa que vai confirmar. `unless` é o nome do campo
    booleano do corpo que, `true`, torna a chamada uma simulação, sem efeito, e a dispensa do
    `confirm` (a publicação com `dry_run`)."""

    def __init__(
        self,
        *,
        destructive: bool,
        external_effect: bool,
        what: str | None = None,
        unless: str | None = None,
    ) -> None:
        if not (destructive or external_effect):
            raise ValueError("uma confirmação sem marca não tem o que confirmar")
        if what is not None and not what.strip():
            raise ValueError("o texto da confirmação desta operação está vazio")
        if unless is not None and not unless.strip():
            raise ValueError("o campo que dispensa a confirmação desta operação está vazio")
        self.destructive = destructive
        self.external_effect = external_effect
        self.what = what.strip() if what is not None else None
        self.unless = unless.strip() if unless is not None else None

    async def __call__(self, request: Request) -> None:
        if await confirmed(request):
            return
        if self.unless is not None and await exempted(request, self.unless):
            return
        where = "na query (`confirm=true`)" if request.method in QUERY_METHODS else "no corpo"
        simulate = (
            f", ou `{self.unless}: true` para só simular, que não pede confirmação"
            if self.unless is not None
            else ""
        )
        if self.what is not None:
            detail = (
                f"{self.what} Mande `confirm: true` {where} para confirmar{simulate}. "
                "Nada foi feito."
            )
        else:
            detail = (
                f"Esta operação tem {self._effect()}. Mande `confirm: true` {where} para dizer que "
                f"você sabe qual é{simulate}. Nada foi feito."
            )
        raise ApiError("confirmation_required", detail)

    def _effect(self) -> str:
        if self.destructive and self.external_effect:
            return "efeito difícil de desfazer e que alcança fora do sistema"
        if self.destructive:
            return "efeito difícil de desfazer"
        return "efeito fora do sistema ou na lead"


# Os caracteres de controle: o NUL, a quebra de linha, o tabulador e o DEL entre eles.
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
CONTROL_IN_QUERY = "Não use caracteres de controle."


async def plain_query(request: Request) -> None:
    """`422 validation_failed` se um parâmetro de query que a operação declara tem caractere de
    controle no valor. A mensagem fixa nomeia o campo e nunca repete o valor. Corre depois da
    autenticação, do papel e da confirmação, e antes da validação dos parâmetros."""
    route = request.scope.get("route")
    if not isinstance(route, OperationRoute):
        return
    refused = [
        name
        for name in sorted(_query_names(route.dependant))
        if any(CONTROL.search(value) for value in request.query_params.getlist(name))
    ]
    if refused:
        raise ApiError(
            "validation_failed",
            "A requisição não vale; veja o motivo de cada campo em `errors`.",
            errors=[ErrorItem(field=name, message=CONTROL_IN_QUERY) for name in refused],
        )


class OperationRoute(APIRoute):
    """A rota de uma operação da API: lê da dependência o papel e as marcas e os põe no OpenAPI,
    declara os erros que ela tem pelo que ela é, e recusa registrar uma rota sem papel."""

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        super().__init__(path, endpoint, **kwargs)
        gate = self._declared(RoleGate)
        if gate is None:
            raise RuntimeError(
                f"{sorted(self.methods)} {path}: operação sem papel declarado. Toda rota da API de "
                "admin pede `requires('leitura')` ou `requires('total')` (negado por padrão); "
                "declare-a com `OperationRouter.operation`."
            )
        missing = sorted(self._errors_of(path, gate) - set(self.responses))
        if missing:
            # O que a rota é só se sabe depois de montada (os parâmetros e o corpo saem da função e
            # das dependências dela), mas as respostas entram na montagem: monta-se de novo, com elas.
            kwargs["responses"] = {**self.responses, **error_responses(*missing)}
            super().__init__(path, endpoint, **kwargs)
        extra: dict[str, Any] = {"x-required-role": gate.role}
        confirmation = self._declared(Confirmation)
        if confirmation is not None:
            if confirmation.destructive:
                extra["x-destructive"] = True
            if confirmation.external_effect:
                extra["x-external-effect"] = True
            if confirmation.unless is not None:
                extra["x-confirm-unless"] = confirmation.unless
        self.openapi_extra = {**(self.openapi_extra or {}), **extra}
        if confirmation is not None and not self._declares_confirm():
            where = "na query" if self._confirms_in_query() else "no corpo"
            raise RuntimeError(
                f"{sorted(self.methods)} {path}: a operação é marcada e exige `confirm: true`, mas "
                f"não declara o campo `confirm` {where}. Use `ConfirmedInput` (corpo) ou "
                "`ConfirmQuery` (query, em `DELETE`)."
            )
        if confirmation is not None and confirmation.unless is not None:
            self._check_unless(path, confirmation.unless)

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """O tratador da rota. Uma rota de **corpo de formulário** (`multipart`: o envio de uma
        imagem) autentica antes de ler o corpo: o FastAPI lê o formulário antes de rodar as
        dependências, e o arquivo, de até 8 MiB, iria para a memória e o disco de quem nem tem chave
        ou papel. A recusa (`401`, `403`, `429`) sai sem ler nada; a dependência `requires` reconhece
        a chamada já autenticada (`RoleGate.authenticate` é idempotente na mesma chamada). Um corpo
        que não se lê como o formulário que o cabeçalho diz (sem `boundary`, cortado) é `422
        validation_failed` no campo do arquivo do modelo, e não um `400`."""
        handler = super().get_route_handler()
        gate = self._declared(RoleGate)
        if gate is None or not self._reads_a_form():
            return handler

        async def authenticated_first(request: Request) -> Response:
            await gate.authenticate(request)
            try:
                return await handler(request)
            except StarletteHTTPException as unreadable:
                if unreadable.status_code != 400:  # o 413 do limite de corpo, por exemplo, segue
                    raise
                # O Starlette recusa o corpo que o cabeçalho diz ser um formulário e não é (sem
                # `boundary`, cortado, lixo): é o campo do arquivo que está errado.
                raise self._unreadable_form() from None

        return authenticated_first

    def _reads_a_form(self) -> bool:
        return self.body_field is not None and isinstance(self.body_field.field_info, params.Form)

    def _unreadable_form(self) -> ApiError:
        """O `422` de um corpo que não se lê como o formulário: no campo do arquivo do modelo."""
        return ApiError(
            "validation_failed",
            "A requisição não vale; veja o motivo de cada campo em `errors`.",
            errors=[ErrorItem(field=self._file_field(), message=NOT_A_FORM)],
        )

    def _file_field(self) -> str:
        """O nome do campo `UploadFile` do modelo do corpo (vazio, se o modelo não tem arquivo)."""
        model = self.body_field.field_info.annotation if self.body_field is not None else None
        if isinstance(model, type) and issubclass(model, BaseModel):
            for name, info in model.model_fields.items():
                kind = info.annotation
                if isinstance(kind, type) and issubclass(kind, StarletteUploadFile):
                    return name
        return ""

    def _errors_of(self, path: str, gate: RoleGate) -> set[int]:
        """Os erros que toda operação desta forma tem: `401` e `429` sempre, `403` nas `total`, `404`
        nas que têm um recurso no caminho e `422` nas que têm algo a validar (corpo ou parâmetro)."""
        found = {401, 429}
        if gate.role == "total":
            found.add(403)
        if "{" in path:
            found.add(404)
        if self.body_field is not None or _parameters(self.dependant):
            found.add(422)
        return found

    def _declared(self, kind: type) -> Any:
        for dependency in self.dependant.dependencies:
            if isinstance(dependency.call, kind):
                return dependency.call
        return None

    def _confirms_in_query(self) -> bool:
        return all(method in QUERY_METHODS for method in self.methods)

    def _check_unless(self, path: str, field: str) -> None:
        """A dispensa do `confirm` lê um campo booleano do corpo JSON: ele tem de estar no contrato
        (o OpenAPI e o MCP o mostram) e começar falso, senão a dispensa não seria vista ou valeria
        sem ninguém pedir."""
        where = f"{sorted(self.methods)} {path}: `confirm_unless={field!r}`"
        if self._confirms_in_query() or self._reads_a_form():
            raise RuntimeError(
                f"{where} lê o campo do corpo JSON, e uma operação sem corpo (`GET`, `DELETE`) ou "
                "de formulário não o tem."
            )
        model = self.body_field.field_info.annotation if self.body_field is not None else None
        fields = (
            model.model_fields if isinstance(model, type) and issubclass(model, BaseModel) else {}
        )
        if field not in fields:
            raise RuntimeError(f"{where}, mas o corpo não declara o campo `{field}`.")
        if fields[field].annotation is not bool or fields[field].default is not False:
            raise RuntimeError(
                f"{where}, mas `{field}` não é um booleano que começa falso (`bool = False`)."
            )

    def _declares_confirm(self) -> bool:
        """Se o `confirm` que a dependência confere está no contrato: na query (`DELETE`) ou no
        primeiro nível do corpo (as chaves do modelo, ou de cada parâmetro de corpo, se há mais de
        um)."""
        if self._confirms_in_query():
            return "confirm" in _query_names(self.dependant)
        model = self.body_field.field_info.annotation if self.body_field is not None else None
        return (
            isinstance(model, type)
            and issubclass(model, BaseModel)
            and "confirm" in model.model_fields
        )


def _query_names(dependant: Dependant) -> set[str]:
    """Os parâmetros de query de uma rota, os das dependências dela também."""
    names = {param.alias for param in dependant.query_params}
    for sub in dependant.dependencies:
        names |= _query_names(sub)
    return names


def _parameters(dependant: Dependant) -> list[Any]:
    """Os parâmetros de caminho, query, cabeçalho e cookie de uma rota, os das dependências dela
    também: o que o FastAPI valida e pode recusar com `422`."""
    found = [
        *dependant.path_params,
        *dependant.query_params,
        *dependant.header_params,
        *dependant.cookie_params,
    ]
    for sub in dependant.dependencies:
        found += _parameters(sub)
    return found


class OpenRoute(APIRoute):
    """Uma rota da API de admin **sem chave**: a exceção explícita à regra de que toda rota tem papel.
    Só para o que a ADR manda servir com a sessão do admin ou aberto em desenvolvimento (a referência
    e o `openapi.json`, de `app.admin.api.reference`); quem a declara responde pela autenticação
    dela."""


class OpenRouter(APIRouter):
    """O roteador das rotas abertas (`OpenRoute`)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(route_class=OpenRoute, **kwargs)


def refuse_unguarded_routes(api: FastAPI) -> None:
    """Negado por padrão, na montagem: toda rota da sub-aplicação é uma `OperationRoute` (com papel) ou
    uma `OpenRoute` (a exceção declarada). Uma rota registrada por um `APIRouter` comum, ou um `Route`
    ou `Mount` do Starlette, ficaria aberta, sem chave nem papel."""
    unguarded = [
        context.route
        for context in iter_route_contexts(api.routes)
        if not isinstance(context.route, OperationRoute | OpenRoute)
    ]
    if unguarded:
        route = unguarded[0]
        raise RuntimeError(
            f"{getattr(route, 'path', route)}: rota da API de admin sem papel declarado "
            f"({type(route).__name__}). Declare-a com `OperationRouter.operation`; só a "
            "`OpenRouter` abre uma rota sem chave."
        )


def count_operations(api: FastAPI) -> int:
    """Quantas operações a sub-aplicação tem. A versão do contrato (`1.N.0`) é este `N`."""
    return sum(
        isinstance(context.route, OperationRoute) for context in iter_route_contexts(api.routes)
    )


class OperationRouter(APIRouter):
    """O roteador de um recurso da API de admin: todas as suas rotas são `OperationRoute`. A `tag`
    agrupa as operações do recurso e a `description` a explica (o que a referência e o MCP mostram):
    as duas saem juntas, e uma tag sem descrição não existe."""

    def __init__(self, *, tag: str, description: str, **kwargs: Any) -> None:
        if not description.strip():
            raise ValueError(f"a tag «{tag}» precisa de uma descrição")
        super().__init__(route_class=OperationRoute, tags=[tag], **kwargs)
        self.tag = tag
        self.description = description

    def operation(
        self,
        method: str,
        path: str,
        *,
        operation_id: str,
        role: str,
        summary: str,
        description: str,
        returns: str,
        response_model: Any,
        response_class: type[Response] | None = None,
        status_code: int = 200,
        destructive: bool = False,
        external_effect: bool = False,
        lead_data: bool = False,
        heavy: bool = False,
        confirmation_text: str | None = None,
        confirm_unless: str | None = None,
        errors: Iterable[int] | Mapping[int, str] = (),
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Declara uma operação (ver o texto do módulo). `summary` e `description` são em
        português e dizem o efeito e os efeitos colaterais, para quem nunca viu o sistema: é o que o
        MCP mostra ao modelo. `returns` diz o que a resposta de sucesso traz. `errors` são os erros
        além dos que a rota já tem: uma lista de status, ou um mapa de status para o texto da
        resposta nesta operação (o `409` exige o texto). `confirmation_text` é a frase que a recusa
        `confirmation_required` diz **desta** operação, no lugar do efeito em geral (só numa operação
        marcada). `confirm_unless` é o nome do campo booleano do corpo (`dry_run`) que, `true`, faz a
        chamada só simular e dispensa o `confirm` (só numa operação marcada).

        Uma operação que **entrega um arquivo** (o PDF de um pedido) passa `response_class`
        (`PdfResponse`) e `response_model=None`: o sucesso é o arquivo, declarado no OpenAPI como o
        tipo da classe em binário, e as recusas seguem em JSON, no formato de erro da API. Uma que
        **entrega uma página** (`HtmlResponse`, as prévias) faz o mesmo, com o tipo `text/html`
        declarado como texto."""
        dependencies = [Depends(requires(role, cost=HEAVY_COST if heavy else 1))]
        if confirmation_text is not None and not (destructive or external_effect):
            raise ValueError(
                f"{operation_id}: confirmation_text só vale numa operação marcada "
                "(destructive ou external_effect): sem marca não há o que confirmar"
            )
        if confirm_unless is not None and not (destructive or external_effect):
            raise ValueError(
                f"{operation_id}: confirm_unless só vale numa operação marcada "
                "(destructive ou external_effect): sem marca não há confirmação a dispensar"
            )
        if destructive or external_effect:
            confirmation = Confirmation(
                destructive=destructive,
                external_effect=external_effect,
                what=confirmation_text,
                unless=confirm_unless,
            )
            dependencies.append(Depends(confirmation))
        dependencies.append(Depends(plain_query))  # depois da chave, do papel e da confirmação
        texts = dict(errors) if isinstance(errors, Mapping) else {}
        statuses = sorted(set(errors))  # um mapa percorre as chaves: os status
        responses = error_responses(*statuses, texts=texts)
        route_kwargs: dict[str, Any] = {}
        if response_class is not None:
            if response_model is not None:
                raise ValueError(
                    f"{operation_id}: uma operação que entrega um arquivo não declara "
                    "response_model (use response_model=None): o sucesso é o arquivo"
                )
            if not response_class.media_type:
                raise ValueError(f"{operation_id}: a classe da resposta não diz o tipo do arquivo")
            # Um arquivo é declarado em binário; uma página (`text/*`), como texto.
            schema = (
                {"type": "string"}
                if response_class.media_type.startswith("text/")
                else {"type": "string", "format": "binary"}
            )
            responses[status_code] = {
                "description": returns,
                "content": {response_class.media_type: {"schema": schema}},
            }
            # Para o FastAPI a rota devolve uma `Response` sem tipo: se a rota levasse o tipo do
            # arquivo, ele o poria também nas respostas de erro (que são JSON). O tipo do sucesso é
            # o `content` acima, e a resposta de verdade é a que a função devolve.
            route_kwargs["response_class"] = Response
        return self.api_route(
            path,
            methods=[method],
            operation_id=operation_id,
            summary=summary,
            description=description,
            response_description=returns,
            response_model=response_model,
            status_code=status_code,
            dependencies=dependencies,
            responses=responses,
            openapi_extra={"x-lead-data": True} if lead_data else None,
            **route_kwargs,
        )


def tags_of(routers: Iterable[OperationRouter]) -> list[dict[str, str]]:
    """A lista de tags do OpenAPI, a partir dos roteadores que a API inclui: o nome e a descrição de
    cada um. Dois roteadores não dividem uma tag (a descrição de um apagaria a do outro), então a
    repetida derruba a montagem."""
    tags: dict[str, str] = {}
    for router in routers:
        if router.tag in tags:
            raise RuntimeError(
                f"a tag «{router.tag}» é de mais de um roteador da API de admin: cada recurso tem "
                "a sua, com a descrição dela"
            )
        tags[router.tag] = router.description
    return [{"name": name, "description": text} for name, text in tags.items()]
