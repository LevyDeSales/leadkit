"""Ativar, desativar, remover, testar e reenviar a integração com o Baserow na API de admin
(`/admin/api/v1`): `activate_form_integration`, `deactivate_form_integration`,
`remove_form_integration`, `send_form_integration_test` e `resend_failed_form_integration_syncs`, pela
costura HTTP.

Agem pelas mesmas ações que as páginas (`app.admin.actions.integrations`): a conferência do relatório
antes de ativar, a versão esperada, o envio da linha de teste e o reenvio em lote são uma regra só, e a
paridade com a página (o mesmo estado do banco, a mesma auditoria, o mesmo tráfego ao Baserow e os
mesmos disparos no Hatchet, menos o autor) está no fim de cada operação que escreve. O Baserow é o falso
em memória do `conftest` (sobre `httpx.MockTransport`) e o Hatchet de verdade fica fora: o
`launch_sync` do `app.workflows` é trocado por um registro. **Um disparo que não chega ao engine não é
erro:** a resposta é `200` com `launched: false`, e o reconciliador dispara. As chaves destes testes são
sorteadas na execução, e o token do Baserow também.

**O token só entra.** Cada teste que age confere que ele não volta na resposta, no log, na auditoria nem
numa mensagem de erro; o envio de teste e os disparos também não levam dado de lead.
"""

from __future__ import annotations

import copy
import logging
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from admin_api_matrix import operations_of
from admin_api_support import (
    ORIGIN,
    ApiClient,
    assert_same_effect,
    built_openapi,
    make_api,
    url_of,
)
from conftest import (
    BASEROW_TABLE,
    BASEROW_TOKEN,
    FakeBaserow,
    baserow_field,
    demo_baserow_fields,
)
from cryptography.fernet import Fernet
from test_admin import FakeClock, csrf_token, login
from test_admin_actions_integrations import clone_failed_syncs
from test_admin_api_integrations import (
    IGNORE,
    NOT_CANONICAL,
    OMIT,
    PAGE_USER,
    audits,
    recorded,
    stored,
)
from test_admin_destinations import stored_text
from test_admin_integrations import failed_sync
from test_integrations import SLUG, completed_job, connect_form, sync_ids, sync_state

from app import workflows
from app.deps import Deps
from app.destinations import SecretCipher
from app.errors import TerminalError
from app.forms import drafts
from app.integrations import push, syncs
from app.models import Integration, IntegrationSync

PAGE = "/admin"
SENTINEL = "ZZ_SENT_" + secrets.token_hex(8).upper()
Client = Callable[..., ApiClient]
OPERATIONS = (
    "activate_form_integration",
    "deactivate_form_integration",
    "remove_form_integration",
    "send_form_integration_test",
    "resend_failed_form_integration_syncs",
)
# O que a lead de verdade do mundo do teste escreveu: nada disto pode chegar à linha de teste, ao
# Hatchet, ao log nem à auditoria.
REAL_LEAD = {
    "nome": "Zélia Verdadeira Dos Santos",
    "escritorio": "Escritório Verdadeiro Da Zélia",
    "objetivo_90_dias": "OBJETIVO_PARTICULAR_DA_LEAD_REAL_" + secrets.token_hex(4),
}
REAL_EMAIL = "lead.real@example.test"


@pytest.fixture
def baserow(deps: Deps) -> FakeBaserow:
    return FakeBaserow().install(deps)


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """O Hatchet de mentira: o engine recebe todo disparo (`launch_sync` devolve um run id) e cada
    sincronização disparada fica na lista, na ordem."""
    launched: list[uuid.UUID] = []
    monkeypatch.setattr(workflows, "launch_sync", lambda target: launched.append(target) or "run-s")
    return launched


@pytest.fixture
def deaf_engine(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """O engine fora do ar: o SDK devolve `None`, e o disparo tentado fica na lista."""
    attempts: list[uuid.UUID] = []
    monkeypatch.setattr(workflows, "launch_sync", lambda target: attempts.append(target))
    return attempts


def until(condition: Callable[[], bool], seconds: float = 5.0) -> bool:
    """Espera a condição (o disparo em segundo plano), sem prazo fixo."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def lead_in(*places: str) -> bool:
    """Se algum texto leva dado da lead de verdade."""
    secrets_of_the_lead = [*REAL_LEAD.values(), REAL_EMAIL]
    return any(piece in place for place in places for piece in secrets_of_the_lead)


def real_job(deps: Deps) -> str:
    """Um pedido concluído de uma lead de verdade (com a integração ativa, cria a sincronização)."""
    answers = {**REAL_LEAD, "email": REAL_EMAIL}
    job = completed_job(deps, key="lead.real", **answers)
    return job.public_id


def current(deps: Deps) -> str:
    found = stored(deps)
    return str(found.version) if found else "0"


def choose(changes: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in changes.items() if value is not OMIT}


def activate(client: ApiClient, deps: Deps, slug: str = SLUG, **changes: Any) -> httpx.Response:
    """Ativa, na versão que a integração tem agora (a menos que `changes` diga outra)."""
    body = {"expected_version": current(deps), "confirm": True, **changes}
    return client.post(url_of("activate_form_integration", slug=slug), json=choose(body))


def deactivate(client: ApiClient, deps: Deps, slug: str = SLUG, **changes: Any) -> httpx.Response:
    body = {"expected_version": current(deps), **changes}
    return client.post(url_of("deactivate_form_integration", slug=slug), json=choose(body))


def remove(client: ApiClient, deps: Deps, slug: str = SLUG, **query: Any) -> httpx.Response:
    """Remove, com a versão e a confirmação na query (é um `DELETE`)."""
    params = {"expected_version": current(deps), "confirm": "true", **query}
    return client.delete(url_of("remove_form_integration", slug=slug), params=choose(params))


def send_test(client: ApiClient, slug: str = SLUG, **changes: Any) -> httpx.Response:
    return client.post(
        url_of("send_form_integration_test", slug=slug), json=choose({"confirm": True, **changes})
    )


def resend(client: ApiClient, slug: str = SLUG, **changes: Any) -> httpx.Response:
    return client.post(
        url_of("resend_failed_form_integration_syncs", slug=slug),
        json=choose({"confirm": True, **changes}),
    )


def audit_trail(deps: Deps) -> list[uuid.UUID]:
    """Os ids da auditoria, menos a criação da chave que o próprio teste faz."""
    return [entry.id for entry in audits(deps) if not entry.action.startswith("api_key.")]


def count_of(deps: Deps, model: type) -> int:
    with deps.sessions() as session:
        return session.scalar(sa.select(sa.func.count()).select_from(model)) or 0


def sync_statuses(deps: Deps) -> dict[uuid.UUID, str]:
    with deps.sessions() as session:
        return dict(session.execute(sa.select(IntegrationSync.id, IntegrationSync.status)).all())


# --- activate_form_integration ------------------------------------------------------------------


def test_activate_form_integration_needs_a_key_and_a_read_key_is_refused_and_nothing_happens(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    url = url_of("activate_form_integration", slug=SLUG)
    anonymous = make_api(deps, FakeClock()).post(
        url, json={"expected_version": current(deps), "confirm": True}
    )
    reader = activate(api_client("leitura"), deps)
    baserow.calls.clear()

    assert anonymous.status_code == 401 and anonymous.json()["code"] == "invalid_credentials"
    assert reader.status_code == 403 and reader.json()["code"] == "forbidden_role"
    found = stored(deps)
    assert found is not None and not found.active and baserow.calls == []


def test_activate_form_integration_of_a_form_that_does_not_exist_is_a_404_that_echoes_nothing(
    api_client: Client, deps: Deps, baserow: FakeBaserow
) -> None:
    response = activate(api_client("total"), deps, slug=SENTINEL)

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert SENTINEL not in response.text and baserow.calls == []


def test_activate_form_integration_reads_the_live_table_once_turns_it_on_and_audits_it(
    deps: Deps, api_client: Client, baserow: FakeBaserow, caplog: pytest.LogCaptureFixture
) -> None:
    connect_form(deps, active=False)
    before = stored(deps)
    assert before is not None
    baserow.calls.clear()
    client = api_client("total")

    with caplog.at_level(logging.DEBUG):
        response = activate(client, deps)

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert (body["active"], body["version"]) == (True, str(before.version + 1))
    assert (body["token_set"], body["token_unreadable"], body["mapped_fields"]) == (True, False, 9)
    assert body["updated_by"] == client.actor and not {"token", "token_sealed"} & set(body)
    assert baserow.calls == [
        ("GET", f"fields/table/{BASEROW_TABLE}/")
    ]  # a tabela de agora, uma vez
    found = stored(deps)
    assert found is not None and found.active
    (entry,) = [e for e in audits(deps) if e.action == "integration.activate"]
    assert entry.actor == client.actor and entry.details["host"] == "baserow.example.test"
    for place in (response.text, caplog.text, stored_text(deps), str(entry.details)):
        assert BASEROW_TOKEN not in place


def test_activate_form_integration_with_a_dirty_report_is_a_422_with_the_pendencies(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    baserow.fields[7] = baserow_field(108, "Equipe", "single_select", options=("Só eu",))
    del baserow.fields[4]  # o campo 105 sumiu da tabela
    trail = audit_trail(deps)
    before = stored(deps)

    response = activate(api_client("total"), deps)

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    items = response.json()["errors"]
    assert {item["field"] for item in items} == {"mapping"}
    messages = [item["message"] for item in items]
    assert "O campo «Respostas» não existe mais na tabela." in messages
    assert any("3 opções do formulário não têm par no Baserow" in message for message in messages)
    assert stored(deps) == before and not before.active  # type: ignore[union-attr]
    assert audit_trail(deps) == trail  # nada auditado
    assert BASEROW_TOKEN not in response.text


def test_activate_form_integration_after_the_table_is_fixed_turns_it_on(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    client = api_client("total")
    baserow.fields[7] = baserow_field(108, "Equipe", "single_select", options=("Só eu",))

    refused = activate(client, deps)
    baserow.fields = demo_baserow_fields()  # a operadora corrigiu a tabela
    accepted = activate(client, deps)

    assert (refused.status_code, accepted.status_code) == (422, 200)
    assert accepted.json()["active"] is True


def test_activate_form_integration_with_nothing_mapped_says_so(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps, entries=[], active=False)

    response = activate(api_client("total"), deps)

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    (item,) = response.json()["errors"]
    assert item["field"] == "mapping" and "Nenhum campo está mapeado" in item["message"]
    assert not stored(deps).active  # type: ignore[union-attr]


def test_activate_form_integration_of_a_form_without_a_published_version_is_a_409_state_conflict(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    drafts.create(deps, title="Em rascunho", slug="em-rascunho", actor="testes")

    response = activate(api_client("total"), deps, slug="em-rascunho", expected_version="1")

    assert response.status_code == 409 and response.json()["code"] == "state_conflict"
    assert "Publique o formulário antes" in response.json()["detail"] and baserow.calls == []


@pytest.mark.parametrize("version", ["0", "1", "3"], ids=["creation", "first", "later"])
def test_activate_form_integration_of_a_form_without_integration_is_a_409_version_zero(
    deps: Deps, api_client: Client, baserow: FakeBaserow, version: str
) -> None:
    """Como `save_form_integration_mapping`: uma integração removida (ou nunca conectada) é a versão
    `0`, e o Baserow nem é chamado."""
    response = activate(api_client("total"), deps, expected_version=version)

    assert response.status_code == 409 and response.json()["code"] == "version_conflict"
    assert response.json()["current_version"] == "0" and "não existe" in response.json()["detail"]
    assert baserow.calls == []


def test_activate_form_integration_on_a_stale_version_is_a_409_even_when_baserow_is_down(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)  # a versão de agora é a 2
    before = stored(deps)
    baserow.calls.clear()
    baserow.fail("GET", 503)  # se o Baserow fosse lido, seria `502`

    stale = activate(api_client("total"), deps, expected_version="1")

    assert stale.status_code == 409 and stale.json()["code"] == "version_conflict"
    assert stale.json()["current_version"] == "2"
    assert baserow.calls == [] and len(baserow.failures) == 1  # o Baserow nem foi chamado
    assert stored(deps) == before


@pytest.mark.parametrize("version", NOT_CANONICAL, ids=[f"v{n}" for n in range(len(NOT_CANONICAL))])
def test_activate_and_deactivate_and_remove_read_a_version_that_is_not_canonical_as_a_409(
    deps: Deps, api_client: Client, baserow: FakeBaserow, version: str
) -> None:
    """A versão é um texto opaco que o servidor deu (`"2"`): `"002"` e um número sem fim nunca são a de
    agora. É `409 version_conflict` em `activate_form_integration`, `deactivate_form_integration` e
    `remove_form_integration`, e não um `500` do `int()`."""
    connect_form(deps, active=False)
    client = api_client("total")
    before = stored(deps)

    responses = [
        activate(client, deps, expected_version=version),
        deactivate(client, deps, expected_version=version),
        remove(client, deps, expected_version=version),
    ]

    for response in responses:
        assert response.status_code == 409 and response.json()["code"] == "version_conflict"
        assert response.json()["current_version"] == "2"
    assert stored(deps) == before


def test_activate_form_integration_with_a_token_that_does_not_open_is_a_409_state_conflict(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))
    baserow.calls.clear()

    response = activate(api_client("total"), deps)

    assert response.status_code == 409 and response.json()["code"] == "state_conflict"
    assert "não abre com a WEBHOOK_ENCRYPTION_KEY" in response.json()["detail"]
    assert baserow.calls == [] and not stored(deps).active  # type: ignore[union-attr]


@pytest.mark.parametrize(("outcome", "message"), [(401, "HTTP 401"), (503, "HTTP 503")])
def test_activate_form_integration_with_a_table_baserow_does_not_read_is_a_502(
    deps: Deps, api_client: Client, baserow: FakeBaserow, outcome: int, message: str
) -> None:
    connect_form(deps, active=False)
    trail = audit_trail(deps)
    baserow.fail("GET", outcome)

    response = activate(api_client("total"), deps)

    assert response.status_code == 502 and response.json()["code"] == "upstream_failed"
    assert message in response.json()["detail"] and BASEROW_TOKEN not in response.text
    assert not stored(deps).active and audit_trail(deps) == trail  # type: ignore[union-attr]


@pytest.mark.parametrize("confirm", [OMIT, False, "true", 1])
def test_activate_form_integration_needs_confirm_true_and_does_nothing_without_it(
    deps: Deps, api_client: Client, baserow: FakeBaserow, confirm: Any
) -> None:
    connect_form(deps, active=False)
    trail = audit_trail(deps)
    before = stored(deps)
    baserow.calls.clear()

    response = activate(api_client("total"), deps, confirm=confirm)

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert "planilha" in response.json()["detail"]  # o texto desta operação
    assert baserow.calls == [] and stored(deps) == before and audit_trail(deps) == trail


def test_activate_form_integration_asks_for_the_confirmation_before_it_looks_at_the_body(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)

    response = activate(api_client("total"), deps, confirm=OMIT, expected_version=OMIT, extra=1)

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert "errors" not in response.json() and not stored(deps).active  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"expected_version": OMIT}, "expected_version"),
        ({"expected_version": ""}, "expected_version"),
        ({"expected_version": 2}, "expected_version"),
        ({"nao_existe": 1}, "nao_existe"),
    ],
)
def test_activate_form_integration_refuses_a_body_that_does_not_serve_with_the_path_of_the_field(
    deps: Deps, api_client: Client, baserow: FakeBaserow, changes: dict[str, Any], field: str
) -> None:
    connect_form(deps, active=False)

    response = activate(api_client("total"), deps, **changes)

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    assert field in {item["field"] for item in response.json()["errors"]}
    assert baserow.calls == [] and not stored(deps).active  # type: ignore[union-attr]


def test_activate_form_integration_costs_five_tokens_of_the_usage_limit(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    client = api_client("total", API_RATE_LIMIT_PER_MINUTE="5")  # o balde tem 5 fichas
    baserow.calls.clear()

    first = activate(client, deps)
    second = activate(client, deps)

    assert first.status_code == 200
    assert second.status_code == 429 and second.json()["code"] == "rate_limited"
    assert len(baserow.calls) == 1  # a recusa não chegou ao Baserow


def test_activate_form_integration_echoes_none_of_a_version_it_refuses(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    """O eco da operação: com os outros campos válidos, só a versão (texto livre) é o sentinela. O eco
    geral para no `422` do Pydantic; aqui a versão chega ao serviço."""
    connect_form(deps, active=False)

    response = activate(api_client("total"), deps, expected_version=SENTINEL)

    assert response.status_code == 409
    assert SENTINEL not in response.text and SENTINEL not in str(response.headers)


def page_post(client: ApiClient, action: str, **form: str) -> Callable[[], httpx.Response]:
    """A ação pela página, como o navegador: entra, lê o token CSRF e manda o formulário."""

    def call() -> httpx.Response:
        assert login(client, PAGE_USER).status_code == 303
        data = {"csrf_token": csrf_token(client), **form}
        return client.post(f"{PAGE}/forms/{SLUG}/baserow/{action}", data=data, headers=ORIGIN)

    return call


READ_TABLE = [("GET", f"/api/database/fields/table/{BASEROW_TABLE}/", f"Token {BASEROW_TOKEN}")]


def test_activating_leaves_the_same_state_audit_and_baserow_traffic_as_the_page(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    connect_form(deps, active=False)
    version = current(deps)
    by_page: list[tuple[str, str, str]] = []
    by_api: list[tuple[str, str, str]] = []

    page, api = assert_same_effect(
        recorded(baserow, by_page, page_post(client, "activate", version=version)),
        recorded(baserow, by_api, lambda: activate(client, deps)),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
    )

    assert page.status_code == 303 and api.status_code == 200
    assert by_page == by_api == READ_TABLE
    found = stored(deps)
    assert found is not None and found.active
    assert [e.action for e in audits(deps)].count("integration.activate") == 1


def test_a_dirty_report_leaves_the_same_nothing_on_the_page_and_in_the_api(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    connect_form(deps, active=False)
    baserow.fields[7] = baserow_field(108, "Equipe", "single_select", options=("Só eu",))
    version = current(deps)
    by_page: list[tuple[str, str, str]] = []
    by_api: list[tuple[str, str, str]] = []

    page, api = assert_same_effect(
        recorded(baserow, by_page, page_post(client, "activate", version=version)),
        recorded(baserow, by_api, lambda: activate(client, deps)),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
        expect_no_change=True,
    )

    assert page.status_code == 422 and api.status_code == 422
    assert "pendências" in page.text and "3 opções do formulário não têm par" in api.text
    # a ação lê a tabela uma vez nas duas; a página, ao desenhar a recusa, a lê de novo (a tela mostra
    # os campos de agora), e a API não desenha nada
    assert by_page[:1] == by_api == READ_TABLE


# --- deactivate_form_integration ----------------------------------------------------------------


def test_deactivate_form_integration_needs_a_key_and_a_read_key_is_refused_and_nothing_happens(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps)  # ativa
    url = url_of("deactivate_form_integration", slug=SLUG)
    anonymous = make_api(deps, FakeClock()).post(url, json={"expected_version": current(deps)})
    reader = deactivate(api_client("leitura"), deps)

    assert anonymous.status_code == 401 and anonymous.json()["code"] == "invalid_credentials"
    assert reader.status_code == 403 and reader.json()["code"] == "forbidden_role"
    assert stored(deps).active  # type: ignore[union-attr]


def test_deactivate_form_integration_of_a_form_that_does_not_exist_is_a_404(
    api_client: Client, deps: Deps, baserow: FakeBaserow
) -> None:
    response = deactivate(api_client("total"), deps, slug=SENTINEL)

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert SENTINEL not in response.text


def test_deactivate_form_integration_turns_it_off_without_a_confirm_or_a_call_to_baserow(
    deps: Deps, api_client: Client, baserow: FakeBaserow, caplog: pytest.LogCaptureFixture
) -> None:
    connect_form(deps)  # mapeada e ativa
    before = stored(deps)
    assert before is not None
    client = api_client("total")
    baserow.calls.clear()

    with caplog.at_level(logging.DEBUG):
        response = deactivate(client, deps)  # o corpo é só a versão: a operação não tem marca

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert (body["active"], body["version"]) == (False, str(before.version + 1))
    assert body["updated_by"] == client.actor and body["mapped_fields"] == 9
    assert baserow.calls == []
    (entry,) = [e for e in audits(deps) if e.action == "integration.deactivate"]
    assert entry.actor == client.actor
    for place in (response.text, caplog.text, stored_text(deps), str(entry.details)):
        assert BASEROW_TOKEN not in place


def test_deactivate_form_integration_works_when_the_token_does_not_open(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    """É o botão de desligar: não precisa do Baserow nem de o token abrir."""
    connect_form(deps)
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))

    response = deactivate(api_client("total"), deps)

    assert response.status_code == 200 and response.json()["active"] is False
    assert response.json()["token_set"] is True


def test_deactivate_form_integration_has_no_mark_and_does_not_ask_for_confirm() -> None:
    document = built_openapi()
    operation = next(
        op for _, _, op in operations_of(document) if op["operationId"] == OPERATIONS[1]
    )

    assert operation["x-required-role"] == "total"
    assert "x-destructive" not in operation and "x-external-effect" not in operation
    schema = document["components"]["schemas"]["DeactivateFormIntegrationRequest"]
    assert set(schema["properties"]) == {"expected_version"}


def test_deactivate_form_integration_on_a_stale_version_or_a_gone_integration_is_a_409(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    client = api_client("total")
    gone = deactivate(client, deps, expected_version="1")
    connect_form(deps)
    before = stored(deps)

    stale = deactivate(client, deps, expected_version="1")

    assert gone.status_code == 409 and gone.json()["current_version"] == "0"
    assert stale.status_code == 409 and stale.json()["code"] == "version_conflict"
    assert stale.json()["current_version"] == "3" and stored(deps) == before


def test_deactivate_form_integration_repeated_after_a_lost_answer_is_a_409_and_changes_nothing(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    client = api_client("total")
    version = current(deps)

    first = deactivate(client, deps, expected_version=version)
    repeated = deactivate(client, deps, expected_version=version)

    assert (first.status_code, repeated.status_code) == (200, 409)
    assert repeated.json()["code"] == "version_conflict"
    assert repeated.json()["current_version"] == first.json()["version"]
    assert [e.action for e in audits(deps)].count("integration.deactivate") == 1


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"expected_version": OMIT}, "expected_version"),
        ({"expected_version": ""}, "expected_version"),
        ({"confirm": True}, "confirm"),
        ({"nao_existe": 1}, "nao_existe"),
    ],
)
def test_deactivate_form_integration_refuses_a_body_that_does_not_serve_with_the_field(
    deps: Deps, api_client: Client, baserow: FakeBaserow, changes: dict[str, Any], field: str
) -> None:
    connect_form(deps)

    response = deactivate(api_client("total"), deps, **changes)

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    assert field in {item["field"] for item in response.json()["errors"]}
    assert stored(deps).active  # type: ignore[union-attr]


def test_deactivate_form_integration_echoes_none_of_a_version_it_refuses(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps)

    response = deactivate(api_client("total"), deps, expected_version=SENTINEL)

    assert response.status_code == 409
    assert SENTINEL not in response.text and SENTINEL not in str(response.headers)


def test_deactivating_leaves_the_same_state_audit_and_baserow_traffic_as_the_page(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    connect_form(deps)
    version = current(deps)
    by_page: list[tuple[str, str, str]] = []
    by_api: list[tuple[str, str, str]] = []

    page, api = assert_same_effect(
        recorded(baserow, by_page, page_post(client, "deactivate", version=version)),
        recorded(baserow, by_api, lambda: deactivate(client, deps)),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
    )

    assert page.status_code == 303 and api.status_code == 200
    assert by_page == by_api == []
    found = stored(deps)
    assert found is not None and not found.active


# --- remove_form_integration --------------------------------------------------------------------


def test_remove_form_integration_needs_a_key_and_a_read_key_is_refused_and_nothing_happens(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    url = url_of("remove_form_integration", slug=SLUG)
    params = {"expected_version": current(deps), "confirm": "true"}
    anonymous = make_api(deps, FakeClock()).delete(url, params=params)
    reader = remove(api_client("leitura"), deps)

    assert anonymous.status_code == 401 and anonymous.json()["code"] == "invalid_credentials"
    assert reader.status_code == 403 and reader.json()["code"] == "forbidden_role"
    assert stored(deps) is not None


def test_remove_form_integration_of_a_form_that_does_not_exist_is_a_404(
    api_client: Client, deps: Deps, baserow: FakeBaserow
) -> None:
    response = remove(api_client("total"), deps, slug=SENTINEL)

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert SENTINEL not in response.text


@pytest.mark.parametrize("confirm", [OMIT, "false", "sim", ""], ids=["omitted", "false", "sim", ""])
def test_remove_form_integration_needs_confirm_true_in_the_query_and_does_nothing_without_it(
    deps: Deps, api_client: Client, baserow: FakeBaserow, confirm: Any
) -> None:
    connect_form(deps)
    failed_sync(deps)
    before = stored(deps)
    trail = audit_trail(deps)

    response = remove(api_client("total"), deps, confirm=confirm)

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert "apaga o token" in response.json()["detail"]  # o texto desta operação
    assert stored(deps) == before and count_of(deps, IntegrationSync) == 1
    assert audit_trail(deps) == trail


def test_remove_form_integration_deletes_the_token_and_the_sync_state_but_not_the_table_rows(
    deps: Deps, api_client: Client, baserow: FakeBaserow, caplog: pytest.LogCaptureFixture
) -> None:
    connect_form(deps)
    client = api_client("total")
    assert send_test(client).status_code == 200  # uma linha na tabela
    failed_sync(deps)
    before = stored(deps)
    assert before is not None
    rows, calls = copy.deepcopy(baserow.rows), len(baserow.calls)

    with caplog.at_level(logging.DEBUG):
        response = remove(client, deps)

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert (body["form_slug"], body["host"], body["table_id"]) == (
        SLUG,
        "baserow.example.test",
        BASEROW_TABLE,
    )
    assert (body["was_active"], body["mapped_fields"]) == (True, 9)
    assert body["sync_counts"] == {"pending": 0, "succeeded": 0, "failed": 1}
    assert stored(deps) is None and count_of(deps, Integration) == 0  # o token saiu com a linha
    assert count_of(deps, IntegrationSync) == 0  # e o estado das sincronizações
    assert baserow.rows == rows and len(baserow.calls) == calls  # a tabela não foi tocada
    (entry,) = [e for e in audits(deps) if e.action == "integration.remove"]
    assert entry.actor == client.actor
    for place in (response.text, caplog.text, stored_text(deps), str(entry.details)):
        assert BASEROW_TOKEN not in place


def test_remove_form_integration_on_a_stale_version_or_a_gone_integration_is_a_409(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    client = api_client("total")
    gone = remove(client, deps, expected_version="1")
    connect_form(deps)
    before = stored(deps)

    stale = remove(client, deps, expected_version="1")

    assert gone.status_code == 409 and gone.json()["current_version"] == "0"
    assert stale.status_code == 409 and stale.json()["code"] == "version_conflict"
    assert stale.json()["current_version"] == "3"
    assert stored(deps) == before and "integration.remove" not in [a.action for a in audits(deps)]


def test_remove_form_integration_repeated_after_a_lost_answer_is_a_409_and_removes_nothing_else(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    client = api_client("total")
    version = current(deps)

    first = remove(client, deps, expected_version=version)
    repeated = remove(client, deps, expected_version=version)

    assert (first.status_code, repeated.status_code) == (200, 409)
    assert (
        repeated.json()["code"] == "version_conflict" and repeated.json()["current_version"] == "0"
    )
    assert [e.action for e in audits(deps)].count("integration.remove") == 1


@pytest.mark.parametrize("version", [OMIT, ""], ids=["omitted", "blank"])
def test_remove_form_integration_needs_the_version_it_expects_in_the_query(
    deps: Deps, api_client: Client, baserow: FakeBaserow, version: Any
) -> None:
    connect_form(deps)

    response = remove(api_client("total"), deps, expected_version=version)

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    assert "expected_version" in {item["field"] for item in response.json()["errors"]}
    assert stored(deps) is not None


def test_remove_form_integration_echoes_none_of_a_version_it_refuses(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps)

    response = remove(api_client("total"), deps, expected_version=SENTINEL)

    assert response.status_code == 409
    assert SENTINEL not in response.text and SENTINEL not in str(response.headers)
    assert stored(deps) is not None


def test_removing_leaves_the_same_state_audit_and_baserow_traffic_as_the_page(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    connect_form(deps)
    failed_sync(deps)
    version = current(deps)
    by_page: list[tuple[str, str, str]] = []
    by_api: list[tuple[str, str, str]] = []

    page, api = assert_same_effect(
        recorded(baserow, by_page, page_post(client, "remove", version=version, confirm="on")),
        recorded(baserow, by_api, lambda: remove(client, deps)),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
    )

    assert page.status_code == 303 and api.status_code == 200
    assert by_page == by_api == []  # o app não apaga nada no Baserow
    assert stored(deps) is None and count_of(deps, IntegrationSync) == 0


# --- send_form_integration_test -----------------------------------------------------------------


def test_send_form_integration_test_needs_a_key_and_a_read_key_is_refused_and_nothing_happens(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    baserow.calls.clear()
    url = url_of("send_form_integration_test", slug=SLUG)
    anonymous = make_api(deps, FakeClock()).post(url, json={"confirm": True})
    reader = send_test(api_client("leitura"))

    assert anonymous.status_code == 401 and anonymous.json()["code"] == "invalid_credentials"
    assert reader.status_code == 403 and reader.json()["code"] == "forbidden_role"
    assert baserow.calls == [] and baserow.rows == {}


def test_send_form_integration_test_of_a_form_that_does_not_exist_is_a_404(
    api_client: Client, baserow: FakeBaserow
) -> None:
    response = send_test(api_client("total"), slug=SENTINEL)

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert SENTINEL not in response.text and baserow.calls == []


@pytest.mark.parametrize("confirm", [OMIT, False, "true", 1])
def test_send_form_integration_test_needs_confirm_true_and_does_nothing_without_it(
    deps: Deps, api_client: Client, baserow: FakeBaserow, confirm: Any
) -> None:
    connect_form(deps, active=False)
    trail = audit_trail(deps)
    baserow.calls.clear()

    response = send_test(api_client("total"), confirm=confirm)

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert "linha" in response.json()["detail"]  # o texto desta operação
    assert baserow.calls == [] and baserow.rows == {} and audit_trail(deps) == trail


def test_send_form_integration_test_writes_the_sample_row_and_the_second_call_updates_it(
    deps: Deps, api_client: Client, baserow: FakeBaserow, caplog: pytest.LogCaptureFixture
) -> None:
    connect_form(deps)
    real_job(deps)  # uma lead de verdade existe no banco, e a linha de teste não a leva
    client = api_client("total")

    with caplog.at_level(logging.DEBUG):
        first = send_test(client)
        second = send_test(client)

    assert first.status_code == second.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    one, two = first.json(), second.json()
    assert (one["ok"], one["outcome"], one["public_id"]) == (True, "created", f"teste-{SLUG}")
    assert (two["ok"], two["outcome"], two["row_id"]) == (True, "updated", one["row_id"])
    assert one["error"] is None and len(baserow.rows) == 1  # a segunda atualizou a mesma linha
    row = baserow.row_where(104, f"teste-{SLUG}")
    assert row["field_102"] == "teste@example.test"
    notes = [e for e in audits(deps) if e.action == "integration.test"]
    assert [(e.actor, e.details["outcome"]) for e in notes] == [
        (client.actor, "created"),
        (client.actor, "updated"),
    ]
    kept = str(baserow.rows) + str([e.details for e in notes])
    assert not lead_in(kept, first.text, second.text, caplog.text)
    assert BASEROW_TOKEN not in first.text + second.text + caplog.text + stored_text(deps)


def test_send_form_integration_test_does_not_go_through_hatchet_and_sends_no_sync(
    deps: Deps, api_client: Client, baserow: FakeBaserow, engine: list[uuid.UUID]
) -> None:
    connect_form(deps)
    real_job(deps)
    syncs_before = sync_statuses(deps)

    response = send_test(api_client("total"))

    assert response.status_code == 200 and engine == []
    assert sync_statuses(deps) == syncs_before  # o teste não mexe nas sincronizações


@pytest.mark.parametrize(("outcome", "message"), [(401, "HTTP 401"), (503, "HTTP 503")])
def test_send_form_integration_test_that_baserow_refuses_is_a_result_and_leaves_no_row(
    deps: Deps, api_client: Client, baserow: FakeBaserow, outcome: int, message: str
) -> None:
    connect_form(deps, active=False)
    baserow.fail("GET", outcome, where="fields")
    client = api_client("total")

    response = send_test(client)

    assert response.status_code == 200  # o teste rodou: o resultado é que não passou
    body = response.json()
    assert (body["ok"], body["outcome"], body["row_id"]) == (False, None, None)
    assert message in body["error"] and BASEROW_TOKEN not in response.text
    assert baserow.rows == {}
    (entry,) = [e for e in audits(deps) if e.action == "integration.test"]
    assert entry.actor == client.actor and entry.details["ok"] is False


def test_send_form_integration_test_with_nothing_mapped_says_why_it_did_not_pass(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps, entries=[], active=False)

    response = send_test(api_client("total"))

    body = response.json()
    assert response.status_code == 200 and body["ok"] is False
    assert "mapeamento está vazio" in body["error"] and baserow.rows == {}


def test_send_form_integration_test_of_a_form_without_integration_is_a_409_state_conflict(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    response = send_test(api_client("total"))

    assert response.status_code == 409 and response.json()["code"] == "state_conflict"
    assert "ainda não tem integração" in response.json()["detail"] and baserow.calls == []


def test_send_form_integration_test_with_a_token_that_does_not_open_is_a_409_and_sends_nothing(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))
    baserow.calls.clear()

    response = send_test(api_client("total"))

    assert response.status_code == 409 and response.json()["code"] == "state_conflict"
    assert "não abre com a WEBHOOK_ENCRYPTION_KEY" in response.json()["detail"]
    assert baserow.calls == [] and "integration.test" not in [a.action for a in audits(deps)]


def test_send_form_integration_test_refuses_a_body_that_does_not_serve_with_the_field(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)

    response = send_test(api_client("total"), nao_existe=1)

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    assert "nao_existe" in {item["field"] for item in response.json()["errors"]}
    assert baserow.rows == {}


def test_send_form_integration_test_costs_five_tokens_of_the_usage_limit(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    client = api_client("total", API_RATE_LIMIT_PER_MINUTE="5")  # o balde tem 5 fichas

    first = send_test(client)
    second = send_test(client)

    assert first.status_code == 200
    assert second.status_code == 429 and second.json()["code"] == "rate_limited"
    assert len(baserow.rows) == 1


def test_send_form_integration_test_echoes_none_of_a_value_it_refuses(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    """O eco da operação: o texto livre aqui é só a confirmação e o campo desconhecido."""
    connect_form(deps, active=False)
    client = api_client("total")

    wrong = send_test(client, confirm=SENTINEL)
    unknown = send_test(client, **{SENTINEL: 1})

    assert (wrong.status_code, unknown.status_code) == (422, 422)
    for response in (wrong, unknown):
        assert SENTINEL not in str(response.headers)
    assert SENTINEL not in wrong.text and baserow.rows == {}


def fresh(baserow: FakeBaserow, call: Callable[[], Any]) -> Callable[[], Any]:
    """`call`, com a tabela do Baserow como estava antes (sem linhas): o banco volta ao estado de
    antes entre a página e a API, e a tabela também tem de voltar."""

    def run() -> Any:
        baserow.rows.clear()
        baserow._next_id = 1
        return call()

    return run


def test_the_test_leaves_the_same_state_audit_baserow_traffic_and_row_as_the_page(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    connect_form(deps, active=False)
    by_page: list[tuple[str, str, str]] = []
    by_api: list[tuple[str, str, str]] = []
    rows: list[dict[int, dict[str, Any]]] = []

    def kept(call: Callable[[], Any]) -> Callable[[], Any]:
        def run() -> Any:
            result = call()
            rows.append(copy.deepcopy(baserow.rows))
            return result

        return run

    page, api = assert_same_effect(
        kept(fresh(baserow, recorded(baserow, by_page, page_post(client, "test")))),
        kept(fresh(baserow, recorded(baserow, by_api, lambda: send_test(client)))),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
    )

    assert page.status_code == 200 and api.status_code == 200
    assert "A linha de teste foi criada" in page.text and api.json()["outcome"] == "created"
    # as mesmas chamadas da ação (a página ainda lê a tabela de novo ao desenhar o resultado)
    assert by_page[: len(by_api)] == by_api and len(by_api) >= 2
    assert [req[0] for req in by_api].count("POST") == 1  # e uma escrita
    assert rows[0] == rows[1] and len(rows[0]) == 1  # a mesma linha na tabela
    assert [e.action for e in audits(deps)].count("integration.test") == 1


def test_a_test_baserow_refuses_leaves_the_same_audit_on_the_page_and_in_the_api(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    connect_form(deps, active=False)

    def failing(call: Callable[[], Any]) -> Callable[[], Any]:
        def run() -> Any:
            baserow.fail("GET", 401, where="fields")
            return call()

        return run

    page, api = assert_same_effect(
        failing(page_post(client, "test")),
        failing(lambda: send_test(client)),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
    )

    assert "O envio de teste não passou" in page.text and "HTTP 401" in page.text
    assert api.json()["ok"] is False and "HTTP 401" in api.json()["error"]
    assert baserow.rows == {}


# --- resend_failed_form_integration_syncs -------------------------------------------------------


def test_resend_failed_form_integration_syncs_needs_a_key_and_a_read_key_is_refused(
    deps: Deps, api_client: Client, baserow: FakeBaserow, engine: list[uuid.UUID]
) -> None:
    connect_form(deps)
    _, sync_id = failed_sync(deps)
    url = url_of("resend_failed_form_integration_syncs", slug=SLUG)
    anonymous = make_api(deps, FakeClock()).post(url, json={"confirm": True})
    reader = resend(api_client("leitura"))

    assert anonymous.status_code == 401 and anonymous.json()["code"] == "invalid_credentials"
    assert reader.status_code == 403 and reader.json()["code"] == "forbidden_role"
    assert sync_state(deps, sync_id).status == "failed" and engine == []


def test_resend_failed_form_integration_syncs_of_a_form_that_does_not_exist_is_a_404(
    api_client: Client, engine: list[uuid.UUID]
) -> None:
    response = resend(api_client("total"), slug=SENTINEL)

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert SENTINEL not in response.text and engine == []


@pytest.mark.parametrize("confirm", [OMIT, False, "true", 1])
def test_resend_failed_form_integration_syncs_needs_confirm_true_and_does_nothing_without_it(
    deps: Deps, api_client: Client, baserow: FakeBaserow, engine: list[uuid.UUID], confirm: Any
) -> None:
    connect_form(deps)
    _, sync_id = failed_sync(deps)
    trail = audit_trail(deps)

    response = resend(api_client("total"), confirm=confirm)

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert "200" in response.json()["detail"]  # o texto desta operação
    assert sync_state(deps, sync_id).status == "failed"  # nem estado novo,
    assert audit_trail(deps) == trail and engine == []  # nem auditoria, nem disparo


def test_resend_failed_form_integration_syncs_puts_them_back_in_the_queue_and_launches_them(
    deps: Deps, api_client: Client, baserow: FakeBaserow, engine: list[uuid.UUID]
) -> None:
    connect_form(deps)
    _, first = failed_sync(deps)
    others = clone_failed_syncs(deps, first, 2)
    client = api_client("total")

    response = resend(client)

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"resent": 3, "limit": 200, "still_failed": 0, "launched": True}
    assert until(lambda: len(engine) == 3) and engine == [
        first,
        *others,
    ]  # as mais antigas primeiro
    assert set(sync_statuses(deps).values()) == {"pending"}
    (entry,) = [e for e in audits(deps) if e.action == "sync.resend_failed"]
    assert (entry.actor, entry.details) == (client.actor, {"count": 3})


def test_resend_failed_form_integration_syncs_after_the_table_is_fixed_brings_the_row_in(
    deps: Deps, api_client: Client, baserow: FakeBaserow, engine: list[uuid.UUID]
) -> None:
    """O caso de uso: uma opção do formulário não tinha par no Baserow, a sincronização falhou; a
    operadora corrige a tabela e reenvia, e o worker (aqui, o `push`) grava a linha."""
    connect_form(deps)
    baserow.fields[7] = baserow_field(108, "Equipe", "single_select", options=("Só eu",))
    public_id = real_job(deps)
    (sync_id,) = sync_ids(deps)
    with pytest.raises(TerminalError):
        push.push(deps, sync_id)
    with deps.sessions.begin() as session:
        syncs.mark_failed(session, sync_id, "integration_mapping")
    baserow.fields = demo_baserow_fields()  # corrigida

    response = resend(api_client("total"))

    assert response.json()["resent"] == 1 and response.json()["launched"] is True
    assert until(lambda: engine == [sync_id]) and sync_state(deps, sync_id).status == "pending"
    assert push.push(deps, sync_id) == "succeeded"  # o worker pega a sincronização que voltou
    assert baserow.row_where(104, public_id)["field_102"] == REAL_EMAIL


def test_resend_failed_form_integration_syncs_takes_at_most_two_hundred_the_oldest_first(
    deps: Deps, api_client: Client, baserow: FakeBaserow, engine: list[uuid.UUID]
) -> None:
    connect_form(deps)
    _, first = failed_sync(deps)
    clones = clone_failed_syncs(deps, first, 200)  # 201 falhadas ao todo
    client = api_client("total")

    response = resend(client)

    assert response.json() == {"resent": 200, "limit": 200, "still_failed": 1, "launched": True}
    assert until(lambda: len(engine) == 200) and engine == [first, *clones[:199]]
    assert sync_state(deps, clones[-1]).status == "failed"  # a que passou do limite espera
    (entry,) = [e for e in audits(deps) if e.action == "sync.resend_failed"]
    assert entry.details == {"count": 200}
    again = resend(client)  # repetir leva as que sobraram
    assert again.json() == {"resent": 1, "limit": 200, "still_failed": 0, "launched": True}
    assert until(lambda: engine[-1:] == [clones[-1]]) and len(engine) == 201


def test_resend_failed_form_integration_syncs_with_an_engine_that_did_not_receive_it_is_launched_false(
    deps: Deps, api_client: Client, baserow: FakeBaserow, deaf_engine: list[uuid.UUID]
) -> None:
    connect_form(deps)
    _, first = failed_sync(deps)
    clone_failed_syncs(deps, first, 1)

    response = resend(api_client("total"))

    assert response.status_code == 200  # o disparo que não chega ao engine não é erro
    assert response.json() == {"resent": 2, "limit": 200, "still_failed": 0, "launched": False}
    assert set(sync_statuses(deps).values()) == {
        "pending"
    }  # voltaram para a fila, de qualquer modo
    with deps.sessions() as session:  # e o reconciliador as encontra, sem run id
        found = syncs.unlaunched(session, older_than=timedelta(0), limit=10)
    assert {row.id for row in found} == set(sync_statuses(deps))
    assert deaf_engine[:1] == [first]  # a primeira foi tentada


def test_resend_failed_form_integration_syncs_waits_no_longer_than_the_trigger_timeout(
    deps: Deps, api_client: Client, baserow: FakeBaserow, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect_form(deps)
    _, first = failed_sync(deps)
    clones = clone_failed_syncs(deps, first, 1)
    release = threading.Event()
    seen: list[uuid.UUID] = []

    def stuck(target: uuid.UUID) -> str | None:
        seen.append(target)
        if target == first:
            release.wait(timeout=10)  # o SDK insiste por dezenas de segundos sem engine
        return "run-s"

    monkeypatch.setattr(workflows, "launch_sync", stuck)
    client = api_client("total", HATCHET_TRIGGER_TIMEOUT_SECONDS="0.2")

    started = time.monotonic()
    response = resend(client)
    elapsed = time.monotonic() - started
    release.set()

    assert response.status_code == 200 and response.json()["launched"] is False and elapsed < 5
    assert response.json()["resent"] == 2
    assert until(lambda: seen == [first, *clones])  # a fila andou sem quem esperava


def test_resend_failed_form_integration_syncs_with_nothing_failed_resends_nothing(
    deps: Deps, api_client: Client, baserow: FakeBaserow, engine: list[uuid.UUID]
) -> None:
    connect_form(deps)
    trail = audit_trail(deps)

    response = resend(api_client("total"))

    assert response.status_code == 200
    assert response.json() == {"resent": 0, "limit": 200, "still_failed": 0, "launched": False}
    assert engine == [] and audit_trail(deps) == trail  # sem auditoria e sem disparo


def test_resend_failed_form_integration_syncs_repeated_resends_nothing_the_second_time(
    deps: Deps, api_client: Client, baserow: FakeBaserow, engine: list[uuid.UUID]
) -> None:
    """Seguro de repetir depois de uma resposta perdida: as que voltaram para a fila já não estão em
    `failed`."""
    connect_form(deps)
    failed_sync(deps)
    client = api_client("total")

    first = resend(client)
    second = resend(client)

    assert (first.json()["resent"], second.json()["resent"]) == (1, 0)
    assert until(lambda: len(engine) == 1)
    assert [e.action for e in audits(deps)].count("sync.resend_failed") == 1


def test_resend_failed_form_integration_syncs_of_a_form_without_integration_is_a_409(
    deps: Deps, api_client: Client, baserow: FakeBaserow, engine: list[uuid.UUID]
) -> None:
    response = resend(api_client("total"))

    assert response.status_code == 409 and response.json()["code"] == "state_conflict"
    assert "ainda não tem integração" in response.json()["detail"] and engine == []


def test_resend_failed_form_integration_syncs_refuses_a_body_that_does_not_serve(
    deps: Deps, api_client: Client, baserow: FakeBaserow, engine: list[uuid.UUID]
) -> None:
    connect_form(deps)
    _, sync_id = failed_sync(deps)

    response = resend(api_client("total"), nao_existe=1)

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    assert "nao_existe" in {item["field"] for item in response.json()["errors"]}
    assert sync_state(deps, sync_id).status == "failed" and engine == []


def test_resend_failed_form_integration_syncs_costs_one_token_and_calls_no_baserow(
    deps: Deps, api_client: Client, baserow: FakeBaserow, engine: list[uuid.UUID]
) -> None:
    connect_form(deps)
    failed_sync(deps)
    baserow.calls.clear()
    client = api_client("total", API_RATE_LIMIT_PER_MINUTE="5")  # o balde tem 5 fichas

    responses = [resend(client) for _ in range(6)]

    assert [r.status_code for r in responses] == [200, 200, 200, 200, 200, 429]
    assert baserow.calls == []  # enfileirar e disparar não fala com o Baserow


def test_resend_failed_form_integration_syncs_launches_only_ids_and_leaks_no_lead_data(
    deps: Deps,
    api_client: Client,
    baserow: FakeBaserow,
    engine: list[uuid.UUID],
    caplog: pytest.LogCaptureFixture,
) -> None:
    connect_form(deps)
    real_job(deps)
    (sync_id,) = sync_ids(deps)
    with deps.sessions.begin() as session:
        syncs.mark_failed(session, sync_id, "integration_unavailable")

    with caplog.at_level(logging.DEBUG):
        response = resend(api_client("total"))

    assert response.json()["resent"] == 1 and until(lambda: len(engine) == 1)
    assert [type(target) for target in engine] == [uuid.UUID]  # a task recebe só o id
    notes = [e for e in audits(deps) if e.action == "sync.resend_failed"]
    assert notes and not lead_in(response.text, caplog.text, *(str(e.details) for e in notes))


def test_resend_failed_form_integration_syncs_echoes_none_of_a_value_it_refuses(
    deps: Deps, api_client: Client, baserow: FakeBaserow, engine: list[uuid.UUID]
) -> None:
    connect_form(deps)
    client = api_client("total")

    wrong = resend(client, confirm=SENTINEL)
    unknown = resend(client, **{SENTINEL: 1})

    assert (wrong.status_code, unknown.status_code) == (422, 422)
    assert SENTINEL not in wrong.text
    for response in (wrong, unknown):
        assert SENTINEL not in str(response.headers)
    assert engine == []


def test_resending_leaves_the_same_state_audit_and_launches_as_the_page(
    deps: Deps, api_client: Client, baserow: FakeBaserow, engine: list[uuid.UUID]
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    connect_form(deps)
    _, first = failed_sync(deps)
    clone_failed_syncs(deps, first, 2)
    launches: list[list[uuid.UUID]] = []

    def launched(call: Callable[[], Any]) -> Callable[[], Any]:
        def run() -> Any:
            start = len(engine)
            result = call()
            assert until(lambda: len(engine) - start >= 3)  # a página dispara em segundo plano
            launches.append(engine[start:])
            return result

        return run

    page, api = assert_same_effect(
        launched(page_post(client, "resend-failed")),
        launched(lambda: resend(client)),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
    )

    assert page.status_code == 303 and api.status_code == 200
    assert (
        launches[0] == launches[1] and len(launches[0]) == 3
    )  # os mesmos disparos, na mesma ordem
    assert set(sync_statuses(deps).values()) == {"pending"}


def test_resending_with_nothing_failed_leaves_the_same_nothing_on_the_page_and_in_the_api(
    deps: Deps, api_client: Client, baserow: FakeBaserow, engine: list[uuid.UUID]
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    connect_form(deps)

    page, api = assert_same_effect(
        page_post(client, "resend-failed"),
        lambda: resend(client),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
        expect_no_change=True,
    )

    assert page.status_code == 303 and api.json()["resent"] == 0 and engine == []


# --- o contrato ---------------------------------------------------------------------------------


def test_the_five_operations_carry_the_role_and_the_marks_of_the_matrix() -> None:
    document = built_openapi()
    by_id = {op["operationId"]: (m, p, op) for m, p, op in operations_of(document)}

    marks = {
        "activate_form_integration": ("POST", "x-external-effect"),
        "deactivate_form_integration": ("POST", None),
        "remove_form_integration": ("DELETE", "x-destructive"),
        "send_form_integration_test": ("POST", "x-external-effect"),
        "resend_failed_form_integration_syncs": ("POST", "x-external-effect"),
    }
    for operation_id, (method, mark) in marks.items():
        found_method, path, operation = by_id[operation_id]
        assert found_method == method and operation["x-required-role"] == "total"
        present = {key for key in ("x-destructive", "x-external-effect") if key in operation}
        assert present == ({mark} if mark else set()), operation_id
        assert "integrations/baserow" in path


def test_the_marked_operations_declare_confirm_where_the_matrix_says_it_goes() -> None:
    document = built_openapi()
    by_id = {op["operationId"]: op for _, _, op in operations_of(document)}

    remove_query = {p["name"] for p in by_id["remove_form_integration"]["parameters"]}
    assert {"confirm", "expected_version"} <= remove_query  # em `DELETE`, na query
    schemas = document["components"]["schemas"]
    for name in (
        "ActivateFormIntegrationRequest",
        "SendFormIntegrationTestRequest",
        "ResendFailedFormIntegrationSyncsRequest",
    ):
        assert "confirm" in schemas[name]["properties"], name
    assert "expected_version" in schemas["ActivateFormIntegrationRequest"]["properties"]


def test_no_response_of_the_five_operations_has_a_field_that_could_hold_the_token() -> None:
    document = built_openapi()
    schemas = document["components"]["schemas"]
    for name in (
        "SentFormIntegrationTest",
        "RemovedFormIntegration",
        "ResentFailedFormIntegrationSyncs",
    ):
        assert not {"token", "token_sealed", "secret"} & set(schemas[name]["properties"]), name
