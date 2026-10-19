"""As operações que cada editor estruturado declara (`OPERATIONS`).

O teste de cobertura da API de admin (`test_admin_api_coverage.py`) cobra uma linha da matriz
(`docs/api/cobertura.md`) para cada operação dos quatro editores. Para isso ele lê a lista que cada
editor declara, em vez de procurar o nome das operações no código com expressão regular. Os editores
das etapas e da pontuação tiram a lista da tabela que o `apply` consulta. Os do plano e dos dados
tratam o `op` por expressão regular, e montam as expressões e a lista das mesmas tuplas de verbos:
um verbo novo só entra no `apply` por elas, e então aparece na lista. Aqui está a prova de que cada
nome declarado é uma operação que o `apply` executa.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.admin.data_editor import BlockEdit, DataEdit, WindowEdit
from app.admin.form_editor import FormEdit
from app.admin.plan_editor import NodeEdit, PlanEdit, RuleEdit
from app.admin.scoring_editor import ScoringEdit


def test_the_four_editors_declare_their_operations_without_repeating_one() -> None:
    editors = (FormEdit, PlanEdit, DataEdit, ScoringEdit)

    assert all(editor.OPERATIONS for editor in editors)
    declared = [name for editor in editors for name in editor.OPERATIONS]
    assert len(declared) == len(set(declared))  # quantas são, quem diz é a matriz


def test_the_names_are_the_ones_of_the_op_field_without_the_numbers() -> None:
    assert {"add-step", "up-question", "refresh-operator", "remove-condition"} <= set(
        FormEdit.OPERATIONS
    )
    assert {"scoring-add-score", "scoring-down-rule", "scoring-refresh-rule"} <= set(
        ScoringEdit.OPERATIONS
    )
    assert all(":" not in name for editor in (FormEdit, ScoringEdit) for name in editor.OPERATIONS)


# Uma operação de cada nome, escrita como a página a manda, e o estado em que ela vale.


def plan() -> PlanEdit:
    return PlanEdit(
        fields=[NodeEdit(name="titulo"), NodeEdit(name="resumo")], rules=[RuleEdit(), RuleEdit()]
    )


PLAN_SAMPLES = {
    "plan-add-field": "plan-add-field:plan",
    "plan-remove": "plan-remove:plan.fields.1",
    "plan-up": "plan-up:plan.fields.1",
    "plan-down": "plan-down:plan.fields.0",
    "plan-refresh": "plan-refresh:plan.fields.0",
    "plan-add-rule": "plan-add-rule",
    "plan-remove-rule": "plan-remove-rule:0",
}


def data() -> DataEdit:
    return DataEdit(
        blocks=[
            BlockEdit(name="lista"),
            BlockEdit(
                name="janelas", kind="windows", windows=[WindowEdit(name="a"), WindowEdit(name="b")]
            ),
        ]
    )


DATA_SAMPLES = {
    "data-add": "data-add",
    "data-remove": "data-remove:0",
    "data-refresh": "data-refresh:1",
    "data-add-window": "data-add-window:1",
    "data-remove-window": "data-remove-window:1:0",
    "data-up-window": "data-up-window:1:1",
    "data-down-window": "data-down-window:1:0",
}


def test_every_sample_names_one_declared_operation_and_the_other_way_round() -> None:
    assert set(PLAN_SAMPLES) == set(PlanEdit.OPERATIONS)
    assert set(DATA_SAMPLES) == set(DataEdit.OPERATIONS)


@pytest.mark.parametrize(
    ("make", "name", "op"),
    [(plan, name, op) for name, op in PLAN_SAMPLES.items()]
    + [(data, name, op) for name, op in DATA_SAMPLES.items()],
)
def test_each_declared_operation_is_one_the_editor_applies(
    make: Callable[[], PlanEdit | DataEdit], name: str, op: str
) -> None:
    assert op.split(":")[0] == name

    focus = make().apply(op)

    assert focus, f"{op}: o editor não executou a operação que declara"


def test_an_operation_the_editor_does_not_declare_does_nothing() -> None:
    assert plan().apply("plan-clone:plan.fields.0") is None
    assert data().apply("data-clone:0") is None
