"""Simulação pelo HTTP administrativo, com respostas sintéticas e fila simulada."""


# ruff: noqa: F811

import json

import pytest
from test_admin_drafts import DRAFT, client, clock, hidden, post, worker  # noqa: F401
from test_admin_forms import DEFINITION

EXAMPLE = json.loads(DEFINITION)["examples"]["answers"]


def visual_answers(answers):
    """Os nomes de transporte HTML; o JSON continua usando os nomes do domínio."""
    return {"_simulation_" + name: value for name, value in answers.items()}


@pytest.mark.parametrize("invalid", [False, True])
def test_capture_validation_has_no_paid_command_identity(client, deps, invalid):
    from fastapi.testclient import TestClient

    from app.forms import drafts

    slug = drafts.create(
        deps, title="Pesquisa sintética", slug="local-capture", actor="operadora", generation="none"
    )
    path = f"/admin/forms/{slug}/draft"
    page = client.get(path + "/tests")
    assert page.status_code == 200
    assert 'name="command_id"' not in page.text
    with deps.sessions() as session:
        answers = drafts.load(session, slug).document["examples"]["answers"]
    if invalid:
        answers["email"] = "invalido"
    http = TestClient(client.app, follow_redirects=False, raise_server_exceptions=False)
    http.cookies.update(client.cookies)
    response = post(
        http,
        path + "/capture-test",
        input_mode="fields",
        expected=hidden(page.text, "expected"),
        command_id="identidade-ignorada-na-captura",
        **visual_answers(answers),
    )
    assert response.status_code == (422 if invalid else 200)
    assert 'name="command_id"' not in response.text
    if invalid:
        assert 'value="invalido"' in response.text
        assert 'aria-invalid="true"' in response.text


def test_simulation_renders_saved_fields_and_preserves_invalid_values(client):
    page = client.get(f"{DRAFT}/tests", follow_redirects=True)
    assert page.history[0].status_code == 303
    assert "command_id=" in str(page.url)
    assert 'name="_simulation_escritorio"' in page.text
    assert "Rascunho salvo" in page.text
    expected = hidden(page.text, "expected")
    response = post(
        client,
        f"{DRAFT}/prompt",
        input_mode="fields",
        expected=expected,
        _simulation_escritorio="0",
        date="2027-03-01",
    )
    assert response.status_code == 422
    assert 'value="0"' in response.text
    assert 'value="2027-03-01"' in response.text
    assert 'aria-invalid="true"' in response.text
    assert 'name="_privacy"' not in response.text


@pytest.mark.parametrize("action", ["prompt", "score", "tests"])
def test_visual_simulation_requires_current_saved_origin(client, worker, action):
    for expected in ("", "stale"):
        response = post(
            client,
            f"{DRAFT}/{action}",
            input_mode="fields",
            expected=expected,
            **visual_answers(EXAMPLE),
        )
        assert response.status_code == 409
        assert "O rascunho salvo mudou" in response.text
        assert worker.queued == []


def test_fields_and_json_produce_same_prompt_for_all_six_types(client, deps, worker):
    from test_admin_forms import SLUG

    from app.forms import drafts

    with deps.sessions() as session:
        draft = drafts.load(session, SLUG)
    document = json.loads(DEFINITION)
    document["steps"][0]["questions"] += [
        {"name": "telefone", "kind": "tel", "label": "Telefone fictício", "required": False},
        {
            "name": "canais",
            "kind": "choices",
            "label": "Canais fictícios",
            "required": False,
            "options": [{"value": "email", "label": "E-mail"}, {"value": "site", "label": "Site"}],
        },
    ]
    draft = drafts.replace_document(deps, SLUG, document, actor="testes", expected=draft.token)
    answers = {
        **EXAMPLE,
        "escritorio": "0",
        "telefone": "+55 11 99999-0000",
        "canais": ["email", "site"],
    }
    page = client.get(f"{DRAFT}/tests", follow_redirects=True)
    for kind in ('type="email"', 'type="tel"', 'type="checkbox"', "<textarea"):
        assert kind in page.text
    fields = post(
        client,
        f"{DRAFT}/prompt",
        input_mode="fields",
        expected=draft.token,
        date="2027-03-01",
        **visual_answers(answers),
    )
    technical = post(
        client,
        f"{DRAFT}/prompt",
        input_mode="json",
        expected=draft.token,
        date="2027-03-01",
        answers=json.dumps(answers),
    )
    assert fields.status_code == technical.status_code == 200
    from test_admin_forms import section_of

    assert section_of(fields, "prompt-montado") == section_of(technical, "prompt-montado")
    assert 'value="0"' in fields.text
    assert 'value="site" checked' in fields.text
    assert worker.queued == []


def test_visual_empty_answers_never_load_examples_and_json_empty_object_is_explicit(client):
    expected = hidden(client.get(f"{DRAFT}/tests", follow_redirects=True).text, "expected")
    empty = post(client, f"{DRAFT}/prompt", input_mode="fields", expected=expected)
    assert empty.status_code == 422
    empty_json = post(client, f"{DRAFT}/prompt", answers="{}")
    assert empty_json.status_code == 422
    example = post(client, f"{DRAFT}/prompt", answers="")
    assert example.status_code == 200
    assert "Com as respostas de exemplo" in example.text


def test_simulation_conditions_remove_hidden_questions_and_refuse_hidden_options(client, deps):
    from test_form_conditions import DEFINITION_TEXT, EMPLOYEE, SLUG

    from app.forms import stored

    stored.import_definition(deps, DEFINITION_TEXT, actor="testes")
    path = f"/admin/forms/{SLUG}/draft"
    expected = hidden(client.get(f"{path}/tests", follow_redirects=True).text, "expected")
    accepted = post(
        client,
        f"{path}/prompt",
        input_mode="fields",
        expected=expected,
        **visual_answers(
            {
                **EMPLOYEE,
                "clientes_proprios": "nao",
                "carteira": "um_a_dez",
                "nome_escritorio": "RESPOSTA_OCULTA_REMOVIDA",
            }
        ),
    )
    assert accepted.status_code == 200
    assert "RESPOSTA_OCULTA_REMOVIDA" not in accepted.text
    refused = post(
        client,
        f"{path}/prompt",
        input_mode="fields",
        expected=expected,
        **visual_answers({**EMPLOYEE, "carteira": "zero"}),
    )
    assert refused.status_code == 422


def test_only_confirmed_visual_generation_enqueues_and_snapshot_matches(client, deps, worker):
    from datetime import UTC, datetime

    from test_admin_actions_draft_tests import counts
    from test_admin_forms import SLUG

    from app.admin.actions import draft_tests

    expected = hidden(client.get(f"{DRAFT}/tests", follow_redirects=True).text, "expected")
    before = counts(deps)
    for action in ("prompt", "score"):
        assert (
            post(
                client,
                f"{DRAFT}/{action}",
                input_mode="fields",
                expected=expected,
                **visual_answers(EXAMPLE),
            ).status_code
            == 200
        )
    assert counts(deps) == before
    assert (
        post(
            client,
            f"{DRAFT}/tests",
            input_mode="fields",
            expected=expected,
            **visual_answers(EXAMPLE),
        ).status_code
        == 422
    )
    assert worker.queued == []
    started = post(
        client,
        f"{DRAFT}/tests",
        input_mode="fields",
        expected=expected,
        confirm="on",
        date="2027-03-01",
        **visual_answers(EXAMPLE),
    )
    assert started.status_code == 303
    assert len(worker.queued) == 1
    found = draft_tests.find(deps, SLUG, str(worker.queued[0]), now=datetime.now(UTC))
    assert found.result.definition_fingerprint == expected
    assert found.result.answers["nome"] == EXAMPLE["nome"]
    assert found.result.simulated_date.isoformat() == "2027-03-01"
    after = counts(deps)
    for table in ("submissions", "jobs", "deliveries", "capture_receipts", "capture_deliveries"):
        if table in before:
            assert after[table] == before[table]


def test_json_mode_also_requires_paid_confirmation(client, worker):
    response = post(client, f"{DRAFT}/tests", input_mode="json", answers=json.dumps(EXAMPLE))
    assert response.status_code == 422
    assert worker.queued == []


def test_capture_visual_preserves_errors_and_refuses_stale_origin(client, deps, worker):
    from test_admin_actions_draft_tests import counts

    from app.forms import drafts

    slug = drafts.create(
        deps,
        title="Pesquisa sintética",
        slug="simulation-capture",
        actor="operadora",
        generation="none",
    )
    path = f"/admin/forms/{slug}/draft"
    page = client.get(f"{path}/tests")
    expected = hidden(page.text, "expected")
    with deps.sessions() as session:
        example = drafts.load(session, slug).document["examples"]["answers"]
    before = counts(deps)
    response = post(
        client,
        f"{path}/capture-test",
        input_mode="fields",
        expected=expected,
        **visual_answers({**example, "email": "invalido"}),
        date="2027-03-01",
    )
    assert response.status_code == 422
    assert 'value="invalido"' in response.text
    assert 'aria-invalid="true"' in response.text
    stale = post(
        client,
        f"{path}/capture-test",
        input_mode="fields",
        expected="stale",
        _simulation_nome="Participante Sintética",
        _simulation_email="exemplo@example.test",
    )
    assert stale.status_code == 409
    accepted = post(
        client,
        f"{path}/capture-test",
        input_mode="fields",
        expected=expected,
        **visual_answers({**example, "email": "exemplo@example.test"}),
        date="2027-03-01",
    )
    assert accepted.status_code == 200
    assert "Respostas válidas" in accepted.text
    assert counts(deps) == before
    assert worker.queued == []


def test_invalid_simulated_date_has_navigable_error_and_keeps_input(client):
    expected = hidden(client.get(f"{DRAFT}/tests", follow_redirects=True).text, "expected")
    response = post(
        client,
        f"{DRAFT}/prompt",
        input_mode="fields",
        expected=expected,
        date="1999-01-01",
        **visual_answers(EXAMPLE),
    )
    assert response.status_code == 422
    assert 'href="#f-date"' in response.text
    assert 'id="f-date-erro"' in response.text
    assert 'value="1999-01-01"' in response.text


def test_visual_answer_names_cannot_override_admin_controls(client, deps, worker):
    from datetime import UTC, datetime

    from test_admin_forms import SLUG

    from app.admin.actions import draft_tests
    from app.forms import drafts

    with deps.sessions() as session:
        draft = drafts.load(session, SLUG)
    document = json.loads(DEFINITION)
    special = {
        "expected": "outra versão",
        "input_mode": "json",
        "answers": "{}",
        "date": "data respondida como texto",
        "confirm": "on",
        "csrf_token": "resposta sintética",
    }
    document["steps"][0]["questions"] += [
        {"name": name, "kind": "text", "label": "Pergunta " + name, "max_length": 160}
        for name in special
    ]
    document["examples"]["answers"].update(special)
    draft = drafts.replace_document(deps, SLUG, document, actor="testes", expected=draft.token)
    fields = {"_simulation_" + name: value for name, value in {**EXAMPLE, **special}.items()}
    page = client.get(f"{DRAFT}/tests", follow_redirects=True)
    for name in special:
        assert 'name="_simulation_' + name + '"' in page.text
    # A resposta confirm=on não autoriza a chamada paga, nem toma o lugar do checkbox administrativo.
    refused = post(
        client,
        f"{DRAFT}/tests",
        input_mode="fields",
        expected=draft.token,
        date="2027-03-01",
        **fields,
    )
    assert refused.status_code == 422
    assert worker.queued == []
    prompt = post(
        client,
        f"{DRAFT}/prompt",
        input_mode="fields",
        expected=draft.token,
        date="2027-03-01",
        **fields,
    )
    assert prompt.status_code == 200
    started = post(
        client,
        f"{DRAFT}/tests",
        input_mode="fields",
        expected=draft.token,
        confirm="on",
        date="2027-03-01",
        **fields,
    )
    assert started.status_code == 303
    found = draft_tests.find(deps, SLUG, str(worker.queued[0]), now=datetime.now(UTC))
    assert {name: found.result.answers[name] for name in special} == special
    assert found.result.simulated_date.isoformat() == "2027-03-01"
    assert found.result.definition_fingerprint == draft.token
