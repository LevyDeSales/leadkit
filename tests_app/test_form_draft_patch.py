"""O JSON Patch (RFC 6902) sobre o documento do rascunho (`app.forms.draft_patch`): aplica numa cópia,
é tudo ou nada e, quando uma operação não se aplica, diz qual (`patch.<n>`) sem repetir o que recebeu."""

from __future__ import annotations

import copy
import json
import secrets
import time
from typing import Any

import pytest

from app.api.main import MAX_BODY_BYTES
from app.forms import draft_patch
from app.forms.definition import DefinitionError

DOCUMENT: dict[str, Any] = {
    "title": "Diagnóstico",
    "steps": [{"title": "Um"}, {"title": "Dois"}, {"title": "Três"}],
    "plan": {"fields": [{"name": "titulo"}]},
}


def refused(operations: list[dict[str, Any]]) -> DefinitionError:
    with pytest.raises(DefinitionError) as error:
        draft_patch.apply(DOCUMENT, operations)
    return error.value


def test_every_operation_of_the_rfc_applies() -> None:
    patched = draft_patch.apply(
        DOCUMENT,
        [
            {"op": "replace", "path": "/title", "value": "Novo"},
            {"op": "add", "path": "/steps/-", "value": {"title": "Quatro"}},
            {"op": "add", "path": "/steps/0", "value": {"title": "Zero"}},
            {"op": "remove", "path": "/plan/fields/0"},
            {"op": "move", "from": "/steps/0", "path": "/steps/2"},
            {"op": "copy", "from": "/title", "path": "/plan/description"},
            {"op": "test", "path": "/title", "value": "Novo"},
        ],
    )

    assert patched["title"] == "Novo"
    assert [step["title"] for step in patched["steps"]] == ["Um", "Dois", "Zero", "Três", "Quatro"]
    assert patched["plan"] == {"fields": [], "description": "Novo"}


def test_the_document_of_the_caller_is_never_touched() -> None:
    before = copy.deepcopy(DOCUMENT)

    draft_patch.apply(DOCUMENT, [{"op": "remove", "path": "/steps/0"}])
    refused([{"op": "remove", "path": "/steps/0"}, {"op": "remove", "path": "/nao-existe"}])

    assert DOCUMENT == before


def test_a_value_that_is_null_is_a_value() -> None:
    patched = draft_patch.apply(DOCUMENT, [{"op": "add", "path": "/scoring", "value": None}])

    assert patched["scoring"] is None


def test_moving_a_step_up_and_down_is_a_move_to_the_neighbouring_index() -> None:
    up = draft_patch.apply(DOCUMENT, [{"op": "move", "from": "/steps/1", "path": "/steps/0"}])
    down = draft_patch.apply(DOCUMENT, [{"op": "move", "from": "/steps/1", "path": "/steps/2"}])

    assert [step["title"] for step in up["steps"]] == ["Dois", "Um", "Três"]
    assert [step["title"] for step in down["steps"]] == ["Um", "Três", "Dois"]


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "remove", "path": "/nao-existe"},
        {"op": "remove", "path": "/steps/9"},
        {"op": "add", "path": "/steps/9", "value": {}},
        {"op": "add", "path": "/nao-existe/filho", "value": 1},
        {"op": "replace", "path": "/nao-existe", "value": 1},
        {"op": "move", "from": "/nao-existe", "path": "/title"},
        {"op": "copy", "from": "/nao-existe", "path": "/outro"},
        {"op": "move", "from": "/plan", "path": "/plan/fields/0/dentro"},
        {"op": "add", "path": "/steps/-1", "value": {}},
        {"op": "remove", "path": "/steps/-"},  # o `-` só vale em `add`
        {"op": "add", "path": "/title/filho", "value": 1},  # o título é um texto
        {"op": "add", "path": "steps", "value": {}},  # sem a barra inicial
    ],
)
def test_an_operation_that_does_not_apply_is_the_problem_of_its_position(
    operation: dict[str, Any],
) -> None:
    error = refused([{"op": "replace", "path": "/title", "value": "ok"}, operation])

    (problem,) = error.problems
    assert problem.path == "patch.1" and problem.message == draft_patch.NOT_APPLICABLE


def test_a_test_that_fails_is_the_problem_of_its_position() -> None:
    error = refused([{"op": "test", "path": "/title", "value": "Outro"}])

    (problem,) = error.problems
    assert problem.path == "patch.0" and problem.message == draft_patch.TEST_FAILED


def test_an_invalid_operation_is_the_problem_of_its_position() -> None:
    error = refused([{"op": "replace", "path": "/title", "value": "ok"}, {"path": "/title"}])
    unknown = refused([{"op": "apague", "path": "/title"}])

    assert [problem.path for problem in error.problems] == ["patch.1"]
    assert [problem.path for problem in unknown.problems] == ["patch.0"]
    assert error.problems[0].message == draft_patch.INVALID


def test_the_first_operation_that_fails_stops_the_patch() -> None:
    error = refused(
        [
            {"op": "remove", "path": "/nao-existe"},
            {"op": "remove", "path": "/tambem-nao-existe"},
        ]
    )

    assert [problem.path for problem in error.problems] == ["patch.0"]


def test_what_was_sent_never_comes_back_in_the_message() -> None:
    """A biblioteca cita o documento (`member 'x' not found in {...}`); a mensagem é fixa."""
    sentinel = f"sentinela-{secrets.token_hex(8)}"
    operations = [
        {"op": "remove", "path": f"/{sentinel}"},
        {"op": "replace", "path": f"/{sentinel}", "value": sentinel},
        {"op": "test", "path": "/title", "value": sentinel},
        {"op": "move", "from": f"/{sentinel}", "path": "/title"},
        {"op": "add", "path": f"/{sentinel}/filho", "value": sentinel},
        {"op": sentinel, "path": "/title"},
    ]

    for operation in operations:
        (problem,) = refused([operation]).problems
        assert sentinel not in problem.message and "Diagnóstico" not in problem.message


def test_replacing_the_root_can_leave_what_is_not_an_object_for_the_caller_to_check() -> None:
    assert draft_patch.apply(DOCUMENT, [{"op": "replace", "path": "", "value": [1]}]) == [1]


# --- o zero à esquerda de um índice (RFC 6901) ---------------------------------------------------


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "add", "path": "/steps/01", "value": {}},
        {"op": "remove", "path": "/steps/007"},
        {"op": "replace", "path": "/steps/00/title", "value": "x"},
        {"op": "test", "path": "/01", "value": 1},
        {"op": "move", "from": "/steps/01", "path": "/steps/0"},
        {"op": "copy", "from": "/steps/0", "path": "/steps/02"},
    ],
    ids=["add", "remove", "replace", "test", "move-from", "copy-path"],
)
def test_an_index_with_a_leading_zero_is_invalid_in_path_and_in_from(
    operation: dict[str, Any],
) -> None:
    """O RFC 6901 não aceita `01`, e a `jsonpointer` 3.0.0 o lê como o índice 1 (a 3.2.1 o recusa): o
    aplicador recusa antes de a biblioteca ver, para o resultado não depender da versão dela."""
    error = refused([{"op": "replace", "path": "/title", "value": "ok"}, operation])

    (problem,) = error.problems
    assert problem.path == "patch.1" and problem.message == draft_patch.INVALID


@pytest.mark.parametrize(
    "pointer", ["/steps/0", "/steps/10", "/steps/0/title", "/0", "/a0", "/a/0b", "/x/100/y", ""]
)
def test_a_zero_alone_or_inside_a_name_or_after_another_digit_is_not_a_leading_zero(
    pointer: str,
) -> None:
    assert draft_patch.LEADING_ZERO.search(pointer) is None


# --- a versão do texto do consentimento é do sistema ---------------------------------------------

WITH_CONSENT = {**DOCUMENT, "consent": {"version": "demo-v1", "notice": "Aviso."}}
GUARD = {"op": "test", "path": "/title", "value": "Diagnóstico"}


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "replace", "path": "/consent/version", "value": "outra"},
        {"op": "add", "path": "/consent/version", "value": "outra"},
        {"op": "remove", "path": "/consent/version"},
        {"op": "move", "from": "/consent/notice", "path": "/consent/version"},
        {"op": "copy", "from": "/consent/notice", "path": "/consent/version"},
        {"op": "move", "from": "/consent/version", "path": "/consent/antiga"},
    ],
    ids=["replace", "add", "remove", "move-to", "copy-to", "move-from"],
)
def test_an_operation_that_writes_the_consent_version_is_refused_by_position(
    operation: dict[str, Any],
) -> None:
    with pytest.raises(DefinitionError) as error:
        draft_patch.apply(WITH_CONSENT, [GUARD, operation])

    (problem,) = error.value.problems
    assert problem.path == "patch.1" and problem.message == draft_patch.SYSTEM_PATH


def test_the_consent_version_can_be_tested_and_copied_from_and_the_consent_replaced() -> None:
    patched = draft_patch.apply(
        WITH_CONSENT,
        [
            {"op": "test", "path": "/consent/version", "value": "demo-v1"},
            {"op": "copy", "from": "/consent/version", "path": "/rotulo"},
            {"op": "replace", "path": "/consent/notice", "value": "Outro aviso."},
        ],
    )

    assert patched["rotulo"] == "demo-v1" and patched["consent"]["notice"] == "Outro aviso."


# --- copy: quantos, e quanto cresce ---------------------------------------------------------------


def test_a_patch_has_at_most_twenty_copy_operations() -> None:
    copies = [{"op": "copy", "from": "/title", "path": f"/titulo{n}"} for n in range(21)]

    assert draft_patch.apply(DOCUMENT, copies[:20])
    with pytest.raises(DefinitionError) as error:
        draft_patch.apply(DOCUMENT, copies)

    (problem,) = error.value.problems
    assert problem.path == "patch.20" and problem.message == draft_patch.TOO_MANY_COPIES


def test_five_hundred_copy_and_remove_pairs_stop_at_the_twenty_first_copy_in_a_blink() -> None:
    """Sem o teto de `copy`, cada par copiaria um pedaço do documento e o tiraria, sem nunca passar do
    tamanho: quinhentos pares seriam quinhentas cópias e quinhentas contas do tamanho."""
    big = {"steps": [{"title": "x" * 1000} for _ in range(100)]}  # cerca de 100 KB
    pairs: list[dict[str, Any]] = []
    for _ in range(500):
        pairs += [
            {"op": "copy", "from": "/steps", "path": "/copia"},
            {"op": "remove", "path": "/copia"},
        ]

    started = time.perf_counter()
    with pytest.raises(DefinitionError) as error:
        draft_patch.apply(big, pairs)
    elapsed = time.perf_counter() - started

    (problem,) = error.value.problems
    assert problem.path == "patch.40" and problem.message == draft_patch.TOO_MANY_COPIES
    assert elapsed < 1.5, f"{elapsed:.2f}s"  # 20 cópias de 100 KB e 21 contas: milissegundos


def test_a_patch_cannot_double_the_document_into_itself_past_what_a_put_takes() -> None:
    """`copy` da caixa para dentro dela dobra o documento a cada operação."""
    big = {"caixa": [{"title": "x" * 1000} for _ in range(20)]}  # cerca de 20 KB
    doubling = [{"op": "copy", "from": "/caixa", "path": "/caixa/-"}] * 20

    with pytest.raises(DefinitionError) as error:
        draft_patch.apply(big, doubling)

    (problem,) = error.value.problems
    assert problem.message == draft_patch.TOO_BIG and problem.path == "patch.3"
    assert draft_patch.apply(big, doubling[:3])


# --- o que cabe num PUT -----------------------------------------------------------------------------


def test_the_limit_of_the_document_is_the_body_of_the_api_minus_the_envelope_of_the_put() -> None:
    assert draft_patch.BODY_LIMIT_BYTES == MAX_BODY_BYTES
    assert draft_patch.MAX_DOCUMENT_BYTES + draft_patch.PUT_ENVELOPE_BYTES == MAX_BODY_BYTES
    envelope = json.dumps({"expected_version": "f" * 64, "document": None})
    assert len(envelope) < draft_patch.PUT_ENVELOPE_BYTES  # e a folga é de sobra


def escaped_size(document: dict[str, Any]) -> int:
    """Os bytes do `json.dumps` padrão: acentos escapados e um espaço depois de `,` e de `:`."""
    return len(json.dumps(document))


def test_the_document_is_measured_with_the_accents_escaped_as_a_put_may_carry_it() -> None:
    accents = (
        draft_patch.MAX_DOCUMENT_BYTES - escaped_size({"note": ""})
    ) // 6  # `é` escapado: 6 bytes
    at_the_limit = {"note": "é" * accents}
    one_more = {"note": "é" * (accents + 1)}
    assert escaped_size(at_the_limit) <= draft_patch.MAX_DOCUMENT_BYTES < escaped_size(one_more)
    # Em UTF-8, sem escape, o mesmo documento ocupa um terço: a conta pelo pior caso é a que vale.
    assert len(json.dumps(one_more, ensure_ascii=False).encode()) < escaped_size(one_more) // 2

    assert draft_patch.apply(at_the_limit, [{"op": "replace", "path": "/note", "value": "é" * 10}])
    with pytest.raises(DefinitionError) as error:
        draft_patch.apply({"note": ""}, [{"op": "add", "path": "/note", "value": one_more["note"]}])

    (problem,) = error.value.problems
    assert problem.path == "patch" and problem.message == draft_patch.TOO_BIG


def test_a_document_that_was_already_past_the_limit_may_shrink_or_stay_but_not_grow() -> None:
    """Um `PUT` com os acentos sem escape cabe no corpo e passa do teto escapado: o patch não é
    culpado do que já estava lá."""
    over = {"note": "é" * (draft_patch.MAX_DOCUMENT_BYTES // 5)}  # 2 bytes em UTF-8, 6 escapado
    assert escaped_size(over) > draft_patch.MAX_DOCUMENT_BYTES

    assert draft_patch.apply(over, [{"op": "test", "path": "/note", "value": over["note"]}])
    assert draft_patch.apply(over, [{"op": "replace", "path": "/note", "value": "curta"}])
    with pytest.raises(DefinitionError):
        draft_patch.apply(over, [{"op": "add", "path": "/outra", "value": "é" * 5000}])


# --- aninhamento ------------------------------------------------------------------------------------


def nested(levels: int) -> Any:
    """Listas dentro de listas, `levels` níveis, sem recursão."""
    value: Any = []
    for _ in range(levels - 1):
        value = [value]
    return value


def test_depth_of_counts_the_levels_of_lists_and_objects_without_recursing() -> None:
    assert draft_patch.depth_of("texto") == 0 and draft_patch.depth_of(7) == 0
    assert draft_patch.depth_of([]) == 1 and draft_patch.depth_of({"a": [1, {"b": 2}]}) == 3
    assert draft_patch.depth_of(nested(64)) == 64
    assert draft_patch.depth_of(nested(65)) == 65  # para ao passar do teto
    assert draft_patch.depth_of(nested(200_000)) > draft_patch.MAX_VALUE_DEPTH  # e não estoura


def test_a_document_nested_past_what_json_walks_is_a_problem_and_not_a_crash() -> None:
    deep: Any = nested(5000)

    with pytest.raises(DefinitionError) as error:
        draft_patch.apply({"a": deep}, [{"op": "test", "path": "/b", "value": 1}])

    (problem,) = error.value.problems
    assert problem.path == "patch" and problem.message == draft_patch.TOO_DEEP


def test_a_recursion_error_inside_an_operation_is_the_problem_of_that_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Se a biblioteca estoura a pilha (uma cópia profunda de um valor aninhado), o problema é da
    operação, e nunca um 500."""

    def overflow(*args: Any, **kwargs: Any) -> Any:
        raise RecursionError

    monkeypatch.setattr(draft_patch.jsonpatch, "apply_patch", overflow)

    with pytest.raises(DefinitionError) as error:
        draft_patch.apply(DOCUMENT, [GUARD, {"op": "add", "path": "/x", "value": 1}])

    (problem,) = error.value.problems
    assert problem.path == "patch.0" and problem.message == draft_patch.TOO_DEEP
