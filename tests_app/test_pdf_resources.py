"""O que o template do PDF e o CSS dele pedem de fora do app, e a linha de cada pedido."""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from app.forms.definition.resources import OUTSIDE, asset_names, resource_problems


def lines_of(html: str = "", css: str = "") -> list[tuple[str, int | None]]:
    return [(problem.path, problem.line) for problem in resource_problems(html, css)]


def test_the_line_of_a_tag_is_the_line_where_it_starts_even_after_a_jinja_comment() -> None:
    html = (
        "<p>a</p>\n{# um comentário\nde duas linhas #}\n<img\n  src='https://example.test/a.png'>"
    )

    assert lines_of(html) == [("pdf.html", 4)]


def test_a_style_block_reports_the_line_of_the_url_inside_it() -> None:
    html = "<style>\nbody { color: red; }\n.capa { background: url(http://example.test/a.png) }\n</style>"

    assert lines_of(html) == [("pdf.html", 3)]


@pytest.mark.parametrize(
    "tag",
    [
        '<object data="https://example.test/a.pdf"></object>',
        '<video poster="https://example.test/a.png" src="brands/ok.mp4"></video>',
        '<embed src="ftp://example.test/a">',
        '<image href="//example.test/a.svg">',
        '<img src="file:///etc/passwd">',
        '<img src="  https://example.test/a.png">',
        '<img src="https://example.test/{{ plan.titulo }}.png">',
    ],
)
def test_every_kind_of_outside_address_is_found(tag: str) -> None:
    assert lines_of(tag) == [("pdf.html", 1)]


@pytest.mark.parametrize(
    "tag",
    [
        '<img src="brand:logo-preta.svg">',
        '<img src="data:image/png;base64,AAAA">',
        '<img src="DATA:image/png;base64,AAAA">',
        '<img src="{{ brand_path }}logo.svg">',
        '<a href="https://example.test">link</a>',
        '<link rel="canonical" href="https://example.test/">',
        '<p style="color: red">x</p>',
        '<p title="https://example.test/a.png">x</p>',
    ],
)
def test_app_files_data_uris_computed_addresses_and_plain_links_pass(tag: str) -> None:
    assert lines_of(tag) == []


def test_the_css_is_read_for_url_and_import_with_the_line_of_each() -> None:
    css = (
        '@import url("brands/leadkit/print.css");\n'
        "@import 'https://example.test/a.css';\n"
        ".a { background: url( 'data:image/png;base64,AAAA' ) }\n"
        ".b { background: url(//example.test/b.png) }\n"
        ".c { background: url(../fora.png) }\n"
    )

    assert lines_of(css=css) == [("pdf.css", 2), ("pdf.css", 4), ("pdf.css", 5)]


def test_the_message_says_what_the_pdf_may_load() -> None:
    (problem,) = resource_problems('<img src="https://example.test/a.png">', "")

    assert problem.message == OUTSIDE and "app/assets" in OUTSIDE and "data:" in OUTSIDE


# --- imagens do formulário: asset:<nome> ----------------------------------------------------------


def image_problems(
    html: str = "", css: str = "", preview: str = "", known: tuple[str, ...] = ("logo-black",)
) -> list[tuple[str, int | None, str]]:
    found = resource_problems(html, css, preview=preview, assets=frozenset(known))
    return [(problem.path, problem.line, problem.message) for problem in found]


def test_an_asset_address_is_not_an_outside_resource() -> None:
    assert (
        lines_of('<img src="asset:logo-black">', ".a { background: url(asset:logo-black) }") == []
    )


def test_an_image_that_does_not_exist_is_reported_with_its_name_and_line() -> None:
    html = '<p>a</p>\n<img src="asset:logo-black">\n<img src="asset:nao-existe">'

    ((path, line, message),) = image_problems(html)

    assert (path, line) == ("pdf.html", 3)
    assert "nao-existe" in message and "não existe" in message


def test_the_css_and_the_preview_name_their_images_too() -> None:
    css = ".a { background: url(asset:logo-black) }\n.b { background: url(asset:sumiu) }"
    preview = '<p>\n<img src="asset:outra">'

    assert [(path, line) for path, line, _ in image_problems("", css, preview)] == [
        ("pdf.css", 2),
        ("preview", 2),
    ]


def test_a_name_that_is_not_a_valid_image_name_is_reported_as_such() -> None:
    ((_, _, message),) = image_problems('<img src="asset:Logo Black">')

    assert "minúsculas" in message


def test_a_name_built_with_jinja_cannot_be_checked_here_and_passes() -> None:
    html = '<img src="asset:{{ plan.imagem }}"><img src="asset:capa-{{ plan.n }}">'

    assert image_problems(html) == []


def test_without_the_list_of_images_only_the_name_format_is_checked() -> None:
    found = resource_problems('<img src="asset:qualquer-uma"><img src="asset:A B">', "")

    assert [problem.line for problem in found] == [1]


def test_an_outside_address_is_still_reported_next_to_an_image() -> None:
    html = '<img src="asset:logo-black"><img src="https://example.test/a.png">'

    assert image_problems(html) == [("pdf.html", 1, OUTSIDE)]


def test_the_names_of_the_images_a_template_asks_for_come_back_once_in_order() -> None:
    html = '<img src="asset:b-um"><img src="asset:a-dois"><img src="asset:b-um">'
    css = ".x { background: url(asset:c-tres) }"
    preview = '<img src="asset:a-dois"><img src="asset:{{ plan.x }}">'

    assert asset_names(html, css, preview) == ["b-um", "a-dois", "c-tres"]


# --- o tempo: o que se manda para ser lido não pode custar mais que o tamanho dele ----------------

MAX_SECONDS = 1.0


def took(read: Callable[[], object]) -> float:
    started = time.perf_counter()
    read()
    return time.perf_counter() - started


COMMENTS = {
    "open-comments": "{#" * 125_000,  # comentários Jinja que nunca fecham
    "one-open-comment": "{#" + "x" * 250_000,
    "closed-then-open": "<p>{# a #}</p>" + "{#" * 100_000,
}
ADDRESSES = {
    "url-and-spaces": "url(" + " " * 20_000,  # um `url(` e só espaços, sem fechar
    "urls": "url(" * 60_000,  # `url(` sem fechar, um depois do outro
    "single-quoted-urls": "url('" * 60_000,
    "double-quoted-urls": 'url("' * 60_000,
    "urls-and-spaces": "url( " * 40_000,
    "import-and-spaces": "@import" + " " * 100_000,
    "imports": "@import " * 30_000,
    "import-and-quote": "@import " + "'" + "x" * 200_000,
}


# Endereços que fecham, às dezenas de milhares (o CSS do PDF tem até 256 KiB): achar a linha de cada um
# não pode custar uma volta pelo CSS inteiro.
CLOSED_ADDRESSES = {
    "urls": "url(a) " * 37_000,
    "urls-one-per-line": "url(a)\n" * 37_000,
    "imports": "@import 'a'; " * 19_000,
}


@pytest.mark.parametrize("hostile", list(COMMENTS.values()), ids=list(COMMENTS))
def test_comments_that_never_close_are_read_in_linear_time(hostile: str) -> None:
    assert took(lambda: resource_problems(hostile, "", preview=hostile)) < MAX_SECONDS


@pytest.mark.parametrize("hostile", list(ADDRESSES.values()), ids=list(ADDRESSES))
def test_css_addresses_that_never_close_are_read_in_linear_time(hostile: str) -> None:
    as_attribute = hostile.replace('"', "")
    html = f'<style>{hostile}</style><p style="{as_attribute}">x</p>'

    assert took(lambda: resource_problems("", hostile)) < MAX_SECONDS
    assert took(lambda: resource_problems(html, "", preview=html)) < MAX_SECONDS
    assert took(lambda: asset_names(html, hostile, html)) < MAX_SECONDS


@pytest.mark.parametrize("many", list(CLOSED_ADDRESSES.values()), ids=list(CLOSED_ADDRESSES))
def test_many_closed_css_addresses_are_read_in_linear_time(many: str) -> None:
    html = f"<style>{many}</style><p style='{many}'>x</p>"

    assert took(lambda: resource_problems("", many)) < MAX_SECONDS
    assert took(lambda: resource_problems(html, "", preview=html)) < MAX_SECONDS


def test_the_addresses_of_a_css_are_still_read_after_the_change() -> None:
    css = (
        "a { background: url( 'https://example.test/a.png' ) }\n"
        'b { background: url("https://example.test/b.png") }\n'
        "c { background: url(  https://example.test/c.png  ) }\n"
        "@import 'https://example.test/d.css';\n"
        '@import   "https://example.test/e.css";\n'
        "d { background: url(brands/ok.png) }"
    )

    assert lines_of(css=css) == [("pdf.css", line) for line in (1, 2, 3, 4, 5)]
