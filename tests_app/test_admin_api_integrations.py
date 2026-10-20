"""A integração do formulário com o Baserow na API de admin (`/admin/api/v1`): `get_form_integration`,
`connect_form_integration` e `save_form_integration_mapping`, pela costura HTTP.

Elas agem pelas mesmas ações e pela mesma consulta que a página (`app.admin.actions.integrations`,
`app.admin.queries.integrations`): as conferências, a leitura da tabela de agora e a decisão de
desativar a integração são uma só, e a paridade com a página (o mesmo estado do banco, a mesma
auditoria e as mesmas chamadas ao Baserow, menos o autor) está no fim de cada operação que escreve.
O Baserow é o falso em memória do `conftest` (sobre `httpx.MockTransport`): a suíte não usa rede. As
chaves destes testes são sorteadas na execução, e o token do Baserow também (`BASEROW_TOKEN`).

**O token só entra.** Cada teste que lê ou escreve confere que ele não volta na resposta, no log, na
auditoria nem numa mensagem de erro.
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from admin_api_support import (
    ORIGIN,
    ApiClient,
    assert_same_effect,
    make_api,
    snapshot,
    url_of,
)
from conftest import (
    BASEROW_TABLE,
    BASEROW_TOKEN,
    BASEROW_URL,
    FakeBaserow,
    baserow_field,
)
from cryptography.fernet import Fernet
from test_admin import FakeClock, csrf_token, login
from test_admin_destinations import stored_text
from test_admin_integrations import failed_sync
from test_integration_mapping import FULL_MAPPING
from test_integrations import SLUG, connect_form

from app.deps import Deps
from app.destinations import SecretCipher
from app.forms import drafts
from app.integrations import config
from app.models import AuditLog

PAGE = "/admin"
SENTINEL = "ZZ_SENT_" + secrets.token_hex(8).upper()
Client = Callable[..., ApiClient]


@pytest.fixture
def baserow(deps: Deps) -> FakeBaserow:
    return FakeBaserow().install(deps)


def stored(deps: Deps) -> config.StoredIntegration | None:
    with deps.sessions() as session:
        return config.load(session, SLUG)


def audits(deps: Deps) -> list[AuditLog]:
    with deps.sessions() as session:
        return list(session.scalars(sa.select(AuditLog).order_by(AuditLog.at)))


# --- get_form_integration -----------------------------------------------------------------------


def read(client: ApiClient, slug: str = SLUG) -> httpx.Response:
    return client.get(url_of("get_form_integration", slug=slug))


def fields_of(body: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {item["id"]: item for item in body["fields"]}


def test_get_form_integration_needs_a_key_and_a_read_key_is_enough(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    baserow.calls.clear()
    anonymous = make_api(deps, FakeClock()).get(url_of("get_form_integration", slug=SLUG))

    reader = read(api_client("leitura"))

    assert anonymous.status_code == 401 and anonymous.json()["code"] == "invalid_credentials"
    assert reader.status_code == 200 and reader.headers["cache-control"] == "no-store"
    assert len(baserow.calls) == 1  # só a leitura com a chave certa chegou ao Baserow


def test_get_form_integration_of_a_form_that_does_not_exist_is_a_404_that_echoes_nothing(
    api_client: Client, baserow: FakeBaserow
) -> None:
    response = read(api_client("leitura"), SENTINEL)

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert SENTINEL not in response.text and baserow.calls == []


def test_get_form_integration_of_a_form_without_integration_offers_to_connect_it(
    api_client: Client, baserow: FakeBaserow
) -> None:
    response = read(api_client("leitura"))

    assert response.status_code == 200
    assert response.json() == {
        "form_slug": SLUG,
        "published": True,
        "connected": False,
        "connection": None,
        "mapping": [],
        "fields": None,
        "fields_error": None,
        "report": None,
        "sync_counts": {"pending": 0, "succeeded": 0, "failed": 0},
        "failed_syncs": [],
        "capture_deliveries": [],
        "capture_delivery_counts": {"pending": 0, "succeeded": 0, "failed": 0},
        "capture_mapping": [],
        "profile": "pdf",
    }
    assert baserow.calls == []  # sem conexão, nada a ler


def test_get_form_integration_of_a_form_without_a_published_version_says_so(
    api_client: Client, baserow: FakeBaserow, deps: Deps
) -> None:
    drafts.create(deps, title="Em rascunho", slug="em-rascunho", actor="testes")

    body = read(api_client("leitura"), "em-rascunho").json()

    assert (body["published"], body["connected"]) == (False, False)
    assert baserow.calls == []


def test_get_form_integration_gives_the_connection_without_ever_giving_the_token(
    deps: Deps, api_client: Client, baserow: FakeBaserow, caplog: pytest.LogCaptureFixture
) -> None:
    connect_form(deps, active=False)

    with caplog.at_level(logging.DEBUG):
        response = read(api_client("leitura"))

    assert response.status_code == 200
    connection = response.json()["connection"]
    assert connection["base_url"] == BASEROW_URL and connection["host"] == "baserow.example.test"
    assert (connection["table_id"], connection["version"]) == (BASEROW_TABLE, "2")
    assert (connection["active"], connection["mapped_fields"]) == (False, len(FULL_MAPPING))
    assert (connection["token_set"], connection["token_unreadable"]) == (True, False)
    assert connection["key_field_id"] == 104 and connection["created_by"] == "operadora"
    assert connection["created_at"].endswith("Z") and connection["token_changed_at"].endswith("Z")
    assert not {"token", "token_sealed", "secret"} & set(connection)
    assert BASEROW_TOKEN not in response.text and BASEROW_TOKEN not in caplog.text
    assert BASEROW_TOKEN not in stored_text(deps)


def test_get_form_integration_reads_the_live_table_and_suggests_a_source_for_each_field(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps, entries=[], active=False)  # conectada, nada mapeado
    baserow.calls.clear()

    response = api_client("leitura").get(url_of("get_form_integration", slug=SLUG))
    body = response.json()
    fields = fields_of(body)

    assert baserow.calls == [("GET", f"fields/table/{BASEROW_TABLE}/")]
    assert list(fields) == list(range(101, 111))  # a ordem da tabela
    assert (
        fields[101]["name"] == "Nome" and fields[101]["primary"] and fields[101]["type"] == "text"
    )
    assert (fields[101]["source"], fields[101]["suggested_source"]) == (None, "lead.name")
    assert fields[104]["suggested_source"] == "job.public_id"
    assert "lead.name" in fields[101]["sources"]
    assert fields[108]["sources"] == ["answer:tamanho_equipe", "answer:principal_desafio"]
    assert fields[108]["suggested_source"] is None  # "Equipe" não é o nome de nada
    assert (fields[110]["unsupported"], fields[110]["sources"]) == ("read_only", [])
    assert body["report"] is None  # nada mapeado: o relatório só existe depois de salvar


def test_get_form_integration_gives_the_saved_mapping_and_the_report_against_the_live_table(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)

    body = read(api_client("leitura")).json()

    assert body["mapping"] == [
        {"field_id": e.field_id, "name": e.name, "type": e.type, "source": e.source}
        for e in FULL_MAPPING
    ]
    fields = fields_of(body)
    assert (fields[101]["source"], fields[101]["suggested_source"]) == ("lead.name", None)
    assert fields[110]["source"] is None  # o que não está no mapeamento guardado
    report = body["report"]
    assert (report["ok"], report["problems"], report["unmatched_options"]) == (True, [], 0)
    equipe = next(item for item in report["fields"] if item["field_id"] == 108)
    assert (equipe["name"], equipe["type"], equipe["source"]) == (
        "Equipe",
        "single_select",
        "answer:tamanho_equipe",
    )
    assert {"value": "so_eu", "label": "Só eu", "baserow_option": "Só eu"} in equipe["options"]
    assert next(item for item in report["fields"] if item["field_id"] == 101)["options"] == []


def test_get_form_integration_reports_the_options_the_table_does_not_have(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    baserow.fields[7] = baserow_field(108, "Equipe", "single_select", options=("Só eu", "so  EU"))

    report = read(api_client("leitura")).json()["report"]

    assert report["ok"] is False and report["unmatched_options"] == 3
    equipe = next(item for item in report["fields"] if item["field_id"] == 108)
    assert [o["baserow_option"] for o in equipe["options"]].count(None) == 3
    assert any(
        o["label"] == "De 2 a 5 pessoas" and o["baserow_option"] is None for o in equipe["options"]
    )


def test_get_form_integration_says_why_when_baserow_does_not_answer_and_still_gives_the_rest(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    baserow.fail("GET", 500, where="fields")

    response = read(api_client("leitura"))

    body = response.json()
    assert response.status_code == 200  # a leitura não cai: o resto está no banco
    assert body["fields"] is None and body["report"] is None
    assert (
        "Não deu para ler a tabela" in body["fields_error"] and "HTTP 500" in body["fields_error"]
    )
    assert len(body["mapping"]) == len(FULL_MAPPING) and body["connection"]["mapped_fields"] == 9
    assert BASEROW_TOKEN not in response.text


def test_get_form_integration_of_a_token_that_does_not_open_says_so_without_calling_baserow(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))
    baserow.calls.clear()

    response = read(api_client("leitura"))

    body = response.json()
    assert response.status_code == 200 and baserow.calls == []
    assert (body["connection"]["token_set"], body["connection"]["token_unreadable"]) == (True, True)
    assert "não abre com a WEBHOOK_ENCRYPTION_KEY" in body["fields_error"]
    assert body["fields"] is None and body["report"] is None


def test_get_form_integration_counts_the_syncs_and_lists_the_ones_that_failed_without_free_text(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    public_id, sync_id = failed_sync(deps)  # "[integration_unavailable] criar a linha: HTTP 503"

    response = read(api_client("leitura"))

    body = response.json()
    assert body["sync_counts"] == {"pending": 0, "succeeded": 0, "failed": 1}
    (failed,) = body["failed_syncs"]
    assert failed["id"] == str(sync_id) and failed["job_public_id"] == public_id
    assert (failed["attempts"], failed["attempt"]) == (4, 1)
    assert failed["failure_code"] == "integration_unavailable"
    assert failed["created_at"].endswith("Z")
    assert "criar a linha" not in response.text  # o texto livre do erro só vai em `get_job`


def test_get_form_integration_writes_nothing(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    client = api_client("leitura")
    before = snapshot()
    trail = [entry.id for entry in audits(deps)]

    read(client)
    read(client)

    after = snapshot()
    # a chamada grava o último uso da chave, e só isso
    assert {table: rows for table, rows in after.items() if table != "api_keys"} == {
        table: rows for table, rows in before.items() if table != "api_keys"
    }
    assert [entry.id for entry in audits(deps)] == trail  # ler não audita


def test_get_form_integration_costs_five_tokens_of_the_usage_limit(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    client = api_client("leitura", API_RATE_LIMIT_PER_MINUTE="5")  # o balde tem 5 fichas

    first = read(client)
    second = read(client)

    assert first.status_code == 200
    assert second.status_code == 429 and second.json()["code"] == "rate_limited"
    assert len(baserow.calls) == 1  # a recusa não chegou ao Baserow


def test_get_form_integration_shows_what_the_page_shows_for_the_same_data(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    """Paridade de leitura: a página e a API leem a mesma tabela, uma vez cada, e mostram os mesmos
    campos, as mesmas escolhas e as mesmas pendências."""
    connect_form(deps, active=False)
    baserow.fields[7] = baserow_field(108, "Equipe", "single_select", options=("Só eu",))
    del baserow.fields[4]  # o campo 105 sumiu da tabela: uma pendência que não é de opção
    client = api_client("leitura")
    assert login(client).status_code == 303
    baserow.calls.clear()

    page = client.get(f"{PAGE}/forms/{SLUG}/baserow")
    by_page = list(baserow.calls)
    baserow.calls.clear()
    body = read(client).json()

    assert page.status_code == 200 and by_page == baserow.calls == [("GET", "fields/table/7/")]
    for item in body["fields"]:
        assert f"<b>{item['name']}</b>" in page.text
        if item["unsupported"]:
            assert "campo de somente leitura" in page.text  # sem escolha, nas duas
            assert item["sources"] == [] and item["source"] is None
            continue
        selected = re.search(rf'id="fonte-{item["id"]}".*?</select>', page.text, re.DOTALL)
        assert selected is not None
        chosen = re.findall(r'<option value="([^"]*)" selected>', selected.group(0))
        assert chosen == ([item["source"]] if item["source"] else [])
    assert f"{body['report']['unmatched_options']} opções do formulário não têm par" in page.text
    assert body["report"]["problems"] == ["O campo «Respostas» não existe mais na tabela."]
    for problem in body["report"]["problems"]:
        assert problem in page.text


# --- connect_form_integration -------------------------------------------------------------------

OMIT = object()  # tira o campo do corpo
WRONG_TOKEN = "token-errado-do-teste"


def connect_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "base_url": BASEROW_URL,
        "table_id": BASEROW_TABLE,
        "token": BASEROW_TOKEN,
        "expected_version": "0",
        "confirm": True,
        **changes,
    }
    return {key: value for key, value in body.items() if value is not OMIT}


def connect(client: ApiClient, slug: str = SLUG, **changes: Any) -> httpx.Response:
    return client.put(url_of("connect_form_integration", slug=slug), json=connect_body(**changes))


def fields_of_error(response: httpx.Response) -> dict[str, str]:
    return {item["field"]: item["message"] for item in response.json()["errors"]}


def version_of(deps: Deps) -> str:
    found = stored(deps)
    assert found is not None
    return str(found.version)


def test_connect_form_integration_needs_a_key_and_a_read_key_is_refused_and_nothing_happens(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    url = url_of("connect_form_integration", slug=SLUG)
    anonymous = make_api(deps, FakeClock()).put(url, json=connect_body())
    reader = connect(api_client("leitura"))

    assert anonymous.status_code == 401 and anonymous.json()["code"] == "invalid_credentials"
    assert reader.status_code == 403 and reader.json()["code"] == "forbidden_role"
    assert baserow.calls == [] and stored(deps) is None
    assert BASEROW_TOKEN not in anonymous.text + reader.text


def test_connect_form_integration_of_a_form_that_does_not_exist_is_a_404(
    api_client: Client, baserow: FakeBaserow
) -> None:
    response = connect(api_client("total"), slug=SENTINEL)

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert SENTINEL not in response.text and baserow.calls == []


def test_connect_form_integration_reads_the_table_stores_the_connection_and_never_gives_the_token(
    deps: Deps, api_client: Client, baserow: FakeBaserow, caplog: pytest.LogCaptureFixture
) -> None:
    client = api_client("total")

    with caplog.at_level(logging.DEBUG):
        response = connect(client)

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert (body["base_url"], body["table_id"], body["version"]) == (
        BASEROW_URL,
        BASEROW_TABLE,
        "1",
    )
    assert (body["active"], body["mapped_fields"], body["key_field_id"]) == (False, 0, None)
    assert (body["token_set"], body["token_unreadable"]) == (True, False)
    assert body["created_by"] == client.actor == body["updated_by"]
    assert baserow.calls == [("GET", f"fields/table/{BASEROW_TABLE}/")]
    assert baserow.requests[0].headers["Authorization"] == f"Token {BASEROW_TOKEN}"
    with deps.sessions() as session:
        opened = config.stored_connection(session, deps.cipher, SLUG)
    assert opened is not None and opened.token == BASEROW_TOKEN  # cifrado no banco, aberto aqui
    (entry,) = [e for e in audits(deps) if e.action == "integration.create"]
    assert entry.actor == client.actor and entry.details["host"] == "baserow.example.test"
    for place in (response.text, caplog.text, stored_text(deps)):
        assert BASEROW_TOKEN not in place
    assert not {"token", "token_sealed"} & set(body)


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        ({"table_id": 0}, "table_id", "maior ou igual a 1"),
        ({"table_id": -3}, "table_id", "maior ou igual a 1"),
        ({"table_id": 2**31}, "table_id", "menor ou igual a"),
        ({"table_id": "sete"}, "table_id", "número inteiro"),
        ({"base_url": "baserow.example.test"}, "base_url", "https://"),
        ({"base_url": "http://169.254.169.254"}, "base_url", "link-local"),
        (
            {"base_url": "https://[::1"},
            "base_url",
            "não é válido",
        ),  # `urlsplit` levantava ValueError
        ({"base_url": "https://baserow.example.test℀"}, "base_url", "não é válido"),
        ({"base_url": "https://usuario@baserow.example.test"}, "base_url", "usuário"),
        ({"token": OMIT}, "token", "Cole o token"),
        ({"token": "   "}, "token", "Cole o token"),
        ({"expected_version": OMIT}, "expected_version", "obrigatório"),
        ({"nao_existe": 1}, "nao_existe", "desconhecido"),
    ],
)
def test_connect_form_integration_refuses_what_does_not_serve_with_the_path_of_the_field(
    deps: Deps,
    api_client: Client,
    baserow: FakeBaserow,
    changes: dict[str, Any],
    field: str,
    message: str,
) -> None:
    response = connect(api_client("total"), **changes)

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    assert message in fields_of_error(response)[field]
    assert baserow.calls == [] and stored(deps) is None  # nada lido, nada gravado
    assert BASEROW_TOKEN not in response.text


def test_connect_form_integration_collects_the_reasons_the_page_collects(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    response = connect(api_client("total"), base_url="x", token=OMIT)

    assert response.status_code == 422
    assert set(fields_of_error(response)) == {"base_url", "token"}


def test_connect_form_integration_of_a_form_without_a_published_version_is_a_409_state_conflict(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    """Como `save_form_integration_mapping`: o estado do formulário é `409 state_conflict`, antes de
    olhar o corpo (mesmo um corpo inválido recebe este, e não um `422`)."""
    drafts.create(deps, title="Em rascunho", slug="em-rascunho", actor="testes")
    client = api_client("total")

    valid = connect(client, slug="em-rascunho")
    invalid = connect(client, slug="em-rascunho", base_url="x", token=OMIT)

    for response in (valid, invalid):
        assert response.status_code == 409 and response.json()["code"] == "state_conflict"
        assert "Publique o formulário antes" in response.json()["detail"]
    assert baserow.calls == []


@pytest.mark.parametrize(
    ("outcome", "message"),
    [(401, "HTTP 401"), (404, "tabela não encontrada"), (503, "HTTP 503")],
)
def test_connect_form_integration_with_a_table_the_token_cannot_read_is_a_502_and_saves_nothing(
    deps: Deps,
    api_client: Client,
    baserow: FakeBaserow,
    outcome: int,
    message: str,
) -> None:
    baserow.fail("GET", outcome)

    response = connect(api_client("total"), token=WRONG_TOKEN)

    assert response.status_code == 502 and response.json()["code"] == "upstream_failed"
    assert "Não deu para ler a tabela" in response.json()["detail"]
    assert message in response.json()["detail"]
    assert WRONG_TOKEN not in response.text and BASEROW_TOKEN not in response.text
    assert stored(deps) is None and audits(deps)[-1].action == "api_key.create"  # só a da chave


def test_connect_form_integration_with_a_token_baserow_refuses_does_not_echo_it(
    deps: Deps, api_client: Client, baserow: FakeBaserow, caplog: pytest.LogCaptureFixture
) -> None:
    """O eco da operação: os outros campos valem e só o token (texto livre) é o sentinela. O
    Baserow o recusa, e a mensagem que sobe diz o status, nunca o que foi mandado."""
    with caplog.at_level(logging.DEBUG):
        response = connect(api_client("total"), token=SENTINEL)

    assert response.status_code == 502 and "HTTP 401" in response.json()["detail"]
    assert SENTINEL not in response.text and SENTINEL not in str(response.headers)
    assert SENTINEL not in caplog.text and SENTINEL not in stored_text(deps)


def test_connect_form_integration_echoes_no_address_that_it_refuses_or_that_baserow_does_not_find(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    client = api_client("total")
    refused = connect(client, base_url=f"ftp://{SENTINEL}.example.test")
    not_found = connect(client, base_url=f"{BASEROW_URL}/{SENTINEL}")

    assert refused.status_code == 422 and not_found.status_code == 502
    for response in (refused, not_found):
        assert SENTINEL not in response.text and SENTINEL not in str(response.headers)


@pytest.mark.parametrize("confirm", [OMIT, False, "true", 1])
def test_connect_form_integration_needs_confirm_true_on_every_call_and_does_nothing_without_it(
    deps: Deps, api_client: Client, baserow: FakeBaserow, confirm: Any
) -> None:
    client = api_client("total")
    created = connect(client, confirm=confirm)  # criar também pede a confirmação
    connect_form(deps, active=False)
    before = stored(deps)
    trail = [e.id for e in audits(deps)]

    changed = connect(client, confirm=confirm, token="", expected_version=version_of(deps))

    for response in (created, changed):
        assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
        assert "apaga o mapeamento" in response.json()["detail"]  # o texto desta operação
        assert BASEROW_TOKEN not in response.text
    assert baserow.calls == [] and stored(deps) == before
    assert [e.id for e in audits(deps)] == trail  # nada gravado nem auditado


def test_connect_form_integration_asks_for_the_confirmation_before_it_looks_at_the_body(
    api_client: Client, baserow: FakeBaserow
) -> None:
    response = connect(api_client("total"), confirm=OMIT, table_id=0, base_url="x")

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert "errors" not in response.json() and baserow.calls == []


def test_connect_form_integration_costs_five_tokens_of_the_usage_limit(
    api_client: Client, baserow: FakeBaserow
) -> None:
    client = api_client("total", API_RATE_LIMIT_PER_MINUTE="5")  # o balde tem 5 fichas

    first = connect(client)
    second = connect(client, expected_version="1")

    assert first.status_code == 200
    assert second.status_code == 429 and second.json()["code"] == "rate_limited"
    assert len(baserow.calls) == 1


def test_connect_form_integration_with_no_token_or_a_blank_one_keeps_the_stored_token(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    client = api_client("total")
    connect_form(deps, active=False)
    baserow.calls.clear()
    baserow.requests.clear()

    omitted = connect(client, token=OMIT, expected_version=version_of(deps))
    blank = connect(client, token="", expected_version=version_of(deps))
    null = connect(client, token=None, expected_version=version_of(deps))

    assert [r.status_code for r in (omitted, blank, null)] == [200, 200, 200]
    assert [r.headers["Authorization"] for r in baserow.requests] == [f"Token {BASEROW_TOKEN}"] * 3
    with deps.sessions() as session:
        opened = config.stored_connection(session, deps.cipher, SLUG)
    assert opened is not None and opened.token == BASEROW_TOKEN
    changed = [
        e.details["token_changed"] for e in audits(deps) if e.action == "integration.connect"
    ]
    assert changed == [False, False, False]


def test_connect_form_integration_with_a_new_token_replaces_it_and_audits_that_it_changed(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    baserow.token = WRONG_TOKEN  # o token novo é o que o Baserow aceita agora
    before = stored(deps)
    assert before is not None

    response = connect(api_client("total"), token=WRONG_TOKEN, expected_version=version_of(deps))

    assert response.status_code == 200 and WRONG_TOKEN not in response.text
    with deps.sessions() as session:
        opened = config.stored_connection(session, deps.cipher, SLUG)
    assert opened is not None and opened.token == WRONG_TOKEN
    changed_at = datetime.fromisoformat(response.json()["token_changed_at"])
    assert changed_at > before.token_changed_at
    (entry,) = [e for e in audits(deps) if e.action == "integration.connect"]
    assert entry.details["token_changed"] is True and WRONG_TOKEN not in str(entry.details)


def test_connect_form_integration_to_another_table_drops_the_mapping_and_deactivates(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps)  # mapeada e ativa
    FakeBaserow(table_id=9).install(deps)

    response = connect(api_client("total"), table_id=9, token="", expected_version=version_of(deps))

    body = response.json()
    assert response.status_code == 200
    assert (body["table_id"], body["mapped_fields"], body["active"]) == (9, 0, False)
    assert body["key_field_id"] is None and body["version"] == "4"
    found = stored(deps)
    assert found is not None and (found.entries, found.active) == ((), False)
    (entry,) = [e for e in audits(deps) if e.action == "integration.connect"]
    assert entry.details["mapping_reset"] is True


def test_connect_form_integration_to_another_address_drops_the_mapping_too(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    other = "https://outro-baserow.example.test"

    response = connect(api_client("total"), base_url=other, expected_version=version_of(deps))

    body = response.json()
    assert response.status_code == 200  # com o token de novo: o guardado não vai a outro servidor
    assert (body["base_url"], body["mapped_fields"], body["active"]) == (other, 0, False)
    (entry,) = [e for e in audits(deps) if e.action == "integration.connect"]
    assert entry.details["mapping_reset"] is True
    assert entry.details["previous_host"] == "baserow.example.test"


@pytest.mark.parametrize("token", [OMIT, "", None], ids=["omitted", "blank", "null"])
def test_connect_form_integration_to_another_server_without_a_new_token_is_refused_on_the_token(
    deps: Deps, api_client: Client, baserow: FakeBaserow, token: Any
) -> None:
    """O token guardado nunca vai a outro servidor: mudar o host (ou a porta, ou o `https`) pede o
    token de novo, no campo `token`, e o Baserow nem é chamado."""
    connect_form(deps)
    before = stored(deps)
    baserow.calls.clear()

    response = connect(
        api_client("total"),
        base_url="https://outro-baserow.example.test",
        token=token,
        expected_version=version_of(deps),
    )

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    assert "exige colar o token de novo" in fields_of_error(response)["token"]
    assert set(fields_of_error(response)) == {"token"}
    assert baserow.calls == [] and stored(deps) == before
    assert BASEROW_TOKEN not in response.text


HUGE = "9" * 5000  # `int()` de tanto algarismo levanta ValueError (500, se ninguém o segura)
NOT_CANONICAL = ["00", "002", "03", "-1", "1.0", "٣", "12345678901", HUGE]


@pytest.mark.parametrize("version", NOT_CANONICAL, ids=[f"v{n}" for n in range(len(NOT_CANONICAL))])
def test_a_version_that_is_not_a_canonical_number_is_a_409_on_connect_and_on_mapping(
    deps: Deps, api_client: Client, baserow: FakeBaserow, version: str
) -> None:
    """A versão é um texto opaco que o servidor deu (`"2"`): `"002"`, `"00"` e um número sem fim
    nunca são a de agora. É `409 version_conflict`, e não um `500` do `int()`."""
    connected(deps)
    client = api_client("total")
    before = stored(deps)

    on_connect = connect(client, token="", expected_version=version)
    on_mapping = save_mapping(client, deps, expected_version=version)

    for response in (on_connect, on_mapping):
        assert response.status_code == 409 and response.json()["code"] == "version_conflict"
        assert response.json()["current_version"] == "2"
    assert stored(deps) == before


def test_connect_form_integration_keeping_address_and_table_keeps_the_mapping(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps)

    response = connect(api_client("total"), token="", expected_version=version_of(deps))

    body = response.json()
    assert response.status_code == 200 and (body["mapped_fields"], body["active"]) == (9, True)
    (entry,) = [e for e in audits(deps) if e.action == "integration.connect"]
    assert entry.details["mapping_reset"] is False


def test_connect_form_integration_with_a_stale_version_is_a_409_with_the_version_of_now(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)  # a versão de agora é a 2
    client = api_client("total")
    before = stored(deps)

    stale = connect(client, token="", expected_version="1")
    creating = connect(client)  # "0": criar o que já existe
    not_a_number = connect(client, token="", expected_version="abc")

    for response in (stale, creating, not_a_number):
        assert response.status_code == 409 and response.json()["code"] == "version_conflict"
        assert response.json()["current_version"] == "2"
        assert BASEROW_TOKEN not in response.text
    assert stored(deps) == before  # nada mudou


@pytest.mark.parametrize(
    "token", [OMIT, "", BASEROW_TOKEN], ids=["omitted", "blank", "typed"]
)  # ids fixos: o token é sorteado por processo
def test_connect_form_integration_expecting_a_version_of_an_integration_that_is_gone_says_zero(
    deps: Deps, api_client: Client, baserow: FakeBaserow, token: Any
) -> None:
    """Com ou sem token, a integração que a chamada espera já não existe: é conflito, e o Baserow nem
    é lido (um token em branco iria vazio, e a recusa dele esconderia o motivo)."""
    response = connect(api_client("total"), expected_version="3", token=token)

    assert response.status_code == 409 and response.json()["code"] == "version_conflict"
    assert response.json()["current_version"] == "0"  # a criação espera "0"
    assert stored(deps) is None and baserow.calls == []


def test_connect_form_integration_with_a_stored_token_that_does_not_open_asks_for_the_token(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))
    baserow.calls.clear()
    client = api_client("total")

    blank = connect(client, token="", expected_version=version_of(deps))
    repaired = connect(client, token=BASEROW_TOKEN, expected_version=version_of(deps))

    assert blank.status_code == 422
    assert "não abre com a WEBHOOK_ENCRYPTION_KEY" in fields_of_error(blank)["token"]
    assert repaired.status_code == 200 and repaired.json()["token_unreadable"] is False
    assert len(baserow.calls) == 1  # o token que não abre nunca foi ao Baserow


def test_connect_form_integration_without_the_encryption_key_cannot_keep_the_token(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    deps.cipher = None

    response = connect(api_client("total"))

    assert response.status_code == 422
    assert "WEBHOOK_ENCRYPTION_KEY" in fields_of_error(response)["token"]
    assert stored(deps) is None


# Os efeitos que a API tem e a página não, ou o contrário, e a hora que o banco carimba na hora:
# o último uso da chave, o token cifrado (o Fernet sorteia a cada vez; o teste confere que abrem no
# mesmo token) e as horas da integração.
IGNORE = {
    "api_keys": {"last_used_at"},
    "integrations": {"token_sealed", "token_changed_at"},
    "*": {"created_at", "updated_at"},
}
PAGE_USER = "pagina"  # o autor da página, diferente do `operadora` que o mundo do teste grava


def page_connect(client: ApiClient, **form: str) -> Callable[[], httpx.Response]:
    """A ação pela página, como o navegador: entra, lê o token CSRF e manda o formulário."""

    def call() -> httpx.Response:
        assert login(client, PAGE_USER).status_code == 303
        data = {"csrf_token": csrf_token(client), **form}
        return client.post(f"{PAGE}/forms/{SLUG}/baserow/connect", data=data, headers=ORIGIN)

    return call


def recorded(
    baserow: FakeBaserow, sink: list[tuple[str, str, str]], call: Callable[[], Any]
) -> Callable[[], Any]:
    """`call`, anotando em `sink` o que ela mandou ao Baserow (método, caminho e o `Authorization`)."""

    def run() -> Any:
        start = len(baserow.requests)
        result = call()
        sink.extend(
            (r.method, r.url.path, r.headers.get("Authorization", ""))
            for r in baserow.requests[start:]
        )
        return result

    return run


def opened_token(deps: Deps) -> str | None:
    with deps.sessions() as session:
        found = config.stored_connection(session, deps.cipher, SLUG)
    return found.token if found else None


def test_connecting_leaves_the_same_state_audit_and_baserow_traffic_as_the_page(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    by_page: list[tuple[str, str, str]] = []
    by_api: list[tuple[str, str, str]] = []
    tokens: list[str | None] = []

    def after(call: Callable[[], Any]) -> Callable[[], Any]:
        def run() -> Any:
            result = call()
            tokens.append(opened_token(deps))
            return result

        return run

    page, api = assert_same_effect(
        after(
            recorded(
                baserow,
                by_page,
                page_connect(
                    client,
                    base_url=BASEROW_URL,
                    table_id=str(BASEROW_TABLE),
                    token=BASEROW_TOKEN,
                    version="0",
                ),
            )
        ),
        after(recorded(baserow, by_api, lambda: connect(client))),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore={**IGNORE, "audit_log": {"target_id"}},  # o id da integração nova é sorteado
    )

    assert page.status_code == 303 and api.status_code == 200
    found = stored(deps)
    assert found is not None
    assert [e.target_id for e in audits(deps) if e.action == "integration.create"] == [
        str(found.id)
    ]
    assert (
        by_page
        == by_api
        == [("GET", f"/api/database/fields/table/{BASEROW_TABLE}/", f"Token {BASEROW_TOKEN}")]
    )
    assert tokens == [BASEROW_TOKEN, BASEROW_TOKEN]  # os dois guardaram o mesmo token


def test_reconnecting_to_another_table_leaves_the_same_state_audit_and_traffic_as_the_page(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    connect_form(deps)  # mapeada e ativa
    other = FakeBaserow(table_id=9).install(deps)
    version = version_of(deps)
    by_page: list[tuple[str, str, str]] = []
    by_api: list[tuple[str, str, str]] = []

    page, api = assert_same_effect(
        recorded(
            other,
            by_page,
            page_connect(
                client,
                base_url=BASEROW_URL,
                table_id="9",
                token="",
                version=version,
            ),
        ),
        recorded(
            other, by_api, lambda: connect(client, table_id=9, token="", expected_version=version)
        ),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
    )

    assert page.status_code == 303 and api.status_code == 200
    assert by_page == by_api == [("GET", "/api/database/fields/table/9/", f"Token {BASEROW_TOKEN}")]
    found = stored(deps)
    assert found is not None and (found.entries, found.active, found.table_id) == ((), False, 9)


def test_a_table_baserow_does_not_read_leaves_the_same_nothing_on_the_page_and_in_the_api(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    baserow.token = "outro-token-do-servidor"

    page, api = assert_same_effect(
        page_connect(
            client,
            base_url=BASEROW_URL,
            table_id=str(BASEROW_TABLE),
            token=WRONG_TOKEN,
            version="0",
        ),
        lambda: connect(client, token=WRONG_TOKEN),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
        expect_no_change=True,
    )

    assert page.status_code == 422 and api.status_code == 502
    assert "HTTP 401" in page.text and "HTTP 401" in api.json()["detail"]
    assert WRONG_TOKEN not in page.text and WRONG_TOKEN not in api.text


# --- save_form_integration_mapping --------------------------------------------------------------

CHOSEN = {str(entry.field_id): entry.source for entry in FULL_MAPPING}


def mapping_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"expected_version": OMIT, "sources": CHOSEN, **changes}
    return {key: value for key, value in body.items() if value is not OMIT}


def save_mapping(client: ApiClient, deps: Deps, slug: str = SLUG, **changes: Any) -> httpx.Response:
    """Manda o mapeamento, na versão que a integração tem agora (a menos que `changes` diga outra)."""
    found = stored(deps)
    version = str(found.version) if found else "0"
    return client.put(
        url_of("save_form_integration_mapping", slug=slug),
        json=mapping_body(**{"expected_version": version, **changes}),
    )


def connected(deps: Deps) -> None:
    """A integração conectada, sem mapeamento e desativada (o que `connect_form_integration` deixa)."""
    connect_form(deps, entries=[], active=False)


def test_save_form_integration_mapping_needs_a_key_and_a_read_key_is_refused_and_nothing_happens(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connected(deps)
    url = url_of("save_form_integration_mapping", slug=SLUG)
    anonymous = make_api(deps, FakeClock()).put(url, json=mapping_body(expected_version="2"))
    reader = save_mapping(api_client("leitura"), deps)

    assert anonymous.status_code == 401 and anonymous.json()["code"] == "invalid_credentials"
    assert reader.status_code == 403 and reader.json()["code"] == "forbidden_role"
    assert baserow.calls == [] and stored(deps).entries == ()  # type: ignore[union-attr]


def test_save_form_integration_mapping_of_a_form_that_does_not_exist_is_a_404(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    response = save_mapping(api_client("total"), deps, slug=SENTINEL)

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert SENTINEL not in response.text and baserow.calls == []


@pytest.mark.parametrize("version", ["0", "1", "3"], ids=["creation", "first", "later"])
def test_save_form_integration_mapping_of_a_form_without_integration_is_a_409_like_connect(
    deps: Deps, api_client: Client, baserow: FakeBaserow, version: str
) -> None:
    """Uma integração removida (ou nunca conectada) é a versão `0`: quem espera outra, ou a própria
    `0`, recebe `409 version_conflict`, como `connect_form_integration`, e o Baserow nem é chamado."""
    response = save_mapping(api_client("total"), deps, expected_version=version)

    assert response.status_code == 409 and response.json()["code"] == "version_conflict"
    assert response.json()["current_version"] == "0"
    assert "não existe" in response.json()["detail"] and baserow.calls == []


def test_a_stale_version_is_a_409_even_when_baserow_is_down_on_connect_and_on_mapping(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connected(deps)
    client = api_client("total")
    baserow.fail("GET", 503)
    baserow.fail("GET", 503)

    on_connect = connect(client, token="", expected_version="1")
    on_mapping = save_mapping(client, deps, expected_version="1")

    for response in (on_connect, on_mapping):
        assert response.status_code == 409 and response.json()["code"] == "version_conflict"
        assert response.json()["current_version"] == "2"
    assert baserow.calls == [] and len(baserow.failures) == 2  # o Baserow nem foi chamado


def test_save_form_integration_mapping_of_a_form_without_a_published_version_is_a_409(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    drafts.create(deps, title="Em rascunho", slug="em-rascunho", actor="testes")

    response = save_mapping(api_client("total"), deps, slug="em-rascunho")

    assert response.status_code == 409 and response.json()["code"] == "state_conflict"
    assert "Publique o formulário antes" in response.json()["detail"] and baserow.calls == []


def test_save_form_integration_mapping_stores_it_checked_against_the_live_table_and_audits_it(
    deps: Deps, api_client: Client, baserow: FakeBaserow, caplog: pytest.LogCaptureFixture
) -> None:
    connected(deps)
    client = api_client("total")
    baserow.calls.clear()

    with caplog.at_level(logging.DEBUG):
        response = save_mapping(client, deps)

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert (body["version"], body["active"], body["deactivated"]) == ("3", False, False)
    assert body["key_field_id"] == 104
    assert body["entries"] == [
        {"field_id": e.field_id, "name": e.name, "type": e.type, "source": e.source}
        for e in FULL_MAPPING
    ]
    assert (body["report"]["ok"], body["report"]["problems"]) == (True, [])
    assert baserow.calls == [
        ("GET", f"fields/table/{BASEROW_TABLE}/")
    ]  # a tabela de agora, uma vez
    entry = [e for e in audits(deps) if e.action == "integration.mapping"][-1]
    assert entry.actor == client.actor  # a anterior, de `connected`, é de quem montou o mundo
    assert entry.details["fields"] == len(FULL_MAPPING) and entry.details["deactivated"] is False
    assert BASEROW_TOKEN not in response.text + caplog.text + stored_text(deps)


def test_save_form_integration_mapping_leaves_out_a_blank_source_and_a_field_the_table_lacks(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connected(deps)

    response = save_mapping(
        api_client("total"), deps, sources={**CHOSEN, "105": "  ", "999": "lead.name"}
    )

    assert response.status_code == 200
    assert 105 not in {e["field_id"] for e in response.json()["entries"]}
    assert 999 not in {e["field_id"] for e in response.json()["entries"]}
    assert len(response.json()["entries"]) == len(FULL_MAPPING) - 1


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"104": ""}, "código do pedido"),
        ({"108": "lead.name"}, "não recebe"),
        ({"108": "answer:nao_existe"}, "fonte que o formulário não tem"),
        ({"107": "lead.email"}, "não recebe"),
    ],
)
def test_save_form_integration_mapping_refuses_what_does_not_serve_and_says_what_to_fix(
    deps: Deps, api_client: Client, baserow: FakeBaserow, change: dict[str, str], message: str
) -> None:
    connected(deps)
    client = api_client("total")
    trail = [e.id for e in audits(deps)]

    response = save_mapping(client, deps, sources={**CHOSEN, **change})

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    items = response.json()["errors"]
    assert items and {item["field"] for item in items} == {"sources"}
    assert any(message in item["message"] for item in items)
    assert stored(deps).entries == ()  # type: ignore[union-attr]
    assert [e.id for e in audits(deps)] == trail  # nada auditado


def test_save_form_integration_mapping_without_any_source_asks_for_the_order_code(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connected(deps)

    response = save_mapping(api_client("total"), deps, sources={})

    assert response.status_code == 422
    assert "código do pedido" in response.json()["errors"][0]["message"]


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"sources": OMIT}, "sources"),
        ({"sources": ["lead.name"]}, "sources"),
        ({"sources": {"abc": "lead.name"}}, "sources"),
        ({"sources": {"0": "lead.name"}}, "sources"),
        ({"sources": {"01": "lead.name"}}, "sources"),
        ({"sources": {"-1": "lead.name"}}, "sources"),
        ({"sources": {"٣": "lead.name"}}, "sources"),
        ({"sources": {"101": 5}}, "sources"),
        ({"sources": {"101": None}}, "sources"),
        ({"source_101": "lead.name"}, "source_101"),
    ],
)
def test_save_form_integration_mapping_refuses_a_body_that_is_not_an_object_of_sources(
    deps: Deps,
    api_client: Client,
    baserow: FakeBaserow,
    changes: dict[str, Any],
    field: str,
) -> None:
    connected(deps)

    response = save_mapping(api_client("total"), deps, **changes)

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    assert field in {item["field"] for item in response.json()["errors"]}
    assert baserow.calls == [] and stored(deps).entries == ()  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "key",
    ["0", "00101", "12345678901", "2147483648", HUGE],
    ids=["zero", "zeros", "eleven", "max", "huge"],
)
def test_save_form_integration_mapping_refuses_a_field_id_that_is_not_a_canonical_id_in_portuguese(
    deps: Deps, api_client: Client, baserow: FakeBaserow, key: str
) -> None:
    connected(deps)

    response = save_mapping(api_client("total"), deps, sources={**CHOSEN, key: "lead.name"})

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    (item,) = response.json()["errors"]
    assert item["field"] == "sources" and "ids dos campos da tabela" in item["message"]
    assert (len(key) < 5 or key not in response.text) and baserow.calls == []  # sem eco


def test_save_form_integration_mapping_needs_the_version_it_expects(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connected(deps)
    client = api_client("total")

    response = client.put(
        url_of("save_form_integration_mapping", slug=SLUG), json={"sources": CHOSEN}
    )

    assert response.status_code == 422
    assert "expected_version" in {item["field"] for item in response.json()["errors"]}


def test_save_form_integration_mapping_echoes_no_source_it_refuses_nor_a_key_it_does_not_take(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    """O eco da operação: com os outros campos válidos, só a fonte (texto livre) e a chave são o
    sentinela. Nem a mensagem do serviço nem a do Pydantic o repetem."""
    connected(deps)
    client = api_client("total")

    unknown_source = save_mapping(client, deps, sources={**CHOSEN, "108": SENTINEL})
    odd_key = save_mapping(client, deps, sources={**CHOSEN, SENTINEL: "lead.name"})
    odd_value = save_mapping(client, deps, sources={**CHOSEN, SENTINEL: 1})

    assert [r.status_code for r in (unknown_source, odd_key, odd_value)] == [422, 422, 422]
    for response in (unknown_source, odd_key, odd_value):
        assert SENTINEL not in response.text and SENTINEL not in str(response.headers)


def test_save_form_integration_mapping_with_pending_options_deactivates_an_active_integration(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps)  # mapeada e ativa
    baserow.fields[7] = baserow_field(108, "Equipe", "single_select", options=("Só eu",))

    response = save_mapping(api_client("total"), deps)

    body = response.json()
    assert response.status_code == 200
    assert (body["active"], body["deactivated"], body["report"]["ok"]) == (False, True, False)
    assert body["report"]["unmatched_options"] == 3
    found = stored(deps)
    assert found is not None and not found.active
    entry = [e for e in audits(deps) if e.action == "integration.mapping"][-1]
    assert entry.details["deactivated"] is True


def test_save_form_integration_mapping_that_is_clean_keeps_an_active_integration_active(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    sources = {key: source for key, source in CHOSEN.items() if key != "105"}

    response = save_mapping(api_client("total"), deps, sources=sources)

    body = response.json()
    assert response.status_code == 200 and (body["active"], body["deactivated"]) == (True, False)
    assert len(body["entries"]) == len(FULL_MAPPING) - 1


def test_save_form_integration_mapping_with_a_token_that_does_not_open_is_a_409(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connected(deps)
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))
    baserow.calls.clear()

    response = save_mapping(api_client("total"), deps)

    assert response.status_code == 409 and response.json()["code"] == "state_conflict"
    assert "não abre com a WEBHOOK_ENCRYPTION_KEY" in response.json()["detail"]
    assert baserow.calls == [] and stored(deps).entries == ()  # type: ignore[union-attr]


@pytest.mark.parametrize(("outcome", "message"), [(401, "HTTP 401"), (503, "HTTP 503")])
def test_save_form_integration_mapping_with_a_table_baserow_does_not_read_is_a_502(
    deps: Deps, api_client: Client, baserow: FakeBaserow, outcome: int, message: str
) -> None:
    connected(deps)
    baserow.fail("GET", outcome)

    response = save_mapping(api_client("total"), deps)

    assert response.status_code == 502 and response.json()["code"] == "upstream_failed"
    assert message in response.json()["detail"] and BASEROW_TOKEN not in response.text
    assert stored(deps).entries == ()  # type: ignore[union-attr]


def test_save_form_integration_mapping_with_a_stale_version_is_a_409_and_changes_nothing(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connected(deps)  # a versão de agora é a 2
    before = stored(deps)

    stale = save_mapping(api_client("total"), deps, expected_version="1")
    not_a_number = save_mapping(api_client("total"), deps, expected_version="abc")

    for response in (stale, not_a_number):
        assert response.status_code == 409 and response.json()["code"] == "version_conflict"
        assert response.json()["current_version"] == "2"
    assert stored(deps) == before


def test_save_form_integration_mapping_costs_five_tokens_of_the_usage_limit(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connected(deps)
    client = api_client("total", API_RATE_LIMIT_PER_MINUTE="5")  # o balde tem 5 fichas

    first = save_mapping(client, deps)
    second = save_mapping(client, deps)

    assert first.status_code == 200
    assert second.status_code == 429 and second.json()["code"] == "rate_limited"
    assert len(baserow.calls) == 1


def page_mapping(client: ApiClient, version: str, **sources: str) -> Callable[[], httpx.Response]:
    """A ação pela página: o formulário com um campo `source_<id>` por campo da tabela."""

    def call() -> httpx.Response:
        assert login(client, PAGE_USER).status_code == 303
        data = {"csrf_token": csrf_token(client), "version": version, **sources}
        return client.post(f"{PAGE}/forms/{SLUG}/baserow/mapping", data=data, headers=ORIGIN)

    return call


PAGE_SOURCES = {f"source_{key}": source for key, source in CHOSEN.items()}


def test_saving_the_mapping_leaves_the_same_state_audit_and_baserow_traffic_as_the_page(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    connected(deps)
    version = version_of(deps)
    by_page: list[tuple[str, str, str]] = []
    by_api: list[tuple[str, str, str]] = []

    page, api = assert_same_effect(
        recorded(baserow, by_page, page_mapping(client, version, **PAGE_SOURCES)),
        recorded(baserow, by_api, lambda: save_mapping(client, deps)),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
    )

    assert page.status_code == 303 and api.status_code == 200
    expected = [("GET", f"/api/database/fields/table/{BASEROW_TABLE}/", f"Token {BASEROW_TOKEN}")]
    assert by_page == by_api == expected
    found = stored(deps)
    assert found is not None and found.key_field_id == 104 and len(found.entries) == 9


def test_a_mapping_with_pending_options_deactivates_the_same_way_on_the_page_and_in_the_api(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    connect_form(deps)  # mapeada e ativa
    baserow.fields[7] = baserow_field(108, "Equipe", "single_select", options=("Só eu",))
    version = version_of(deps)

    page, api = assert_same_effect(
        page_mapping(client, version, **PAGE_SOURCES),
        lambda: save_mapping(client, deps),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
    )

    assert page.status_code == 303 and api.status_code == 200
    assert api.json()["deactivated"] is True
    found = stored(deps)
    assert found is not None and not found.active
    entry = [e for e in audits(deps) if e.action == "integration.mapping"][-1]
    assert entry.details["deactivated"] is True


def test_a_clean_remap_keeps_the_integration_active_the_same_way_on_the_page_and_in_the_api(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    connect_form(deps)
    version = version_of(deps)
    fewer = {key: source for key, source in PAGE_SOURCES.items() if key != "source_105"}

    page, api = assert_same_effect(
        page_mapping(client, version, **fewer),
        lambda: save_mapping(client, deps, sources={k: v for k, v in CHOSEN.items() if k != "105"}),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
    )

    assert page.status_code == 303 and api.status_code == 200
    found = stored(deps)
    assert found is not None and found.active and len(found.entries) == 8


def test_a_mapping_that_does_not_serve_leaves_the_same_nothing_on_the_page_and_in_the_api(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    connected(deps)
    version = version_of(deps)

    page, api = assert_same_effect(
        page_mapping(client, version, **{**PAGE_SOURCES, "source_108": "lead.name"}),
        lambda: save_mapping(client, deps, sources={**CHOSEN, "108": "lead.name"}),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
        expect_no_change=True,
    )

    assert page.status_code == 422 and api.status_code == 422
    assert "não recebe" in page.text and "não recebe" in api.json()["errors"][0]["message"]
