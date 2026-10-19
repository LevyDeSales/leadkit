"""A compilação que os testes dividem (`compile_each_definition_once`, no `conftest.py`).

`compile_definition` custa uns 7 ms e a suíte a chama uns 9000 vezes para as mesmas definições, então
os testes compilam cada uma uma vez por processo. Estes testes provam que isso não muda o que um teste
vê: a mesma definição dá o mesmo contrato, uma diferente dá outro, quem não compila reprova sempre, e o
relógio de um teste não fica no contrato de outro.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime

import conftest
import pytest
from conftest import _COMPILED, legacy_definition

from app.forms import stored
from app.forms.definition import DefinitionError, compile_definition
from app.forms.definition import compiler as definition_compiler


def test_every_module_that_imported_the_compiler_by_name_uses_the_shared_one() -> None:
    assert hasattr(compile_definition, "__wrapped__")
    assert stored.compile_definition is compile_definition
    assert conftest.compile_definition is compile_definition  # o `legacy_spec` também
    assert definition_compiler.compile_definition is compile_definition


def test_the_same_definition_compiles_to_the_same_contract() -> None:
    document = legacy_definition("demo")

    first = compile_definition(document)
    again = compile_definition(copy.deepcopy(document))
    as_text = compile_definition(json.dumps(document, ensure_ascii=False))

    assert first is again
    assert first.slug == "demo" and first.version_number == 1
    assert as_text.slug == "demo"  # o texto tem a sua entrada, e o mesmo resultado


def test_another_definition_or_another_version_is_another_contract() -> None:
    document = legacy_definition("demo")
    changed = copy.deepcopy(document)
    changed["title"] = "Outro título"

    original = compile_definition(document)

    assert compile_definition(changed) is not original
    assert compile_definition(changed).title == "Outro título"
    assert compile_definition(document, version_number=2) is not original
    assert compile_definition(document, version_number=2).version_number == 2


def test_a_definition_that_does_not_compile_fails_every_time_and_is_not_kept() -> None:
    broken = legacy_definition("demo")
    broken["slug"] = "Não é um endereço"
    kept = len(_COMPILED)

    for _ in range(2):
        with pytest.raises(DefinitionError) as error:
            compile_definition(broken)
        assert error.value.problems

    assert len(_COMPILED) == kept


def test_a_clock_of_the_test_gets_its_own_contract() -> None:
    document = legacy_definition("demo")
    shared = compile_definition(document)

    def clock() -> datetime:
        return datetime(2030, 1, 1, tzinfo=UTC)

    own = compile_definition(document, clock=clock)

    assert own is not shared
    assert compile_definition(document, clock=clock) is not own  # com relógio, sempre compila
