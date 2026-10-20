"""Conclusão versionada pela costura do compilador público de formulários."""

import pytest
from conftest import legacy_definition

from app.admin.form_editor import FormEdit
from app.forms.definition import DefinitionError, compile_definition
from app.forms.definition.changes import llm_changes, llm_fingerprint
from app.forms.definition.normal import same_meaning


def test_confirmation_is_versioned_without_changing_what_the_llm_reads() -> None:
    original = legacy_definition("demo")
    document = {
        **original,
        "completion": {"mode": "confirmation", "message": "Recebemos suas respostas."},
    }

    spec = compile_definition(document, version_number=2)

    assert spec.completion.mode == "confirmation"
    assert spec.completion.message == "Recebemos suas respostas."
    assert llm_changes(original, document) == ()
    assert llm_fingerprint(original) == llm_fingerprint(document)


def test_omitted_and_explicit_default_completion_have_the_same_meaning() -> None:
    original = legacy_definition("demo")
    assert compile_definition(original).completion.mode == "result"
    assert same_meaning(original, {**original, "completion": {"mode": "result"}})


@pytest.mark.parametrize("mode", ["button", "redirect"])
def test_external_completion_preserves_the_exact_configured_address(mode: str) -> None:
    url = "https://example.test/start?origin=form#next"
    spec = compile_definition(
        {**legacy_definition("demo"), "completion": {"mode": mode, "url": url}}
    )
    assert spec.completion.url == url
    assert spec.completion.button_label == "Continuar"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "http://example.test",
        "https://u:p@example.test",
        "//unsafe.example.test",
        "https://example.test/\nnext",
    ],
)
def test_external_completion_rejects_unsafe_addresses_without_echoing_them(url: str) -> None:
    with pytest.raises(DefinitionError) as caught:
        compile_definition(
            {**legacy_definition("demo"), "completion": {"mode": "redirect", "url": url}}
        )
    assert any(problem.path == "completion.url" for problem in caught.value.problems)
    assert url not in str(caught.value)


def test_external_completion_requires_an_address_at_its_field() -> None:
    with pytest.raises(DefinitionError) as caught:
        compile_definition({**legacy_definition("demo"), "completion": {"mode": "button"}})
    assert any(problem.path == "completion.url" for problem in caught.value.problems)


def test_completion_survives_the_structured_editor_without_entering_the_llm() -> None:
    document = {
        **legacy_definition("demo"),
        "completion": {
            "mode": "button",
            "url": "https://example.test/next",
            "message": "Obrigada!",
            "button_label": "Ver próximos passos",
        },
    }
    edited = FormEdit.of_document(document).screen()
    assert edited["completion"] == document["completion"]
