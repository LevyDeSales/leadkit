"""O aviso extra das páginas de espera e de falha, configurado na definição do formulário
(`notices`): o formato e a conferência, pela costura do compilador, e o que o aviso muda no resto do
formulário. As páginas da lead que o mostram estão em `test_public_notices.py`."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import legacy_definition

from app.forms import Notice, NoticeLink, Notices
from app.forms.definition import DefinitionError, compile_definition
from app.forms.definition.changes import llm_changes, llm_fingerprint

WAITING = {
    "title": "Seu primeiro passo também pode ser manual",
    "text": (
        "Enquanto o seu mapa não fica pronto, baixe o material de uma página.\n\n"
        "Esta pesquisa é complementar e não confirma a sua inscrição."
    ),
    "link": {"label": "Baixar o material (PDF)", "url": "https://example.test/material.pdf"},
}
FAILED = {
    "title": "Enquanto a equipe resolve",
    "text": "Você pode começar pelo material de uma página.",
    "link": {"label": "Baixar o material (PDF)", "url": "https://example.test/material.pdf"},
}


def with_notices(notices: dict[str, Any] | None, **changes: Any) -> dict[str, Any]:
    """O demo (a versão 1 migrada) com os avisos dados e as mudanças pedidas."""
    document = legacy_definition("demo")
    if notices is not None:
        document["notices"] = notices
    return {**document, **changes}


def problems(document: dict[str, Any]) -> list[str]:
    with pytest.raises(DefinitionError) as caught:
        compile_definition(document)
    return [str(problem) for problem in caught.value.problems]


# --- o formato -----------------------------------------------------------------------------------


def test_a_definition_without_notices_compiles_to_a_screen_without_them() -> None:
    spec = compile_definition(with_notices(None))

    assert spec.screen is not None and spec.screen.notices == Notices()


def test_the_notices_of_a_definition_reach_the_screen_of_the_form() -> None:
    spec = compile_definition(with_notices({"waiting": WAITING, "failed": FAILED}))

    assert spec.screen is not None
    assert spec.screen.notices == Notices(
        waiting=Notice(
            WAITING["title"],
            WAITING["text"],
            NoticeLink("Baixar o material (PDF)", "https://example.test/material.pdf"),
        ),
        failed=Notice(
            FAILED["title"],
            FAILED["text"],
            NoticeLink("Baixar o material (PDF)", "https://example.test/material.pdf"),
        ),
    )


def test_each_page_has_its_own_optional_notice_and_the_link_is_optional() -> None:
    only_failed = compile_definition(
        with_notices({"failed": {"title": "Aviso", "text": "Só o texto."}})
    )

    assert only_failed.screen is not None
    assert only_failed.screen.notices == Notices(failed=Notice("Aviso", "Só o texto."))


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/material.pdf",
        "javascript:alert(1)",
        "data:text/html,<b>oi</b>",
        "ftp://example.test/material.pdf",
        "//example.test/material.pdf",
        "example.test/material.pdf",
        "/material.pdf",
        "https://",
        "https:///material.pdf",
        "https:example.test",
        "https://usuario:senha@example.test/material.pdf",
        "https://example.test/material de graça.pdf",
        "https://example.test\\@outro.com.br",
        "https://example.test/\x01",
        "https://example.test:porta/material.pdf",
    ],
)
def test_the_link_of_a_notice_must_be_an_https_address(url: str) -> None:
    link = {"label": "Baixar", "url": url}
    for page in ("waiting", "failed"):
        found = problems(with_notices({page: {"title": "Aviso", "text": "Texto.", "link": link}}))

        hint = "use um endereço que comece por https://, como https://example.test/material.pdf"
        assert found == [f"notices.{page}.link.url: {hint}"]  # o aviso não repete o que veio


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/material.pdf",
        "https://example.test/a?b=1&c=2#topo",
        "HTTPS://example.test/",
        "https://example.test:8443/material.pdf",
        "https://sub.example.test/material%20de%20graca.pdf",
    ],
)
def test_an_https_address_is_accepted(url: str) -> None:
    spec = compile_definition(
        with_notices(
            {"waiting": {"title": "Aviso", "text": "Texto.", "link": {"label": "Ir", "url": url}}}
        )
    )

    assert spec.screen is not None and spec.screen.notices.waiting is not None
    assert spec.screen.notices.waiting.link == NoticeLink("Ir", url)


@pytest.mark.parametrize(
    ("notice", "expected"),
    [
        ({"text": "Texto."}, "notices.waiting.title: campo obrigatório"),
        ({"title": "Aviso"}, "notices.waiting.text: campo obrigatório"),
        ({"title": "", "text": "Texto."}, "notices.waiting.title: não pode ficar vazio"),
        ({"title": "Aviso", "text": "x" * 2001}, "notices.waiting.text: use no máximo 2000"),
        (
            {"title": "Aviso", "text": "Texto.", "link": {"url": "https://example.test/"}},
            "notices.waiting.link.label: campo obrigatório",
        ),
        (
            {"title": "Aviso", "text": "Texto.", "link": {"label": "Ir"}},
            "notices.waiting.link.url: campo obrigatório",
        ),
        (
            {"title": "Aviso", "text": "Texto.", "html": "<b>oi</b>"},
            "notices.waiting.html: campo desconhecido",
        ),
        (
            {
                "title": "Aviso",
                "text": "Texto.",
                "link": {"label": "Ir", "url": "https://example.test/", "target": "_self"},
            },
            "notices.waiting.link.target: campo desconhecido",
        ),
    ],
)
def test_a_notice_needs_a_title_and_a_text_and_takes_nothing_else(
    notice: dict[str, Any], expected: str
) -> None:
    found = problems(with_notices({"waiting": notice}))

    assert any(line.startswith(expected) for line in found), found


def test_only_the_waiting_and_failed_pages_have_a_notice() -> None:
    found = problems(with_notices({"result": WAITING}))

    assert found == ["notices.result: campo desconhecido"]


# --- o que o aviso muda no resto do formulário -------------------------------------------------


def test_a_notice_is_page_text_and_never_goes_to_the_llm() -> None:
    before = with_notices(None)
    after = with_notices({"waiting": WAITING, "failed": FAILED})

    assert llm_changes(before, after) == ()
    assert llm_fingerprint(before) == llm_fingerprint(after)
