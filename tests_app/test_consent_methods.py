"""Método de consentimento versionado, pela compilação e pelas interfaces públicas."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.forms.definition import compile_definition
from app.forms.definition.changes import consent_version
from app.forms.definition.normal import normalized

DEMO = json.loads((Path(__file__).parent / "fixtures/demo_definition.json").read_text())


def definition(**consent):
    document = copy.deepcopy(DEMO)
    document["slug"] = "demo"
    document["consent"].update(consent)
    return document


def test_consent_configuration_has_independent_axes_and_legacy_defaults():
    old = compile_definition(DEMO)
    assert old.screen.consent.acquisition_method == "checkbox"
    assert old.screen.consent.presentation == "panel"
    assert old.screen.consent.position == "first_step"
    assert "acquisition_method" not in normalized(DEMO)["consent"]
    document = definition(
        acquisition_method="submit", presentation="compact", position="before_submit"
    )
    spec = compile_definition(document)
    assert spec.screen.consent.acquisition_method == "submit"
    assert spec.screen.consent.presentation == "compact"
    assert spec.screen.consent.position == "before_submit"
    assert consent_version(document, DEMO, number=2) != DEMO["consent"]["version"]
    visual = definition(presentation="compact", position="before_submit")
    assert consent_version(visual, DEMO, number=2) is None


def test_submit_records_declared_evidence_and_replays_first_fact(deps):
    from datetime import UTC, datetime

    import pytest
    import sqlalchemy as sa
    from conftest import DEMO_ANSWERS

    from app.intake import IdempotencyConflict, SubmissionIn, accept_submission
    from app.models import Submission

    spec = compile_definition(definition(acquisition_method="submit"))
    payload = {
        "answers": DEMO_ANSWERS,
        "consent": {
            "method": "submit",
            "action": "submit",
            "text_version": spec.consent_text_version,
            "marketing_email": True,
        },
    }
    body = SubmissionIn.model_validate(payload)
    now = datetime(2026, 10, 23, 12, tzinfo=UTC)

    def send(body, **kwargs):
        with deps.sessions.begin() as session:
            return accept_submission(
                session,
                spec,
                body,
                idempotency_key="submit-method",
                signer=deps.signer,
                link_ttl=deps.link_ttl,
                limits=deps.submission_limits,
                **kwargs,
            )

    accepted = send(body, now=now)
    assert not accepted.replay
    with deps.sessions() as session:
        evidence = session.scalars(sa.select(Submission)).one().consent
    assert evidence == {
        "evidence_schema": 2,
        "method": "submit",
        "action": "submit",
        "source": "public_api_declaration",
        "privacy_at": now.isoformat(),
        "text_version": spec.consent_text_version,
        "marketing_email": True,
        "marketing_whatsapp": False,
    }
    assert send(body, source="public_html").replay
    altered = SubmissionIn.model_validate(
        {**payload, "consent": {**payload["consent"], "marketing_email": False}}
    )
    with pytest.raises(IdempotencyConflict):
        send(altered)
    with deps.sessions() as session:
        assert session.scalars(sa.select(Submission)).one().consent == evidence


def test_mapping_accepts_both_methods_but_never_truthy_or_invalid_evidence():
    from dataclasses import replace

    from conftest import legacy_spec

    from app.integrations import mapping

    data = mapping.sample_data(legacy_spec("demo"), "synthetic", profile="capture")
    evidence = {
        "evidence_schema": 2,
        "method": "submit",
        "action": "submit",
        "source": "public_html",
        "privacy_at": "2026-10-23T12:00:00+00:00",
        "text_version": "demo-v1",
        "marketing_email": False,
        "marketing_whatsapp": False,
    }
    assert (
        mapping.raw_value("consent.privacy", replace(data, consent=evidence), profile="capture")
        is True
    )
    for invalid in (
        {"processing": {"text": "não é evidência"}},
        {**evidence, "action": "confirm"},
        {**evidence, "method": "unknown"},
        {**evidence, "privacy_at": "invalid"},
    ):
        assert (
            mapping.raw_value("consent.privacy", replace(data, consent=invalid), profile="capture")
            is False
        )


def test_html_submit_without_checkbox_records_only_final_valid_post(deps):
    import uuid

    import sqlalchemy as sa
    from conftest import publish
    from fastapi.testclient import TestClient
    from test_public import tags

    from app.api.main import create_app
    from app.models import Submission

    spec = compile_definition(
        definition(acquisition_method="submit", position="before_submit", presentation="compact"),
        version_number=2,
    )
    publish(deps, spec)

    async def trigger(job_id, attempt):
        return "synthetic-run"

    client = TestClient(create_app(trigger=trigger), follow_redirects=False)
    assert deps.forms.current("demo").screen.consent.acquisition_method == "submit"
    response = client.get("/p/demo")
    html = response.text
    assert "demo-v2" in html, html[:400]
    assert not tags(html, "input", name="_privacy")
    assert "Ao enviar" in html
    assert "consent--compact" in html
    assert len(tags(html, "input", name="_mkt_email")) == 1
    fields = {field["name"]: field.get("value", "") for field in tags(html, "input", type="hidden")}
    from conftest import DEMO_ANSWERS

    fields.update(DEMO_ANSWERS, _idem=str(uuid.uuid4()))
    invalid = client.post("/p/demo", data={**fields, "email": "invalid"})
    assert invalid.status_code == 422
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Submission)) == 0
    response = client.post("/p/demo", data=fields)
    assert response.status_code == 303, response.text
    with deps.sessions() as session:
        evidence = session.scalars(sa.select(Submission)).one().consent
    assert evidence["method"] == "submit" and evidence["source"] == "public_html"
    assert evidence["marketing_email"] is False


def test_api_method_is_decided_by_version_and_protected_details_agree(deps, api_client):
    import uuid

    from conftest import DEMO_ANSWERS, publish
    from fastapi.testclient import TestClient

    from app.api.main import create_app

    spec = compile_definition(definition(acquisition_method="submit"), version_number=2)
    publish(deps, spec)

    async def trigger(job_id, attempt):
        return "synthetic-run"

    client = TestClient(create_app(trigger=trigger))
    payload = {
        "answers": DEMO_ANSWERS,
        "consent": {
            "method": "submit",
            "action": "submit",
            "text_version": spec.consent_text_version,
        },
    }
    for consent in (
        {"privacy": True, "text_version": spec.consent_text_version},
        {**payload["consent"], "privacy": True},
        {**payload["consent"], "checked": True},
        {**payload["consent"], "source": "public_html"},
        {**payload["consent"], "action": "confirm"},
        {**payload["consent"], "marketing_whatsapp": True},
    ):
        response = client.post(
            "/v1/forms/demo/submissions",
            json={**payload, "consent": consent},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert response.status_code == 422, response.text
    response = client.post(
        "/v1/forms/demo/submissions", json=payload, headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    assert response.status_code == 202, response.text
    public_id = response.json()["public_id"]
    total = api_client("total")
    detail = total.get(f"/admin/api/v1/jobs/{public_id}", params={"include_lead_data": "true"})
    assert detail.status_code == 200
    evidence = detail.json()["lead_data"]["consent"]
    assert evidence["method"] == "submit" and evidence["source"] == "public_api_declaration"
    assert evidence["privacy_accepted_at"] and evidence["action"] == "submit"
    assert total.get(f"/admin/api/v1/jobs/{public_id}").json()["lead_data"] is None
    reader = api_client("leitura")
    assert (
        reader.get(
            f"/admin/api/v1/jobs/{public_id}", params={"include_lead_data": "true"}
        ).status_code
        == 403
    )


@pytest.mark.parametrize("method", ["checkbox", "submit"])
@pytest.mark.parametrize("generation", ["pdf", "none"])
@pytest.mark.parametrize("position", ["first_step", "before_submit"])
def test_versioned_methods_work_for_pdf_and_capture_with_separate_marketing(
    deps, method, generation, position
):
    import uuid

    import sqlalchemy as sa
    from conftest import DEMO_ANSWERS, publish
    from fastapi.testclient import TestClient
    from test_public import tags

    from app.api.main import create_app
    from app.models import Job, Submission

    document = definition(acquisition_method=method, position=position, presentation="compact")
    if generation == "none":
        document["generation"] = "none"
        document["completion"] = {"mode": "confirmation"}
        for key in ("instructions", "prompt", "plan", "pdf", "preview"):
            document.pop(key, None)
        document["examples"].pop("plan", None)
    spec = compile_definition(document, version_number=2)
    publish(deps, spec)

    async def trigger(*args):
        return "synthetic-run"

    client = TestClient(create_app(trigger=trigger), follow_redirects=False)
    page = client.get("/p/demo")
    assert page.status_code == 200
    assert bool(tags(page.text, "input", name="_privacy")) is (method == "checkbox")
    assert len(tags(page.text, "input", name="_mkt_email")) == 1
    fields = {
        field["name"]: field.get("value", "") for field in tags(page.text, "input", type="hidden")
    }
    fields.update(DEMO_ANSWERS, _idem=str(uuid.uuid4()))
    if method == "checkbox":
        assert client.post("/p/demo", data=fields).status_code == 422
        fields["_privacy"] = "1"
    else:
        assert client.post("/p/demo", data={**fields, "_privacy": "0"}).status_code == 422
    assert client.post("/p/demo", data={**fields, "_mkt_whatsapp": "1"}).status_code == 422
    stale = client.post("/p/demo", data={**fields, "_consent_version": "old"})
    assert stale.status_code == 409
    if method == "submit":
        assert "marque a autorização" not in stale.text
    response = client.post("/p/demo", data=fields)
    assert response.status_code == 303, response.text
    with deps.sessions() as session:
        submission = session.scalars(sa.select(Submission)).one()
        evidence = submission.consent
        assert session.scalar(sa.select(sa.func.count()).select_from(Job)) == (
            1 if generation == "pdf" else 0
        )
    assert evidence["method"] == method and evidence["source"] == "public_html"
    assert evidence["marketing_email"] is False and evidence["marketing_whatsapp"] is False
    assert evidence["privacy_at"]
    assert client.post("/p/demo", data=fields).status_code == 303
    assert client.post("/p/demo", data={**fields, "_mkt_email": "1"}).status_code == 409


def test_submit_uses_notice_without_requiring_inapplicable_checkbox_text():
    document = definition(acquisition_method="submit")
    document["consent"].pop("processing")
    assert compile_definition(document).screen.consent.processing is None
    from app.forms.definition import DefinitionError

    document["consent"]["acquisition_method"] = "checkbox"
    with pytest.raises(DefinitionError):
        compile_definition(document)


def test_legacy_defaults_are_not_injected_in_document_serialization():
    from app.forms.definition.document import parse

    consent = parse(DEMO).model_dump(mode="json")["consent"]
    assert not {"acquisition_method", "presentation", "position"} & consent.keys()
    verbose = definition(acquisition_method="checkbox", presentation="panel", position="first_step")
    assert normalized(verbose)["consent"] == normalized(DEMO)["consent"]
    assert consent_version(verbose, DEMO, number=2) is None


def test_discriminated_replay_uses_historical_version_after_publication(deps):
    import uuid

    from conftest import DEMO_ANSWERS, publish
    from fastapi.testclient import TestClient

    from app.api.main import create_app

    first = definition(acquisition_method="submit")
    first["consent"]["version"] = "demo-v2"
    publish(deps, compile_definition(first, version_number=2))

    async def trigger(*args):
        return "synthetic-run"

    client = TestClient(create_app(trigger=trigger))
    key = str(uuid.uuid4())
    payload = {
        "answers": DEMO_ANSWERS,
        "consent": {"method": "submit", "action": "submit", "text_version": "demo-v2"},
    }
    original = client.post(
        "/v1/forms/demo/submissions", json=payload, headers={"Idempotency-Key": key}
    )
    assert original.status_code == 202
    second = definition(acquisition_method="checkbox")
    second["consent"]["version"] = "demo-v3"
    publish(
        deps,
        compile_definition(first, version_number=2),
        compile_definition(second, version_number=3),
    )
    replay = client.post(
        "/v1/forms/demo/submissions", json=payload, headers={"Idempotency-Key": key}
    )
    assert replay.status_code == 202 and replay.json()["replay"] is True
    assert replay.json()["public_id"] == original.json()["public_id"]
    assert (
        client.post(
            "/v1/forms/demo/submissions",
            json=payload,
            headers={"Idempotency-Key": str(uuid.uuid4())},
        ).status_code
        == 409
    )
    incompatible = {**payload, "consent": {**payload["consent"], "text_version": "demo-v3"}}
    assert (
        client.post(
            "/v1/forms/demo/submissions",
            json=incompatible,
            headers={"Idempotency-Key": str(uuid.uuid4())},
        ).status_code
        == 422
    )


@pytest.mark.parametrize("method", ["checkbox", "submit"])
@pytest.mark.parametrize("generation", ["pdf", "none"])
def test_commit_failure_never_persists_consent_or_launches_work(deps, method, generation):
    import uuid

    import sqlalchemy as sa
    from conftest import DEMO_ANSWERS, publish
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session

    from app.api.main import create_app
    from app.models import Submission

    document = definition(acquisition_method=method)
    if generation == "none":
        document["generation"] = "none"
        document["completion"] = {"mode": "confirmation"}
        for key in ("instructions", "prompt", "plan", "pdf", "preview"):
            document.pop(key, None)
        document["examples"].pop("plan", None)
    spec = compile_definition(document, version_number=2)
    publish(deps, spec)
    launches = []

    async def trigger(*args):
        launches.append(args)
        return "synthetic-run"

    client = TestClient(create_app(trigger=trigger))

    def fail_commit(session):
        raise sa.exc.SQLAlchemyError("falha sintética de commit")

    consent = {
        "method": method,
        "action": "submit" if method == "submit" else "confirm",
        "text_version": spec.consent_text_version,
    }
    if method == "checkbox":
        consent["privacy"] = True
    sa.event.listen(Session, "before_commit", fail_commit)
    try:
        response = client.post(
            "/v1/forms/demo/submissions",
            json={"answers": DEMO_ANSWERS, "consent": consent},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    finally:
        sa.event.remove(Session, "before_commit", fail_commit)
    assert response.status_code == 503
    assert launches == []
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Submission)) == 0


def test_admin_put_patch_export_and_copy_preserve_consent_options(deps, api_client):
    from app.forms import stored

    client = api_client("total")
    document = definition(
        acquisition_method="submit", presentation="compact", position="before_submit"
    )
    document["slug"] = "consent-source"
    stored.import_definition(deps, json.dumps(document), actor="testes")
    path = "/admin/api/v1/forms/consent-source/draft"
    before = client.get(path).json()
    assert before["document"]["consent"] == document["consent"]
    changed = client.patch(
        path,
        json={
            "expected_version": before["version"],
            "patch": [{"op": "replace", "path": "/consent/position", "value": "first_step"}],
        },
    )
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert body["document"]["consent"]["acquisition_method"] == "submit"
    assert body["document"]["consent"]["position"] == "first_step"
    assert (
        client.put(
            path, json={"expected_version": body["version"], "document": body["document"]}
        ).json()["document"]
        == body["document"]
    )
    exported = client.get("/admin/api/v1/forms/consent-source/versions/1")
    assert exported.status_code == 200
    assert exported.json()["definition"]["consent"] == document["consent"]
    copied = client.post(
        "/admin/api/v1/forms",
        json={"title": "Cópia sintética", "slug": "consent-copy", "source": "consent-source"},
    )
    assert copied.status_code == 201, copied.text
    copy = client.get("/admin/api/v1/forms/consent-copy/draft").json()["document"]["consent"]
    assert copy == {**document["consent"], "version": "consent-copy-v1"}


def test_legacy_payload_records_new_evidence_but_historical_replay_never_gains_method(
    deps, api_client
):
    import uuid

    import sqlalchemy as sa
    from conftest import DEMO_ANSWERS, DEMO_CONSENT
    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from app.models import Submission

    async def trigger(*args):
        return "synthetic-run"

    client = TestClient(create_app(trigger=trigger))
    key = str(uuid.uuid4())
    body = {"answers": DEMO_ANSWERS, "consent": DEMO_CONSENT}
    original = client.post(
        "/v1/forms/demo/submissions", json=body, headers={"Idempotency-Key": key}
    )
    assert original.status_code == 202
    with deps.sessions.begin() as session:
        row = session.scalars(sa.select(Submission)).one()
        assert row.consent["method"] == "checkbox"
        assert row.consent["source"] == "public_api_declaration"
        historical = {
            k: v
            for k, v in row.consent.items()
            if k not in {"evidence_schema", "method", "action", "source"}
        }
        row.consent = historical
    replay = client.post(
        "/v1/forms/demo/submissions",
        json={**body, "consent": {**DEMO_CONSENT, "marketing_email": True}},
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 202 and replay.json()["replay"] is True
    with deps.sessions() as session:
        assert session.scalars(sa.select(Submission)).one().consent == historical
    total = api_client("total")
    consent = total.get(
        "/admin/api/v1/jobs/" + original.json()["public_id"], params={"include_lead_data": "true"}
    ).json()["lead_data"]["consent"]
    assert consent["marketing_email"] is False
    assert not {"method", "action", "source", "evidence_schema"} & consent.keys()


def test_logo_and_consent_survive_copy_editor_normalization_and_publication(deps, api_client):
    from fastapi.testclient import TestClient
    from test_form_opening_copy import load, source_with_logo
    from test_public import tags

    from app.admin.actions.previews import public_page
    from app.admin.form_editor import FormEdit
    from app.api.main import create_app
    from app.forms import drafts
    from app.forms.definition.normal import same_meaning

    source, image = source_with_logo(deps, generation="none")
    draft = load(deps, source)
    document = {
        **draft.document,
        "consent": {
            **draft.document["consent"],
            "acquisition_method": "submit",
            "presentation": "compact",
            "position": "before_submit",
        },
    }
    draft = drafts.replace_document(deps, source, document, actor="testes", expected=draft.token)
    screen = FormEdit.of_document(document).screen()
    reconstructed = {
        **document,
        **screen,
        "consent": {**screen["consent"], "version": document["consent"]["version"]},
    }
    assert same_meaning(document, reconstructed)
    assert reconstructed["opening_logo"] == document["opening_logo"]
    drafts.publish(
        deps, source, actor="testes", expected=draft.token, note="Logo e consentimento sintéticos"
    )
    client = api_client("total")
    copied = client.post(
        "/admin/api/v1/forms",
        json={"title": "Cópia composta", "slug": "consent-logo-copy", "source": source},
    )
    assert copied.status_code == 201, copied.text
    path = "/admin/api/v1/forms/consent-logo-copy/draft"
    body = client.get(path).json()
    assert body["document"]["opening_logo"] == document["opening_logo"]
    assert body["document"]["consent"]["acquisition_method"] == "submit"
    assert (
        client.put(
            path, json={"expected_version": body["version"], "document": body["document"]}
        ).status_code
        == 200
    )
    copied_draft = load(deps, "consent-logo-copy")
    preview = public_page(deps, "consent-logo-copy", 1)
    assert preview.screen.opening_logo.url == image.ref.url
    assert preview.screen.consent.acquisition_method == "submit"
    drafts.publish(
        deps,
        "consent-logo-copy",
        actor="testes",
        expected=copied_draft.token,
        note="Publicação composta sintética",
    )
    public = TestClient(create_app())
    html = public.get("/p/consent-logo-copy").text
    assert f'src="{image.ref.url}"' in html
    assert not tags(html, "input", name="_privacy")
    assert "Ao enviar" in html and "consent--compact" in html
