"""Macros compatíveis para PDF e prévia, com identidade neutra Leadkit.

As classes ac-* e as assinaturas são contratos de templates publicados. A marca privada é
resolvida por app.branding; não precisa alterar a definição nem copiar assets comerciais.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.forms.sandbox import SandboxTemplate

LOGO_FOLDER = "brand:"

LIBRARY = """\
{#- Os parágrafos de um texto de várias linhas: um <p> por linha não vazia. -#}
{%- macro paragraphs(text) -%}
{%- for paragraph in text.splitlines() if paragraph.strip() %}
<p>{{ paragraph.strip() }}</p>
{%- endfor %}
{%- endmacro -%}

{#- A primeira frase da primeira linha, com ponto final, cortada em `limit` caracteres. -#}
{%- macro first_sentence(text, limit=280) -%}
{%- set sentence = ((text.strip().splitlines() | first).split(". ") | first).strip() -%}
{%- if not sentence.endswith((".", "!", "?", "…")) %}{% set sentence = sentence ~ "." %}{% endif -%}
{{ sentence | truncate(limit, end="…") }}
{%- endmacro -%}

{#- O nome da fase sem o prefixo "FASE 1 — " que o legado escrevia: o número já está na faixa. -#}
{%- macro phase_name(name) -%}
{%- set parts = name.split(" — ", 1) -%}
{{ parts[1] if parts | length == 2 and parts[0].upper().startswith("FASE") else name }}
{%- endmacro -%}

{#- Os dias da fase de posição `index` (a partir de 0), com `length` dias cada uma. -#}
{%- macro phase_days(index, length=30) -%}
Dias {{ index * length + 1 }} a {{ (index + 1) * length }}
{%- endmacro -%}

{%- macro eyebrow(text) -%}<p class="ac-eyebrow">{{ text }}</p>{%- endmacro -%}
{%- macro rule() -%}<div class="ac-rule"></div>{%- endmacro -%}
{%- macro lead(text) -%}<p class="ac-lead">{{ text }}</p>{%- endmacro -%}
{%- macro chip(text) -%}<p class="ac-chip">{{ text }}</p>{%- endmacro -%}
{%- macro numeral(value) -%}<span class="ac-numeral">{{ value }}</span>{%- endmacro -%}

{#- A assinatura da marca: "preta" para fundo claro, "branca" para fundo escuro. -#}
{%- macro logo(variant="preta", css="") -%}
{%- set variant = variant if variant in ("preta", "branca") else "preta" -%}
<img{% if css %} class="{{ css }}"{% endif %} src="LOGO_FOLDERlogo-{{ variant }}.svg" alt="{{ brand_name | default('Leadkit') }}">
{%- endmacro -%}

{#- A assinatura que se repete no topo de cada página (o `running(header)` do CSS). -#}
{%- macro running_header() -%}<div class="running-header">{{ logo("preta") }}</div>{%- endmacro -%}

{%- macro panel() -%}<div class="ac-panel">{{ caller() }}</div>{%- endmacro -%}
{%- macro wine(css="") -%}<div class="ac-on-wine{% if css %} {{ css }}{% endif %}">{{ caller() }}</div>{%- endmacro -%}
""".replace("LOGO_FOLDER", LOGO_FOLDER)


@dataclass(frozen=True)
class Component:
    """Um componente da biblioteca, como o editor do PDF o descreve."""

    name: str
    signature: str  # como o template o chama
    summary: str
    example: str  # um trecho de template que renderiza com qualquer plano de exemplo
    visual: bool = False  # um bloco do kit da marca (e não um auxiliar de texto)
    pdf_only: bool = False  # depende dos arquivos do app: só vale no PDF


COMPONENTS: tuple[Component, ...] = (
    Component(
        "paragraphs",
        "brand.paragraphs(texto)",
        "Um parágrafo (<p>) por linha não vazia do texto.",
        '{{ brand.paragraphs("Primeira linha.\\nSegunda linha.") }}',
    ),
    Component(
        "first_sentence",
        "brand.first_sentence(texto, limite)",
        "A primeira frase da primeira linha, com ponto final, cortada em até 280 caracteres "
        "(ou no limite dado).",
        '{{ brand.first_sentence("Escritório enxuto. Precisa de rotina.") }}',
    ),
    Component(
        "phase_name",
        "brand.phase_name(nome)",
        'O nome da fase sem o prefixo "FASE 1 — " que o legado escrevia.',
        '{{ brand.phase_name("FASE 1 — Organizar a casa") }}',
    ),
    Component(
        "phase_days",
        "brand.phase_days(posição, dias)",
        'Os dias da fase na posição dada (a partir de 0), de 30 em 30 ou de "dias" em "dias": '
        '"Dias 31 a 60".',
        "{{ brand.phase_days(1) }}",
    ),
    Component(
        "eyebrow",
        "brand.eyebrow(texto)",
        "O sobretítulo em caixa alta, em azul, acima de um título.",
        '{{ brand.eyebrow("Seus 90 dias") }}',
        visual=True,
    ),
    Component(
        "rule",
        "brand.rule()",
        "O fio azul curto que separa o título do texto.",
        "{{ brand.rule() }}",
        visual=True,
    ),
    Component(
        "lead",
        "brand.lead(texto)",
        "O texto de abertura de uma seção, maior que o corrido.",
        '{{ brand.lead("Leitura do momento do escritório.") }}',
        visual=True,
    ),
    Component(
        "chip",
        "brand.chip(texto)",
        "A etiqueta em azul claro, como a dos dias de cada fase. Dentro de .phase, use "
        "`display: table` nela, para a quebra de página não a deixar sozinha.",
        '{{ brand.chip("Dias 1 a 30") }}',
        visual=True,
    ),
    Component(
        "numeral",
        "brand.numeral(valor)",
        "Um número de destaque, em Source Serif Pro e azul (texto grande).",
        "{{ brand.numeral(87) }}",
        visual=True,
    ),
    Component(
        "logo",
        "brand.logo(variante, classes)",
        'A assinatura da marca: "preta" (padrão) para fundo claro, "branca" para azul escuro; '
        "`classes` acrescenta classes à imagem (`cover__logo`, por exemplo).",
        '{{ brand.logo("branca", "cover__logo") }}',
        visual=True,
        pdf_only=True,
    ),
    Component(
        "running_header",
        "brand.running_header()",
        "O cabeçalho que se repete no topo de cada página: a assinatura preta dentro de "
        ".running-header, que o CSS do PDF posiciona com `position: running(header)`.",
        "{{ brand.running_header() }}",
        visual=True,
        pdf_only=True,
    ),
    Component(
        "panel",
        "{% call brand.panel() %}…{% endcall %}",
        "Um painel em branco institucional, que não se parte entre duas páginas.",
        "{% call brand.panel() %}<p>Texto do painel.</p>{% endcall %}",
        visual=True,
    ),
    Component(
        "wine",
        "{% call brand.wine(classes) %}…{% endcall %}",
        "Um bloco em fundo azul escuro, com títulos e texto em branco; `classes` acrescenta classes "
        "(`cover`, por exemplo).",
        '{% call brand.wine("cover") %}<h1>Plano</h1>{% endcall %}',
        visual=True,
    ),
)
COMPONENT_NAMES = frozenset(component.name for component in COMPONENTS)

BRAND = SandboxTemplate(LIBRARY, name="brand", html=True).module()
