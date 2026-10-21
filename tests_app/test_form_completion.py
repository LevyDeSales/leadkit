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


def test_manual_release_and_handoff_survive_compiler_and_editor() -> None:
    from uuid import UUID

    receiver_id = UUID("00000000-0000-0000-0000-000000000010")
    document = {
        **legacy_definition("demo"),
        "release": {"mode": "manual"},
        "completion": {
            "mode": "redirect",
            "url": "https://example.test/next",
            "handoff": {"receiver_id": str(receiver_id), "receiver_revision": 2},
        },
    }
    spec = compile_definition(document)
    assert spec.release_mode == "manual"
    assert spec.completion.handoff.receiver_id == receiver_id
    edited = FormEdit.of_document(document).screen()
    assert edited["release"] == {"mode": "manual"}
    assert edited["completion"]["handoff"] == document["completion"]["handoff"]
    assert llm_fingerprint(document) == llm_fingerprint(legacy_definition("demo"))
    assert same_meaning(
        legacy_definition("demo"), {**legacy_definition("demo"), "release": {"mode": "automatic"}}
    )


@pytest.mark.parametrize("mode", ["result", "confirmation"])
def test_local_completion_rejects_handoff_at_its_field(mode):
    document = {
        **legacy_definition("demo"),
        "completion": {
            "mode": mode,
            "handoff": {
                "receiver_id": "00000000-0000-0000-0000-000000000010",
                "receiver_revision": 1,
            },
        },
    }
    with pytest.raises(DefinitionError) as caught:
        compile_definition(document)
    assert any(p.path == "completion.handoff" for p in caught.value.problems)


@pytest.mark.parametrize("mode", ["manual", "automatic"])
def test_capture_rejects_even_explicit_automatic_release(mode):
    from app.forms import starter

    document = starter.definition(slug="capture", title="Captura", generation="none")
    with pytest.raises(DefinitionError) as caught:
        compile_definition({**document, "release": {"mode": mode}})
    assert any(p.path == "release" for p in caught.value.problems)


@pytest.mark.parametrize(
    "extension",
    [
        {"release": {"mode": "automatic"}},
        {"release": {"mode": "manual"}},
        {"completion": {"mode": "result", "handoff": None}},
    ],
)
def test_draft_persistence_refuses_new_format_before_verified_floor_two(deps, extension):
    from app.deployment_contract import DeploymentContractError
    from app.forms import drafts

    with deps.sessions() as session:
        draft = drafts.load(session, "demo")
    with pytest.raises(DeploymentContractError):
        drafts.save_document(
            deps, "demo", {**draft.document, **extension}, actor="teste", expected=draft.token
        )
    with deps.sessions() as session:
        assert drafts.load(session, "demo").token == draft.token
