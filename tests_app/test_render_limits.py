"""Prazo e teto de memória ao renderizar um template e um PDF (issue #139).

O Jinja e o WeasyPrint rodam na thread de quem pede e não tinham prazo nem teto: um template feito
para pesar prendia a thread, e um PDF que não acaba seguraria o lock de renderização de todo o
processo. Os testes põem os limites em décimos de segundo e conferem o comportamento: o que passa do
limite sai como erro definido, com mensagem fixa, e o plano normal segue renderizando. As folgas de
tempo são largas, porque a suíte roda em paralelo.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable

import pytest
from test_form_definition import changed, problems
from test_weasyprint_pdf import DEMO, EXAMPLE_PLAN, context, fonts_of, form_with_template, text_of
from weasyprint.text.fonts import FontConfiguration

from app.adapters import weasyprint_pdf
from app.adapters.weasyprint_pdf import WeasyPrintRenderer
from app.errors import FailureCode, TerminalError
from app.forms import render_guard, sandbox
from app.forms.render_guard import RenderLimit, run_within_limits
from app.forms.sandbox import SandboxTemplate, TemplateProblem

SLACK = 30.0  # o corte é de décimos de segundo; isto só impede um teste de ficar preso para sempre
SECRET = "texto-da-lead-que-nao-pode-voltar"
# Dois laços de cem mil voltas e nenhuma saída: o teto de 1 MB de texto nunca dispara.
NESTED_LOOPS = "{% for i in range(100000) %}{% for j in range(100000) %}{% endfor %}{% endfor %}"
# 50 bytes de template que viram cem mil parágrafos para o WeasyPrint (43 s sem prazo).
MANY_PARAGRAPHS = "{% for i in range(100000) %}<p>x</p>{% endfor %}"
PLAN_TITLE = "Organizar atividades do projeto"


def forever() -> None:
    while True:
        pass


def forever_behind_a_wide_except() -> None:
    """O que o `except Exception` largo de um motor faria com um erro qualquer."""
    while True:
        try:
            forever()
        except Exception:  # noqa: BLE001, S110
            pass


@pytest.fixture
def ten_gigabytes_more(monkeypatch: pytest.MonkeyPatch) -> None:
    """A memória do processo lida como 0 ao começar o trecho e 10 GB depois."""
    readings = itertools.chain([0], itertools.repeat(10 * 2**30))
    monkeypatch.setattr(render_guard, "_resident_bytes", lambda: next(readings))


# --- o guarda ------------------------------------------------------------------------------------


def test_a_call_within_the_limits_returns_its_result() -> None:
    assert run_within_limits(lambda: "ok", seconds=SLACK, memory=2**40) == "ok"


def test_an_error_of_the_call_goes_through_untouched() -> None:
    def broken() -> None:
        raise ValueError("falha do trecho")

    with pytest.raises(ValueError, match="falha do trecho"):
        run_within_limits(broken, seconds=SLACK, memory=2**40)


@pytest.mark.parametrize("call", [forever, forever_behind_a_wide_except])
def test_a_call_that_never_ends_is_cut_off_at_the_deadline(call: Callable[[], None]) -> None:
    with pytest.raises(RenderLimit, match="prazo de 0.3 s"):
        run_within_limits(call, seconds=0.3, memory=2**40)


@pytest.mark.usefixtures("ten_gigabytes_more")
def test_a_call_that_grows_the_memory_past_the_cap_is_cut_off() -> None:
    with pytest.raises(RenderLimit, match="128 MB de memória"):
        run_within_limits(forever, seconds=SLACK, memory=128 * 2**20)


def test_the_memory_is_only_read_when_a_cap_is_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[int] = []
    monkeypatch.setattr(render_guard, "_resident_bytes", lambda: reads.append(1) or 10 * 2**30)
    monkeypatch.setattr(render_guard, "POLL_SECONDS", 0.01)

    def wait_for_several_polls() -> str:
        time.sleep(0.2)
        return "ok"

    assert run_within_limits(wait_for_several_polls, seconds=SLACK) == "ok"
    assert reads == []


def test_the_watcher_never_outlives_the_guard_even_when_the_deadline_lands_as_the_call_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O guarda roda em threads de um pool: uma vigia que sobrasse cortaria a renderização de outra
    pessoa. O prazo é de 3 ms para uma chamada de 4 ms, com a vigia olhando a cada 1 ms, então o corte
    cai ora no meio da chamada, ora na limpeza."""
    monkeypatch.setattr(render_guard, "POLL_SECONDS", 0.001)

    def busy() -> None:
        end = time.perf_counter() + 0.004
        while time.perf_counter() < end:
            pass

    for _ in range(150):
        try:
            run_within_limits(busy, seconds=0.003)
        except RenderLimit:
            pass

    assert [t for t in threading.enumerate() if t.name == "render-guard"] == []
    time.sleep(0.1)  # uma interrupção que sobrasse cairia aqui, fora do guarda


def test_the_cut_leaves_no_interruption_behind() -> None:
    with pytest.raises(RenderLimit):
        run_within_limits(forever, seconds=0.2, memory=2**40)

    time.sleep(0.3)  # uma interrupção agendada e solta cairia aqui, fora do guarda
    assert run_within_limits(lambda: sum(range(1000)), seconds=SLACK, memory=2**40) == 499_500


# --- o template ----------------------------------------------------------------------------------


@pytest.fixture
def quick_template(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "RENDER_SECONDS", 1.0)


@pytest.mark.usefixtures("quick_template")
def test_a_template_that_never_ends_is_a_problem_of_the_template_with_its_line() -> None:
    template = SandboxTemplate(f"primeira linha\n{NESTED_LOOPS}", name="prompt", html=False)

    with pytest.raises(TemplateProblem) as problem:
        template.render({"lead": SECRET})

    assert (problem.value.name, problem.value.line) == ("prompt", 2)
    assert problem.value.detail == "a renderização passou do prazo de 1 s"
    assert SECRET not in str(problem.value)


@pytest.mark.usefixtures("ten_gigabytes_more")
def test_the_memory_of_the_process_does_not_cut_a_template(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outra thread (o otimizador de imagens, com até 40 MP) faz a memória do processo crescer: a
    prévia ou a conferência que roda ao lado não pode ser cortada por isso."""
    monkeypatch.setattr(render_guard, "POLL_SECONDS", 0.01)
    template = SandboxTemplate("{{ wait() }}", name="prompt", html=False)

    assert template.render({"wait": lambda: time.sleep(0.2) or "ok"}) == "ok"


@pytest.mark.parametrize(
    "call",
    [
        '"x".ljust(400000000)',
        '"x".rjust(400000000)',
        '"x".center(400000000)',
        '"7".zfill(400000000)',
    ],
)
def test_padding_a_text_to_hundreds_of_megabytes_is_refused_before_it_is_allocated(
    call: str,
) -> None:
    template = SandboxTemplate(f"{{% set pad = {call} %}}ok", name="prompt", html=False)

    with pytest.raises(TemplateProblem) as problem:
        template.render({})

    assert problem.value.detail == "operação grande demais para o ambiente isolado"


def test_padding_a_text_to_an_ordinary_width_still_works() -> None:
    template = SandboxTemplate(
        '{{ "ab".ljust(5, ".") }}|{{ "7".zfill(3) }}', name="prompt", html=False
    )

    assert template.render({}) == "ab...|007"


@pytest.mark.usefixtures("quick_template")
def test_an_ordinary_template_still_renders_after_a_cut_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(TemplateProblem):
        SandboxTemplate(NESTED_LOOPS, name="prompt", html=False).render({})
    monkeypatch.setattr(sandbox, "RENDER_SECONDS", SLACK)

    ordinary = SandboxTemplate("Olá, {{ nome }}! {{ [1, 2, 3]|sum }}", name="prompt", html=False)

    assert ordinary.render({"nome": "Ana"}) == "Olá, Ana! 6"


@pytest.mark.usefixtures("quick_template")
def test_saving_a_definition_with_a_template_that_never_ends_names_the_template() -> None:
    document = changed({"prompt": NESTED_LOOPS})

    assert problems(document) == ["prompt, linha 1: a renderização passou do prazo de 1 s"]


# --- o PDF ---------------------------------------------------------------------------------------


def test_a_pdf_that_takes_too_long_fails_terminally_and_the_next_one_still_renders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configurations: list[FontConfiguration] = []

    class CountedFonts(FontConfiguration):
        def __init__(self) -> None:
            super().__init__()
            configurations.append(self)

    monkeypatch.setattr(weasyprint_pdf, "FontConfiguration", CountedFonts)
    monkeypatch.setattr(weasyprint_pdf, "PDF_SECONDS", 1.0)
    renderer = WeasyPrintRenderer()

    with pytest.raises(TerminalError) as caught:
        renderer.render(form_with_template(MANY_PARAGRAPHS), EXAMPLE_PLAN, context(name=SECRET))

    assert caught.value.code is FailureCode.RENDER_FAILED
    assert "o PDF passou do prazo de 1 s" in str(caught.value)
    assert SECRET not in str(caught.value)
    # O lock foi solto e o motor segue servindo, o mesmo renderizador e um novo. O corte pode ter
    # caído no registro das fontes da marca: o renderizador refaz a configuração delas.
    monkeypatch.setattr(weasyprint_pdf, "PDF_SECONDS", SLACK)
    rendered = renderer.render(DEMO, EXAMPLE_PLAN, context())
    assert PLAN_TITLE in text_of(rendered.content)
    assert len(configurations) == 2 and all(fonts_of(rendered.content).values())
    other = WeasyPrintRenderer().render(DEMO, EXAMPLE_PLAN, context())
    assert PLAN_TITLE in text_of(other.content)


@pytest.mark.usefixtures("ten_gigabytes_more")
def test_a_pdf_that_grows_the_memory_past_the_cap_fails_terminally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(weasyprint_pdf, "PDF_SECONDS", SLACK)

    with pytest.raises(TerminalError) as caught:
        WeasyPrintRenderer().render(form_with_template(MANY_PARAGRAPHS), EXAMPLE_PLAN, context())

    assert caught.value.code is FailureCode.RENDER_FAILED
    assert "o PDF passou de 384 MB de memória" in str(caught.value)


@pytest.mark.usefixtures("quick_template")
def test_a_pdf_template_that_never_ends_fails_terminally_before_the_engine_starts() -> None:
    spec = form_with_template(NESTED_LOOPS)

    with pytest.raises(TerminalError) as caught:
        WeasyPrintRenderer().render(spec, EXAMPLE_PLAN, context())

    assert caught.value.code is FailureCode.RENDER_FAILED
    assert "template do PDF falhou" in str(caught.value) and "passou do prazo" in str(caught.value)


def test_the_default_limits_leave_room_for_an_ordinary_plan() -> None:
    assert sandbox.RENDER_SECONDS >= 2 and weasyprint_pdf.PDF_SECONDS >= 30

    rendered = WeasyPrintRenderer().render(DEMO, EXAMPLE_PLAN, context())

    assert PLAN_TITLE in text_of(rendered.content)
