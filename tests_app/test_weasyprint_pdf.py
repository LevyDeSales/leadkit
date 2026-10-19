"""PDF real com WeasyPrint: renderiza PDFs de verdade e afere texto extraído, páginas e fontes."""

from __future__ import annotations

import copy
import re
import socket
import threading
import time
import traceback
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

import pytest
import weasyprint
from conftest import legacy_spec, publish
from pypdf import PdfReader
from test_intake import submit

from app import jobs, pipeline
from app.adapters.weasyprint_pdf import WeasyPrintRenderer
from app.deps import Deps, build_pdf
from app.errors import FailureCode, TerminalError
from app.forms import FormSpec, starter
from app.forms.definition import compile_definition
from app.forms.sandbox import SandboxTemplate
from app.models import Artifact
from app.settings import Settings

GENERATED_AT = datetime(2026, 10, 8, 2, 30, tzinfo=UTC)  # 23h30 de 7/10 em São Paulo
DEMO = legacy_spec("demo")
EXAMPLE_PLAN = DEMO.example_plan


def context(
    name: str | None = "Contadora Exemplo", generated_at: datetime = GENERATED_AT
) -> dict[str, Any]:
    return {
        "lead": {"name": name},
        "form": {"slug": "demo", "title": "Projeto de demonstração"},
        "generated_at": generated_at,
    }


def text_of(pdf: bytes) -> str:
    """Texto de todas as páginas com espaços normalizados (quebras de linha viram espaço)."""
    pages = PdfReader(BytesIO(pdf)).pages
    return " ".join(" ".join((page.extract_text() or "").split()) for page in pages)


def squash(text: str) -> str:
    """Sem nenhum espaço: compara trechos que podem quebrar de linha em qualquer ponto."""
    return "".join(text.split())


def pages_of(pdf: bytes) -> int:
    return len(PdfReader(BytesIO(pdf)).pages)


def fonts_of(pdf: bytes) -> dict[str, bool]:
    """Família de cada fonte usada no PDF (sem o prefixo do subconjunto) -> está embutida?"""
    fonts: dict[str, bool] = {}
    for page in PdfReader(BytesIO(pdf)).pages:
        for ref in page["/Resources"].get("/Font", {}).values():
            font = ref.get_object()
            if "/DescendantFonts" in font:
                font_program = font["/DescendantFonts"][0].get_object()
            else:
                font_program = font
            descriptor = font_program["/FontDescriptor"].get_object()
            name = str(font["/BaseFont"]).split("+")[-1].replace("-", "")
            fonts[name] = any(
                key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")
            )
    return fonts


def test_demo_pdf_prints_plan_lead_and_long_date_in_sao_paulo() -> None:
    rendered = WeasyPrintRenderer().render(DEMO, EXAMPLE_PLAN, context())

    text = text_of(rendered.content)
    for expected in (
        "Organizar atividades do projeto",
        EXAMPLE_PLAN["resumo"],
        "A participante combina uma atividade simples com a equipe e registra o próximo.",
        "A participante combina uma atividade simples com a equipe e.",
        "A participante combina uma atividade simples com a equipe e registra.",
        "A participante combina uma atividade simples com a equipe e registra o próx.",
        "Contadora Exemplo",
        "7 de outubro de 2026",
    ):
        assert expected in text, expected
    assert rendered.renderer == "weasyprint"
    assert rendered.page_count == pages_of(rendered.content) == 1


def test_demo_pdf_reads_in_document_order() -> None:
    """Copiar/colar e leitores de tela seguem a ordem em que o texto está no PDF."""
    rendered = WeasyPrintRenderer().render(DEMO, EXAMPLE_PLAN, context())

    text = squash(text_of(rendered.content))
    in_order = [
        "Preparado para Contadora Exemplo",
        "7 de outubro de 2026",
        EXAMPLE_PLAN["titulo"],
        "Resumo:",
        "Ações",
        "Comece esta semana:",
        "Página 1 de 1",
    ]
    positions = [text.find(squash(piece)) for piece in in_order]
    assert -1 not in positions and positions == sorted(positions), positions


@pytest.mark.parametrize(
    ("generated_at", "expected"),
    [
        (datetime(2027, 1, 1, 12, 0, tzinfo=UTC), "1º de janeiro de 2027"),
        (datetime(2026, 3, 1, 2, 59, tzinfo=UTC), "28 de fevereiro de 2026"),
    ],
)
def test_generation_date_is_written_out_the_brazilian_way(
    generated_at: datetime, expected: str
) -> None:
    rendered = WeasyPrintRenderer().render(DEMO, EXAMPLE_PLAN, context(generated_at=generated_at))

    assert expected in text_of(rendered.content)


def test_demo_pdf_embeds_only_the_bundled_fonts() -> None:
    rendered = WeasyPrintRenderer().render(DEMO, EXAMPLE_PLAN, context())

    fonts = fonts_of(rendered.content)
    assert fonts and all(fonts.values()), fonts
    assert all(name.startswith(("SourceSansPro", "SourceSerifPro")) for name in fonts), fonts


def test_long_plan_breaks_into_pages_numbered_x_of_y() -> None:
    long_action = "Ação detalhada com acentuação, números como 1.234,56 e rotina semanal. " * 28
    plan = {**EXAMPLE_PLAN, "acoes": [long_action] * 5}

    rendered = WeasyPrintRenderer().render(DEMO, plan, context())

    pages = pages_of(rendered.content)
    text = text_of(rendered.content)
    assert pages >= 2 and rendered.page_count == pages
    assert f"Página 1 de {pages}" in text and f"Página {pages} de {pages}" in text


HOSTILE = (
    '<b>negrito</b> & <script>alert(1)</script> &amp; <img src="http://169.254.169.254/latest/">'
    ' <link rel="stylesheet" href="https://evil.example/x.css"> {{ 7*7 }}'
)


def test_hostile_text_prints_literally_and_never_touches_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[tuple[object, ...]] = []

    def no_network(*args: object, **kwargs: object) -> None:
        attempts.append(args)
        raise OSError("rede bloqueada no teste")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    plan = {
        **EXAMPLE_PLAN,
        "resumo": f"{HOSTILE} {EXAMPLE_PLAN['resumo']}",
        "acoes": [HOSTILE, *EXAMPLE_PLAN["acoes"][1:]],
    }

    rendered = WeasyPrintRenderer().render(
        DEMO, plan, context(name="<img src=http://169.254.169.254/> Ana & Bia")
    )

    text = squash(text_of(rendered.content))
    assert attempts == []
    assert text.count(squash(HOSTILE)) == 2
    assert squash("Preparado para <img src=http://169.254.169.254/> Ana & Bia") in text


def form_with_template(html: str, css: str | None = None) -> FormSpec:
    """O formulário demo com outro template de PDF, sem a conferência da compilação: o que o
    adaptador faz com um template que pede um recurso de fora ou que falha ao renderizar."""
    assert DEMO.templates is not None
    pdf = SandboxTemplate(html, name="pdf.html", html=True)
    return replace(DEMO, templates=replace(DEMO.templates, pdf=pdf, pdf_css=css or ""))


@pytest.mark.parametrize(
    ("html", "css"),
    [
        ('<img src="http://169.254.169.254/latest/meta-data/">', None),
        ('<img src="https://evil.example/logo.png">', None),
        ('<img src="file:///etc/hosts">', None),
        ('<link rel="stylesheet" href="file:///etc/hosts">', None),
        ("<p>x</p>", '@import url("https://evil.example/x.css");'),
        ("<p>x</p>", 'body { background: url("../forms/__init__.py"); }'),
        ("<p>x</p>", '@font-face { font-family: X; src: url("https://evil.example/x.ttf"); }'),
    ],
)
def test_template_cannot_load_anything_outside_app_assets(
    monkeypatch: pytest.MonkeyPatch, html: str, css: str | None
) -> None:
    attempts: list[tuple[object, ...]] = []
    monkeypatch.setattr(socket.socket, "connect", lambda *args: attempts.append(args))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kw: attempts.append(args))
    spec = form_with_template(html, css)

    with pytest.raises(TerminalError) as caught:
        WeasyPrintRenderer().render(spec, EXAMPLE_PLAN, context())

    assert caught.value.code is FailureCode.RENDER_FAILED
    assert attempts == []


def test_template_can_inline_images_as_data_uris() -> None:
    pixel = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    spec = form_with_template(f'<p>Logo</p><img src="data:image/png;base64,{pixel}">')

    rendered = WeasyPrintRenderer().render(spec, EXAMPLE_PLAN, context())

    images = PdfReader(BytesIO(rendered.content)).pages[0].images
    assert len(images) == 1 and "Logo" in text_of(rendered.content)


LEAD_NAME = "Lead Secreta da Silva"
LEAD_EMAIL = "lead.secreta@example.test"


def traceback_text(error: BaseException) -> str:
    """O que o Hatchet guardaria da falha: mensagem e traceback, com as exceções encadeadas."""
    return "".join(traceback.format_exception(error))


@pytest.mark.parametrize(
    ("html", "document"),
    [
        (None, {**EXAMPLE_PLAN, "acoes": f"Ligar para {LEAD_EMAIL}"}),
        (None, {**EXAMPLE_PLAN, "titulo": f"Plano de {LEAD_EMAIL} " * 5}),
        ("<p>{{ plan.campo_que_nao_existe }}</p>", EXAMPLE_PLAN),
        ("<p>{{ lead.name + 1 }}</p>", EXAMPLE_PLAN),
    ],
)
def test_plan_or_template_errors_fail_terminally_without_lead_data(
    html: str | None, document: dict[str, Any]
) -> None:
    spec = DEMO if html is None else form_with_template(html)

    with pytest.raises(TerminalError) as caught:
        WeasyPrintRenderer().render(spec, document, context(name=LEAD_NAME))

    assert caught.value.code is FailureCode.RENDER_FAILED
    trace = traceback_text(caught.value)
    assert LEAD_NAME not in trace and LEAD_EMAIL not in trace


def test_engine_failures_fail_terminally_without_lead_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_layout(*args: object, **kwargs: object) -> None:
        raise ValueError(f"layout quebrou no parágrafo de {LEAD_NAME}")

    monkeypatch.setattr(weasyprint.HTML, "render", broken_layout)

    with pytest.raises(TerminalError) as caught:
        WeasyPrintRenderer().render(DEMO, EXAMPLE_PLAN, context(name=LEAD_NAME))

    assert caught.value.code is FailureCode.RENDER_FAILED
    assert LEAD_NAME not in traceback_text(caught.value)


def test_renders_one_document_at_a_time_across_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    """O WeasyPrint não é thread-safe, e o worker roda tasks síncronas num pool de threads."""
    active = peak = 0
    counter = threading.Lock()
    engine_render = weasyprint.HTML.render

    def observed_render(self: weasyprint.HTML, *args: Any, **kwargs: Any) -> Any:
        nonlocal active, peak
        with counter:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.05)  # alarga a janela em que outra thread entraria junto
            return engine_render(self, *args, **kwargs)
        finally:
            with counter:
                active -= 1

    monkeypatch.setattr(weasyprint.HTML, "render", observed_render)
    renderers = [WeasyPrintRenderer() for _ in range(4)]
    texts: list[str] = []

    def render_with(renderer: WeasyPrintRenderer) -> None:
        texts.append(text_of(renderer.render(DEMO, EXAMPLE_PLAN, context()).content))

    threads = [threading.Thread(target=render_with, args=(r,)) for r in renderers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert peak == 1
    assert len(texts) == 4 and len(set(texts)) == 1


def test_render_step_in_weasyprint_mode_stores_the_lead_pdf_without_internal_data(
    deps: Deps,
) -> None:
    deps.pdf = build_pdf(Settings(_env_file=None, PDF_MODE="weasyprint"))  # type: ignore[call-arg]
    job = submit(deps).job  # escore "porte" 50 e prioridade P2 ficam na submissão
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "run-1")
    plan_id, _ = pipeline.generate_plan(deps, job.id, 1)

    artifact_id, _ = pipeline.render_pdf(deps, job.id, 1, plan_id)

    with deps.sessions() as session:
        artifact = session.get_one(Artifact, artifact_id)
    text = text_of(deps.storage.get(artifact.storage_key))
    assert artifact.renderer == "weasyprint"
    assert "Contadora Exemplo" in text and "Organizar atividades do projeto" in text
    for internal in ("P2", "porte", "UTC", "demo-v1", "pdf.html", "fake"):
        assert internal not in text, internal
    assert re.search(r"\d{1,2}:\d{2}", text) is None  # nenhum horário impresso


def test_each_pdf_is_rendered_with_the_form_version_of_its_plan(
    deps: Deps, newer_demo: FormSpec
) -> None:
    deps.pdf = build_pdf(Settings(_env_file=None, PDF_MODE="weasyprint"))  # type: ignore[call-arg]
    before = submit(deps, key="antes").job  # versão 1
    publish(deps, newer_demo)  # o plano da versão 2 tem um campo obrigatório a mais
    after = submit(deps, key="depois").job

    texts = {}
    for job in (before, after):
        with deps.sessions.begin() as session:
            jobs.claim(session, job.id, 1, "run-1")
        plan_id, _ = pipeline.generate_plan(deps, job.id, 1)
        artifact_id, _ = pipeline.render_pdf(deps, job.id, 1, plan_id)
        with deps.sessions() as session:
            key = session.get_one(Artifact, artifact_id).storage_key
        texts[job.id] = text_of(deps.storage.get(key))

    assert "Organizar atividades do projeto" in texts[before.id]
    assert "Plano da versão 2" in texts[after.id]


# --- o chip "Dias 31 a 60" não fica sozinho no fim de uma página (issue #26, A-4) ---------------


def orphan_chips(pdf: bytes) -> list[tuple[int, str]]:
    """As páginas cujo último texto do corpo é a etiqueta de uma fase ("Dias 31 a 60"), sem o
    título nem o objetivo dela: o rodapé da página não conta."""
    found = []
    for number, page in enumerate(PdfReader(BytesIO(pdf)).pages, start=1):
        lines = [line.strip() for line in (page.extract_text() or "").splitlines() if line.strip()]
        body = [
            line for line in lines if not line.startswith("Página ") and "Plano de ação" not in line
        ]
        # A nota da capa e o rodapé trazem o nome do formulário ou "Orientação inicial".
        body = [line for line in body if "Orientação inicial" not in line]
        if body and body[-1].startswith("Dias "):
            found.append((number, body[-1]))
    return found


def with_long_actions(plan: dict[str, Any], *, extra: int, words: int) -> dict[str, Any]:
    """O plano com `extra` ações a mais na fase 1, cada uma com um "como fazer" de `words` palavras."""
    longer = copy.deepcopy(plan)
    for number in range(extra):
        longer["fases"][0]["acoes"].append(
            {"titulo": f"Ação extra {number}", "como_fazer": " ".join(["passo"] * words)}
        )
    return longer


def starter_spec(plan: dict[str, Any]) -> FormSpec:
    document = starter.definition(slug="modelo-inicial", title="Modelo inicial")
    document["examples"]["plan"] = plan
    return compile_definition(document, version_number=1)


@pytest.mark.parametrize("words", [18, 24, 36, 51])
def test_starter_pdf_keeps_the_phase_chip_with_its_heading(words: int) -> None:
    base = starter.definition(slug="modelo-inicial", title="Modelo inicial")["examples"]["plan"]
    plan = with_long_actions(base, extra=2, words=words)  # o modelo aceita até 4 ações por fase
    spec = starter_spec(plan)

    rendered = WeasyPrintRenderer().render(spec, plan, context())

    assert orphan_chips(rendered.content) == []


@pytest.mark.parametrize("words", [18, 30, 45, 60])
def test_reforma_2027_pdf_keeps_the_phase_chip_with_its_heading(words: int) -> None:
    spec = legacy_spec("reforma-2027")
    plan = copy.deepcopy(spec.example_plan)
    while len(plan["fases"][0]["acoes"]) < 4:
        plan["fases"][0]["acoes"].append(copy.deepcopy(plan["fases"][0]["acoes"][-1]))
    for action in plan["fases"][0]["acoes"]:
        action["como_fazer"] = " ".join(["passo"] * words)[:360]
    scores = {
        "prontidao": 40,
        "rotulo_prontidao": "Em construção",
        "nivel_prontidao": "em_construcao",
        "exposicao": 55,
        "rotulo_exposicao": "Moderada",
    }

    rendered = WeasyPrintRenderer().render(spec, plan, {**context(), "scores": scores})

    assert orphan_chips(rendered.content) == []
