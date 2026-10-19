"""O teste de cobertura da API de admin (ADR 0015, "O teste de cobertura"): liga a página, a matriz e
o contrato.

Ele lê a matriz (`docs/api/cobertura.md`, pela gramática dela), enumera as rotas do admin e as
operações dos quatro editores, lê o `docs/api/openapi.json` e cobra as **sete regras** da ADR (as mesmas
de "Como o teste lê este arquivo", na matriz):

1. rotas e linhas, nos dois sentidos (com o OpenAPI do app dizendo as mesmas rotas);
2. o `openapi.json` tem a operação de toda linha `feita` (com método, caminho, papel e marcas da linha)
   e não tem a de nenhuma `falta`;
3. toda linha `feita` cita um `arquivo::teste` que existe, cujo nome começa por `test_` e que fala da
   operação (o `operation_id` ou o caminho aparece no código dele, ou no das funções e constantes do
   mesmo arquivo que ele usa);
4. toda linha `fora` tem uma letra que está em "O que não entra na API", e o conjunto delas é travado
   numa constante (`OUT_OF_THE_API`), para uma linha `fora` nova exigir uma edição deliberada;
5. toda operação do `openapi.json` está em alguma linha;
6. as operações que os quatro editores declaram estão todas listadas, e as listadas existem;
7. a cobertura fechada: nenhuma linha está `falta` (a catraca chegou a zero em #112).

Roda na suíte inteira, sem rede. Cada regra tem o teste do próprio teste (uma matriz ou um mundo
alterado, que a regra tem de reprovar), e a mensagem de cada falha aponta o arquivo da matriz.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from admin_api_matrix import (
    EDITOR,
    EDITORS,
    HTTP_METHODS,
    MATRIX_FILE,
    METHODS,
    NEW,
    OPENAPI_FILE,
    ROLES,
    ROOT,
    ROUTES,
    Matrix,
    MatrixError,
    Row,
    citation_exists,
    citation_mentions,
    differences,
    duplicates,
    operations_of,
    parse,
    path_is_shared,
    read,
    snake_case,
)
from admin_api_support import admin_routes, is_page, make_api
from fastapi import APIRouter, FastAPI
from starlette.routing import Route
from test_admin import FakeClock

from app.admin.api.errors import PREFIX
from app.admin.data_editor import DataEdit
from app.admin.form_editor import FormEdit
from app.admin.plan_editor import PlanEdit
from app.admin.scoring_editor import ScoringEdit
from app.deps import Deps

# A catraca fechou (regra 7). Ela foi o número de linhas `falta` da matriz, que só baixava: cada fase de
# recurso a baixava pelas linhas que passava a `feita`, no mesmo PR. A última (#112, `list_audit_log`)
# a levou a zero, e a regra agora é exigir zero: uma rota nova do admin sem operação reprova (regra 1), e
# a que chega com uma linha `falta` reprova também. A constante fica em 0 de propósito, para a regra
# dizer o que é; subi-la reabriria a cobertura, e a revisão tem de ver isso.
MISSING_ROWS = 0

# As linhas `fora` (a regra 4), travadas: o que fica fora da API é decisão deliberada, e uma linha
# `fora` nova exige editar esta constante no mesmo PR, onde a revisão a vê. As 13 de hoje: o login, a
# sessão e a saída (A); as três telas sem dado nem ação (B); as seis operações dos editores que só
# redesenham a tela (C); e a tela de configurar o MCP (D). A chave é `Row.key` (a rota, ou o editor e a
# operação), e o valor, a letra de "O que não entra na API".
OUT_OF_THE_API = {
    "GET /admin/login": "A",
    "POST /admin/login": "A",
    "POST /admin/logout": "A",
    "GET /admin/forms/import": "B",
    "GET /admin/forms/new": "B",
    "GET /admin/forms/{slug}/draft/preview": "B",
    "GET /admin/mcp": "D",
    "FormEdit refresh-question": "C",
    "FormEdit refresh-rule": "C",
    "FormEdit refresh-operator": "C",
    "PlanEdit plan-refresh": "C",
    "DataEdit data-refresh": "C",
    "ScoringEdit scoring-refresh-rule": "C",
}


@dataclass
class World:
    """Tudo o que a regra confere contra a matriz: as rotas do app e o OpenAPI dele, o `openapi.json`
    e as operações que cada editor declara."""

    routes: set[tuple[str, str]]  # (método, caminho) das páginas do admin, pelo roteador
    schema_routes: set[tuple[str, str]]  # os mesmos pares, pelo OpenAPI do app
    document: dict[str, Any]  # o `docs/api/openapi.json`
    editors: dict[str, tuple[str, ...]]
    exists: Any = field(default=citation_exists)  # o teste citado existe? (troca-se nos testes)
    mentions: Any = field(default=citation_mentions)  # ele fala da operação? (idem)


# --- as rotas do admin --------------------------------------------------------------------------


def schema_routes(app: FastAPI) -> set[tuple[str, str]]:
    """Os mesmos pares, pelo OpenAPI do app (`app.openapi()["paths"]`) filtrado por `/admin`."""
    return {
        (method.upper(), path)
        for path, item in app.openapi()["paths"].items()
        if is_page(path)
        for method in item
        if method in HTTP_METHODS
    }


def the_editors() -> dict[str, tuple[str, ...]]:
    return {
        "FormEdit": FormEdit.OPERATIONS,
        "PlanEdit": PlanEdit.OPERATIONS,
        "DataEdit": DataEdit.OPERATIONS,
        "ScoringEdit": ScoringEdit.OPERATIONS,
    }


# --- as sete regras -----------------------------------------------------------------------------


def rule_1_routes_and_rows(matrix: Matrix, world: World) -> list[str]:
    found = []
    rows = matrix.of_kind(ROUTES)
    for repeated in duplicates(row.route or "" for row in rows):
        found.append(f"{MATRIX_FILE}: a rota {repeated} aparece em mais de uma linha")
    listed = {row.route_pair: row for row in rows}
    for method, path in sorted(world.routes - listed.keys()):
        found.append(
            f"{MATRIX_FILE}: a rota {method} {path} existe no app e não tem linha na matriz. "
            "Inclua a linha (e a operação da API que a cobre, ou `fora (letra)` com o motivo)."
        )
    for pair in sorted(listed.keys() - world.routes):
        found.append(f"{listed[pair].label}: a linha cita uma rota que o app não registra")
    if world.routes != world.schema_routes:
        for method, path in sorted(world.routes ^ world.schema_routes):
            where = "só no roteador" if (method, path) in world.routes else "só no OpenAPI"
            found.append(f"o OpenAPI do app e o roteador divergem em {method} {path} ({where})")
    return found


def _grammar(row: Row) -> list[str]:
    """O que uma linha de rota ou de operação nova precisa trazer, pelo status dela."""
    if row.status == "fora":
        if row.operation_id or row.operation or row.role or row.marks:
            return [f"{row.label}: uma linha `fora` não tem operação, papel nem marcas"]
        return []
    if not (row.operation_id and row.operation and row.role):
        return [f"{row.label}: `{row.status}` pede a operação, o `operation_id` e o papel"]
    found = []
    if not snake_case(row.operation_id):
        found.append(f"{row.label}: «{row.operation_id}» não é `snake_case` (verbo e recurso)")
    method, _, path = row.operation.partition(" ")
    if method not in METHODS or not path.startswith("/"):
        found.append(f"{row.label}: «{row.operation}» não é `MÉTODO /caminho`")
    if row.role not in ROLES:
        found.append(f"{row.label}: o papel «{row.role}» não é um de {', '.join(ROLES)}")
    return found


def rule_2_against_the_file(matrix: Matrix, world: World) -> list[str]:
    found = []
    in_file = {
        operation["operationId"]: (method, path, operation)
        for method, path, operation in operations_of(world.document)
    }
    by_id: dict[str, list[Row]] = defaultdict(list)
    for row in [*matrix.of_kind(ROUTES), *matrix.of_kind(NEW)]:
        found += _grammar(row)
        if row.status != "fora" and row.operation_id:
            by_id[row.operation_id].append(row)
    for operation_id, rows in sorted(by_id.items()):
        first = rows[0]
        if len({(row.operation, row.role, row.marks) for row in rows}) > 1:
            found.append(
                f"{first.label}: as linhas de {operation_id} (as rotas que dividem a operação) "
                "dizem operação, papel ou marcas diferentes"
            )
        if len({row.status for row in rows}) > 1:
            found.append(f"{first.label}: as linhas de {operation_id} têm status diferentes")
        if first.status == "falta" and operation_id in in_file:
            found.append(
                f"{first.label}: {operation_id} está `falta`, mas a operação já está no "
                f"{OPENAPI_FILE}. Passe a linha para `feita` e cite o teste."
            )
        if first.status == "feita":
            if operation_id not in in_file:
                found.append(
                    f"{first.label}: {operation_id} está `feita`, mas o {OPENAPI_FILE} não tem a operação"
                )
            else:
                found += [
                    f"{first.label}: {operation_id}: {difference}"
                    for difference in differences(first, *in_file[operation_id])
                ]
    pairs: dict[tuple[str, str], set[str]] = defaultdict(set)
    for operation_id, rows in by_id.items():
        if rows[0].operation:
            pairs[rows[0].operation_pair].add(operation_id)
    for pair, ids in sorted(pairs.items()):
        if len(ids) > 1:
            found.append(f"{MATRIX_FILE}: {' '.join(pair)} é a operação de {sorted(ids)}")
    return found


def rule_3_cited_tests(matrix: Matrix, world: World) -> list[str]:
    found = []
    pairs = [(method, path) for method, path, _ in operations_of(world.document)]
    for row in matrix.rows:
        if row.status != "feita":
            continue
        if not (row.test and world.exists(row.test)):
            found.append(f"{row.label}: a linha `feita` cita {row.test}, que não existe")
            continue
        name = row.test.rpartition("::")[2]
        if not name.startswith("test_"):
            found.append(
                f"{row.label}: a linha `feita` cita {row.test}, que não é um teste "
                "(o nome começa por `test_`)"
            )
        elif row.operation_id and row.operation:
            # O caminho que é prefixo do de outra operação (`/jobs` e `/jobs/{public_id}`) não diz
            # qual das duas o teste exerce: ali o `operation_id` tem de estar no código dele.
            shared = path_is_shared(row.operation_pair, pairs)
            if not world.mentions(row.test, row.operation_id, row.operation, id_in_code=shared):
                found.append(
                    f"{row.label}: o caminho de {row.operation_id} ({row.operation}) é prefixo do "
                    f"de outra operação, e o teste {row.test} não diz qual delas exerce: o "
                    "`operation_id` precisa estar no código do teste (um texto, um nome; não vale "
                    "só no docstring ou num comentário)"
                    if shared
                    else f"{row.label}: o teste {row.test} não menciona a operação "
                    f"{row.operation_id} ({row.operation}): cite o teste que a exercita, com o "
                    "`operation_id` ou o caminho no código dele"
                )
    return found


def rule_4_exceptions(
    matrix: Matrix, world: World, locked: dict[str, str] = OUT_OF_THE_API
) -> list[str]:
    found = []
    if not matrix.exceptions:
        found.append(f"{MATRIX_FILE}: a seção «O que não entra na API» não lista nenhuma letra")
    outside = {row.key: row for row in matrix.rows if row.status == "fora"}
    for row in outside.values():
        if row.letter not in matrix.exceptions:
            found.append(
                f"{row.label}: a letra ({row.letter}) de `fora` não está em «O que não entra na "
                f"API» ({', '.join(sorted(matrix.exceptions))})"
            )
        if row.key not in locked:
            found.append(
                f"{row.label}: `fora ({row.letter})` novo. O que fica fora da API é decisão "
                "deliberada: inclua a linha em OUT_OF_THE_API, em tests_app/test_admin_api_coverage.py, "
                "no mesmo PR"
            )
        elif locked[row.key] != row.letter:
            found.append(
                f"{row.label}: a letra de `fora` é {row.letter}, e OUT_OF_THE_API diz {locked[row.key]}"
            )
    for key in sorted(locked.keys() - outside.keys()):
        found.append(
            f"{MATRIX_FILE}: OUT_OF_THE_API lista {key}, que a matriz não tem mais como `fora`. "
            "Tire a linha da constante (ou volte a linha da matriz para `fora`)."
        )
    return found


def rule_5_every_operation_has_a_row(matrix: Matrix, world: World) -> list[str]:
    listed = {
        row.operation_id
        for row in [*matrix.of_kind(ROUTES), *matrix.of_kind(NEW)]
        if row.operation_id
    }
    return [
        f"{MATRIX_FILE}: a operação {operation['operationId']} ({method} {path}) está no "
        f"{OPENAPI_FILE} e não tem linha na matriz"
        for method, path, operation in operations_of(world.document)
        if operation["operationId"] not in listed
    ]


def rule_6_editors(matrix: Matrix, world: World) -> list[str]:
    found = []
    for row in matrix.of_kind(EDITOR):
        if row.editor not in EDITORS:
            found.append(f"{row.label}: a tabela não está numa seção de editor (`Editor`, N)")
    for editor in EDITORS:
        declared = world.editors.get(editor, ())
        listed = [row.editor_operation for row in matrix.of_kind(EDITOR) if row.editor == editor]
        for repeated in duplicates(listed):
            found.append(f"{MATRIX_FILE}: {editor}: a operação {repeated} aparece duas vezes")
        for name in declared:
            if name not in listed:
                found.append(
                    f"{MATRIX_FILE}: o editor {editor} declara a operação {name}, que a matriz "
                    "não lista. Inclua a linha (ou `fora (C)`, se só redesenha a tela)."
                )
        for name in listed:
            if name not in declared:
                found.append(
                    f"{MATRIX_FILE}: lista a operação {name} para {editor}, que o editor não declara"
                )
        counted = matrix.editor_counts.get(editor)
        if counted != len(listed):
            found.append(
                f"{MATRIX_FILE}: o título da seção de {editor} diz {counted} operações e a tabela "
                f"tem {len(listed)}"
            )
    return found


def rule_7_closed(matrix: Matrix, world: World) -> list[str]:
    missing = matrix.missing
    if missing > MISSING_ROWS:
        plural = "linha" if missing == 1 else "linhas"
        return [
            (
                f"{MATRIX_FILE}: há {missing} {plural} `falta`, e a cobertura está fechada (a catraca "
                f"chegou a {MISSING_ROWS} em #112). Toda rota do admin traz, no mesmo PR, a operação "
                "da API (a linha `feita`, com o teste) ou o motivo de ficar de fora (`fora (letra)`)."
            )
        ]
    return []


def problems(matrix: Matrix, world: World) -> list[str]:
    return [
        *rule_1_routes_and_rows(matrix, world),
        *rule_2_against_the_file(matrix, world),
        *rule_3_cited_tests(matrix, world),
        *rule_4_exceptions(matrix, world),
        *rule_5_every_operation_has_a_row(matrix, world),
        *rule_6_editors(matrix, world),
        *rule_7_closed(matrix, world),
    ]


# --- o teste ------------------------------------------------------------------------------------


# As rotas do app e as do OpenAPI dele não mudam de um teste para o outro: o app se monta uma vez por
# processo (um por worker), e cada teste recebe cópias novas, porque os testes do teste as mudam.
_APP_ROUTES: list[tuple[frozenset[tuple[str, str]], frozenset[tuple[str, str]]]] = []


def routes_of_the_app(deps: Deps) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(as rotas das páginas do admin pelo roteador, as mesmas pelo OpenAPI do app), do app inteiro
    montado de verdade; cada chamada devolve conjuntos novos."""
    if not _APP_ROUTES:
        app = make_api(deps, FakeClock()).app
        _APP_ROUTES.append((frozenset(admin_routes(app)), frozenset(schema_routes(app))))
    routes, from_the_schema = _APP_ROUTES[0]
    return set(routes), set(from_the_schema)


@pytest.fixture
def world(deps: Deps) -> World:
    routes, from_the_schema = routes_of_the_app(deps)
    return World(
        routes=routes,
        schema_routes=from_the_schema,
        document=json.loads((ROOT / OPENAPI_FILE).read_text(encoding="utf-8")),
        editors=the_editors(),
    )


@pytest.fixture
def matrix() -> Matrix:
    return read()


def test_the_matrix_covers_every_admin_route_operation_and_editor_operation(
    matrix: Matrix, world: World
) -> None:
    found = problems(matrix, world)

    assert not found, "\n".join(found)


def test_the_world_the_test_reads_is_not_empty(matrix: Matrix, world: World) -> None:
    """Um teste que não olha nada passa sempre: o mundo tem as rotas, as operações e as operações
    dos editores que a matriz diz (pelo menos as 67 rotas, as 4 operações das chaves e as 48
    operações dos editores de hoje; as fases de recurso só acrescentam)."""
    assert len(world.routes) == len(matrix.of_kind(ROUTES)) >= 67
    assert len(operations_of(world.document)) >= 4
    editor_rows = len(matrix.of_kind(EDITOR))
    assert sum(len(names) for names in world.editors.values()) == editor_rows >= 48


# --- o teste do teste: cada regra reprova o que ela cobra ---------------------------------------


def row_of(matrix: Matrix, *, route: str | None = None, operation_id: str | None = None) -> Row:
    return next(
        row
        for row in matrix.rows
        if (route is None or row.route == route)
        and (operation_id is None or row.operation_id == operation_id)
    )


def route_done_alone(matrix: Matrix) -> Row:
    """Uma rota `feita` que é a única linha da operação dela, para o teste virá-la `falta` ou `fora`
    sem contradizer as irmãs. Os testes das regras 2 e 4 não esperam que reste uma `falta` de verdade
    na matriz: a catraca chegou a zero em #112."""
    rows_of: dict[str | None, int] = defaultdict(int)
    for row in matrix.rows:
        rows_of[row.operation_id] += 1
    return next(
        row
        for row in matrix.of_kind(ROUTES)
        if row.status == "feita" and rows_of[row.operation_id] == 1
    )


def failing(matrix: Matrix, world: World, **options: Any) -> str:
    found = problems(matrix, world, **options)
    assert found, "a regra não reprovou"
    return "\n".join(found)


def test_rule_1_a_new_admin_route_without_a_row_fails_and_points_to_the_matrix(
    matrix: Matrix, world: World
) -> None:
    world.routes.add(("POST", "/admin/forms/{slug}/archive"))
    world.schema_routes.add(("POST", "/admin/forms/{slug}/archive"))

    message = failing(matrix, world)

    assert MATRIX_FILE in message and "POST /admin/forms/{slug}/archive" in message
    assert "não tem linha" in message


def test_rule_1_a_row_without_a_route_fails(matrix: Matrix, world: World) -> None:
    ghost = replace(row_of(matrix, route="GET /admin/login"), route="GET /admin/fantasma")

    message = failing(matrix.with_row(ghost), world)

    assert "GET /admin/fantasma" in message and "não registra" in message


def test_rule_1_a_row_removed_leaves_its_route_without_a_row(matrix: Matrix, world: World) -> None:
    login = row_of(matrix, route="GET /admin/login")

    assert "GET /admin/login existe no app e não tem linha" in failing(
        matrix.replace(login, None), world
    )


def test_rule_1_a_route_listed_twice_fails(matrix: Matrix, world: World) -> None:
    again = replace(row_of(matrix, route="GET /admin/login"), line=999)

    assert "aparece em mais de uma linha" in failing(matrix.with_row(again), world)


def test_rule_1_the_router_and_the_openapi_of_the_app_must_say_the_same_routes(
    matrix: Matrix, world: World
) -> None:
    world.schema_routes.discard(("GET", "/admin"))

    assert "GET /admin (só no roteador)" in failing(matrix, world)


def test_rule_1_the_enumeration_goes_into_included_routers_and_leaves_the_api_and_head_out() -> (
    None
):
    """`app.routes` traz cada `include_router` como um item só: o teste desce neles com
    `iter_route_contexts`. A sub-aplicação montada em `/admin/api/v1`, `HEAD` e `OPTIONS` ficam de
    fora; o que não começa por `/admin` também."""
    app = FastAPI()
    pages = APIRouter(prefix="/admin")
    nested = APIRouter(prefix="/forms")

    @nested.get("/{slug}")
    def form(slug: str) -> dict[str, str]:
        return {}

    @nested.post("/{slug}/publish")
    def publish(slug: str) -> dict[str, str]:
        return {}

    pages.include_router(nested)
    app.include_router(pages)
    app.router.routes.append(
        Route("/admin/plain", lambda request: None, methods=["GET"])
    )  # tem HEAD
    sub = FastAPI()

    @sub.get("/me")
    def me() -> dict[str, str]:
        return {}

    app.mount(PREFIX, sub)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {}

    assert admin_routes(app) == {
        ("GET", "/admin/forms/{slug}"),
        ("POST", "/admin/forms/{slug}/publish"),
        ("GET", "/admin/plain"),
    }
    assert schema_routes(app) == {
        ("GET", "/admin/forms/{slug}"),
        ("POST", "/admin/forms/{slug}/publish"),
    }  # a rota do Starlette não está no OpenAPI: a regra 1 também reprovaria essa divergência


def test_rule_2_a_falta_row_whose_operation_is_already_in_the_file_fails(
    matrix: Matrix, world: World
) -> None:
    done = route_done_alone(matrix)  # a operação dela já está no arquivo
    planned = replace(done, status="falta", test=None)

    message = failing(matrix.replace(done, planned), world)

    assert (
        f"{planned.operation_id} está `falta`, mas a operação já está no docs/api/openapi.json"
        in (message)
    )


def test_rule_2_a_feita_row_whose_operation_left_the_file_fails(
    matrix: Matrix, world: World
) -> None:
    del world.document["paths"]["/me"]

    assert "get_current_api_key está `feita`, mas o docs/api/openapi.json não tem" in failing(
        matrix, world
    )


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"role": "total"}, "o papel é «total» na matriz e «leitura» no OpenAPI"),
        ({"marks": frozenset({"x-destructive"})}, "as marcas são ['x-destructive'] na matriz"),
        ({"operation": "GET /eu"}, "a matriz diz GET /eu e o OpenAPI tem GET /me"),
    ],
)
def test_rule_2_a_feita_row_must_say_the_method_path_role_and_marks_of_the_file(
    matrix: Matrix, world: World, change: dict[str, Any], expected: str
) -> None:
    row = row_of(matrix, operation_id="get_current_api_key")

    assert expected in failing(matrix.replace(row, replace(row, **change)), world)


def test_rule_2_the_rows_of_one_operation_must_agree(matrix: Matrix, world: World) -> None:
    sibling = row_of(matrix, route="GET /admin/destinations/forms/{slug}")

    message = failing(matrix.replace(sibling, replace(sibling, role="total")), world)

    assert "as linhas de get_destination" in message


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"operation_id": "ListJobs"}, "não é `snake_case`"),
        ({"operation_id": "jobs"}, "não é `snake_case`"),
        ({"operation": "FETCH /jobs"}, "não é `MÉTODO /caminho`"),
        ({"operation": "GET jobs"}, "não é `MÉTODO /caminho`"),
        ({"role": "admin"}, "o papel «admin» não é um de leitura, total"),
        ({"role": None}, "pede a operação, o `operation_id` e o papel"),
    ],
)
def test_rule_2_a_planned_operation_must_be_well_formed(
    matrix: Matrix, world: World, change: dict[str, Any], expected: str
) -> None:
    row = row_of(matrix, operation_id="list_jobs")

    assert expected in failing(matrix.replace(row, replace(row, **change)), world)


def test_rule_2_a_fora_row_has_no_operation(matrix: Matrix, world: World) -> None:
    row = row_of(matrix, route="GET /admin/login")

    message = failing(matrix.replace(row, replace(row, operation_id="login_admin")), world)

    assert "uma linha `fora` não tem operação" in message


def test_rule_2_two_operations_cannot_share_a_method_and_path(matrix: Matrix, world: World) -> None:
    jobs = row_of(matrix, operation_id="list_jobs")
    twin = replace(row_of(matrix, operation_id="get_job"), operation=jobs.operation)

    message = failing(matrix.replace(row_of(matrix, operation_id="get_job"), twin), world)

    assert "é a operação de ['get_job', 'list_jobs']" in message


def test_rule_3_a_feita_row_that_cites_a_test_that_does_not_exist_fails(
    matrix: Matrix, world: World
) -> None:
    row = row_of(matrix, operation_id="get_current_api_key")
    missing = "tests_app/test_admin_api_keys_api.py::test_that_was_never_written"

    message = failing(matrix.replace(row, replace(row, test=missing)), world)

    assert missing in message and "não existe" in message


def test_rule_3_a_test_in_a_file_that_does_not_exist_fails_too(
    matrix: Matrix, world: World
) -> None:
    row = row_of(matrix, operation_id="get_current_api_key")

    message = failing(
        matrix.replace(row, replace(row, test="tests_app/test_nao_existe.py::test_x")), world
    )

    assert "test_nao_existe.py::test_x" in message


def test_rule_3_a_citation_that_is_not_a_test_fails(matrix: Matrix, world: World) -> None:
    row = row_of(matrix, operation_id="get_current_api_key")
    helper = "tests_app/test_admin_api_keys_api.py::create"  # existe, mas é um auxiliar

    message = failing(matrix.replace(row, replace(row, test=helper)), world)

    assert helper in message and "não é um teste" in message


def test_rule_3_a_test_that_never_mentions_the_operation_fails(
    matrix: Matrix, world: World
) -> None:
    row = row_of(matrix, operation_id="get_current_api_key")
    other = "tests_app/test_admin_api_keys_api.py::test_the_list_never_carries_a_secret_nor_a_hash"

    message = failing(matrix.replace(row, replace(row, test=other)), world)

    assert other in message and "não menciona a operação get_current_api_key" in message
    assert "GET /me" in message


def test_rule_3_the_four_citations_of_the_keys_reach_their_operation_through_helpers_and_constants(
    matrix: Matrix, world: World
) -> None:
    """O teste de `create_api_key` chama `create(...)`, que usa `KEYS`, e `KEYS` é `{API}/api-keys`:
    ele fala da operação sem escrever o caminho."""
    cited = {
        row.operation_id: (row.test, row.operation)
        for row in matrix.rows
        if row.status == "feita" and row.operation_id
    }

    assert {"create_api_key", "revoke_api_key", "list_api_keys", "get_current_api_key"} <= set(
        cited
    )
    for operation_id, (test, operation) in cited.items():
        assert test and operation and citation_mentions(test, operation_id, operation), operation_id


def write_tests(tmp_path: Path, source: str) -> str:
    """Um arquivo de teste de brinquedo, e a citação para a primeira função `test_` dele."""
    (tmp_path / "tests_app").mkdir(exist_ok=True)
    (tmp_path / "tests_app" / "test_toy.py").write_text(source, encoding="utf-8")
    return "tests_app/test_toy.py::test_it"


def mentions(tmp_path: Path, source: str, operation_id: str, operation: str) -> bool:
    return citation_mentions(write_tests(tmp_path, source), operation_id, operation, tmp_path)


@pytest.mark.parametrize(
    "source",
    [
        'def test_it():\n    """list_jobs lista os pedidos."""\n',
        'def test_it():\n    call("list_jobs")\n',
        'JOBS = f"{API}/jobs"\n\ndef test_it():\n    client.get(JOBS)\n',
        'def get():\n    return client.get("/admin/api/v1/jobs")\n\ndef test_it():\n    get()\n',
        'A = "/jobs"\nB = A\n\ndef test_it():\n    client.get(B)\n',  # constante de constante
    ],
)
def test_rule_3_the_citation_mentions_the_operation_by_id_by_path_or_through_what_it_uses(
    tmp_path: Path, source: str
) -> None:
    assert mentions(tmp_path, source, "list_jobs", "GET /jobs")


@pytest.mark.parametrize(
    "source",
    [
        "def test_it():\n    assert True\n",
        'def test_it():\n    client.get("/jobs-old")\n',  # outro caminho que começa igual
        'def test_it():\n    client.get("/members")\n',  # `/me` não está em `/members`
        'def other():\n    client.get("/jobs")\n\ndef test_it():\n    pass\n',  # outra função
        'def test_it():\n    call("list_jobs_all")\n',  # outro id que contém o nome
    ],
)
def test_rule_3_the_citation_that_does_not_mention_the_operation_does_not_pass(
    tmp_path: Path, source: str
) -> None:
    assert not mentions(tmp_path, source, "list_jobs", "GET /jobs")


def in_code(tmp_path: Path, source: str, operation_id: str, operation: str) -> bool:
    return citation_mentions(
        write_tests(tmp_path, source), operation_id, operation, tmp_path, id_in_code=True
    )


@pytest.mark.parametrize(
    "source",
    [
        'def test_it():\n    call("list_jobs")\n',  # um literal
        'def test_it():\n    client.get(url_of("list_jobs"))\n',  # o id num argumento
        "def test_it():\n    list_jobs(client)\n",  # um nome
        'OPERATION = "list_jobs"\n\ndef test_it():\n    call(OPERATION)\n',  # uma constante
        'def ask():\n    return call("list_jobs")\n\ndef test_it():\n    ask()\n',  # um auxiliar
        'def test_it():\n    """Lista."""\n    call(f"{x}/list_jobs/{y}")\n',  # dentro de um f-string
    ],
)
def test_rule_3_the_id_in_the_code_counts_as_a_mention_where_the_path_alone_does_not(
    tmp_path: Path, source: str
) -> None:
    assert in_code(tmp_path, source, "list_jobs", "GET /jobs")


@pytest.mark.parametrize(
    "source",
    [
        'def test_it():\n    client.get("/jobs")\n',  # só o caminho
        'def test_it():\n    """list_jobs lista os pedidos."""\n    client.get("/jobs")\n',  # docstring
        'def test_it():\n    client.get("/jobs")  # list_jobs\n',  # comentário
        'def test_it():\n    # list_jobs\n    client.get("/jobs")\n',
        'def test_list_jobs():\n    client.get("/jobs")\n',  # o nome do teste
        'def helper():\n    """list_jobs"""\n\ndef test_it():\n    helper()\n',  # docstring de auxiliar
        'def test_it():\n    call("list_jobs_all")\n',  # outro id que o contém
    ],
)
def test_rule_3_the_id_only_in_a_docstring_a_comment_or_a_name_is_not_in_the_code(
    tmp_path: Path, source: str
) -> None:
    assert not in_code(tmp_path, source, "list_jobs", "GET /jobs")


@pytest.mark.parametrize(
    ("operation", "others", "shared"),
    [
        (("GET", "/jobs"), [("GET", "/jobs/{public_id}")], True),  # prefixo
        (("GET", "/jobs/{public_id}"), [("GET", "/jobs/{public_id}/pdf")], True),
        (("GET", "/jobs/{public_id}/pdf"), [("GET", "/jobs/{public_id}")], False),  # é o maior
        (("GET", "/jobs"), [("GET", "/jobs-old"), ("GET", "/me")], False),  # fim de trecho
        (("GET", "/forms/{slug}/draft"), [("PUT", "/forms/{slug}/draft")], True),  # mesmo caminho
        (("GET", "/jobs"), [("GET", "/jobs")], False),  # ela mesma não conta
    ],
)
def test_rule_3_a_path_is_shared_when_it_is_the_prefix_of_another_operations(
    operation: tuple[str, str], others: list[tuple[str, str]], shared: bool
) -> None:
    assert path_is_shared(operation, others) is shared


def test_rule_3_a_path_that_is_the_prefix_of_another_needs_the_id_in_the_cited_test(
    matrix: Matrix, world: World
) -> None:
    """`/api-keys` é prefixo de `/api-keys/{key_id}/revoke`: um teste que só fala do caminho não diz
    qual das duas o teste exerce."""
    row = row_of(matrix, operation_id="list_api_keys")
    only_the_path = (
        "tests_app/test_admin_api_keys_api.py::test_the_list_never_carries_a_secret_nor_a_hash"
    )
    assert world.mentions(only_the_path, "list_api_keys", "GET /api-keys")  # pelo caminho, passa

    message = failing(matrix.replace(row, replace(row, test=only_the_path)), world)

    assert "o caminho de list_api_keys (GET /api-keys) é prefixo do de outra operação" in message
    assert "o `operation_id` precisa estar no código do teste" in message


def test_rule_3_the_operations_with_a_shared_path_of_the_matrix_cite_a_test_with_the_id_in_it(
    matrix: Matrix, world: World
) -> None:
    pairs = [(method, path) for method, path, _ in operations_of(world.document)]
    shared = {
        row.operation_id: row
        for row in matrix.rows
        if row.status == "feita" and row.operation_id and path_is_shared(row.operation_pair, pairs)
    }

    assert {"list_api_keys", "create_api_key"} <= set(shared)  # /api-keys
    assert {"list_jobs", "get_job"} <= set(shared)  # /jobs e /jobs/{public_id}
    assert "revoke_api_key" not in shared and "download_job_pdf" not in shared
    for operation_id, row in shared.items():
        assert row.test and row.operation
        assert citation_mentions(row.test, operation_id, row.operation, id_in_code=True), (
            operation_id
        )


def test_rule_3_every_static_part_of_the_path_must_appear(tmp_path: Path) -> None:
    only_the_resource = 'def test_it():\n    client.get(f"{API}/jobs/{public_id}")\n'
    the_whole = 'def test_it():\n    client.get(f"{API}/jobs/{public_id}/pdf")\n'

    assert not mentions(
        tmp_path, only_the_resource, "download_job_pdf", "GET /jobs/{public_id}/pdf"
    )
    assert mentions(tmp_path, the_whole, "download_job_pdf", "GET /jobs/{public_id}/pdf")
    assert mentions(
        tmp_path, 'def test_it():\n    client.get("/me")\n', "get_current_api_key", "GET /me"
    )
    assert not mentions(tmp_path, "def test_it():\n    pass\n", "get_current_api_key", "GET /me")


def test_rule_3_a_cycle_between_helpers_does_not_hang(tmp_path: Path) -> None:
    source = "def a():\n    b()\n\ndef b():\n    a()\n\ndef test_it():\n    a()\n"

    assert not mentions(tmp_path, source, "list_jobs", "GET /jobs")


def test_rule_3_every_citation_of_the_matrix_is_a_real_test(matrix: Matrix) -> None:
    cited = [row.test for row in matrix.rows if row.status == "feita"]

    assert len(cited) >= 4  # as quatro das chaves; cada fase de recurso cita as suas
    assert all(citation_exists(test) for test in cited if test)
    assert not citation_exists("tests_app/test_admin_api_keys_api.py::nao_e_um_teste")
    assert not citation_exists("docs/api/cobertura.md::test_x")  # só código Python


def test_rule_4_a_fora_row_with_a_letter_that_does_not_exist_fails(
    matrix: Matrix, world: World
) -> None:
    row = row_of(matrix, route="GET /admin/login")

    message = failing(matrix.replace(row, replace(row, letter="Z")), world)

    assert "a letra (Z) de `fora` não está em «O que não entra na API» (A, B, C, D)" in message


def test_rule_4_the_set_of_fora_rows_is_locked_to_the_thirteen_of_today(matrix: Matrix) -> None:
    outside = {row.key: row.letter for row in matrix.rows if row.status == "fora"}

    assert outside == OUT_OF_THE_API
    assert len(OUT_OF_THE_API) == 13


def test_rule_4_a_new_fora_row_demands_editing_the_locked_set(matrix: Matrix, world: World) -> None:
    row = route_done_alone(matrix)
    out = replace(
        row,
        status="fora",
        letter="A",
        operation=None,
        operation_id=None,
        role=None,
        marks=frozenset(),
        test=None,
    )

    message = failing(matrix.replace(row, out), world)

    assert (
        f"{row.route}" in message and "OUT_OF_THE_API" in message and "`fora (A)` novo" in message
    )


def test_rule_4_a_fora_row_that_left_the_set_must_leave_the_constant(
    matrix: Matrix, world: World
) -> None:
    row = row_of(matrix, route="GET /admin/forms/import")
    back = replace(
        row,
        status="falta",
        letter=None,
        operation="POST /forms/import",
        operation_id="import_form",
        role="total",
    )

    message = failing(matrix.replace(row, back), world)

    assert (
        "OUT_OF_THE_API lista GET /admin/forms/import, que a matriz não tem mais como `fora`"
        in (message)
    )


def test_rule_4_the_letter_of_a_fora_row_is_the_one_of_the_locked_set(
    matrix: Matrix, world: World
) -> None:
    row = row_of(matrix, route="GET /admin/login")

    message = failing(matrix.replace(row, replace(row, letter="B")), world)

    assert "a letra de `fora` é B, e OUT_OF_THE_API diz A" in message


def test_rule_4_the_exceptions_section_cannot_be_empty(matrix: Matrix, world: World) -> None:
    emptied = Matrix(matrix.rows, frozenset(), matrix.editor_counts)

    assert "não lista nenhuma letra" in failing(emptied, world)


def test_rule_5_an_operation_in_the_file_without_a_row_fails(matrix: Matrix, world: World) -> None:
    world.document["paths"]["/forms/{slug}/archive"] = {
        "post": {"operationId": "archive_form", "x-required-role": "total"}
    }

    message = failing(matrix, world)

    assert "a operação archive_form (POST /forms/{slug}/archive)" in message
    assert "não tem linha na matriz" in message and MATRIX_FILE in message


def test_rule_5_an_operation_that_only_a_new_operations_row_covers_is_covered(
    matrix: Matrix, world: World
) -> None:
    """`get_current_api_key` não vem de uma rota do admin: é a tabela das operações novas que a
    cobre."""
    assert row_of(matrix, operation_id="get_current_api_key").kind == NEW
    assert problems(matrix, world) == []


def test_rule_6_an_editor_that_gained_an_operation_without_a_row_fails(
    matrix: Matrix, world: World
) -> None:
    world.editors["PlanEdit"] = (*world.editors["PlanEdit"], "plan-clone")

    message = failing(matrix, world)

    assert "o editor PlanEdit declara a operação plan-clone, que a matriz não lista" in message
    assert MATRIX_FILE in message


def test_rule_6_a_listed_operation_the_editor_does_not_declare_fails(
    matrix: Matrix, world: World
) -> None:
    world.editors["DataEdit"] = world.editors["DataEdit"][:-1]

    assert "lista a operação data-down-window para DataEdit, que o editor não declara" in failing(
        matrix, world
    )


def test_rule_6_the_count_in_the_section_title_must_match_the_table(
    matrix: Matrix, world: World
) -> None:
    real = matrix.editor_counts["ScoringEdit"]
    padded = Matrix(
        matrix.rows, matrix.exceptions, {**matrix.editor_counts, "ScoringEdit": real + 1}
    )

    expected = f"o título da seção de ScoringEdit diz {real + 1} operações e a tabela tem {real}"
    assert expected in failing(padded, world)


def test_rule_6_an_operation_listed_twice_fails(matrix: Matrix, world: World) -> None:
    again = replace(next(row for row in matrix.of_kind(EDITOR) if row.editor == "DataEdit"), line=1)

    assert "DataEdit: a operação data-add aparece duas vezes" in failing(
        matrix.with_row(again), world
    )


def test_rule_6_the_names_are_matched_without_the_numbers_the_table_writes(
    matrix: Matrix,
) -> None:
    names = {row.name: row.editor_operation for row in matrix.of_kind(EDITOR)}

    assert names["remove-step:i"] == "remove-step"
    assert names["add-condition:i:j[:k]"] == "add-condition"
    assert names["plan-add-field:<endereço>"] == "plan-add-field"
    assert names["scoring-remove-rule:<lista>:i"] == "scoring-remove-rule"


def test_rule_7_the_coverage_is_closed_and_no_row_of_the_matrix_is_falta(matrix: Matrix) -> None:
    assert MISSING_ROWS == 0
    assert matrix.missing == 0
    assert all(row.status in ("feita", "fora") for row in matrix.rows)


def test_rule_7_a_matrix_with_one_falta_row_fails_and_says_the_coverage_is_closed(
    matrix: Matrix, world: World
) -> None:
    done = next(row for row in matrix.rows if row.status == "feita" and row.kind == ROUTES)
    worse = matrix.replace(done, replace(done, status="falta", test=None))

    message = failing(worse, world)

    assert "há 1 linha `falta`" in message and "a cobertura está fechada" in message
    assert MATRIX_FILE in message


def test_rule_7_a_new_admin_route_with_a_falta_row_fails_too(matrix: Matrix, world: World) -> None:
    """Quem traz a rota nova com uma linha `falta` (para fazer a operação depois) não passa: a rota tem
    linha, mas a cobertura está fechada."""
    route = "POST /admin/forms/{slug}/archive"
    world.routes.add(("POST", "/admin/forms/{slug}/archive"))
    world.schema_routes.add(("POST", "/admin/forms/{slug}/archive"))
    planned = Row(
        kind=ROUTES,
        line=1,
        section="Formulários",
        status="falta",
        route=route,
        operation="POST /forms/{slug}/archive",
        operation_id="archive_form",
        role="total",
    )

    message = failing(matrix.with_row(planned), world)

    assert "há 1 linha `falta`" in message and "a cobertura está fechada" in message
    assert "não tem linha" not in message  # a rota tem linha: é a linha que está aberta


def test_rule_7_a_new_admin_route_without_a_row_fails_on_rule_1_whatever_the_ratchet(
    matrix: Matrix, world: World
) -> None:
    world.routes.add(("POST", "/admin/forms/{slug}/archive"))
    world.schema_routes.add(("POST", "/admin/forms/{slug}/archive"))

    message = failing(matrix, world)

    assert "POST /admin/forms/{slug}/archive existe no app e não tem linha" in message


def test_the_ratchet_constant_is_the_number_of_falta_rows_of_the_matrix(matrix: Matrix) -> None:
    assert matrix.missing == MISSING_ROWS


def test_the_openapi_has_exactly_the_operations_of_the_closed_matrix(
    matrix: Matrix, world: World
) -> None:
    """Fechada a cobertura, o `openapi.json` e a matriz dizem as mesmas operações: nem uma a mais no
    arquivo, nem uma `feita` que o arquivo não tenha. A conta da matriz é a dos totais (as operações das
    rotas do admin, uma por operação mesmo quando duas rotas a dividem, mais as novas)."""
    in_the_matrix = {
        row.operation_id
        for row in matrix.rows
        if row.operation_id and row.kind in (ROUTES, NEW) and row.status == "feita"
    }
    in_the_file = {operation["operationId"] for _, _, operation in operations_of(world.document)}

    assert in_the_file == in_the_matrix
    assert len(in_the_file) == len(operations_of(world.document))  # nenhum `operation_id` repetido
    assert world.document["info"]["version"] == f"1.{len(in_the_file)}.0"


# --- a gramática da matriz ----------------------------------------------------------------------

MINI = """\
## Inventário

### Pedidos

| Rota atual | O que faz | Serviço hoje | Operação da API | `operation_id` | Papel | Marcas | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `GET /admin/jobs` | Lista | s | `GET /jobs` | `list_jobs` | leitura | lead | falta |
| `POST /admin/jobs/{id}/reprocess` | Refaz | s | `POST /jobs/{id}/reprocess` | `reprocess_job` | total | destrutiva, externo | feita · tests_app/test_x.py::test_y |
| `GET /admin/login` | Entra | s | — | — | — | — | fora (A) |

### Etapas (`FormEdit`, 1)

| Operação | O que faz | Equivalente no documento | Status |
| --- | --- | --- | --- |
| `add-step:i` | Inclui | `add /steps/-` | falta |

## Operações novas

| `operation_id` | Operação da API | O que faz | Papel | Marcas | Status |
| --- | --- | --- | --- | --- | --- |
| `list_audit_log` | `GET /audit-log` | Lê | leitura | — | falta |

## O que não entra na API e por quê

- **(A) Login** (1 rota).
"""


def test_the_grammar_reads_the_three_kinds_of_table_the_status_and_the_exceptions() -> None:
    matrix = parse(MINI)

    jobs, reprocess, login, step, audit = matrix.rows
    assert (jobs.kind, jobs.status, jobs.route_pair) == (ROUTES, "falta", ("GET", "/admin/jobs"))
    assert (jobs.operation_pair, jobs.operation_id, jobs.role) == (
        ("GET", "/jobs"),
        "list_jobs",
        "leitura",
    )
    assert jobs.marks == {"x-lead-data"}
    assert reprocess.marks == {"x-destructive", "x-external-effect"}
    assert (reprocess.status, reprocess.test) == ("feita", "tests_app/test_x.py::test_y")
    assert (login.status, login.letter, login.operation, login.marks) == ("fora", "A", None, set())
    assert (step.kind, step.editor, step.editor_operation) == (EDITOR, "FormEdit", "add-step")
    assert (audit.kind, audit.operation_id, audit.status) == (NEW, "list_audit_log", "falta")
    assert matrix.exceptions == {"A"} and matrix.editor_counts == {"FormEdit": 1}
    assert matrix.missing == 3


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("| falta |\n| `POST", "| a |\n| `POST", "o status é `falta`"),
        ("· tests_app/test_x.py::test_y", "· tests_app/test_x.py", "pede `· arquivo::teste`"),
        ("fora (A)", "fora", "`fora` pede a letra"),
        ("destrutiva, externo", "destrutiva, caro", "marca desconhecida"),
        ("| leitura | lead | falta |", "| leitura | falta |", "7 colunas"),
    ],
)
def test_a_table_that_breaks_the_grammar_is_an_error_that_names_the_line(
    old: str, new: str, message: str
) -> None:
    with pytest.raises(MatrixError, match=message) as error:
        parse(MINI.replace(old, new))

    assert "docs/api/cobertura.md:" in str(error.value)


def test_the_tables_that_are_not_the_matrix_are_not_read() -> None:
    assert parse("| Recurso | Rotas |\n| --- | ---: |\n| Pedidos | 6 |\n").rows == ()
