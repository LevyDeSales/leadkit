"""Captura: promessa correta, conteúdo preservado e recuperação navegável de erros."""

import re
from copy import deepcopy
from html import unescape

import pytest
from admin_api_support import api_client
from fastapi.testclient import TestClient
from test_admin import login
from test_admin_drafts import hidden

from app.api.main import create_app
from app.forms import drafts


@pytest.mark.parametrize(
    "phrase", ["Estes dados identificam seu plano.", "Este diagnóstico gera uma orientação inicial"]
)
def test_new_capture_does_not_announce_plan_or_orientation(deps, phrase):
    slug = drafts.create(
        deps, title="Pesquisa sintética", slug="ux-repro", actor="operadora", generation="none"
    )
    with deps.sessions() as session:
        draft = drafts.load(session, slug)
    drafts.publish(deps, slug, actor="operadora", note="Publicação sintética", expected=draft.token)
    with TestClient(create_app()) as client:
        page = client.get("/p/" + slug)
    assert page.status_code == 200
    assert phrase not in page.text


@pytest.mark.parametrize(
    "data,field",
    [
        ({"answers": '{"nome":"Participante Sintética"}'}, "capture-answers"),
        ({"date": "1999-01-01"}, "capture-date"),
    ],
)
def test_local_error_is_associated_and_navigable(deps, data, field):
    slug = drafts.create(
        deps, title="Pesquisa sintética", slug="ux-repro", actor="operadora", generation="none"
    )
    api = api_client(deps)
    assert login(api).status_code == 303
    page = api.get(f"/admin/forms/{slug}/draft/tests")
    response = api.post(
        f"/admin/forms/{slug}/draft/capture-test",
        data={"csrf_token": hidden(page.text, "csrf_token"), **data},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 422
    control = re.search(
        r'<(?:input|textarea)\b[^>]*id="' + field + r'"[^>]*>', response.text
    ).group(0)
    assert 'aria-invalid="true"' in control
    described = re.search(r'aria-describedby="([^"]+)"', control).group(1)
    assert f'id="{described}"' in response.text
    assert "autofocus" in control
    for value in data.values():
        assert value in unescape(response.text)
    assert 'href="#' + field + '"' in response.text


@pytest.mark.parametrize("generation", ["pdf", "none"])
def test_copy_keeps_the_operators_existing_step_text(deps, generation):
    slug = drafts.create(
        deps, title="Pesquisa sintética", slug="original", actor="operadora", generation="none"
    )
    with deps.sessions() as session:
        draft = drafts.load(session, slug)
    document = deepcopy(draft.document)
    custom = "Texto escolhido pela operadora: seu plano de acompanhamento."
    document["steps"][0]["help"] = custom
    drafts.save_document(deps, slug, document, actor="operadora", expected=draft.token)
    copied = drafts.create(
        deps,
        title="Cópia sintética",
        slug="copy",
        source=slug,
        generation=generation,
        actor="operadora",
    )
    with deps.sessions() as session:
        assert drafts.load(session, copied).document["steps"][0]["help"] == custom
        assert drafts.load(session, slug).document["steps"][0]["help"] == custom


def test_pdf_starter_keeps_its_legitimate_plan_promise(deps):
    from app.forms import starter

    document = starter.definition(slug="pdf", title="Plano sintético")
    assert "Estes dados identificam seu plano." in document["steps"][0]["help"]
    with TestClient(create_app()) as client:
        page = client.get("/p/demo")
    assert page.status_code == 200
    assert "Este diagnóstico gera uma orientação inicial" in page.text
