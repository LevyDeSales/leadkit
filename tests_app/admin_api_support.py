"""O que os testes da API de admin (`/admin/api/v1`) dividem: o app com a sub-aplicação montada, as
chaves de teste e os cabeçalhos.

As chaves são sorteadas na execução, nunca literais: o GitGuardian do PR acusa valor com cara de
segredo, e nada com essa cara entra no repositório. Não é um arquivo de teste (o pytest não o coleta);
os arquivos `test_admin_api_*.py` o importam.
"""

from __future__ import annotations

import difflib
import functools
import json
import pickle
import secrets
import uuid
from collections.abc import Callable, Collection, Mapping
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from admin_api_matrix import operations_of
from fastapi import APIRouter, FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from test_admin import FakeClock
from test_admin_destinations import ADMIN_ENV

from app.admin.api import keys, openapi
from app.admin.api.errors import PREFIX
from app.admin.api.operations import refuse_unguarded_routes
from app.admin.auth import instant
from app.admin.launching import DraftTestTrigger
from app.api.main import create_app
from app.deps import Deps, get_deps
from app.models import AuditLog, Base
from app.settings import Settings

API = "/admin/api/v1"
ORIGIN = {"Origin": "http://testserver"}


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def is_page(path: str) -> bool:
    """Uma rota do admin que não é da API: começa por `/admin` e não por `/admin/api/v1`."""
    return path.startswith("/admin") and not (path == PREFIX or path.startswith(PREFIX + "/"))


def admin_routes(app: FastAPI) -> set[tuple[str, str]]:
    """As rotas das páginas do admin, pelo roteador: `iter_route_contexts` desce nos
    `include_router` (o `app.routes` traz cada um como um item só), e cada contexto tem `path` e
    `methods`. Sem `HEAD` e `OPTIONS`. É a lista de que o teste de cobertura e o das palavras
    reservadas (`RESERVED_SLUGS`) partem: o OpenAPI do app só existe fora de produção."""
    found = set()
    for context in iter_route_contexts(app.routes):
        if is_page(context.path):
            for method in context.methods or ():
                if method not in ("HEAD", "OPTIONS"):
                    found.add((method, context.path))
    return found


@functools.cache
def _paths_by_operation_id() -> dict[str, str]:
    return {operation["operationId"]: path for _, path, operation in operations_of(built_openapi())}


@functools.cache
def _built_openapi() -> bytes:
    return pickle.dumps(openapi.build(), protocol=pickle.HIGHEST_PROTOCOL)


def built_openapi() -> dict[str, Any]:
    """O mesmo que `openapi.build()`, que monta a sub-aplicação inteira e leva uns 0,2 s: o
    documento sai de uma montagem só por processo (um por worker), e cada chamada devolve uma cópia
    nova, que o teste pode mudar. Quem testa a montagem em si (que ela é a mesma duas vezes, que o
    arquivo versionado é o que ela gera) chama o `openapi.build()`."""
    found: dict[str, Any] = pickle.loads(_built_openapi())
    return found


def url_of(operation_id: str, **path_parameters: str) -> str:
    """O endereço de uma operação da API pelo `operation_id`, tirado do OpenAPI que a sub-aplicação
    gera: `url_of("get_job", public_id=código)`. O teste fala da operação pelo nome (é o que a
    matriz cobra quando o caminho dela é prefixo do de outra), e um nome que não existe mais, ou um
    parâmetro do caminho que falta, é um `KeyError` aqui, e não um `404` mais adiante."""
    return API + _paths_by_operation_id()[operation_id].format(**path_parameters)


def new_key(
    deps: Deps,
    clock: FakeClock,
    role: str = "leitura",
    *,
    name: str | None = None,
    expires_at: datetime | None = None,
) -> keys.CreatedKey:
    """Cria uma chave pelo serviço, no instante do relógio do teste, com um nome sorteado."""
    with deps.sessions.begin() as session:
        return keys.create(
            session,
            name=name or f"chave-{secrets.token_hex(3)}",
            role=role,
            actor="testes",
            expires_at=expires_at,
            now=instant(clock),
        )


def make_api(
    deps: Deps,
    clock: FakeClock,
    *,
    triggered: list[tuple[uuid.UUID, int]] | None = None,
    test_trigger: DraftTestTrigger | None = None,
    **env: str,
) -> TestClient:
    """O app inteiro, com o admin configurado e a API de admin montada. `env` troca ajustes, como
    `API_RATE_LIMIT_PER_MINUTE`. Sem sessão do admin e sem chave: quem chama escolhe.
    `triggered` guarda o que o disparador da tentativa recebe; `test_trigger` é o disparador da task
    `draft_test` (sem ele, o do app, que fala com o Hatchet: quem inicia um teste passa o seu)."""
    deps.settings = Settings(_env_file=None, **{**ADMIN_ENV, **env})  # type: ignore[call-arg]

    async def trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        if triggered is not None:
            triggered.append((job_id, attempt))
        return "run-fake"

    app = create_app(trigger=trigger, test_trigger=test_trigger, clock=clock)
    return TestClient(app, follow_redirects=False, raise_server_exceptions=False)


def admin_api(client: TestClient) -> FastAPI:
    """A sub-aplicação montada no app do teste (para acrescentar rotas de prova)."""
    found: FastAPI = client.app.state.admin_api  # type: ignore[attr-defined]
    return found


def add_routes(client: TestClient, router: APIRouter) -> None:
    """Acrescenta rotas de prova à sub-aplicação. Valem as mesmas regras da montagem: o roteador tem de
    ser um `OperationRouter` (com papel) ou um `OpenRouter` (aberto de propósito)."""
    api = admin_api(client)
    api.include_router(router)
    refuse_unguarded_routes(api)


def audited(deps: Deps, action: str) -> list[AuditLog]:
    with deps.sessions() as session:
        return list(session.scalars(sa.select(AuditLog).where(AuditLog.action == action)))


# --- o cliente de uma chave ---------------------------------------------------------------------


class ApiClient(TestClient):
    """O cliente de uma chave de API: manda o `Authorization: Bearer` sozinho. `key` é a chave
    (`key.key` é a chave inteira, `key.info`, o que o banco guarda), `role` o papel dela e `clock` o
    relógio do app, que o teste avança. O app dele é o inteiro, com as páginas do admin, então o
    mesmo cliente serve a `assert_same_effect` (a página entra com `login`, e a API usa o
    cabeçalho)."""

    key: keys.CreatedKey
    role: str
    clock: FakeClock

    @property
    def actor(self) -> str:
        """O autor que a auditoria grava quando esta chave escreve (`chave:<nome>`)."""
        return self.key.info.actor


def api_client(
    deps: Deps,
    clock: FakeClock | None = None,
    role: str = "total",
    *,
    name: str | None = None,
    triggered: list[tuple[uuid.UUID, int]] | None = None,
    test_trigger: DraftTestTrigger | None = None,
    **env: str,
) -> ApiClient:
    """Cria uma chave sorteada do papel `role` e devolve o cliente que já a manda (o app inteiro,
    com a API de admin montada). Nenhuma chave é literal: o nome e o segredo são sorteados a cada
    execução. Nos testes, o jeito de pedi-lo é a fixture `api_client` (`api_client("leitura")`)."""
    clock = clock or FakeClock()
    created = new_key(deps, clock, role, name=name)
    app = make_api(deps, clock, triggered=triggered, test_trigger=test_trigger, **env).app
    client = ApiClient(
        app,
        headers=bearer(created.key),
        follow_redirects=False,
        raise_server_exceptions=False,
    )
    client.key, client.role, client.clock = created, role, clock
    return client


# --- o mesmo efeito, pela página e pela API -----------------------------------------------------

PAGE_ACTOR = "operadora"  # o `ADMIN_USERNAME` do `ADMIN_ENV`: o autor das ações da página
NEW = "<novo>"
AUTHOR = "<autor>"
Snapshot = dict[str, list[dict[str, Any]]]


def snapshot() -> Snapshot:
    """Todas as linhas de todas as tabelas do banco do teste."""
    with get_deps().sessions() as session:
        return {
            table.name: [dict(row._mapping) for row in session.execute(sa.select(table))]
            for table in Base.metadata.sorted_tables
        }


def restore(state: Snapshot) -> None:
    """Devolve o banco ao estado de `snapshot()`: esvazia as tabelas e grava as linhas de volta, na
    ordem em que as chaves estrangeiras deixam."""
    tables = Base.metadata.sorted_tables
    with get_deps().sessions.begin() as session:
        names = ", ".join(f'"{table.name}"' for table in tables)
        session.execute(sa.text(f"TRUNCATE {names} CASCADE"))
        for table in tables:
            if state[table.name]:
                session.execute(table.insert(), state[table.name])


def _normal(value: Any, known: set[uuid.UUID], actor: str | None) -> Any:
    """Um valor de coluna sem o que muda de uma execução para a outra: o autor vira `<autor>` (se
    `actor` vem) e um id sorteado que não existia antes vira `<novo>`."""
    if isinstance(value, uuid.UUID):
        return value if value in known else NEW
    if isinstance(value, str):
        return AUTHOR if actor is not None and value == actor else value
    if isinstance(value, dict):
        return {key: _normal(item, known, actor) for key, item in value.items()}
    if isinstance(value, list):
        return [_normal(item, known, actor) for item in value]
    return value


def _json(values: Mapping[str, Any]) -> str:
    return json.dumps(values, sort_keys=True, default=str, ensure_ascii=False)


def _comparable(
    state: Snapshot, before: Snapshot, actor: str, ignore: Mapping[str, Collection[str]]
) -> dict[str, list[str]]:
    """As linhas de `state` prontas para comparar. O autor é normalizado **por célula**: só nas células
    que mudaram em relação à linha de mesma chave primária em `before` e em todas as de uma linha nova.
    Uma célula que a ação não tocou é a mesma antes e depois, ainda que a linha tenha mudado em outra
    coluna: se o mundo do teste a gravou com o nome do autor da página (`operadora`), ela não vira
    `<autor>` de um lado só."""
    known = {
        value
        for rows in before.values()
        for row in rows
        for value in row.values()
        if isinstance(value, uuid.UUID)
    }
    found: dict[str, list[str]] = {}
    for table, rows in state.items():
        dropped = {*ignore.get(table, ()), *ignore.get("*", ())}
        identity = _identity(table)
        old = {_key(row, identity): _kept(row, dropped) for row in before.get(table, ())}
        lines = []
        for row in rows:
            kept = _kept(row, dropped)
            previous = old.get(_key(row, identity))  # None: linha nova
            lines.append(
                _json(
                    {
                        column: _normal(
                            value,
                            known,
                            actor
                            if previous is None
                            or column not in previous
                            or previous[column] != value
                            else None,
                        )
                        for column, value in kept.items()
                    }
                )
            )
        found[table] = sorted(lines)
    return found


def _identity(table: str) -> tuple[str, ...]:
    """As colunas da chave primária da tabela: com elas se acha, no `before`, a linha de uma linha de
    depois. Sem chave primária, a linha inteira é a identidade (e uma linha alterada vira nova)."""
    return tuple(column.name for column in Base.metadata.tables[table].primary_key.columns)


def _key(row: Mapping[str, Any], identity: tuple[str, ...]) -> str:
    return _json({column: row[column] for column in identity} if identity else row)


def _kept(row: Mapping[str, Any], dropped: Collection[str]) -> dict[str, Any]:
    """A linha sem as colunas que a comparação ignora."""
    return {c: v for c, v in row.items() if c not in dropped}


def assert_same_effect(
    page_call: Callable[[], Any],
    api_call: Callable[[], Any],
    *,
    api_actor: str,
    page_actor: str = PAGE_ACTOR,
    ignore: Mapping[str, Collection[str]] | None = None,
    expect_no_change: bool = False,
) -> tuple[Any, Any]:
    """A prova de paridade da ADR 0015: faz a mesma ação pela página (`page_call`) e pela API
    (`api_call`) **a partir do mesmo estado do banco** e confere que o estado final e a auditoria são
    os mesmos, menos o autor.

    O banco é fotografado antes, `page_call` roda e o resultado é fotografado; o banco volta ao estado
    de antes, `api_call` roda e o resultado é fotografado. O autor (`page_actor` e `api_actor`) vira
    um só nos dois, **nas células que a ação mudou** e em todas as de uma linha nova (uma célula antiga,
    que o mundo do teste gravou com o nome de um dos autores, fica como está dos dois lados, ainda que
    a linha tenha mudado em outra coluna), e um id sorteado que a ação criou também. `ignore` tira colunas da comparação
    (`{"api_keys": {"last_used_at"}}`, ou `"*"` para qualquer tabela): é para o que a própria chamada
    da API muda, como o último uso da chave que chama, e para o que a ação sorteia (o segredo de uma
    chave ou de um destino). A hora da auditoria (`audit_log.at`) é sempre tirada: vem do relógio da
    máquina, e não do relógio do app.

    **Duas chamadas que não mudam nada são iguais à toa**, e a prova não prova nada (uma chamada que
    errou o endereço, uma recusa que o teste não esperava): por padrão a função falha se nenhuma das
    duas mudou o estado. `expect_no_change=True` é para quem quer exatamente isso (uma recusa, uma
    simulação como o `dry_run`): então ela falha se alguma das duas mudou o estado. Devolve o que
    cada chamada devolveu, para o teste conferir os status."""
    ignored = {table: set(columns) for table, columns in (ignore or {}).items()}
    ignored.setdefault("audit_log", set()).add(
        "at"
    )  # a hora da auditoria é a do relógio da máquina
    before = snapshot()
    page_result = page_call()
    after_page = snapshot()
    restore(before)
    api_result = api_call()
    after_api = snapshot()

    by_page = _comparable(after_page, before, page_actor, ignored)
    by_api = _comparable(after_api, before, api_actor, ignored)
    page_changed = by_page != _comparable(before, before, page_actor, ignored)
    api_changed = by_api != _comparable(before, before, api_actor, ignored)
    differences = []
    for table in sorted(by_page):
        if by_page[table] != by_api[table]:
            diff = difflib.unified_diff(
                by_page[table], by_api[table], "pela página", "pela API", lineterm="", n=0
            )
            differences.append(f"tabela {table}:\n" + "\n".join(diff))
    assert not differences, "a página e a API não deixaram o mesmo estado:\n" + "\n".join(
        differences
    )
    if expect_no_change:
        assert not (page_changed or api_changed), (
            "esperava que a ação não mudasse o estado (expect_no_change=True), mas "
            + ("a página e a API mudaram" if page_changed and api_changed else "uma delas mudou")
            + " o banco"
        )
    else:
        assert page_changed or api_changed, (
            "nenhuma das duas chamadas mudou o estado do banco: a prova de paridade não provaria "
            "nada (a chamada errou? a recusa era esperada?). Se é isso o que se quer, passe "
            "expect_no_change=True"
        )
    return page_result, api_result
