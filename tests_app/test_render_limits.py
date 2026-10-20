"""Prazo e teto de memória ao renderizar um template e um PDF (issue #139).

O Jinja e o WeasyPrint rodam na thread de quem pede e não tinham prazo nem teto: um template feito
para pesar prendia a thread, e um PDF que não acaba seguraria o lock de renderização de todo o
processo. Os testes põem os limites em décimos de segundo e conferem o comportamento: o que passa do
limite sai como erro definido, com mensagem fixa, e o plano normal segue renderizando. As folgas de
tempo são largas, porque a suíte roda em paralelo.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import os
import subprocess
import sys
import threading
import time
import weakref
from collections.abc import Callable

import pytest
from test_form_definition import changed, problems
from test_form_images_pdf import picture
from test_weasyprint_pdf import DEMO, EXAMPLE_PLAN, context, fonts_of, form_with_template, text_of

from app.adapters import pdf_process, weasyprint_pdf
from app.adapters.assets import AssetStore, _Cache
from app.adapters.pdf import minimal_pdf
from app.adapters.weasyprint_pdf import WeasyPrintRenderer
from app.errors import FailureCode, TerminalError
from app.forms import render_guard, sandbox
from app.forms.definition.images import AssetRef
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
    monkeypatch.setattr(weasyprint_pdf, "PDF_SECONDS", 1.0)
    renderer = WeasyPrintRenderer()

    with pytest.raises(TerminalError) as caught:
        renderer.render(form_with_template(MANY_PARAGRAPHS), EXAMPLE_PLAN, context(name=SECRET))

    assert caught.value.code is FailureCode.RENDER_FAILED
    assert "o PDF passou do prazo de 1 s" in str(caught.value)
    assert SECRET not in str(caught.value)
    # O lock foi solto e ambos os renderizadores usam fontes completas após o corte.
    monkeypatch.setattr(weasyprint_pdf, "PDF_SECONDS", SLACK)
    rendered = renderer.render(DEMO, EXAMPLE_PLAN, context())
    assert PLAN_TITLE in text_of(rendered.content)
    assert fonts_of(rendered.content) and all(fonts_of(rendered.content).values())
    other = WeasyPrintRenderer().render(DEMO, EXAMPLE_PLAN, context())
    assert PLAN_TITLE in text_of(other.content)


def test_a_pdf_that_grows_the_memory_past_the_cap_fails_terminally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(weasyprint_pdf, "PDF_SECONDS", SLACK)
    monkeypatch.setattr(weasyprint_pdf, "PDF_MEMORY", 2 * 2**20)

    with pytest.raises(TerminalError) as caught:
        WeasyPrintRenderer().render(form_with_template(MANY_PARAGRAPHS), EXAMPLE_PLAN, context())

    assert caught.value.code is FailureCode.RENDER_FAILED
    assert "o PDF passou de 2 MB de memória" in str(caught.value)


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


def test_a_pdf_timeout_never_interrupts_an_asset_cleanup_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A limpeza de uma weakref não propaga erros: o corte não pode entrar nesse callback."""
    content = picture((31, 43, 57), "PNG")
    ref = AssetRef(hashlib.sha256(content).hexdigest(), "image/png")
    observed: list[type[BaseException]] = []
    original_hook = sys.unraisablehook

    def observe(event: object) -> None:
        observed.append(event.exc_type)
        original_hook(event)

    class Retired:
        pass

    class Storage:
        def get(self, key: str) -> bytes:
            def cleanup(reference: weakref.ReferenceType[Retired]) -> None:
                time.sleep(1.2)

            retired = Retired()
            reference = weakref.ref(retired, cleanup)
            del retired
            assert reference() is None
            return content

    monkeypatch.setattr(sys, "unraisablehook", observe)
    monkeypatch.setattr(weasyprint_pdf, "PDF_SECONDS", 1.0)
    renderer = WeasyPrintRenderer(AssetStore(Storage(), _Cache()))
    spec = form_with_template('<img src="asset:synthetic">').with_assets({"synthetic": ref})

    with pytest.raises(TerminalError, match="o PDF passou do prazo de 1 s"):
        renderer.render(spec, EXAMPLE_PLAN, context(name=SECRET))

    assert observed == []
    assert not any(thread.name == "render-guard" for thread in threading.enumerate())
    monkeypatch.setattr(weasyprint_pdf, "PDF_SECONDS", SLACK)
    following = renderer.render(DEMO, EXAMPLE_PLAN, context())
    assert PLAN_TITLE in text_of(following.content)
    assert all(fonts_of(following.content).values())


@pytest.mark.parametrize("phase", ["startup", "input", "crash", "cleanup", "late-cleanup"])
def test_a_pdf_process_that_stops_responding_is_reaped_before_the_next_pdf(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """O programa externo trava, ignora terminate e, no segundo caso, nem drena o HTML grande."""
    processes: list[subprocess.Popen[bytes]] = []
    start = subprocess.Popen
    program = (
        "import signal, socket, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "channel = socket.socket(fileno=int(sys.argv[1])); "
    )
    if phase in {"input", "cleanup", "late-cleanup"}:
        program += 'channel.sendall(b\'\\x00\\x00\\x00\\x11{"type":"ready"} \'); '
    if phase == "late-cleanup":
        encoded_pdf = base64.b64encode(minimal_pdf("Plano sintético")).decode("ascii")
        program += (
            "from app.adapters.pdf_ipc import encode, receive; "
            "receive(channel); "
            f"channel.sendall(encode({{'type': 'pdf', 'content': {encoded_pdf!r}, 'pages': 1}})); "
            "sys.exit(75)"
        )
    elif phase in {"crash", "cleanup"}:
        program += f"sys.exit({75 if phase == 'cleanup' else 1})"
    else:
        program += "time.sleep(30)"

    def launch(arguments: list[str], **options: object) -> subprocess.Popen[bytes]:
        process = start([sys.executable, "-c", program, arguments[-1]], **options)
        processes.append(process)
        return process

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", launch)
        patch.setattr(pdf_process, "STARTUP_SECONDS", 0.2)
        patch.setattr(weasyprint_pdf, "PDF_SECONDS", 0.2)
        spec = form_with_template("<p>" + "x" * 500_000 + "</p>")
        with pytest.raises(TerminalError) as caught:
            WeasyPrintRenderer().render(spec, EXAMPLE_PLAN, context(name=SECRET))

    assert SECRET not in str(caught.value)
    if phase == "input":
        assert "o PDF passou do prazo de 0.2 s" in str(caught.value)
    if phase in {"cleanup", "late-cleanup"}:
        assert "RenderCleanupError" in str(caught.value)
    assert len(processes) == 1
    with pytest.raises(ChildProcessError):
        os.waitpid(processes[0].pid, os.WNOHANG)
    following = WeasyPrintRenderer().render(DEMO, EXAMPLE_PLAN, context())
    assert PLAN_TITLE in text_of(following.content)


def test_cancelling_an_asset_read_reaps_the_pdf_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes: list[subprocess.Popen[bytes]] = []
    start = subprocess.Popen

    def launch(arguments: list[str], **options: object) -> subprocess.Popen[bytes]:
        process = start(arguments, **options)
        processes.append(process)
        return process

    class Storage:
        def get(self, key: str) -> bytes:
            raise KeyboardInterrupt

    ref = AssetRef("ab" * 32, "image/png")
    spec = form_with_template('<img src="asset:synthetic">').with_assets({"synthetic": ref})
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", launch)
        with pytest.raises(KeyboardInterrupt):
            WeasyPrintRenderer(AssetStore(Storage(), _Cache())).render(
                spec, EXAMPLE_PLAN, context()
            )

    assert len(processes) == 1
    with pytest.raises(ChildProcessError):
        os.waitpid(processes[0].pid, os.WNOHANG)
    following = WeasyPrintRenderer().render(DEMO, EXAMPLE_PLAN, context())
    assert PLAN_TITLE in text_of(following.content)
