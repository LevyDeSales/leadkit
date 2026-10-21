"""Paridade de gestão HTTP/MCP, confirmação, versão e segredo utilizável uma vez."""

from uuid import uuid4

from admin_api_support import url_of
from admin_mcp_live import Live
from admin_mcp_world import Asker
from handoff_support import isolated_handoff_contract  # noqa: F401
from pydantic import SecretStr
from test_handoffs import NOW, configured

from app import integration_credentials


def test_manage_receiver_and_credentials_requires_total_confirmation_and_explicit_expiry(
    deps, api_client
):
    with deps.sessions.begin() as session:
        configured(session)
    reader, writer = api_client("leitura"), api_client("total")
    path = url_of("get_handoff_receiver", slug="synthetic")
    info = reader.get(path).json()
    assert info["version"] == "2"
    save = {
        "expected_version": "2",
        "name": "Atendimento",
        "entry_url": info["entry_url"],
        "reference_ttl_seconds": 3600,
        "confirm": True,
    }
    assert (
        reader.put(url_of("save_handoff_receiver", slug="synthetic"), json=save).status_code == 403
    )
    assert writer.put(path, json={**save, "confirm": False}).status_code == 422
    listing = reader.get(
        url_of("list_integration_credentials", slug="synthetic"), params={"limit": 1}
    ).json()
    assert len(listing["items"]) == 1 and "secret_hash" not in str(listing)
    creation = {
        "name": "Sintética",
        "receiver_revision": 1,
        "expected_receiver_version": "2",
        "idempotency_key": str(uuid4()),
        "confirm": True,
    }
    create_path = url_of("create_integration_credential", slug="synthetic")
    assert writer.post(create_path, json=creation).status_code == 422
    creation["expires_at"] = None
    first = writer.post(create_path, json=creation)
    assert first.status_code == 201, first.text
    created = first.json()
    assert created["secret"].startswith("lki_")
    repeated = writer.post(create_path, json=creation)
    assert (
        repeated.status_code == 409
        and repeated.json()["code"] == "credential_secret_already_issued"
    )
    assert created["secret"] not in repeated.text
    with deps.sessions() as session:
        assert integration_credentials.authenticate_credential(
            session, SecretStr(created["secret"]), now=NOW
        )
    rotated = writer.post(
        url_of("rotate_integration_credential", credential_id=created["credential"]["id"]),
        json={
            "expected_version": "1",
            "expires_at": None,
            "idempotency_key": str(uuid4()),
            "confirm": True,
        },
    )
    assert rotated.status_code == 200, rotated.text
    revoked = writer.post(
        url_of("revoke_integration_credential", credential_id=rotated.json()["credential"]["id"]),
        json={"expected_version": "1", "confirm": True},
    )
    assert revoked.status_code == 200 and revoked.json()["state"] == "revoked"
    enabled = writer.post(
        url_of("set_handoff_receiver_enabled", slug="synthetic"),
        json={"expected_version": "2", "enabled": False, "confirm": True},
    )
    assert enabled.status_code == 200
    assert writer.put(path, json=save).status_code == 409
    assert writer.put(path, json={**save, "expected_version": "3"}).status_code == 200


def test_mcp_derives_new_tools_confirms_and_exposes_only_the_new_usable_secret(
    deps, api_client, tmp_path
):
    with deps.sessions.begin() as session:
        configured(session)
    live = Live(api_client("total"), tmp_path)
    try:
        names = {
            "get_handoff_receiver",
            "save_handoff_receiver",
            "set_handoff_receiver_enabled",
            "list_integration_credentials",
            "create_integration_credential",
            "rotate_integration_credential",
            "revoke_integration_credential",
            "get_submission_handoff",
            "revoke_submission_handoff",
        }
        assert names <= live.tools.keys()
        assert "resolve_handoff_reference" not in live.tools
        arguments = {
            "slug": "synthetic",
            "name": "MCP sintético",
            "receiver_revision": 1,
            "expected_receiver_version": "2",
            "expires_at": None,
            "idempotency_key": str(uuid4()),
        }
        denied = live.call("create_integration_credential", arguments, Asker(False))
        assert denied.is_error
        created = live.ok("create_integration_credential", arguments, Asker(True))
        assert created["secret"].startswith("lki_") and created["secret"] != "**********"
        with deps.sessions() as session:
            assert integration_credentials.authenticate_credential(
                session, SecretStr(created["secret"]), now=NOW
            )
        output = live.call("create_integration_credential", arguments, Asker(True))
        assert output.is_error and created["secret"] not in output.text
    finally:
        live.close()


def test_get_and_revoke_submission_handoff_do_not_expose_pii_or_reference(deps, api_client):
    from test_handoffs import accept

    from app import handoffs
    from app.destinations import SecretCipher

    with deps.sessions.begin() as session:
        spec, _ = configured(session)
        accepted = accept(session, spec, deps)
        view = handoffs.completion_view(
            session,
            accepted.submission,
            spec,
            cipher=SecretCipher.from_settings(deps.settings),
            now=NOW,
        )
    reader, writer = api_client("leitura"), api_client("total")
    get_path = url_of("get_submission_handoff", submission_id=accepted.submission.id)
    info = reader.get(get_path)
    assert info.status_code == 200
    assert (
        "answers" not in info.text and "token_hash" not in info.text and view.url not in info.text
    )
    revoke_path = url_of("revoke_submission_handoff", submission_id=accepted.submission.id)
    assert (
        reader.post(revoke_path, json={"expected_version": "1", "confirm": True}).status_code == 403
    )
    assert writer.post(revoke_path, json={"expected_version": "1"}).status_code == 422
    revoked = writer.post(revoke_path, json={"expected_version": "1", "confirm": True})
    assert revoked.status_code == 200 and revoked.json()["revoked_at"]


def test_shared_definition_guard_returns_operational_503_instead_of_internal_error(
    deps, api_client
):
    from app.models import DeploymentContract

    client = api_client("total")
    draft = client.get(url_of("get_form_draft", slug="demo")).json()
    with deps.sessions.begin() as session:
        state = session.get(DeploymentContract, 1)
        state.phase = "blocked"
    response = client.patch(
        url_of("patch_form_draft", slug="demo"),
        json={
            "expected_version": draft["version"],
            "patch": [{"op": "add", "path": "/release", "value": {"mode": "manual"}}],
        },
    )
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "unavailable"


def test_management_matrix_matches_the_generated_contract_before_composed_snapshot(api_client):
    from admin_api_matrix import read
    from admin_api_support import admin_routes
    from test_admin_api_coverage import World, problems, schema_routes, the_editors

    app = api_client("total").app
    world = World(
        routes=admin_routes(app),
        schema_routes=schema_routes(app),
        document=app.state.admin_api.openapi(),
        editors=the_editors(),
    )
    assert problems(read(), world) == []
