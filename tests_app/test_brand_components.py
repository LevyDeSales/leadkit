"""A biblioteca de componentes da marca: o que o PDF e a prévia de uma definição podem usar sem
copiar macro de formulário em formulário. Os testes passam pelo compilador e pelo WeasyPrint, com
valores calculados à mão."""

from __future__ import annotations

from typing import Any

import pytest
from test_form_definition import DEFINITION, changed, compiled
from test_weasyprint_pdf import context, squash, text_of

from app.adapters.weasyprint_pdf import WeasyPrintRenderer
from app.forms import FormSpec
from app.forms.definition import DefinitionError
from app.forms.definition.components import COMPONENTS

PLAN: dict[str, Any] = DEFINITION["examples"]["plan"]
HEADER = {"preview": "{{ plan.titulo }}"}


def page(markup: str, **more: Any) -> FormSpec:
    """O formulário do demo com este PDF."""
    return compiled(changed({"pdf.html": markup, **more}))


def printed(markup: str) -> str:
    """O HTML do PDF de um template, com o plano de exemplo."""
    spec = page(markup)
    return spec.templates.render_pdf(  # type: ignore[union-attr]
        spec.plan_model.model_validate(PLAN),
        lead={"name": "Contadora Exemplo"},
        form={"slug": spec.slug, "title": spec.title},
        scores={},
        generated_date="8 de outubro de 2026",
    )


def test_paragraphs_become_one_p_per_nonblank_line_and_escape_the_text() -> None:
    html = printed('{{ brand.paragraphs("Primeira linha.\\n\\n  Segunda <b>linha</b>.  \\n") }}')

    assert html.split() == [
        "<p>Primeira",
        "linha.</p>",
        "<p>Segunda",
        "&lt;b&gt;linha&lt;/b&gt;.</p>",
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Escritório pequeno e enxuto. Precisa organizar a rotina.\nSegunda linha.",
            "Escritório pequeno e enxuto.",
        ),
        ("Sem ponto final", "Sem ponto final."),
        ("Já termina com ponto.", "Já termina com ponto."),
        ("Pergunta que termina com interrogação?", "Pergunta que termina com interrogação?"),
        (
            "  Começa com espaço e linha em branco.\n\nOutra.",
            "Começa com espaço e linha em branco.",
        ),
    ],
)
def test_first_sentence_is_the_first_sentence_of_the_first_line(text: str, expected: str) -> None:
    html = printed("{{ brand.first_sentence(texto) }}".replace("texto", repr(text)))

    assert html.strip() == expected


def test_first_sentence_is_cut_at_280_characters_or_at_the_limit_given() -> None:
    long_text = "palavra " * 60  # 480 caracteres, sem ponto no meio

    default = printed("{{ brand.first_sentence(texto) }}".replace("texto", repr(long_text))).strip()
    short = printed(
        "{{ brand.first_sentence(texto, 20) }}".replace("texto", repr(long_text))
    ).strip()

    assert len(default) <= 280 and default.endswith("…")
    assert short == "palavra palavra…"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("FASE 1 — Organizar a casa", "Organizar a casa"),
        ("Fase 2 — Cobrar com clareza", "Cobrar com clareza"),
        ("Organizar a casa", "Organizar a casa"),
        ("Plano — não é prefixo de fase", "Plano — não é prefixo de fase"),
    ],
)
def test_phase_name_drops_the_legacy_fase_prefix(name: str, expected: str) -> None:
    html = printed("{{ brand.phase_name(nome) }}".replace("nome", repr(name)))

    assert html.strip() == expected


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ("brand.phase_days(0)", "Dias 1 a 30"),
        ("brand.phase_days(1)", "Dias 31 a 60"),
        ("brand.phase_days(2)", "Dias 61 a 90"),
        ("brand.phase_days(1, 15)", "Dias 16 a 30"),
    ],
)
def test_phase_days_counts_the_days_of_each_phase(call: str, expected: str) -> None:
    assert printed("{{ " + call + " }}").strip() == expected


def test_visual_blocks_of_the_brand_kit() -> None:
    html = printed(
        "{{ brand.eyebrow('Seus 90 dias') }}{{ brand.rule() }}{{ brand.lead('Resumo.') }}"
        "{{ brand.chip('Dias 1 a 30') }}{{ brand.numeral(87) }}"
        "{{ brand.logo() }}{{ brand.logo('branca', 'cover__logo') }}{{ brand.running_header() }}"
    )

    base = "brand:"
    assert '<p class="ac-eyebrow">Seus 90 dias</p>' in html
    assert '<div class="ac-rule"></div>' in html
    assert '<p class="ac-lead">Resumo.</p>' in html
    assert '<p class="ac-chip">Dias 1 a 30</p>' in html
    assert '<span class="ac-numeral">87</span>' in html
    assert f'<img src="{base}logo-preta.svg" alt="Leadkit">' in html
    assert (f'<img class="cover__logo" src="{base}logo-branca.svg" alt="Leadkit">') in html
    assert (
        f'<div class="running-header"><img src="{base}logo-preta.svg" alt="Leadkit"></div>'
    ) in html


def test_blocks_escape_the_text_they_receive() -> None:
    html = printed("{{ brand.chip('<script>alert(1)</script>') }}")

    assert "<script>" not in html and "&lt;script&gt;" in html


def test_panel_and_wine_wrap_what_the_template_writes_between_them() -> None:
    html = printed(
        "{% call brand.panel() %}<p>Dentro</p>{% endcall %}"
        "{% call brand.wine('cover') %}<h1>{{ plan.titulo }}</h1>{% endcall %}"
    )

    assert '<div class="ac-panel"><p>Dentro</p></div>' in html
    assert '<div class="ac-on-wine cover"><h1>' in html


def test_an_unknown_logo_falls_back_to_the_dark_one() -> None:
    html = printed("{{ brand.logo('../../etc/passwd') }}")

    assert "logo-preta.svg" in html and "passwd" not in html


def test_the_pdf_renders_with_the_components_inside_the_assets_only_fetcher() -> None:
    markup = (
        "{{ brand.running_header() }}<section class='ac-on-wine'>{{ brand.eyebrow('Plano') }}"
        "<h1>{{ plan.titulo }}</h1></section>"
        "<p>{{ brand.phase_days(1) }}: {{ brand.first_sentence(plan.resumo) }}</p>"
    )
    spec = page(markup)

    rendered = WeasyPrintRenderer().render(spec, PLAN, context())

    text = squash(text_of(rendered.content))
    assert squash(PLAN["titulo"]) in text and "Dias31a60" in text


@pytest.mark.parametrize("slot", ["pdf.html", "preview"])
def test_a_component_that_does_not_exist_is_refused_with_its_line(slot: str) -> None:
    with pytest.raises(DefinitionError) as caught:
        compiled(changed({slot: "<p>ok</p>\n{{ brand.cartao('x') }}"}))

    assert f"{slot}, linha 2: brand.cartao não é um componente da marca" in str(caught.value)


def test_the_library_is_not_part_of_the_prompt_or_the_instructions() -> None:
    with pytest.raises(DefinitionError) as caught:
        compiled(changed({"prompt": "{{ brand.chip('x') }}"}))

    assert "prompt, linha 1: variável inexistente: brand" in str(caught.value)


def test_a_template_can_not_reach_inside_the_library() -> None:
    with pytest.raises(DefinitionError) as caught:
        compiled(changed({"pdf.html": "{{ brand.paragraphs.__globals__ }}"}))

    assert "pdf.html, linha 1: atributo interno proibido: __globals__" in str(caught.value)


def test_every_component_is_documented_and_every_example_renders() -> None:
    from app.forms.definition.components import BRAND

    macros = {name for name in dir(BRAND) if not name.startswith("_")}
    assert macros == {component.name for component in COMPONENTS}
    for component in COMPONENTS:
        html = printed(component.example)
        assert html.strip(), component.name
        assert f"brand.{component.name}(" in component.signature
        assert component.summary and component.example
