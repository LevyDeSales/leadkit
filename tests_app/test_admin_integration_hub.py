"""A central compartilha fatos locais e seguros entre a página e a API/MCP."""

from test_admin_queries_destinations import store

from app.admin.queries import integration_hub
from app.deps import Deps
from app.models import Form


def overview(deps, **filters):
    with deps.sessions() as session:
        return integration_hub.overview(session, cipher=deps.cipher, **filters)


def test_lists_drafts_inactive_forms_and_unambiguous_default_inheritance(deps: Deps):
    with deps.sessions.begin() as session:
        session.add(
            Form(slug="default", title="Rascunho sintético", active=False, created_by="testes")
        )
    store(deps, None)
    found = overview(deps)
    rows = {row.key: row for row in found.items}
    inherited = rows["webhook:form:default"]
    assert inherited.scope_kind == "form" and inherited.form_slug == "default"
    assert inherited.form.published_version is None and inherited.form.active is False
    assert inherited.serving == "default" and inherited.configured is False
    assert inherited.receiver.scope_kind == "default" and inherited.receiver.form_slug is None
    assert rows["webhook:default"].form is None
    assert rows["baserow:form:default"].receiver is None
    assert found.catalog[1].configured == 1
    assert found.catalog[1].served_forms == 4
    filtered = overview(deps, form_slug="default")
    assert [row.key for row in filtered.items] == ["baserow:form:default", "webhook:form:default"]
    assert filtered.catalog == found.catalog


def test_history_separates_streams_revisions_and_effective_receiver_identity(deps: Deps):
    from datetime import UTC, datetime, timedelta

    import sqlalchemy as sa
    from test_integrations import completed_job, connect_form

    from app.models import CaptureDelivery, Delivery, IntegrationSync, WebhookDestination

    integration = connect_form(deps)
    store(deps, None)
    job = completed_job(deps)
    at = datetime(2026, 10, 20, 12, tzinfo=UTC)
    with deps.sessions.begin() as session:
        destination = session.scalar(sa.select(WebhookDestination))
        sync = session.scalar(sa.select(IntegrationSync))
        sync.status, sync.synced_at = "succeeded", at
        delivery = session.scalar(sa.select(Delivery))
        delivery.webhook_destination_id = destination.id
        delivery.config_version = destination.version
        delivery.status, delivery.delivered_at = "succeeded", at
        destination.version += 1
        session.add(
            CaptureDelivery(
                submission_id=job.submission_id,
                integration_id=integration.id,
                config_version=1,
                status="succeeded",
                delivered_at=at + timedelta(hours=1),
            )
        )
    rows = {r.key: r for r in overview(deps).items}
    baserow = rows["baserow:form:demo"].history
    assert baserow.pdf.succeeded == baserow.capture.succeeded == 1
    assert baserow.last_success.stream == "capture"
    assert baserow.last_success.configuration_match == "previous"
    webhook = rows["webhook:form:demo"].history
    assert webhook.pdf.succeeded == 1 and webhook.capture.succeeded == 0
    assert webhook.last_success.configuration_match == "previous"
    assert rows["webhook:default"].history.pdf.succeeded == 1
    assert rows["webhook:form:mcc"].history.last_success is None
    with deps.sessions.begin() as session:
        session.execute(sa.delete(CaptureDelivery))
        session.execute(
            sa.update(Delivery).values(
                webhook_destination_id=None,
                config_version=None,
                authorization_origin="legacy",
                authorized_attempt=None,
            )
        )
    rows = {r.key: r for r in overview(deps).items}
    assert rows["baserow:form:demo"].history.last_success.configuration_match == "unknown"
    assert rows["webhook:default"].history.last_success is None
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(Delivery).values(webhook_destination_id=__import__("uuid").uuid4())
        )
    assert {r.key: r for r in overview(deps).items}["webhook:default"].history.pdf.succeeded == 0


def test_api_and_page_share_local_redacted_data_and_work_without_javascript(
    deps: Deps, api_client, monkeypatch
):
    from conftest import BASEROW_TOKEN
    from test_admin_destinations import admin_client
    from test_integrations import connect_form

    connect_form(deps)
    store(deps, None, "https://hooks.example.test/private-sentinel?key=hidden-sentinel")
    client = api_client("leitura")

    def forbidden(*args, **kwargs):
        raise AssertionError("A central não pode chamar serviços externos")

    monkeypatch.setattr(deps.baserow, "fields", forbidden)
    monkeypatch.setattr(deps.webhook, "send", forbidden)
    monkeypatch.setattr(deps.llm, "generate", forbidden)
    monkeypatch.setattr(deps.pdf, "render", forbidden)
    monkeypatch.setattr(deps.storage, "put", forbidden)
    monkeypatch.setattr(deps.storage, "presigned_url", forbidden)
    response = client.get("/admin/api/v1/integrations", params={"form_slug": "demo"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [r["key"] for r in body["items"]] == ["baserow:form:demo", "webhook:form:demo"]
    assert body["items"][1]["receiver"]["host"] == "hooks.example.test"
    page = admin_client(deps).get("/admin/integrations?form_slug=demo")
    assert page.status_code == 200
    assert 'method="get"' in page.text and 'for="connector"' in page.text
    assert 'for="form_slug"' in page.text and "<caption" in page.text
    assert 'href="/admin/destinations/default#endereco"' in page.text
    assert 'href="/admin/destinations/forms/demo#endereco"' in page.text
    assert 'href="/admin/forms/demo/baserow?profile=pdf#mapeamento"' in page.text
    assert "Sem confirmação registrada" in page.text and "Usa padrão" in page.text
    for secret in (
        "private-sentinel",
        "hidden-sentinel",
        BASEROW_TOKEN,
        "token_sealed",
        "secret_sealed",
    ):
        assert secret not in response.text and secret not in page.text
    assert 'aria-current="page"' in page.text
    assert client.get("/admin/api/v1/integrations", params={"limit": 1}).json()["next_cursor"]


def test_filters_and_cursor_preserve_global_catalog_and_read_authorization(deps: Deps, api_client):
    from admin_api_support import make_api
    from test_admin import FakeClock
    from test_admin_destinations import admin_client

    client = api_client("leitura")
    first = client.get(
        "/admin/api/v1/integrations", params={"connector": "webhook", "limit": 2}
    ).json()
    assert [r["key"] for r in first["items"]] == ["webhook:default", "webhook:form:demo"]
    second = client.get(
        "/admin/api/v1/integrations",
        params={"connector": "webhook", "limit": 2, "cursor": first["next_cursor"]},
    ).json()
    assert [r["key"] for r in second["items"]] == ["webhook:form:mcc", "webhook:form:reforma-2027"]
    assert second["next_cursor"] is None and second["catalog"] == first["catalog"]
    for params in (
        {"connector": "sentinel-invalid"},
        {"limit": 0},
        {"limit": 201},
        {"cursor": "sentinel-invalid"},
    ):
        response = client.get("/admin/api/v1/integrations", params=params)
        assert response.status_code == 422 and response.json()["code"] == "validation_failed"
        assert "sentinel-invalid" not in response.text
    assert client.get("/admin/api/v1/integrations?form_slug=absent").status_code == 404
    anonymous = make_api(deps, FakeClock())
    assert anonymous.get("/admin/api/v1/integrations").status_code == 401
    assert anonymous.get("/admin/integrations").status_code == 303
    page = admin_client(deps).get("/admin/integrations?connector=webhook&limit=2")
    from html.parser import HTMLParser

    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links = []

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.links.append(dict(attrs).get("href", ""))

    parsed = Links()
    parsed.feed(page.text)
    next_url = next(url for url in parsed.links if "cursor=" in url)
    assert "connector=webhook" in next_url and "limit=2" in next_url


def test_capture_profiles_orphans_and_unreadable_credentials_are_local_context(
    deps: Deps, api_client
):
    from cryptography.fernet import Fernet
    from test_capture_deliveries import configured
    from test_integrations import connect_form

    from app.admin.integration_hub_views import actions
    from app.destinations import SecretCipher

    configured(deps)
    connect_form(deps, slug="orphan")
    store(deps, "orphan")
    store(deps, None)
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))
    rows = {r.key: r for r in overview(deps).items}
    capture = rows["baserow:form:capture-test"]
    assert capture.form.profile == "capture" and capture.receiver.active is True
    assert capture.mapping.capture_present is False
    assert capture.receiver.credential_unreadable is True
    assert rows["webhook:form:capture-test"].serving == "own"
    assert rows["webhook:form:capture-test"].receiver.events == ["submissao.persistida"]
    orphan = rows["baserow:form:orphan"]
    assert orphan.form.exists is False and orphan.form.profile is None
    assert actions(orphan) == []
    assert actions(rows["webhook:form:orphan"])[0][1] == "/admin/destinations/forms/orphan#endereco"
    assert (
        api_client("leitura").get("/admin/api/v1/integrations?form_slug=orphan").status_code == 200
    )


def test_mcp_uses_the_generated_read_contract_without_confirmation_or_lead_data(
    deps: Deps, api_client, tmp_path
):
    from admin_mcp_live import Live
    from admin_mcp_world import Asker

    store(deps, None)
    live = Live(api_client("leitura"), tmp_path)
    try:
        tool = live.tools["list_integrations"]
        assert tool.annotations == {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        }
        asker = Asker(False)
        data = live.ok("list_integrations", {"connector": "webhook", "form_slug": "demo"}, asker)
        assert asker.messages == []
        assert [r["key"] for r in data["items"]] == ["webhook:form:demo"]
        assert data["items"][0]["receiver"]["scope_kind"] == "default"
        assert "lead_data" not in repr(data) and "confirm" not in tool.locations
    finally:
        live.close()


def test_last_confirmation_serializes_in_utc_and_does_not_use_configuration_dates(
    deps: Deps, api_client
):
    from datetime import UTC, datetime

    import sqlalchemy as sa
    from test_integrations import completed_job, connect_form

    from app.models import IntegrationSync

    connect_form(deps)
    client = api_client("leitura")
    assert (
        client.get("/admin/api/v1/integrations?connector=baserow&form_slug=demo").json()["items"][
            0
        ]["history"]["last_success"]
        is None
    )
    completed_job(deps)
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(IntegrationSync).values(
                status="succeeded", synced_at=datetime(2026, 10, 20, 12, tzinfo=UTC)
            )
        )
    response = client.get("/admin/api/v1/integrations?connector=baserow&form_slug=demo")
    assert response.json()["items"][0]["history"]["last_success"] == {
        "at": "2026-10-20T12:00:00Z",
        "stream": "pdf",
        "configuration_match": "unknown",
    }


def test_uncompilable_published_form_remains_visible_without_echoing_its_document(
    deps: Deps, api_client
):
    from app.models import FormVersion

    with deps.sessions.begin() as session:
        broken = Form(slug="broken", title="Versão a revisar", active=True, created_by="testes")
        session.add(broken)
        session.flush()
        session.add(
            FormVersion(
                form_id=broken.id,
                number=1,
                definition={"private-sentinel": "private-value"},
                fingerprint="synthetic",
                note="Sintético",
                published_by="testes",
            )
        )
    response = api_client("leitura").get("/admin/api/v1/integrations?form_slug=broken")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 2
    assert all(r["form"]["profile"] is None and r["form_issues"] for r in response.json()["items"])
    assert "private-sentinel" not in response.text and "private-value" not in response.text


def test_distinct_connections_on_the_same_host_keep_identity_and_history(deps: Deps):
    from datetime import UTC, datetime

    import sqlalchemy as sa
    from test_integrations import completed_job, connect_form

    from app.models import Integration, IntegrationSync

    first = connect_form(deps)
    second = connect_form(deps, slug="mcc", active=False)
    completed_job(deps)
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(IntegrationSync).values(
                status="failed", synced_at=datetime(2026, 10, 21, tzinfo=UTC)
            )
        )
    rows = {r.key: r for r in overview(deps).items}
    a, b = rows["baserow:form:demo"], rows["baserow:form:mcc"]
    assert a.receiver.host == b.receiver.host and a.receiver.version != b.receiver.version
    assert a.receiver.active is True and b.receiver.active is False
    assert a.history.pdf.failed == 1 and a.history.last_success is None
    assert b.history.pdf.failed == 0
    with deps.sessions.begin() as session:
        session.execute(sa.delete(Integration).where(Integration.id == first.id))
    recreated = connect_form(deps)
    assert recreated.id not in (first.id, second.id)
    assert {r.key: r for r in overview(deps).items}["baserow:form:demo"].history.pdf.failed == 0


def test_contextual_readiness_uses_existing_rules_without_blending_connectors(deps: Deps):
    import sqlalchemy as sa
    from test_capture_deliveries import configured

    from app.models import Integration, WebhookDestination

    configured(deps)
    with deps.sessions.begin() as session:
        session.execute(sa.update(WebhookDestination).values(events=["plano.concluido"]))
    rows = {r.key: r for r in overview(deps, form_slug="capture-test").items}
    webhook = rows["webhook:form:capture-test"]
    baserow = rows["baserow:form:capture-test"]
    assert webhook.serving == "own"
    assert any("webhook efetivo não aceita" in issue.message for issue in webhook.form_issues)
    assert not any(
        "Baserow" in issue.message or "integração ativa" in issue.message
        for issue in webhook.form_issues
    )
    assert any("perfil incompatível" in issue.message for issue in baserow.form_issues)
    assert not any("webhook" in issue.message for issue in baserow.form_issues)
    with deps.sessions.begin() as session:
        session.execute(sa.update(Integration).values(active=False))
    assert overview(deps, connector="baserow", form_slug="capture-test").items[0].form_issues == []


def test_unconfigured_baserow_only_links_to_existing_connection_section(deps: Deps, api_client):
    from html.parser import HTMLParser

    from test_admin_destinations import admin_client

    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links = []

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                href = dict(attrs).get("href", "")
                if "/baserow" in href:
                    self.links.append(href)

    page = admin_client(deps).get("/admin/integrations?connector=baserow&form_slug=demo")
    links = Links()
    links.feed(page.text)
    assert links.links == ["/admin/forms/demo/baserow#conexao"] * 2


def test_readiness_snapshot_is_pure_and_preserves_connector_rules():
    from types import SimpleNamespace

    from test_capture import capture_document

    from app.forms.definition import compile_definition
    from app.integration_readiness import problems_for

    spec = compile_definition(capture_document())
    destination = SimpleNamespace(purpose="internal", events=["plano.concluido"])
    integration = SimpleNamespace(
        active=True, purpose="internal", capture_mapping=[], capture_key_field_id=None
    )
    found = problems_for(spec, destination=destination, integrations=[integration])
    assert [(p.path, p.message) for p in found] == [
        (
            "integrations",
            "O webhook efetivo não aceita submissao.persistida. Configure os eventos aceitos ou remova o destino, conferindo o padrão herdado.",
        ),
        (
            "integrations",
            "A integração ativa tem perfil incompatível com esta finalidade. Prepare o mapeamento ou desative a conexão antes de publicar.",
        ),
    ]
    integration.active = False
    destination.events = ["submissao.persistida"]
    assert problems_for(spec, destination=destination, integrations=[integration]) == []


def guidance_row(*, connector="baserow", profile="pdf"):
    """Associação sintética com contrato público da consulta local."""
    from app.admin.queries.integration_hub import HubForm, HubRow, MappingState, Receiver

    return HubRow(
        key=f"{connector}:form:demo",
        connector=connector,
        scope_kind="form",
        form_slug="demo",
        form=HubForm("demo", "Pesquisa sintética", True, True, 1, profile),
        configured=True,
        serving="own",
        receiver=Receiver(
            "form",
            "demo",
            "1",
            "example.test",
            1 if connector == "baserow" else None,
            "internal",
            "explicit",
            ["plano.concluido"],
            True if connector == "baserow" else None,
            False,
        ),
        mapping=MappingState(True, True) if connector == "baserow" else None,
        form_issues=[],
    )


def test_form_integration_next_action_matches_local_state():
    from dataclasses import replace

    from app.admin.integration_hub_views import actions, next_action

    row = replace(guidance_row(), configured=False, serving="none", receiver=None, mapping=None)
    assert next_action(row) == ("Configurar", "/admin/forms/demo/baserow#conexao")
    assert next_action(row) in actions(row)


def test_next_action_prioritizes_effective_receiver_mapping_and_profile_failures():
    from dataclasses import replace
    from datetime import UTC, datetime

    from app.admin.integration_hub_views import actions, next_action
    from app.admin.queries.integration_hub import (
        Counts,
        FormIssue,
        History,
        LastSuccess,
        MappingState,
    )

    pdf = guidance_row()
    capture = guidance_row(profile="capture")
    webhook = guidance_row(connector="webhook")
    inherited = replace(
        webhook,
        configured=False,
        serving="default",
        receiver=replace(
            webhook.receiver,
            scope_kind="default",
            form_slug=None,
        ),
    )
    cases = [
        (replace(pdf, receiver=replace(pdf.receiver, credential_unreadable=True)), "#conexao"),
        (
            replace(inherited, receiver=replace(inherited.receiver, credential_unreadable=True)),
            "/admin/destinations/default#endereco",
        ),
        (replace(pdf, mapping=MappingState(False, True)), "?profile=pdf#mapeamento"),
        (replace(capture, mapping=MappingState(True, False)), "?profile=capture#mapeamento"),
        (replace(pdf, form=replace(pdf.form, profile=None)), "#conexao"),
        (replace(pdf, form_issues=[FormIssue("integrations", "Perfil a revisar")]), "#mapeamento"),
        (
            replace(webhook, form_issues=[FormIssue("integrations", "Evento não aceito")]),
            "#eventos",
        ),
        (replace(pdf, receiver=replace(pdf.receiver, active=False)), "#ativacao"),
        (replace(pdf, history=History(pdf=Counts(failed=1))), "#sincronizacoes"),
        (replace(capture, history=History(capture=Counts(failed=1))), "#sincronizacoes"),
        (replace(webhook, history=History(pdf=Counts(failed=1))), "#teste"),
        (replace(pdf, history=History(capture=Counts(failed=7))), "#teste"),
        (inherited, "/admin/destinations/default#teste"),
    ]
    for row, suffix in cases:
        assert row.provider_verification == "not_checked"
        selected = next_action(row)
        assert selected in actions(row)
        assert selected[1].endswith(suffix), (row.key, suffix, selected)
    current = replace(
        pdf,
        history=History(
            last_success=LastSuccess(datetime(2026, 10, 22, tzinfo=UTC), "pdf", "current")
        ),
    )
    assert next_action(current) is None
    orphan = replace(pdf, form=replace(pdf.form, exists=False))
    assert next_action(orphan) is None


def render_guidance(rows):
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    from app.admin.integration_hub_views import actions, next_action
    from app.web.templating import templates

    return templates.get_template("admin/integrations.html").render(
        catalog=[],
        items=rows,
        forms=[row.form for row in rows if row.form],
        connector="",
        form_slug="demo",
        limit=50,
        next_url=None,
        actions=actions,
        next_action=next_action,
        hub_timezone=ZoneInfo("America/Sao_Paulo"),
        session=SimpleNamespace(csrf_token="synthetic", username="Operadora sintética"),
        flash=None,
        env_label="Teste local",
        version_label="Teste sintético",
        operator_guide_url="https://example.test/guia",
    )


def test_integration_statuses_remain_distinct():
    from dataclasses import replace

    row = guidance_row(connector="webhook")
    inherited = replace(
        row,
        configured=False,
        serving="default",
        receiver=replace(
            row.receiver,
            scope_kind="default",
            form_slug=None,
        ),
    )
    html = render_guidance([inherited])
    assert "Para onde vão as respostas de Pesquisa sintética?" in html
    assert "Usa padrão" in html
    assert "Sem receptor" not in html
    assert "Sem verificação registrada" in html
    assert "Sem confirmação registrada" in html
    assert "Alterar o padrão afeta também outros formulários" in html
    assert "Próximo passo" in html
    assert "Outras ações" in html
    assert "Histórico de entregas" in html
    assert "todas as revisões" in html


def test_previous_revision_is_not_current_success():
    from dataclasses import replace
    from datetime import UTC, datetime

    from app.admin.integration_hub_views import next_action
    from app.admin.queries.integration_hub import History, LastSuccess

    row = guidance_row()
    for match in ("previous", "unknown"):
        historical = replace(
            row,
            history=History(
                last_success=LastSuccess(
                    datetime(2026, 10, 22, tzinfo=UTC),
                    "pdf",
                    match,
                )
            ),
        )
        assert next_action(historical)[0] == "Abrir teste"
        html = render_guidance([historical])
        assert "Não confirma a configuração atual" in html
        assert "Sem verificação registrada" in html
        assert "Consultar campos da planilha no Baserow" in html
        assert "consulta os campos ao vivo" in html


def test_explicit_baserow_detail_preserves_live_fields_without_sending(deps, api_client):
    from conftest import FakeBaserow
    from test_admin_destinations import admin_client
    from test_integrations import connect_form

    connect_form(deps)
    provider = FakeBaserow().install(deps)
    provider.calls.clear()
    client = admin_client(deps)
    page = client.get("/admin/forms/demo/baserow?profile=pdf#mapeamento")
    assert page.status_code == 200
    assert "Esta página consulta os campos atuais no Baserow ao abrir" in page.text
    assert len(provider.calls) == 1
    assert all(request.method == "GET" for request in provider.requests)
    assert "Relatório" in page.text and "Mapeamento" in page.text
    provider.calls.clear()
    from admin_api_support import url_of

    response = api_client("leitura").get(url_of("get_form_integration", slug="demo"))
    assert response.status_code == 200
    assert len(provider.calls) == 1
    assert all(request.method == "GET" for request in provider.requests)


def test_hub_guidance_uses_route_context_and_keeps_navigation_local(
    deps, api_client, tmp_path, monkeypatch
):
    from admin_mcp_live import Live
    from conftest import FakeBaserow
    from test_admin_destinations import admin_client
    from test_integrations import connect_form

    connect_form(deps)
    store(deps, None)
    provider = FakeBaserow().install(deps)

    def forbidden(*args, **kwargs):
        raise AssertionError("Navegação local não produz envio nem geração")

    monkeypatch.setattr(deps.webhook, "send", forbidden)
    monkeypatch.setattr(deps.llm, "generate", forbidden)
    monkeypatch.setattr(deps.pdf, "render", forbidden)
    page_client = admin_client(deps)
    for query in ("", "?form_slug=demo", "?connector=baserow&form_slug=demo"):
        page = page_client.get("/admin/integrations" + query)
        assert page.status_code == 200
        assert "Próximo passo: Abrir teste" in page.text
        assert "Consultar campos da planilha no Baserow" in page.text
        assert provider.calls == []
    client = api_client("leitura")
    response = client.get("/admin/api/v1/integrations?form_slug=demo")
    assert response.status_code == 200
    assert provider.calls == []
    live = Live(client, tmp_path)
    try:
        result = live.ok("list_integrations", {"form_slug": "demo"})
        assert len(result["items"]) == 2
        assert provider.calls == []
    finally:
        live.close()


def test_missing_form_profile_does_not_treat_inherited_receiver_as_absent():
    from dataclasses import replace

    from app.admin.integration_hub_views import next_action

    row = guidance_row(connector="webhook")
    row = replace(
        row,
        configured=False,
        serving="default",
        form=replace(row.form, profile=None),
        receiver=replace(row.receiver, scope_kind="default", form_slug=None),
    )
    assert next_action(row) == ("Configurar padrão", "/admin/destinations/default#endereco")
