"""Uma identidade histórica indisponível não impede a consulta administrativa do pedido."""

from __future__ import annotations

import copy

import pytest
import sqlalchemy as sa
from admin_api_support import api_client
from test_admin import codes_on_the_page, login
from test_submission_identity import (
    LATER,
    ORIGINAL,
    contact_form,
    contact_submission,
)

from app import jobs
from app.deps import Deps
from app.forms.definition import fingerprint
from app.models import Form, FormVersion, Lead, Submission

DIAGNOSTIC = "Diagnóstico sintético preservado para a operadora"
GLOBAL_NAME = "Cadastro compartilhado não confiável"


@pytest.mark.parametrize("broken", ["missing_version", "uncompilable_template", "invalid_answers"])
def test_admin_keeps_the_order_and_diagnostics_when_contact_is_unavailable(
    deps: Deps, broken: str
) -> None:
    spec = contact_form(deps, "unavailable-contact")
    first = contact_submission(deps, spec, "first", ORIGINAL)
    other = contact_form(deps, "available-contact")
    second = contact_submission(deps, other, "second", LATER)
    with deps.sessions.begin() as session:
        jobs.claim(session, first.job.id, 1, "synthetic-run")
        jobs.fail(session, first.job.id, 1, "internal", DIAGNOSTIC)
        submission = session.get_one(Submission, first.job.submission_id)
        if broken == "missing_version":
            submission.form_version_number = 99
        elif broken == "invalid_answers":
            submission.answers = {**submission.answers, "email": "email-inválido"}
        else:
            previous = session.scalar(
                sa.select(FormVersion)
                .join(Form)
                .where(Form.slug == spec.slug, FormVersion.number == 1)
            )
            document = copy.deepcopy(previous.definition)
            document["pdf"]["html"] = "{{"
            session.add(
                FormVersion(
                    form_id=previous.form_id,
                    number=2,
                    definition=document,
                    fingerprint=fingerprint(document),
                    note="Fixture histórica",
                    published_by="testes",
                )
            )
            submission.form_version_number = 2
        session.execute(sa.update(Lead).values(name=GLOBAL_NAME, phone="11999999999"))
    with api_client(deps) as client:
        assert login(client).status_code == 303
        listing = client.get("/admin")
        assert listing.status_code == 200
        assert set(codes_on_the_page(listing.text)) == {first.job.public_id, second.job.public_id}
        assert GLOBAL_NAME not in listing.text
        assert "Contato indisponível" in listing.text
        detail = client.get(f"/admin/jobs/{first.job.public_id}")
        assert detail.status_code == 200
        assert DIAGNOSTIC in detail.text
        assert "Contato indisponível" in detail.text
        assert GLOBAL_NAME not in detail.text and "mailto:None" not in detail.text
        page = client.get("/admin/api/v1/jobs", params={"include_lead_data": True})
        assert page.status_code == 200
        data = {row["public_id"]: row for row in page.json()["items"]}
        assert set(data) == {first.job.public_id, second.job.public_id}
        assert data[first.job.public_id]["lead_data"]["name"] is None
        assert data[first.job.public_id]["lead_data"]["email"] is None
        assert data[second.job.public_id]["lead_data"]["name"] == LATER["name"]
        response = client.get(
            f"/admin/api/v1/jobs/{first.job.public_id}", params={"include_lead_data": True}
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["failure_code"] == "internal"
        assert [payload["lead_data"][key] for key in ("name", "email", "phone")] == [None] * 3
        assert payload["lead_data"]["answers"]["nome"] == ORIGINAL["name"]
        assert any(note["text"] == DIAGNOSTIC for note in payload["lead_data"]["notes"])
    with api_client(deps, role="leitura") as client:
        for path in ("/admin/api/v1/jobs", f"/admin/api/v1/jobs/{first.job.public_id}"):
            assert client.get(path, params={"include_lead_data": True}).status_code == 403
            response = client.get(path)
            assert response.status_code == 200
            assert ORIGINAL["name"] not in response.text and DIAGNOSTIC not in response.text


def test_api_search_uses_the_persisted_historical_identity_for_version_filters(
    deps: Deps, newer_demo
) -> None:
    from jobs_world import jobs_by_version

    made = jobs_by_version(deps, newer_demo)
    with deps.sessions() as session:
        assert (
            session.scalar(
                sa.select(FormVersion)
                .join(Form)
                .where(Form.slug == "demo", FormVersion.number == 2)
            )
            is not None
        )
    with api_client(deps) as client:
        response = client.get(
            "/admin/api/v1/jobs",
            params={
                "form": "demo",
                "form_version": "demo-v2",
                "q": "v2_again",
                "include_total": True,
                "include_lead_data": True,
            },
        )
        assert response.status_code == 200
        assert response.json()["total"] == 1
        assert [row["public_id"] for row in response.json()["items"]] == [
            made["v2_again"].public_id
        ]
        assert response.json()["items"][0]["lead_data"]["name"] == "Lead v2_again"
    with api_client(deps, role="leitura") as client:
        response = client.get("/admin/api/v1/jobs", params={"q": "v2_again", "include_total": True})
        assert response.status_code == 200
        assert response.json()["items"] == [] and response.json()["total"] == 0
