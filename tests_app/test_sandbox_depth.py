"""O template que aninha fundo demais é um problema do template, com o nome dele, e não um erro nosso.

O Jinja lê e compila por recursão (um parêntese dentro de outro, um bloco dentro de outro, uma cadeia de
filtros), e o Python limita a indentação (100 níveis) e os blocos de repetição (20) do código que o Jinja
gera: um template assim estourava como `RecursionError`, `IndentationError` ou `SyntaxError`, que ninguém
tratava, e a página e a API respondiam 500. Agora a construção do `SandboxTemplate` os trata como o
`TemplateProblem` que ela já levanta para a sintaxe quebrada, e o `compile_definition` e as prévias, como
um problema do campo.
"""

from __future__ import annotations

import json
import time

import pytest
from test_admin_forms import DEFINITION

from app.forms.definition import DefinitionError, compile_definition
from app.forms.sandbox import SandboxTemplate, TemplateProblem

TOO_DEEP = "fundo demais"
SECRET = "texto-que-nao-pode-voltar"
DEEP = {
    "parens-4000": "{{ " + "(" * 4000 + "1" + ")" * 4000 + " }}",
    "parens-80": "{{ " + "(" * 80 + "1" + ")" * 80 + " }}",
    "ifs-3000": "{% if x %}" * 3000 + SECRET + "{% endif %}" * 3000,
    "ifs-500": "{% if x %}" * 500 + "a" + "{% endif %}" * 500,
    "ifs-150": "{% if x %}" * 150 + "a" + "{% endif %}" * 150,  # IndentationError do código gerado
    "fors-25": "{% for i in x %}" * 25 + "a" + "{% endfor %}" * 25,  # SyntaxError: blocos demais
    "lists-3000": "{{ " + "[" * 3000 + "]" * 3000 + " }}",
    "filters-3000": "{{ x" + "|abs" * 3000 + " }}",
    "sums-5000": "{{ 1" + "+1" * 5000 + " }}",
    "attributes-3000": "{{ x" + ".a" * 3000 + " }}",
}
FINE = {
    "parens-20": "{{ " + "(" * 20 + "1" + ")" * 20 + " }}",
    "ifs-40": "{% if true %}" * 40 + "ok" + "{% endif %}" * 40,
    "fors-10": "{% for i in [1] %}" * 10 + "ok" + "{% endfor %}" * 10,
    "filters-50": "{{ 1" + "|abs" * 50 + " }}",
}
DOCUMENT = json.loads(DEFINITION)


@pytest.mark.parametrize("html", [True, False])
@pytest.mark.parametrize("source", list(DEEP.values()), ids=list(DEEP))
def test_a_template_too_deep_is_a_problem_of_the_template_and_not_an_error(
    source: str, html: bool
) -> None:
    with pytest.raises(TemplateProblem) as problem:
        SandboxTemplate(source, name="pdf.html", html=html)

    assert problem.value.name == "pdf.html" and problem.value.line is None
    assert TOO_DEEP in problem.value.detail
    assert SECRET not in str(problem.value)  # a mensagem nunca cita o texto do template


@pytest.mark.parametrize("source", list(FINE.values()), ids=list(FINE))
def test_a_template_with_a_reasonable_depth_still_compiles_and_renders(source: str) -> None:
    template = SandboxTemplate(source, name="pdf.html", html=True)

    assert template.render({}) in ("1", "ok")


PICKED = {name: DEEP[name] for name in ("parens-4000", "ifs-3000", "fors-25")}


@pytest.mark.parametrize("where", ["prompt", "pdf.html", "preview"])
@pytest.mark.parametrize("source", list(PICKED.values()), ids=list(PICKED))
def test_a_definition_with_a_template_too_deep_does_not_compile_and_names_the_field(
    where: str, source: str
) -> None:
    document = json.loads(json.dumps(DOCUMENT))
    if where == "pdf.html":
        document["pdf"]["html"] = source
    else:
        document[where] = source

    with pytest.raises(DefinitionError) as refused:
        compile_definition(document, version_number=1)

    found = [problem for problem in refused.value.problems if problem.path == where]
    assert found and TOO_DEEP in found[0].message and found[0].line is None


def test_refusing_a_template_too_deep_takes_no_time() -> None:
    started = time.perf_counter()
    for source in DEEP.values():
        with pytest.raises(TemplateProblem):
            SandboxTemplate(source, name="pdf.html", html=True)

    assert time.perf_counter() - started < 5
