"""A integração com o Baserow no painel admin, pela costura HTTP (TestClient), contra o Baserow de
mentira: conectar, mapear, o relatório, o teste, ativar, o estado de cada sincronização e o reenvio."""

from __future__ import annotations

import logging
import re
import threading
import uuid

import pytest
import sqlalchemy as sa
from conftest import (
    BASEROW_TABLE,
    BASEROW_TOKEN,
    BASEROW_URL,
    FakeBaserow,
    baserow_field,
)
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from test_admin import ORIGIN, section
from test_admin_destinations import admin_client, post, stored_text
from test_integration_mapping import FULL_MAPPING
from test_integrations import SLUG, completed_job, connect_form, sync_ids, sync_state

from app import workflows
from app.deps import Deps
from app.destinations import SecretCipher
from app.forms import drafts
from app.integrations import config, push, syncs
from app.models import AuditLog, IntegrationSync

PAGE = f"/admin/forms/{SLUG}/baserow"
FULL_FORM = {f"source_{entry.field_id}": entry.source for entry in FULL_MAPPING}


@pytest.fixture
def baserow(deps: Deps) -> FakeBaserow:
    return FakeBaserow().install(deps)


@pytest.fixture
def admin(deps: Deps, baserow: FakeBaserow) -> TestClient:
    return admin_client(deps)


def connect(client: TestClient, **changes: str) -> object:
    form = {
        "base_url": BASEROW_URL,
        "table_id": str(BASEROW_TABLE),
        "token": BASEROW_TOKEN,
        "version": "0",
        **changes,
    }
    return post(client, f"{PAGE}/connect", **form)


def stored_version(deps: Deps) -> int:
    with deps.sessions() as session:
        found = config.load(session, SLUG)
    assert found is not None
    return found.version


def mapped(client: TestClient, deps: Deps, **extra: str) -> object:
    return post(
        client, f"{PAGE}/mapping", version=str(stored_version(deps)), **{**FULL_FORM, **extra}
    )


def audited(deps: Deps, prefix: str = "integration.") -> list[AuditLog]:
    with deps.sessions() as session:
        return list(
            session.scalars(
                sa.select(AuditLog).where(AuditLog.action.like(f"{prefix}%")).order_by(AuditLog.at)
            )
        )


# --- a página ---------------------------------------------------------------------------------


def test_a_form_without_an_integration_offers_to_connect_it(admin: TestClient) -> None:
    form_page = admin.get(f"/admin/forms/{SLUG}").text
    page = admin.get(PAGE)

    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert "Conectar o Baserow" in section(form_page, "integracao")
    assert PAGE in section(form_page, "integracao")
    connection = section(page.text, "conexao")
    assert 'name="token"' in connection and 'type="password"' in connection
    assert 'id="mapeamento"' not in page.text and "Ativa" not in page.text


def test_an_unknown_form_has_no_integration_page(admin: TestClient) -> None:
    assert admin.get("/admin/forms/nao-existe/baserow").status_code == 404
    assert post(admin, "/admin/forms/nao-existe/baserow/connect", version="0").status_code == 404


def test_a_form_without_a_published_version_cannot_be_connected_yet(
    admin: TestClient, deps: Deps
) -> None:
    drafts.create(deps, title="Em rascunho", slug="em-rascunho", actor="operadora")

    page = admin.get("/admin/forms/em-rascunho/baserow")
    refused = post(
        admin,
        "/admin/forms/em-rascunho/baserow/connect",
        base_url=BASEROW_URL,
        table_id="7",
        token=BASEROW_TOKEN,
        version="0",
    )

    assert "ainda não tem versão publicada" in page.text
    assert refused.status_code == 422 and "Publique o formulário antes" in refused.text


def test_connecting_reads_the_table_keeps_the_token_sealed_and_shows_it_never(
    admin: TestClient, deps: Deps, baserow: FakeBaserow
) -> None:
    connected = connect(admin)

    assert connected.status_code == 303 and connected.headers["location"] == PAGE  # type: ignore[attr-defined]
    page = admin.get(PAGE)
    assert "Conectado ao Baserow" in page.text
    assert (
        BASEROW_TOKEN not in page.text and BASEROW_TOKEN not in admin.get("/admin/forms/demo").text
    )
    assert "guardado, cifrado" in section(page.text, "conexao")
    assert BASEROW_TOKEN not in stored_text(deps)  # nem no banco, nem na auditoria
    assert baserow.calls[0] == ("GET", f"fields/table/{BASEROW_TABLE}/")
    (created,) = audited(deps)
    assert (created.action, created.actor) == ("integration.create", "operadora")
    assert created.details["host"] == "baserow.example.test"
    form_page = section(admin.get(f"/admin/forms/{SLUG}").text, "integracao")
    assert "Desativada" in form_page and f"tabela {BASEROW_TABLE}" in form_page


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        ({"base_url": "baserow.example.test"}, "base_url", "https://"),
        ({"base_url": "http://169.254.169.254"}, "base_url", "link-local"),
        (
            {"base_url": "https://[::1"},
            "base_url",
            "não é válido",
        ),  # `urlsplit` levantava ValueError
        ({"table_id": "abc"}, "table_id", "número da tabela"),
        ({"table_id": "0"}, "table_id", "número da tabela"),
        ({"token": ""}, "token", "Cole o token"),
    ],
)
def test_a_connection_that_does_not_serve_is_refused_with_the_reason_next_to_the_field(
    admin: TestClient, deps: Deps, changes: dict[str, str], field: str, message: str
) -> None:
    refused = connect(admin, **changes)

    assert refused.status_code == 422  # type: ignore[attr-defined]
    assert message in refused.text  # type: ignore[attr-defined]
    assert f'id="{field}-erro"' in refused.text  # type: ignore[attr-defined]
    with deps.sessions() as session:
        assert config.load(session, SLUG) is None


def test_baserow_refusing_the_token_is_said_on_the_page_and_nothing_is_saved_or_echoed(
    admin: TestClient, deps: Deps
) -> None:
    wrong = "token-errado-do-teste"

    refused = connect(admin, token=wrong)

    assert refused.status_code == 422  # type: ignore[attr-defined]
    text = refused.text  # type: ignore[attr-defined]
    assert "A conexão não foi guardada" in text and "HTTP 401" in text
    assert wrong not in text and BASEROW_TOKEN not in text  # o token digitado não volta à tela
    assert BASEROW_URL in text  # o resto do que foi digitado volta
    with deps.sessions() as session:
        assert config.load(session, SLUG) is None


@pytest.mark.parametrize(
    ("status", "message"),
    [(404, "tabela não encontrada"), (503, "HTTP 503")],
)
def test_a_table_that_is_missing_or_a_baserow_that_is_down_is_reported_on_connect(
    admin: TestClient, baserow: FakeBaserow, status: int, message: str
) -> None:
    baserow.fail("GET", status)

    refused = connect(admin)

    assert refused.status_code == 422 and message in refused.text  # type: ignore[attr-defined]


def test_without_the_encryption_key_the_token_cannot_be_kept(admin: TestClient, deps: Deps) -> None:
    deps.cipher = None

    refused = connect(admin)

    assert refused.status_code == 422 and "WEBHOOK_ENCRYPTION_KEY" in refused.text  # type: ignore[attr-defined]


def test_reconnecting_with_a_blank_token_keeps_the_saved_one_and_a_new_table_drops_the_mapping(
    admin: TestClient, deps: Deps, baserow: FakeBaserow
) -> None:
    connect(admin)
    assert mapped(admin, deps).status_code == 303  # type: ignore[attr-defined]
    version = str(stored_version(deps))

    same_token = post(
        admin, f"{PAGE}/connect", base_url=BASEROW_URL, table_id="7", token="", version=version
    )
    assert same_token.status_code == 303
    with deps.sessions() as session:
        kept = config.stored_connection(session, deps.cipher, SLUG)
        assert kept is not None and kept.token == BASEROW_TOKEN
        assert len(config.load(session, SLUG).entries) == len(FULL_MAPPING)  # type: ignore[union-attr]

    other = FakeBaserow(table_id=9).install(deps)
    moved = post(
        admin,
        f"{PAGE}/connect",
        base_url=BASEROW_URL,
        table_id="9",
        token="",
        version=str(stored_version(deps)),
    )
    assert moved.status_code == 303 and other.calls  # leu a tabela nova com o token guardado
    with deps.sessions() as session:
        found = config.load(session, SLUG)
    assert found is not None and (found.entries, found.active, found.table_id) == ((), False, 9)
    assert "integration.connect" in [row.action for row in audited(deps)]


def test_a_new_server_asks_for_the_token_again_and_the_stored_one_never_goes_there(
    admin: TestClient, deps: Deps, baserow: FakeBaserow
) -> None:
    connect(admin)
    baserow.requests.clear()

    refused = post(
        admin,
        f"{PAGE}/connect",
        base_url="https://outro-baserow.example.test",
        table_id=str(BASEROW_TABLE),
        token="",
        version=str(stored_version(deps)),
    )

    assert refused.status_code == 422 and 'id="token-erro"' in refused.text
    assert "exige colar o token de novo" in refused.text
    assert "https://outro-baserow.example.test" in refused.text  # o que foi digitado volta
    # a página relê a tabela guardada, do servidor de sempre; nada vai ao endereço novo
    assert {str(r.url.host) for r in baserow.requests} <= {"baserow.example.test"}
    assert BASEROW_TOKEN not in refused.text
    with deps.sessions() as session:
        found = config.load(session, SLUG)
    assert found is not None and found.base_url == BASEROW_URL


def test_a_page_opened_before_the_integration_was_removed_says_it_changed_without_reading_baserow(
    admin: TestClient, baserow: FakeBaserow
) -> None:
    stale = connect(admin, token="", version="3")  # a página viu a versão 3; a integração sumiu

    assert stale.status_code == 303  # type: ignore[attr-defined]
    assert "A integração mudou" in admin.get(stale.headers["location"]).text  # type: ignore[attr-defined]
    assert baserow.calls == []  # nem tentou ler a tabela com um token vazio


def test_mapping_a_page_whose_integration_was_removed_says_it_changed_without_reading_baserow(
    admin: TestClient, deps: Deps, baserow: FakeBaserow
) -> None:
    connect(admin)
    version = str(stored_version(deps))
    post(admin, f"{PAGE}/remove", version=version, confirm="on")
    baserow.calls.clear()

    stale = post(admin, f"{PAGE}/mapping", version=version, **FULL_FORM)

    assert stale.status_code == 303  # type: ignore[attr-defined]
    assert "A integração mudou" in admin.get(stale.headers["location"]).text  # type: ignore[attr-defined]
    assert baserow.calls == []


def test_a_number_with_thousands_of_digits_is_refused_and_never_a_server_error(
    admin: TestClient, deps: Deps, baserow: FakeBaserow
) -> None:
    """`int()` de milhares de algarismos levanta ValueError: a versão, o número da tabela e o id de um
    campo são lidos sem ele."""
    huge = "9" * 5000
    connect(admin)

    stale = connect(admin, version=huge)
    notice = admin.get(stale.headers["location"]).text  # type: ignore[attr-defined]
    table = connect(admin, table_id=huge, version=str(stored_version(deps)))
    mapping = mapped(admin, deps, **{f"source_{huge}": "lead.name"})  # a chave é ignorada

    assert stale.status_code == 303 and "A integração mudou" in notice  # type: ignore[attr-defined]
    assert table.status_code == 422 and 'id="table_id-erro"' in table.text  # type: ignore[attr-defined]
    assert mapping.status_code == 303  # type: ignore[attr-defined]
    assert len(stored_entries(deps)) == len(FULL_MAPPING)


def stored_entries(deps: Deps) -> tuple[object, ...]:
    with deps.sessions() as session:
        found = config.load(session, SLUG)
    assert found is not None
    return found.entries


@pytest.mark.parametrize("version", ["²", "01", "9" * 5000], ids=["superscript", "zero", "huge"])
@pytest.mark.parametrize("action", ["activate", "deactivate", "remove"])
def test_a_version_that_is_not_a_canonical_number_is_a_notice_and_never_a_server_error(
    admin: TestClient, deps: Deps, action: str, version: str
) -> None:
    """`"²".isdigit()` é verdadeiro e `int("²")` levanta ValueError, como o `int()` de milhares de
    algarismos: a versão se lê como número canônico (`version_number`), então o que não é nunca bate. A
    página avisa que a integração mudou e não mexe em nada, ao ativar, desativar e remover."""
    connect(admin)
    mapped(admin, deps)
    before = (stored_version(deps), stored_entries(deps))

    response = post(admin, f"{PAGE}/{action}", version=version, confirm="on")

    assert response.status_code == 303  # type: ignore[attr-defined]
    notice = admin.get(response.headers["location"]).text  # type: ignore[attr-defined]
    assert "A integração mudou" in notice
    assert (stored_version(deps), stored_entries(deps)) == before
    with deps.sessions() as session:
        found = config.load(session, SLUG)
    assert found is not None and not found.active  # nenhuma das três mexeu


def test_a_stale_page_cannot_overwrite_a_newer_change(admin: TestClient, deps: Deps) -> None:
    connect(admin)
    stale = str(stored_version(deps))
    assert mapped(admin, deps).status_code == 303  # outra aba mudou a integração

    refused = post(admin, f"{PAGE}/deactivate", version=stale)

    assert refused.status_code == 303
    assert "A integração mudou" in admin.get(refused.headers["location"]).text


# --- o mapeamento -----------------------------------------------------------------------------


def test_the_mapping_lists_the_live_table_with_suggestions_and_the_fields_it_cannot_fill(
    admin: TestClient,
) -> None:
    connect(admin)

    mapping = section(admin.get(PAGE).text, "mapeamento")

    for name in ("Nome", "E-mail", "Código do pedido", "Equipe", "Desafio", "Criado em"):
        assert f"<b>{name}</b>" in mapping
    assert "campo de somente leitura: a integração não o preenche" in mapping
    name_select = re.search(r'id="fonte-101".*?</select>', mapping, re.DOTALL)
    assert name_select is not None
    assert '<option value="lead.name" selected>' in name_select.group(0)
    assert "sugerido pelo nome do campo" in mapping
    team = re.search(r'id="fonte-108".*?</select>', mapping, re.DOTALL)
    assert team is not None and "answer:tamanho_equipe" in team.group(0)
    assert "answer:principal_desafio" in team.group(0)  # a lista traz as respostas do formulário
    assert "lead.name" not in team.group(0)  # mas só as fontes que servem a uma seleção


def test_a_field_added_in_baserow_shows_up_on_the_next_visit(
    admin: TestClient, baserow: FakeBaserow
) -> None:
    connect(admin)
    assert "Novo campo" not in admin.get(PAGE).text

    baserow.fields.append(baserow_field(120, "Novo campo"))

    assert "<b>Novo campo</b>" in section(admin.get(PAGE).text, "mapeamento")


def test_saving_the_mapping_stores_it_and_shows_a_clean_report(
    admin: TestClient, deps: Deps
) -> None:
    connect(admin)

    saved = mapped(admin, deps)

    assert saved.status_code == 303  # type: ignore[attr-defined]
    page = admin.get(PAGE).text
    assert "Mapeamento salvo" in page
    report = section(page, "relatorio")
    assert "Tudo casa: nenhuma pendência" in report
    assert "4 de 4 casam" in report and "Só eu" in report
    selected = re.search(r'id="fonte-104".*?</select>', section(page, "mapeamento"), re.DOTALL)
    assert selected is not None and '<option value="job.public_id" selected>' in selected.group(0)
    with deps.sessions() as session:
        found = config.load(session, SLUG)
    assert found is not None and found.key_field_id == 104 and not found.active
    saved_audit = [row for row in audited(deps) if row.action == "integration.mapping"]
    assert saved_audit[0].details["fields"] == len(FULL_MAPPING)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"source_104": ""}, "código do pedido"),
        ({"source_108": "lead.name"}, "não recebe"),
        ({"source_108": "answer:nao_existe"}, "fonte que o formulário não tem"),
        ({"source_107": "lead.email"}, "não recebe"),
    ],
)
def test_a_mapping_that_does_not_serve_is_refused_with_what_to_fix_and_keeps_the_choices(
    admin: TestClient, deps: Deps, changes: dict[str, str], message: str
) -> None:
    connect(admin)

    refused = mapped(admin, deps, **changes)

    assert refused.status_code == 422  # type: ignore[attr-defined]
    problems = section(refused.text, "mapeamento")  # type: ignore[attr-defined]
    assert message in problems and "O mapeamento não foi guardado" in problems
    assert '<option value="lead.email" selected>' in problems  # as escolhas voltam
    with deps.sessions() as session:
        assert config.load(session, SLUG).entries == ()  # type: ignore[union-attr]


def test_a_refused_mapping_shows_nothing_for_the_field_the_person_just_cleared(
    admin: TestClient, deps: Deps
) -> None:
    connect(admin)
    mapped(admin, deps)  # o nome da lead está guardado no campo 101

    refused = mapped(admin, deps, source_101="", source_104="")  # tira o nome e o código

    name_select = re.search(r'id="fonte-101".*?</select>', refused.text, re.DOTALL)  # type: ignore[attr-defined]
    assert refused.status_code == 422 and name_select is not None  # type: ignore[attr-defined]
    assert "selected" not in name_select.group(0)  # voltou "Nada", e não o que estava guardado


def test_the_report_lists_the_options_the_table_does_not_have_and_blocks_the_activation(
    admin: TestClient, deps: Deps, baserow: FakeBaserow
) -> None:
    complete = list(baserow.fields)
    baserow.fields[7] = baserow_field(108, "Equipe", "single_select", options=("Só eu", "so  EU"))
    connect(admin)
    mapped(admin, deps)

    report = section(admin.get(PAGE).text, "relatorio")
    refused = post(admin, f"{PAGE}/activate", version=str(stored_version(deps)))

    assert "Pendências que impedem a ativação" in report
    assert "3 opções do formulário não têm par no Baserow" in report
    assert "De 2 a 5 pessoas <strong>— sem par no Baserow</strong>" in report
    assert "1 de 4 casam" in report
    assert refused.status_code == 422 and "relatório ainda tem pendências" in refused.text
    with deps.sessions() as session:
        assert not config.load(session, SLUG).active  # type: ignore[union-attr]

    baserow.fields[:] = complete  # a operadora acrescentou as opções no Baserow
    activated = post(admin, f"{PAGE}/activate", version=str(stored_version(deps)))
    assert activated.status_code == 303
    assert "Integração ativada" in admin.get(activated.headers["location"]).text


def test_saving_a_mapping_with_pending_options_deactivates_an_active_integration(
    admin: TestClient, deps: Deps, baserow: FakeBaserow
) -> None:
    connect(admin)
    mapped(admin, deps)
    post(admin, f"{PAGE}/activate", version=str(stored_version(deps)))
    baserow.fields[7] = baserow_field(108, "Equipe", "single_select", options=("Só eu",))

    saved = mapped(admin, deps)

    assert saved.status_code == 303  # type: ignore[attr-defined]
    assert "foi desativada" in admin.get(PAGE).text
    with deps.sessions() as session:
        assert not config.load(session, SLUG).active  # type: ignore[union-attr]
    changes = [row for row in audited(deps) if row.action == "integration.mapping"]
    assert changes[-1].details["deactivated"] is True


def test_a_clean_remap_keeps_an_active_integration_active(admin: TestClient, deps: Deps) -> None:
    connect(admin)
    mapped(admin, deps)
    post(admin, f"{PAGE}/activate", version=str(stored_version(deps)))

    mapped(admin, deps, source_105="")  # tira as respostas em JSON

    with deps.sessions() as session:
        found = config.load(session, SLUG)
    assert found is not None and found.active and len(found.entries) == len(FULL_MAPPING) - 1


def test_a_baserow_that_stops_answering_leaves_the_page_up_with_the_reason(
    admin: TestClient, deps: Deps, baserow: FakeBaserow
) -> None:
    connect(admin)
    mapped(admin, deps)
    baserow.fail("GET", 500, where="fields")

    page = admin.get(PAGE)

    assert page.status_code == 200
    assert "Não deu para ler os campos da tabela" in page.text and "HTTP 500" in page.text
    assert f"{len(FULL_MAPPING)} campos estão mapeados" in page.text
    assert 'id="relatorio"' not in page.text


# --- o teste e a ativação ---------------------------------------------------------------------


def test_the_test_writes_a_synthetic_row_and_says_where_to_find_it(
    admin: TestClient, deps: Deps, baserow: FakeBaserow
) -> None:
    connect(admin)
    mapped(admin, deps)

    first = post(admin, f"{PAGE}/test")
    second = post(admin, f"{PAGE}/test")

    result = section(first.text, "teste")
    assert first.status_code == 200 and "A linha de teste foi criada" in result
    assert "teste-demo" in result and "não a apaga" in result
    assert "A linha de teste foi atualizada" in section(second.text, "teste")
    assert len(baserow.rows) == 1
    row = baserow.row_where(104, "teste-demo")
    assert row["field_102"] == "teste@example.test" and row["field_108"] == 10801
    assert [item.details["outcome"] for item in audited(deps, "integration.test")] == [
        "created",
        "updated",
    ]


def test_a_failed_test_shows_the_reason_and_leaves_no_row(
    admin: TestClient, deps: Deps, baserow: FakeBaserow
) -> None:
    connect(admin)
    mapped(admin, deps)
    baserow.fail("GET", 401, where="fields")  # o token foi revogado no Baserow depois de conectar

    result = section(post(admin, f"{PAGE}/test").text, "teste")

    assert "O envio de teste não passou" in result and "HTTP 401" in result
    assert baserow.rows == {}


def test_the_test_with_a_token_the_key_does_not_open_says_so_without_calling_baserow(
    admin: TestClient, deps: Deps, baserow: FakeBaserow
) -> None:
    connect(admin)
    calls = len(baserow.calls)
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))

    response = post(admin, f"{PAGE}/test")
    page = admin.get(PAGE)

    assert response.status_code == 409 and "não abre com WEBHOOK_ENCRYPTION_KEY" in response.text
    assert "O token guardado não abre" in section(page.text, "conexao")
    assert len(baserow.calls) == calls  # nem o teste nem a página falaram com o Baserow


def test_activating_and_deactivating_are_audited_and_shown(admin: TestClient, deps: Deps) -> None:
    connect(admin)
    mapped(admin, deps)

    activated = post(admin, f"{PAGE}/activate", version=str(stored_version(deps)))
    page = admin.get(activated.headers["location"]).text
    deactivated = post(admin, f"{PAGE}/deactivate", version=str(stored_version(deps)))

    assert "Integração ativada" in page and "<strong>Ativa:</strong>" in section(page, "ativacao")
    assert "<strong>Desativada:</strong>" in section(
        admin.get(deactivated.headers["location"]).text, "ativacao"
    )
    assert [row.action for row in audited(deps)] == [
        "integration.create",
        "integration.mapping",
        "integration.activate",
        "integration.deactivate",
    ]


def test_activating_without_a_mapping_is_not_offered_and_is_refused(
    admin: TestClient, deps: Deps
) -> None:
    connect(admin)

    page = admin.get(PAGE).text
    refused = post(admin, f"{PAGE}/activate", version=str(stored_version(deps)))

    assert "disabled" in section(page, "ativacao")
    assert refused.status_code == 422 and "Salve o mapeamento antes de ativar" in refused.text
    with deps.sessions() as session:
        assert not config.load(session, SLUG).active  # type: ignore[union-attr]


def test_removing_the_integration_needs_the_confirmation_and_keeps_the_rows_in_baserow(
    admin: TestClient, deps: Deps, baserow: FakeBaserow
) -> None:
    connect(admin)
    mapped(admin, deps)
    post(admin, f"{PAGE}/test")
    version = str(stored_version(deps))

    unconfirmed = post(admin, f"{PAGE}/remove", version=version)
    removed = post(admin, f"{PAGE}/remove", version=version, confirm="on")

    assert unconfirmed.status_code == 422 and "Confirme" in unconfirmed.text
    assert removed.status_code == 303 and removed.headers["location"] == f"/admin/forms/{SLUG}"
    assert "Integração removida" in admin.get(removed.headers["location"]).text
    with deps.sessions() as session:
        assert config.load(session, SLUG) is None
    assert len(baserow.rows) == 1  # o app não apaga nada no Baserow
    assert audited(deps)[-1].action == "integration.remove"


# --- o estado de cada sincronização -----------------------------------------------------------


def failed_sync(deps: Deps) -> tuple[str, uuid.UUID]:
    """Um pedido concluído com a integração ativa, cuja sincronização esgotou as tentativas."""
    job = completed_job(deps)
    (sync_id,) = sync_ids(deps)
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(IntegrationSync).values(
                attempts=4, last_error="[integration_unavailable] criar a linha: HTTP 503"
            )
        )
        syncs.mark_failed(session, sync_id, "integration_unavailable")
    return job.public_id, sync_id


def test_the_request_page_shows_each_sync_with_its_state_and_a_resend_for_the_failed_one(
    admin: TestClient, deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    public_id, sync_id = failed_sync(deps)

    page = admin.get(f"/admin/jobs/{public_id}")

    block = section(page.text, "sincronizacoes")
    assert "Baserow · baserow.example.test · tabela 7" in block
    assert "Falhou" in block and "criar a linha: HTTP 503" in block
    assert f"/admin/jobs/{public_id}/syncs/{sync_id}/resend" in block
    assert "Reenviar agora" in block


def test_a_synced_request_shows_what_happened_to_its_row(
    admin: TestClient, deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    job = completed_job(deps)
    (sync_id,) = sync_ids(deps)
    push.push(deps, sync_id)

    block = section(admin.get(f"/admin/jobs/{job.public_id}").text, "sincronizacoes")

    assert "Sincronizado" in block and "linha criada (nº 1)" in block
    assert "Reenviar agora" not in block


def test_a_request_without_integration_has_no_sync_section(admin: TestClient, deps: Deps) -> None:
    job = completed_job(deps)

    assert 'id="sincronizacoes"' not in admin.get(f"/admin/jobs/{job.public_id}").text


def test_resending_a_failed_sync_queues_it_again_and_launches_it(
    admin: TestClient, deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect_form(deps)
    public_id, sync_id = failed_sync(deps)
    launched: list[uuid.UUID] = []
    monkeypatch.setattr(workflows, "launch_sync", lambda target: launched.append(target) or "run-s")

    resent = post(admin, f"/admin/jobs/{public_id}/syncs/{sync_id}/resend")

    assert resent.status_code == 303 and resent.headers["location"] == f"/admin/jobs/{public_id}"
    assert launched == [sync_id]
    assert sync_state(deps, sync_id).status == "pending"
    page = admin.get(resent.headers["location"]).text
    assert "Reenvio disparado" in page
    (audit,) = audited(deps, "sync.resend")
    assert (audit.actor, audit.target_id) == ("operadora", str(sync_id))


def test_a_resend_that_never_reaches_the_engine_is_left_for_the_reconciler(
    admin: TestClient, deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect_form(deps)
    public_id, sync_id = failed_sync(deps)
    monkeypatch.setattr(workflows, "launch_sync", lambda target: None)

    resent = post(admin, f"/admin/jobs/{public_id}/syncs/{sync_id}/resend")

    assert "disparado automaticamente" in admin.get(resent.headers["location"]).text
    assert sync_state(deps, sync_id).status == "pending"


def test_only_a_failed_sync_of_that_request_can_be_resent(
    admin: TestClient, deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect_form(deps)
    job = completed_job(deps)
    (sync_id,) = sync_ids(deps)  # pendente
    monkeypatch.setattr(workflows, "launch_sync", lambda target: pytest.fail("não deveria"))

    pending = post(admin, f"/admin/jobs/{job.public_id}/syncs/{sync_id}/resend")
    refused = admin.get(pending.headers["location"]).text  # o aviso aparece uma vez
    other_job = post(admin, f"/admin/jobs/outro-pedido/syncs/{sync_id}/resend")
    garbage = post(admin, f"/admin/jobs/{job.public_id}/syncs/xyz/resend")

    assert "Só dá para reenviar uma sincronização que falhou" in refused
    assert other_job.status_code == 404 and garbage.status_code == 404
    assert sync_state(deps, sync_id).status == "pending"


def test_the_integration_page_counts_the_syncs_and_resends_every_failed_one(
    admin: TestClient, deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect_form(deps)
    public_id, sync_id = failed_sync(deps)
    done = threading.Event()
    launched: list[uuid.UUID] = []

    def launch(target: uuid.UUID) -> str:
        launched.append(target)
        done.set()
        return "run-s"

    monkeypatch.setattr(workflows, "launch_sync", launch)
    block = section(admin.get(PAGE).text, "sincronizacoes")
    assert "<dt>Com falha</dt><dd>1</dd>" in block and public_id in block
    assert "criar a linha: HTTP 503" in block
    assert "1 sincronização falhou" in section(admin.get(f"/admin/forms/{SLUG}").text, "integracao")

    resent = post(admin, f"{PAGE}/resend-failed")

    assert resent.status_code == 303
    assert "1 sincronizações voltaram para a fila" in admin.get(PAGE).text
    assert done.wait(5) and launched == [sync_id]
    assert sync_state(deps, sync_id).status == "pending"
    again = post(admin, f"{PAGE}/resend-failed")
    assert "Nenhuma sincronização falhada" in admin.get(again.headers["location"]).text
    assert [row.action for row in audited(deps, "sync.")] == ["sync.resend_failed"]


# --- segurança --------------------------------------------------------------------------------


def test_every_integration_action_needs_the_session_the_csrf_token_and_the_origin(
    deps: Deps, baserow: FakeBaserow
) -> None:
    anonymous = TestClient(
        __import__("app.api.main", fromlist=["create_app"]).create_app(trigger=None),
        follow_redirects=False,
    )
    anonymous_response = anonymous.post(f"{PAGE}/connect", data={}, headers=ORIGIN)
    admin = admin_client(deps)

    no_token = admin.post(f"{PAGE}/connect", data={"token": BASEROW_TOKEN}, headers=ORIGIN)
    other_origin = admin.post(
        f"{PAGE}/connect",
        data={"csrf_token": "x", "token": BASEROW_TOKEN},
        headers={"Origin": "https://outro.example.test"},
    )

    assert (
        anonymous_response.status_code == 303
        and "/admin/login" in anonymous_response.headers["location"]
    )
    assert no_token.status_code == 403 and other_origin.status_code == 403
    assert baserow.calls == []  # nada chegou ao Baserow


def test_the_token_never_appears_in_pages_logs_or_the_audit_trail(
    admin: TestClient, deps: Deps, baserow: FakeBaserow, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        responses = [
            connect(admin),
            admin.get(PAGE),
            mapped(admin, deps),
            admin.get(PAGE),
            post(admin, f"{PAGE}/test"),
            post(admin, f"{PAGE}/activate", version=str(stored_version(deps))),
            admin.get(f"/admin/forms/{SLUG}"),
            connect(admin, token="token-errado-do-teste", version=str(stored_version(deps))),
        ]
        job = completed_job(deps)
        (sync_id,) = sync_ids(deps)
        push.push(deps, sync_id)
        responses.append(admin.get(f"/admin/jobs/{job.public_id}"))

    assert not [r for r in responses if BASEROW_TOKEN in r.text]  # type: ignore[attr-defined]
    assert BASEROW_TOKEN not in caplog.text and "token-errado-do-teste" not in caplog.text
    assert BASEROW_TOKEN not in stored_text(deps)
    assert "token-errado-do-teste" not in stored_text(deps)
