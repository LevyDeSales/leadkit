"""Painel admin pela seam HTTP (TestClient): login, sessão, CSRF, lista, detalhe e ações."""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta, timezone
from html import unescape

import httpx
import pytest
import sqlalchemy as sa
from argon2 import PasswordHasher
from conftest import DEMO_ANSWERS, legacy_definition, publish, record_webhook
from fastapi.testclient import TestClient
from test_intake import submit

from app import jobs, pipeline, retention, workflows
from app.admin import views
from app.admin.auth import instant
from app.api.main import create_app
from app.deps import Deps
from app.errors import FailureCode, RetryableError
from app.forms import FormSpec
from app.forms.definition import compile_definition
from app.models import Form, Job, utcnow
from app.settings import Settings

PASSWORD = "senha-certa-do-painel"
# Hash barato só para os testes; produção usa os parâmetros padrão do argon2.
PASSWORD_HASH = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
ORIGIN = {"Origin": "http://testserver"}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def triggered() -> list[tuple[uuid.UUID, int]]:
    return []


@pytest.fixture
def admin_env() -> dict[str, str]:
    return {
        "ADMIN_USERNAME": "operadora",
        "ADMIN_PASSWORD_HASH": PASSWORD_HASH,
        "ADMIN_SESSION_SECRET": "segredo-da-sessao-admin-0123456789abcdef",
    }


@pytest.fixture
def client(
    deps: Deps,
    clock: FakeClock,
    triggered: list[tuple[uuid.UUID, int]],
    admin_env: dict[str, str],
) -> TestClient:
    deps.settings = Settings(_env_file=None, **admin_env)  # type: ignore[call-arg]

    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        triggered.append((job_id, attempt))
        return "run-fake"

    return TestClient(create_app(trigger=fake_trigger, clock=clock), follow_redirects=False)


def login(
    client: TestClient, username: str = "operadora", password: str = PASSWORD, **form: str
) -> httpx.Response:
    return client.post(
        "/admin/login",
        data={"username": username, "password": password, **form},
        headers=ORIGIN,
    )


def csrf_token(client: TestClient) -> str:
    """Lê o token como o navegador: do formulário renderizado."""
    match = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/admin").text)
    assert match is not None
    return match.group(1)


def table_body(html: str) -> str:
    """Linhas da tabela; "" quando a página mostra o estado vazio."""
    match = re.search(r"<tbody>(.*?)</tbody>", html, re.DOTALL)
    if match is None:
        assert re.search(r"Nenhum pedido encontrado|Nenhuma (entrega|sincronização)", html)
        return ""
    return match.group(1)


def new_lead_job(deps: Deps, name: str, email: str) -> Job:
    return submit(deps, key=email, answers={**DEMO_ANSWERS, "nome": name, "email": email}).job


def new_mcc_job(deps: Deps, name: str, email: str) -> Job:
    """Um pedido do MCC, o segundo formulário dos testes de lista (o `new_lead_job` é do demo): as
    respostas de exemplo do MCC com o nome e o e-mail da lead."""
    answers = {**legacy_definition("mcc")["examples"]["answers"], "nome": name, "email": email}
    consent = {"privacy": True, "text_version": "mcc-v1"}
    return submit(
        deps, key=email, answers=answers, consent=consent, form=deps.forms.current("mcc")
    ).job


BRT = timezone(timedelta(hours=-3), "BRT")  # Brasília, sem horário de verão desde 2019

# O "agora" dos testes do filtro "Recebido": 23:00 de 15/01/2027 em Brasília, que em UTC já é 16/01.
PERIOD_NOW = datetime(2027, 1, 15, 23, 0, tzinfo=BRT)
# Quando chegou cada pedido de `jobs_by_arrival`, em Brasília, nas bordas dos dias de calendário.
ARRIVALS = {
    "tonight": datetime(2027, 1, 15, 22, 0, tzinfo=BRT),  # 01:00 UTC de 16/01: o dia UTC já virou
    "start_of_today": datetime(2027, 1, 15, 0, 0, tzinfo=BRT),
    "yesterday_late": datetime(2027, 1, 14, 23, 59, tzinfo=BRT),
    "start_of_7_days": datetime(2027, 1, 9, 0, 0, tzinfo=BRT),  # hoje e os seis dias anteriores
    "before_7_days": datetime(2027, 1, 8, 23, 59, tzinfo=BRT),
    "start_of_30_days": datetime(2026, 12, 17, 0, 0, tzinfo=BRT),  # hoje e os 29 anteriores
    "before_30_days": datetime(2026, 12, 16, 23, 59, tzinfo=BRT),
    "old": datetime(2026, 11, 1, 12, 0, tzinfo=BRT),
}
# O que cada opção de "Recebido" mostra, com `PERIOD_NOW`. Calculado à mão, não pelo código.
PERIOD_EXPECTED = {
    "": set(ARRIVALS),
    "today": {"tonight", "start_of_today"},
    "7d": {"tonight", "start_of_today", "yesterday_late", "start_of_7_days"},
    "30d": {
        "tonight",
        "start_of_today",
        "yesterday_late",
        "start_of_7_days",
        "before_7_days",
        "start_of_30_days",
    },
}


def received_at(deps: Deps, job: Job, moment: datetime) -> Job:
    """Muda quando o pedido chegou (`created_at`), por onde a lista ordena e filtra."""
    with deps.sessions.begin() as session:
        session.execute(sa.update(Job).where(Job.id == job.id).values(created_at=moment))
    return job


def jobs_by_arrival(deps: Deps) -> dict[str, Job]:
    """Um pedido do demo em cada instante de `ARRIVALS`, pelo nome do instante."""
    return {
        name: received_at(
            deps, new_lead_job(deps, f"Lead {name}", f"lead-{number}@example.test"), moment
        )
        for number, (name, moment) in enumerate(ARRIVALS.items())
    }


def deliver_to_the_webhook(
    deps: Deps, job_id: uuid.UUID, attempt: int = 1, *, outcome: str = "succeeded"
) -> uuid.UUID:
    """Conclui a tentativa `attempt` e entrega o PDF dela ao webhook: `succeeded` (o destino
    recebeu), `failed` (o destino fora do ar até a entrega esgotar as tentativas, o pedido segue
    concluído) ou `pending` (a entrega nasce e fica na fila). Devolve a entrega."""
    down = RetryableError(FailureCode.DELIVERY_UNAVAILABLE, "HTTP 503")
    record_webhook(deps, error=down if outcome == "failed" else None)
    (delivery_id,) = finish(deps, job_id, attempt)
    if outcome == "succeeded":
        pipeline.deliver(deps, delivery_id)
    elif outcome == "failed":
        with pytest.raises(RetryableError):
            pipeline.deliver(deps, delivery_id)
        pipeline.mark_delivery_failed(deps, delivery_id, "delivery_unavailable")
    return delivery_id


def reprocess_order(deps: Deps, job: Job, *, from_attempt: int = 1) -> None:
    with deps.sessions.begin() as session:
        assert jobs.reprocess(
            session,
            job.id,
            actor="operadora",
            reason="Reprocessado para o teste",
            expected_attempt=from_attempt,
        )


# A situação da entrega que a lista distingue, na ordem em que `jobs_by_delivery` cria os pedidos.
DELIVERY_CASES = (
    "failed_now",  # concluído; a entrega da tentativa 1 falhou
    "pending_now",  # concluído; a entrega da tentativa 1 está na fila
    "delivered",  # concluído; a entrega da tentativa 1 foi recebida
    "retried_ok",  # a entrega da tentativa 1 falhou; reprocessado, a da tentativa 2 foi recebida
    "regressed",  # a da tentativa 1 foi recebida; reprocessado, a da tentativa 2 falhou
    "no_delivery",  # na fila, ainda sem PDF nem entrega
)
# O que cada opção de "Entrega" mostra: a situação das entregas da tentativa **atual**. Calculado à
# mão, não pelo código.
DELIVERY_EXPECTED = {
    "": set(DELIVERY_CASES),
    "failed": {"failed_now", "regressed"},
    "pending": {"pending_now"},
}


def jobs_by_delivery(deps: Deps) -> dict[str, Job]:
    """Um pedido do demo em cada situação de `DELIVERY_CASES`, pelo nome dela. Os do fim da lista são
    os mais novos."""
    made = {
        name: new_lead_job(deps, f"Lead {name}", f"lead-{number}@example.test")
        for number, name in enumerate(DELIVERY_CASES)
    }
    deliver_to_the_webhook(deps, made["failed_now"].id, outcome="failed")
    deliver_to_the_webhook(deps, made["pending_now"].id, outcome="pending")
    deliver_to_the_webhook(deps, made["delivered"].id)
    deliver_to_the_webhook(deps, made["retried_ok"].id, outcome="failed")
    reprocess_order(deps, made["retried_ok"])
    deliver_to_the_webhook(deps, made["retried_ok"].id, 2)
    deliver_to_the_webhook(deps, made["regressed"].id)
    reprocess_order(deps, made["regressed"])
    deliver_to_the_webhook(deps, made["regressed"].id, 2, outcome="failed")
    return made


def finish(deps: Deps, job_id: uuid.UUID, attempt: int = 1) -> list[uuid.UUID]:
    """Roda os passos do pipeline até concluir; devolve as entregas criadas."""
    with deps.sessions.begin() as session:
        jobs.claim(session, job_id, attempt, f"run-{attempt}")
    plan_id, _ = pipeline.generate_plan(deps, job_id, attempt)
    artifact_id, _ = pipeline.render_pdf(deps, job_id, attempt, plan_id)
    return pipeline.finalize(deps, job_id, attempt, artifact_id)


def failed_delivery(deps: Deps, job_id: uuid.UUID) -> uuid.UUID:
    """Conclui o job com o webhook fora do ar e esgota a entrega, como faria o Hatchet."""
    record_webhook(deps, error=RetryableError(FailureCode.DELIVERY_UNAVAILABLE, "HTTP 503"))
    (delivery_id,) = finish(deps, job_id)
    with pytest.raises(RetryableError):
        pipeline.deliver(deps, delivery_id)
    pipeline.mark_delivery_failed(deps, delivery_id, "delivery_unavailable")
    return delivery_id


def section(html: str, section_id: str) -> str:
    match = re.search(rf'<section[^>]*id="{section_id}".*?</section>', html, re.DOTALL)
    assert match is not None, section_id
    return match.group(0)


def timeline_events(html: str) -> str:
    """Só os eventos da linha do tempo, em ordem (sem os selos do título de cada tentativa)."""
    lists = re.findall(
        r'<ol class="timeline">(.*?)</ol>', section(html, "linha-do-tempo"), re.DOTALL
    )
    return "\n".join(lists)


def test_admin_pages_redirect_to_login_without_a_session(client: TestClient) -> None:
    pages = [client.get(path) for path in ("/admin", "/admin/jobs/x", "/admin/jobs/x/pdf")]
    posts = [
        client.post(path, data={"reason": "motivo qualquer", "confirm": "on"}, headers=ORIGIN)
        for path in (
            "/admin/jobs/x/reprocess",
            f"/admin/jobs/x/deliveries/{uuid.uuid4()}/resend",
            "/admin/logout",
        )
    ]
    for response in pages + posts:
        assert response.status_code == 303, response.url
        assert response.headers["location"].startswith("/admin/login"), response.url
        assert response.headers["cache-control"] == "no-store"

    page = client.get("/admin/login")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "noindex" in page.headers["x-robots-tag"]
    assert 'name="username"' in page.text and 'type="password"' in page.text


def test_login_opens_an_http_only_session_and_shows_the_job_list(
    client: TestClient, deps: Deps
) -> None:
    submit(deps, answers={**DEMO_ANSWERS, "nome": "Marina Alves", "email": "marina@example.test"})

    response = login(client)

    assert response.status_code == 303 and response.headers["location"] == "/admin"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/admin" in cookie
    assert "secure" not in cookie  # o teste roda em http
    page = client.get("/admin")
    assert page.status_code == 200
    assert "Marina Alves" in page.text and "marina@example.test" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert client.get("/admin/login").headers["location"] == "/admin"  # já logada


def test_login_returns_to_the_requested_admin_page_only(client: TestClient) -> None:
    assert (
        client.get("/admin/jobs/abc").headers["location"]
        == "/admin/login?next=%2Fadmin%2Fjobs%2Fabc"
    )
    assert login(client, next="/admin/jobs/abc").headers["location"] == "/admin/jobs/abc"
    client.cookies.clear()
    assert login(client, next="https://golpe.example/admin").headers["location"] == "/admin"
    client.cookies.clear()
    assert login(client, next="//golpe.example/admin").headers["location"] == "/admin"


def test_failed_logins_get_one_generic_message_and_lock_out_progressively(
    client: TestClient, clock: FakeClock
) -> None:
    wrong_password = login(client, password="errada")
    wrong_user = login(client, username="outra")
    for response in (wrong_password, wrong_user):
        assert response.status_code == 400
        assert "Usuário ou senha incorretos." in response.text
        assert "set-cookie" not in response.headers
    login(client, password="errada")
    login(client, password="errada")

    fifth = login(client, password="errada")
    assert fifth.status_code == 429 and fifth.headers["retry-after"] == "60"
    assert "Muitas tentativas" in fifth.text
    assert login(client).status_code == 429  # nem a senha certa passa durante o bloqueio

    clock.advance(61)
    assert login(client, password="errada").headers["retry-after"] == "120"  # 6ª: dobra
    clock.advance(121)
    assert login(client).status_code == 303
    client.cookies.clear()
    assert login(client, password="errada").status_code == 400  # o acerto zerou a contagem


def test_the_app_clock_is_read_as_an_aware_utc_instant(clock: FakeClock) -> None:
    now = instant(clock)

    assert now == datetime.fromtimestamp(clock.now, UTC)
    assert now.utcoffset() == timedelta(0)
    clock.advance(90)
    assert instant(clock) - now == timedelta(seconds=90)


def test_session_expires_after_eight_idle_hours(client: TestClient, clock: FakeClock) -> None:
    login(client)
    clock.advance(8 * 3600 + 1)
    expired = client.get("/admin")
    assert expired.status_code == 303
    assert "max-age=0" in expired.headers["set-cookie"].lower()  # o cookie vencido é apagado


def test_activity_renews_the_session_up_to_twelve_hours_after_login(
    client: TestClient, clock: FakeClock
) -> None:
    login(client)
    clock.advance(7 * 3600)
    assert client.get("/admin").status_code == 200
    clock.advance(4 * 3600)  # 4 h sem uso, 11 h desde o login
    assert client.get("/admin").status_code == 200
    clock.advance(2 * 3600)  # 13 h desde o login
    assert client.get("/admin").status_code == 303


def test_changing_the_password_ends_existing_sessions(
    client: TestClient, deps: Deps, clock: FakeClock, admin_env: dict[str, str]
) -> None:
    login(client)
    new_hash = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("outra-senha")
    deps.settings = Settings(  # type: ignore[call-arg]
        _env_file=None, **{**admin_env, "ADMIN_PASSWORD_HASH": new_hash}
    )
    restarted = TestClient(
        create_app(trigger=None, clock=clock), follow_redirects=False, cookies=client.cookies
    )
    assert restarted.get("/admin").status_code == 303


def test_a_tampered_session_cookie_is_not_a_session(client: TestClient) -> None:
    login(client)
    value = client.cookies["mcc_admin"]
    middle = len(value) // 2
    forged = value[:middle] + ("A" if value[middle] != "A" else "B") + value[middle + 1 :]
    client.cookies.clear()
    client.cookies.set("mcc_admin", forged, path="/admin")
    assert client.get("/admin").status_code == 303
    client.cookies.set("mcc_admin", "lixo-qualquer", path="/admin")
    assert client.get("/admin").status_code == 303


def test_posts_need_the_session_csrf_token_and_a_same_origin_request(
    client: TestClient,
) -> None:
    login(client)
    token = csrf_token(client)
    refused = [
        client.post("/admin/logout", headers=ORIGIN),
        client.post("/admin/logout", data={"csrf_token": "outro"}, headers=ORIGIN),
        client.post(
            "/admin/logout", data={"csrf_token": token}, headers={"Origin": "https://golpe.example"}
        ),
        client.post("/admin/logout", data={"csrf_token": token}, headers={"Origin": "null"}),
        client.post("/admin/logout", data={"csrf_token": token}),  # sem Origin nem Referer
    ]
    assert [response.status_code for response in refused] == [403] * 5
    assert client.get("/admin").status_code == 200  # nenhuma delas encerrou a sessão

    by_referer = client.post(
        "/admin/logout", data={"csrf_token": token}, headers={"Referer": "http://testserver/admin"}
    )
    assert by_referer.status_code == 303 and by_referer.headers["location"] == "/admin/login"
    assert client.get("/admin").status_code == 303


def test_htmx_requests_send_the_token_in_a_header(client: TestClient) -> None:
    login(client)
    token = csrf_token(client)
    assert f'"X-CSRF-Token": "{token}"' in client.get("/admin").text  # hx-headers no <body>
    response = client.post("/admin/logout", headers={**ORIGIN, "X-CSRF-Token": token})
    assert response.status_code == 303
    assert client.get("/admin").status_code == 303


def test_login_form_posts_from_other_origins_are_refused(client: TestClient) -> None:
    response = client.post(
        "/admin/login",
        data={"username": "operadora", "password": PASSWORD},
        headers={"Origin": "https://golpe.example"},
    )
    assert response.status_code == 403 and "set-cookie" not in response.headers


def test_job_list_searches_by_name_email_or_code_and_filters_by_status(
    client: TestClient, deps: Deps
) -> None:
    record_webhook(deps)
    marina = new_lead_job(deps, "Marina Alves", "marina@alves.example")
    paula = new_lead_job(deps, "Paula Nogueira", "contato@pn.example")
    renata = new_lead_job(deps, "Renata Souza", "renata@souza.example")
    finish(deps, marina.id)
    with deps.sessions.begin() as session:
        jobs.fail(session, paula.id, 1, "plan_invalid", "plan falhou")
    login(client)

    def rows(**params: str) -> str:
        return table_body(client.get("/admin", params=params).text)

    def names(**params: str) -> set[str]:
        body = rows(**params)
        return {n for n in ("Marina Alves", "Paula Nogueira", "Renata Souza") if n in body}

    assert names() == {"Marina Alves", "Paula Nogueira", "Renata Souza"}
    assert names(q="alves") == {"Marina Alves"}  # parte do nome, sem diferenciar maiúsculas
    assert names(q="CONTATO@pn") == {"Paula Nogueira"}  # parte do e-mail
    assert names(q=renata.public_id) == {"Renata Souza"}  # código exato
    assert names(q="100%_") == set()  # curingas do LIKE contam como texto
    assert names(status="failed") == {"Paula Nogueira"}
    assert names(q="alves", status="failed") == set()

    assert "Concluído" in rows(q="alves") and "Webhook pendente" in rows(q="alves")
    assert "Falhou" in rows(status="failed") and "P2" in rows(status="failed")
    assert "Na fila" in rows(q=renata.public_id)


def test_job_list_shows_the_newest_first_in_pages_of_25(client: TestClient, deps: Deps) -> None:
    for n in range(26):
        new_lead_job(deps, f"Lead {n:02d}", f"lead{n}@example.test")
    login(client)

    first = client.get("/admin").text
    body = table_body(first)
    assert body.index("Lead 25") < body.index("Lead 01") and "Lead 00" not in body
    assert "Página 1 de 2" in first
    second = table_body(client.get("/admin", params={"page": "2"}).text)
    assert "Lead 00" in second and "Lead 25" not in second
    assert "Lead 25" in table_body(client.get("/admin", params={"page": "x"}).text)


def select_choices(html: str, select_id: str) -> list[tuple[str, bool, str]]:
    """As escolhas de um select da barra: (valor, escolhida, texto)."""
    match = re.search(rf'<select class="select" id="{select_id}".*?</select>', html, re.DOTALL)
    assert match is not None
    return [
        (value, bool(chosen), label)
        for value, chosen, label in re.findall(
            r'<option value="([^"]*)"( selected)?>([^<]*)</option>', match.group(0)
        )
    ]


def form_select(html: str) -> list[tuple[str, bool, str]]:
    """As escolhas do select "Formulário"."""
    return select_choices(html, "f-form")


def test_job_list_filters_by_form_and_shows_the_form_of_each_order(
    client: TestClient, deps: Deps
) -> None:
    new_lead_job(deps, "Marina Alves", "marina@alves.example")  # demo
    new_mcc_job(deps, "Fernanda Rocha", "fernanda@rocha.example")  # mcc
    login(client)

    def names(**params: str) -> set[str]:
        body = table_body(client.get("/admin", params=params).text)
        return {n for n in ("Marina Alves", "Fernanda Rocha") if n in body}

    assert names() == {"Marina Alves", "Fernanda Rocha"}
    assert names(form="mcc") == {"Fernanda Rocha"}
    assert names(form="demo") == {"Marina Alves"}
    assert names(form="mcc", q="alves") == set()  # combina com a busca
    assert names(form="mcc", q="rocha") == {"Fernanda Rocha"}
    assert names(form="mcc", status="queued") == {"Fernanda Rocha"}  # e com o status
    assert names(form="mcc", status="failed") == set()

    page = client.get("/admin").text  # a coluna: o título do formulário e, menor, a versão
    assert '<th scope="col">Formulário</th>' in page
    rows = table_body(page)
    mcc_version = deps.forms.current("mcc").version
    assert re.search(
        rf'data-label="Formulário"><span class="cell-title">Planejamento de atividades'
        rf'</span><span class="sub">versão {re.escape(mcc_version)}</span>',
        rows,
    )
    assert re.search(
        r'data-label="Formulário"><span class="cell-title">Projeto de demonstração'
        r'</span><span class="sub">versão ',
        rows,
    )


def test_job_list_offers_every_form_by_title_marks_the_inactive_one_and_the_chosen_one(
    client: TestClient, deps: Deps
) -> None:
    with deps.sessions.begin() as session:
        session.execute(sa.update(Form).where(Form.slug == "reforma-2027").values(active=False))
    login(client)

    everything = form_select(client.get("/admin").text)
    mcc = form_select(client.get("/admin", params={"form": "mcc"}).text)

    assert everything == [
        ("", False, "Todos"),
        ("reforma-2027", False, "Calendário de atividades (inativo)"),
        ("mcc", False, "Planejamento de atividades"),
        ("demo", False, "Projeto de demonstração"),
    ]
    assert [(value, chosen) for value, chosen, _ in mcc if chosen] == [("mcc", True)]


def test_job_list_with_only_the_form_filter_says_so_and_offers_to_clear_it(
    client: TestClient, deps: Deps
) -> None:
    new_lead_job(deps, "Marina Alves", "marina@alves.example")
    new_mcc_job(deps, "Fernanda Rocha", "fernanda@rocha.example")
    login(client)

    plain = client.get("/admin").text
    mcc = client.get("/admin", params={"form": "mcc"}).text

    assert "Limpar filtros" not in plain and "com estes filtros" not in plain
    assert re.search(r'role="status">2 pedidos</p>', plain)
    assert re.search(r'role="status">1 pedido com estes filtros</p>', mcc)
    assert 'href="/admin">Limpar filtros</a>' in mcc


def test_job_list_with_a_form_that_does_not_exist_is_empty_and_a_bad_address_is_ignored(
    client: TestClient, deps: Deps
) -> None:
    new_lead_job(deps, "Marina Alves", "marina@alves.example")
    login(client)

    unknown = client.get("/admin", params={"form": "nao-existe"})
    ignored = client.get("/admin", params={"form": "Nao Existe!", "status": "inexistente"})

    assert unknown.status_code == 200
    assert "Nenhum pedido encontrado com estes filtros." in unknown.text
    assert re.search(r'role="status">0 pedidos com estes filtros</p>', unknown.text)
    assert 'href="/admin">Limpar filtros</a>' in unknown.text
    assert ("nao-existe", True, "nao-existe (não encontrado)") in form_select(unknown.text)
    assert ignored.status_code == 200 and "Marina Alves" in table_body(ignored.text)
    assert "com estes filtros" not in ignored.text and "Limpar filtros" not in ignored.text
    assert all(chosen is False for _, chosen, _ in form_select(ignored.text))


def test_job_list_pages_keep_the_form_and_a_filter_change_starts_over_on_page_1(
    client: TestClient, deps: Deps
) -> None:
    new_lead_job(deps, "Lead Demo", "demo@example.test")
    for n in range(26):
        new_mcc_job(deps, f"Lead {n:02d}", f"lead{n}@example.test")
    login(client)

    first = client.get("/admin", params={"form": "mcc"}).text
    second = client.get("/admin", params={"form": "mcc", "page": "2"}).text

    assert "26 pedidos com estes filtros" in first and "Página 1 de 2" in first
    assert "Lead Demo" not in first and "Página anterior" not in first
    assert 'href="/admin?form=mcc&amp;page=2"' in first
    assert "Lead 00" in table_body(second) and "Lead 25" not in table_body(second)
    assert "Página 2 de 2" in second and 'href="/admin?form=mcc"' in second  # volta para a 1
    assert "Próxima página" not in second
    # O formulário não manda `page`: trocar o select recomeça na primeira página.
    assert 'name="page"' not in first


def test_job_list_asked_by_htmx_pushes_only_the_filters_that_filter(
    client: TestClient, deps: Deps
) -> None:
    new_mcc_job(deps, "Fernanda Rocha", "fernanda@rocha.example")
    login(client)
    empty_fields = {"q": "", "form": "mcc", "status": ""}  # o que o formulário manda

    htmx = client.get("/admin", params=empty_fields, headers={"HX-Request": "true"})
    plain = client.get("/admin", params=empty_fields)
    everything = client.get(
        "/admin", params={"q": "", "form": "", "status": ""}, headers={"HX-Request": "true"}
    )

    assert htmx.headers["HX-Push-Url"] == "/admin?form=mcc"
    assert "HX-Push-Url" not in plain.headers
    assert everything.headers["HX-Push-Url"] == "/admin"  # "Todos" não escreve o parâmetro


def codes_on_the_page(html: str) -> list[str]:
    """Os códigos dos pedidos que a tabela mostra, do primeiro ao último."""
    return re.findall(r'<span class="mono">([^<]+)</span>', table_body(html))


def test_job_list_filters_by_arrival_in_whole_calendar_days_of_brasilia(
    client: TestClient, deps: Deps, clock: FakeClock
) -> None:
    made = jobs_by_arrival(deps)
    clock.now = PERIOD_NOW.timestamp()
    login(client)

    for period, names in PERIOD_EXPECTED.items():
        html = client.get("/admin", params={"period": period} if period else {}).text
        shown = codes_on_the_page(html)
        assert set(shown) == {made[name].public_id for name in names}, period
        # Os mais novos primeiro, e o total e a frase acompanham o filtro.
        assert shown == [
            made[name].public_id for name in sorted(names, key=lambda n: ARRIVALS[n], reverse=True)
        ]
        assert f'role="status">{len(names)} pedidos' in html, period
        assert ("com estes filtros" in html) is bool(period), period
        assert ('href="/admin">Limpar filtros</a>' in html) is bool(period), period

    # A fronteira do fuso: 22:00 em Brasília já é o dia seguinte em UTC, e ainda é "hoje".
    today = codes_on_the_page(client.get("/admin", params={"period": "today"}).text)
    assert made["tonight"].public_id in today
    assert made["yesterday_late"].public_id not in today  # 23:59 de ontem
    week = codes_on_the_page(client.get("/admin", params={"period": "7d"}).text)
    assert made["yesterday_late"].public_id in week


def test_job_list_offers_the_arrival_periods_and_marks_the_chosen_one(
    client: TestClient, deps: Deps, clock: FakeClock
) -> None:
    clock.now = PERIOD_NOW.timestamp()
    login(client)

    plain = client.get("/admin").text
    seven = client.get("/admin", params={"period": "7d"}).text

    assert '<label for="f-period">Recebido</label>' in plain  # o rótulo da coluna, visível
    assert select_choices(plain, "f-period") == [
        ("", False, "Tudo"),  # sem `selected`: o navegador mostra a primeira
        ("today", False, "Hoje"),
        ("7d", False, "Últimos 7 dias"),
        ("30d", False, "Últimos 30 dias"),
    ]
    assert [
        (value, chosen) for value, chosen, _ in select_choices(seven, "f-period") if chosen
    ] == [("7d", True)]
    assert "change from:#f-period" in plain  # escolher dispara a busca, como o Status


def test_job_list_ignores_an_unknown_period_and_combines_the_period_with_the_other_filters(
    client: TestClient, deps: Deps, clock: FakeClock
) -> None:
    made = jobs_by_arrival(deps)
    mcc_tonight = received_at(
        deps,
        new_mcc_job(deps, "Fernanda Rocha", "fernanda@example.test"),
        datetime(2027, 1, 15, 22, 30, tzinfo=BRT),  # depois do `tonight`: sem empate na ordem
    )
    mcc_old = received_at(
        deps, new_mcc_job(deps, "Gabriela Dias", "gabriela@example.test"), ARRIVALS["old"]
    )
    clock.now = PERIOD_NOW.timestamp()
    login(client)

    def shown(**params: str) -> list[str]:
        return codes_on_the_page(client.get("/admin", params=params).text)

    ignored = client.get("/admin", params={"period": "1y"}).text
    assert len(codes_on_the_page(ignored)) == 10 and "com estes filtros" not in ignored
    assert all(chosen is False for _, chosen, _ in select_choices(ignored, "f-period")[1:])
    assert shown(period="30d", form="mcc") == [mcc_tonight.public_id]  # E com o formulário
    assert shown(form="mcc") == [mcc_tonight.public_id, mcc_old.public_id]
    assert shown(period="today", q="rocha") == [mcc_tonight.public_id]  # e com a busca
    assert shown(period="today", q="Lead old") == []
    assert shown(period="today", status="queued") == [
        mcc_tonight.public_id,
        made["tonight"].public_id,
        made["start_of_today"].public_id,
    ]
    assert shown(period="today", status="failed") == []
    empty = client.get("/admin", params={"period": "today", "status": "failed"}).text
    assert "Nenhum pedido encontrado com estes filtros." in empty


def test_job_list_pages_keep_the_period_and_htmx_pushes_it_to_the_address(
    client: TestClient, deps: Deps, clock: FakeClock
) -> None:
    for n in range(26):
        received_at(deps, new_lead_job(deps, f"Lead {n:02d}", f"lead{n}@example.test"), PERIOD_NOW)
    received_at(deps, new_lead_job(deps, "Lead velho", "velho@example.test"), ARRIVALS["old"])
    clock.now = PERIOD_NOW.timestamp()
    login(client)

    first = client.get("/admin", params={"period": "7d"}).text
    second = client.get("/admin", params={"period": "7d", "page": "2"}).text

    assert "26 pedidos com estes filtros" in first and "Página 1 de 2" in first
    assert "Lead velho" not in first + second
    assert 'href="/admin?period=7d&amp;page=2"' in first
    assert "Página 2 de 2" in second and 'href="/admin?period=7d"' in second  # volta para a 1
    assert 'name="page"' not in first  # trocar o select recomeça na primeira página
    # O formulário manda os campos vazios; o endereço guarda só o que filtra.
    fields = {"q": "", "form": "", "period": "7d", "status": ""}
    htmx = client.get("/admin", params=fields, headers={"HX-Request": "true"})
    assert htmx.headers["HX-Push-Url"] == "/admin?period=7d"
    everything = client.get(
        "/admin", params={**fields, "period": ""}, headers={"HX-Request": "true"}
    )
    assert everything.headers["HX-Push-Url"] == "/admin"  # "Tudo" não escreve o parâmetro


def test_job_list_filters_by_the_delivery_of_the_current_attempt(
    client: TestClient, deps: Deps
) -> None:
    made = jobs_by_delivery(deps)
    login(client)

    for delivery, names in DELIVERY_EXPECTED.items():
        html = client.get("/admin", params={"delivery": delivery} if delivery else {}).text
        shown = codes_on_the_page(html)
        # Os mais novos primeiro (a ordem de criação, ao contrário), e o total acompanha o filtro.
        assert shown == [
            made[name].public_id for name in reversed(DELIVERY_CASES) if name in names
        ], delivery
        assert f'role="status">{len(names)} pedido' in html, delivery
        assert ("com estes filtros" in html) is bool(delivery), delivery
        assert ('href="/admin">Limpar filtros</a>' in html) is bool(delivery), delivery

    # A linha filtrada mostra a entrega que casou: a mesma que a coluna "Entregas" sempre mostrou.
    failed = client.get("/admin", params={"delivery": "failed"}).text
    assert failed.count("Webhook falhou") == 2 and "Webhook entregue" not in failed
    pending = client.get("/admin", params={"delivery": "pending"}).text
    assert pending.count("Webhook pendente") == 1 and "Webhook falhou" not in pending
    # Falhou na tentativa 1 e foi entregue na 2: a coluna mostra "entregue", e o filtro não o acha.
    retried = made["retried_ok"].public_id
    assert retried not in failed and retried in client.get("/admin").text


def test_job_list_offers_the_delivery_choices_and_marks_the_chosen_one(
    client: TestClient, deps: Deps
) -> None:
    login(client)

    plain = client.get("/admin").text
    failed = client.get("/admin", params={"delivery": "failed"}).text

    assert '<label for="f-delivery">Entrega</label>' in plain  # o rótulo, visível
    assert select_choices(plain, "f-delivery") == [
        ("", False, "Todas"),  # sem `selected`: o navegador mostra a primeira
        ("failed", False, "Falhou"),
        ("pending", False, "Pendente"),
    ]
    assert [
        (value, chosen) for value, chosen, _ in select_choices(failed, "f-delivery") if chosen
    ] == [("failed", True)]
    assert "change from:#f-delivery" in plain  # escolher dispara a busca, como o Status


def test_job_list_ignores_an_unknown_delivery_and_combines_it_with_the_other_filters(
    client: TestClient, deps: Deps, clock: FakeClock
) -> None:
    made = jobs_by_delivery(deps)
    received_at(deps, made["regressed"], PERIOD_NOW - timedelta(minutes=5))
    clock.now = PERIOD_NOW.timestamp()
    login(client)

    def shown(**params: str) -> list[str]:
        return codes_on_the_page(client.get("/admin", params=params).text)

    ignored = client.get("/admin", params={"delivery": "succeeded"}).text
    assert len(codes_on_the_page(ignored)) == len(DELIVERY_CASES)
    assert "com estes filtros" not in ignored
    assert all(chosen is False for _, chosen, _ in select_choices(ignored, "f-delivery")[1:])
    regressed, failed_now = made["regressed"].public_id, made["failed_now"].public_id
    assert shown(delivery="failed", status="completed") == [regressed, failed_now]  # E com o status
    assert shown(delivery="failed", status="queued") == []
    assert shown(delivery="failed", form="demo") == [regressed, failed_now]  # e com o formulário
    assert shown(delivery="failed", form="mcc") == []
    assert shown(delivery="failed", q="failed_now") == [failed_now]  # e com a busca
    assert shown(delivery="failed", q=made["pending_now"].public_id) == []
    assert shown(delivery="failed", period="today") == [regressed]  # e com a chegada
    assert shown(delivery="failed", period="7d") == [regressed]  # `failed_now` chegou em outubro
    assert shown(delivery="failed") == [regressed, failed_now]


def test_job_list_says_when_no_delivery_failed_or_is_pending(
    client: TestClient, deps: Deps
) -> None:
    made = jobs_by_delivery(deps)
    login(client)

    nothing_failed = client.get("/admin", params={"delivery": "failed", "status": "queued"}).text
    assert "Nenhuma entrega falhou com estes filtros." in nothing_failed
    assert "Nenhum pedido encontrado" not in nothing_failed
    assert 'href="/admin">Limpar filtros</a>' in nothing_failed
    nothing_pending = client.get("/admin", params={"delivery": "pending", "q": "failed_now"}).text
    assert "Nenhuma entrega pendente com estes filtros." in nothing_pending
    # Sem entrega nesse filtro, a frase é a de sempre.
    other = client.get("/admin", params={"status": "queued", "q": "no_delivery"}).text
    assert made["no_delivery"].public_id in other
    unknown = client.get("/admin", params={"q": "ninguem-com-este-nome"}).text
    assert "Nenhum pedido encontrado com estes filtros." in unknown


def test_job_list_pages_keep_the_delivery_and_htmx_pushes_it_to_the_address(
    client: TestClient, deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(views, "PAGE_SIZE", 1)
    made = jobs_by_delivery(deps)
    login(client)

    first = client.get("/admin", params={"delivery": "failed"}).text
    second = client.get("/admin", params={"delivery": "failed", "page": "2"}).text

    assert codes_on_the_page(first) == [made["regressed"].public_id]
    assert codes_on_the_page(second) == [made["failed_now"].public_id]
    assert "2 pedidos com estes filtros" in first and "Página 1 de 2" in first
    assert 'href="/admin?delivery=failed&amp;page=2"' in first
    assert "Página 2 de 2" in second and 'href="/admin?delivery=failed"' in second  # volta para a 1
    assert 'name="page"' not in first  # trocar o select recomeça na primeira página
    # O formulário manda os campos vazios; o endereço guarda só o que filtra.
    fields = {"q": "", "form": "", "period": "", "status": "", "delivery": "failed"}
    htmx = client.get("/admin", params=fields, headers={"HX-Request": "true"})
    assert htmx.headers["HX-Push-Url"] == "/admin?delivery=failed"
    everything = client.get(
        "/admin", params={**fields, "delivery": ""}, headers={"HX-Request": "true"}
    )
    assert everything.headers["HX-Push-Url"] == "/admin"  # "Todas" não escreve o parâmetro


def test_job_detail_shows_what_happened_with_the_request(client: TestClient, deps: Deps) -> None:
    job = new_lead_job(deps, "Marina Alves", "marina@alves.example")
    failed_delivery(deps, job.id)
    login(client)

    page = client.get(f"/admin/jobs/{job.public_id}")

    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert "Marina Alves" in page.text and "marina@alves.example" in page.text
    events = timeline_events(page.text)
    steps = [
        "Pedido recebido",
        "Plano gerado",
        "PDF gerado",
        "Concluído",
        "Entrega ao webhook falhou",
    ]
    assert [events.index(step) for step in steps] == sorted(events.index(s) for s in steps)
    answers = section(page.text, "respostas")  # como a lead viu: etapas, perguntas e opções
    screen_order = [
        "Vamos começar por você",
        "Marina Alves",
        "marina@alves.example",
        "Escritório Exemplo",
        "Seu escritório hoje",
        "Quantas pessoas trabalham no escritório, contando com você?",
        "De 2 a 5 pessoas",
        "Atrair novos clientes",
        "Organizar a captação de clientes",
    ]
    assert [answers.index(v) for v in screen_order] == sorted(
        answers.index(v) for v in screen_order
    )
    assert "2_a_5" not in answers
    plan = unescape(section(page.text, "plano"))
    plan_order = ['"titulo"', '"resumo"', '"acoes"', '"proximo_passo"']
    assert [plan.index(v) for v in plan_order] == sorted(plan.index(v) for v in plan_order)
    scores = section(page.text, "pontuacao")
    assert "Uso interno" in scores and "P2" in scores and "50" in scores
    consents = section(page.text, "consentimentos")
    assert "demo-v1" in consents and "Não autorizado" in consents
    deliveries = section(page.text, "entregas")
    assert "Falhou" in deliveries and "delivery_unavailable" in deliveries
    assert f'href="/admin/jobs/{job.public_id}/pdf"' in page.text


def test_job_detail_shows_the_answers_as_the_version_the_lead_answered(
    client: TestClient, deps: Deps, newer_demo: FormSpec
) -> None:
    before = new_lead_job(deps, "Marina Alves", "marina@example.test")  # versão 1
    publish(deps, newer_demo)  # troca o rótulo da opção "2_a_5"
    after = new_lead_job(deps, "Paula Nogueira", "paula@example.test")  # versão 2
    login(client)

    first = section(client.get(f"/admin/jobs/{before.public_id}").text, "respostas")
    second = section(client.get(f"/admin/jobs/{after.public_id}").text, "respostas")
    assert "De 2 a 5 pessoas" in first and "Entre 2 e 5 pessoas" not in first
    assert "Entre 2 e 5 pessoas" in second


def test_job_detail_names_the_plan_fields_by_their_label_and_falls_back_to_the_field_name(
    client: TestClient, deps: Deps
) -> None:
    before = new_lead_job(deps, "Marina Alves", "marina@example.test")  # versão 1, sem rótulos
    document = legacy_definition("demo")
    document["plan"]["fields"][3]["label"] = "Próximo passo"
    document["plan"]["fields"].append(
        {
            "name": "etapa",
            "kind": "group",
            "label": "Primeira etapa",
            "fields": [
                {"name": "foco", "kind": "text", "label": "Foco da etapa", "max_length": 80},
                {"name": "prazo_final", "kind": "text", "max_length": 80},
            ],
        }
    )
    document["examples"]["plan"]["etapa"] = {"foco": "Captação", "prazo_final": "31/12"}
    publish(deps, compile_definition(document, version_number=2))
    after = new_lead_job(deps, "Paula Nogueira", "paula@example.test")  # versão 2, com rótulos
    finish(deps, before.id)
    finish(deps, after.id)
    login(client)

    old = section(client.get(f"/admin/jobs/{before.public_id}").text, "plano")
    # sem rótulo, o nome do campo, como antes
    assert "<dt>Proximo passo</dt>" in old and "<dt>Resumo</dt>" in old

    new = section(client.get(f"/admin/jobs/{after.public_id}").text, "plano")
    assert "<dt>Próximo passo</dt>" in new and "Proximo passo" not in new
    assert "<dt>Resumo</dt>" in new  # sem rótulo: o nome do campo
    assert "<dt>Primeira etapa</dt>" in new
    assert "<dt>Foco da etapa</dt>" in new  # o rótulo de um campo dentro do grupo
    assert "<dt>Prazo final</dt>" in new  # e o nome dele quando não tem
    assert "proximo_passo" in new  # o JSON validado segue com o nome do campo, não com o rótulo


def reprocess(client: TestClient, public_id: str, token: str, **form: str) -> httpx.Response:
    fields = {
        "csrf_token": token,
        "attempt": "1",
        "reason": "IA estava fora do ar",
        "confirm": "on",
    }
    return client.post(
        f"/admin/jobs/{public_id}/reprocess", data={**fields, **form}, headers=ORIGIN
    )


def test_reprocess_needs_a_reason_and_a_confirmation(
    client: TestClient, deps: Deps, triggered: list[tuple[uuid.UUID, int]]
) -> None:
    job = new_lead_job(deps, "Marina Alves", "marina@alves.example")
    finish(deps, job.id)
    login(client)
    token = csrf_token(client)

    no_reason = reprocess(client, job.public_id, token, reason="  curto ")
    no_confirmation = reprocess(client, job.public_id, token, confirm="")

    assert no_reason.status_code == 422 and "pelo menos 10 caracteres" in no_reason.text
    assert no_confirmation.status_code == 422 and "Confirme" in no_confirmation.text
    assert "IA estava fora do ar" in no_confirmation.text  # o motivo digitado não se perde
    assert triggered == []
    assert "Tentativa 2" not in client.get(f"/admin/jobs/{job.public_id}").text


def test_reprocess_creates_a_new_attempt_audited_in_the_operator_name_and_triggers_once(
    client: TestClient, deps: Deps, triggered: list[tuple[uuid.UUID, int]]
) -> None:
    accepted = submit(deps)
    job = accepted.job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "run-1")
        jobs.fail(session, job.id, 1, "plan_invalid", "plan falhou")
    login(client)
    token = csrf_token(client)

    response = reprocess(client, job.public_id, token, reason="Prompt corrigido pela equipe")

    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/jobs/{job.public_id}"
    assert triggered == [(job.id, 2)]
    page = client.get(f"/admin/jobs/{job.public_id}").text
    assert "Tentativa 2 enfileirada em seu nome." in page
    events = timeline_events(page)
    assert "Falhou: plano inválido" in events  # o fim da tentativa 1 fica no histórico
    assert "Reprocessamento pedido por operadora" in events
    assert "Prompt corrigido pela equipe" in events
    status = client.get(f"/v1/jobs/{job.public_id}", params={"t": accepted.token}).json()
    assert status["status"] == "queued"

    double_click = reprocess(client, job.public_id, token)  # mesmo formulário, tentativa 1
    assert double_click.status_code == 303 and triggered == [(job.id, 2)]
    assert "não pode ser reprocessado" in client.get(f"/admin/jobs/{job.public_id}").text


def test_reprocess_keeps_the_form_version_the_lead_answered(
    client: TestClient,
    deps: Deps,
    triggered: list[tuple[uuid.UUID, int]],
    newer_demo: FormSpec,
) -> None:
    job = new_lead_job(deps, "Marina Alves", "marina@example.test")  # versão 1
    finish(deps, job.id)
    publish(deps, newer_demo)
    login(client)

    reprocess(client, job.public_id, csrf_token(client), reason="Plano revisado pela equipe")
    finish(deps, job.id, attempt=2)  # o worker roda a tentativa que o admin disparou

    assert triggered == [(job.id, 2)]
    plan = section(client.get(f"/admin/jobs/{job.public_id}").text, "plano")
    assert "tentativa 2 · modelo fake · prompt demo-v1" in plan


def test_reprocess_is_refused_while_the_job_is_queued_or_without_csrf(
    client: TestClient, deps: Deps, triggered: list[tuple[uuid.UUID, int]]
) -> None:
    job = new_lead_job(deps, "Marina Alves", "marina@alves.example")
    login(client)
    token = csrf_token(client)
    actions = section(client.get(f"/admin/jobs/{job.public_id}").text, "acoes")
    assert "/reprocess" not in actions  # na fila: sem formulário

    reprocess(client, job.public_id, token)
    with deps.sessions.begin() as session:
        jobs.fail(session, job.id, 1, "internal", "falhou")
    forged = reprocess(client, job.public_id, "token-errado")

    assert forged.status_code == 403
    assert triggered == []
    assert "Tentativa 2" not in client.get(f"/admin/jobs/{job.public_id}").text


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """Troca o disparo do workflow `deliver` no Hatchet (a fronteira) por um registro."""
    calls: list[uuid.UUID] = []

    def fake_launch(delivery_id: uuid.UUID) -> str | None:
        calls.append(delivery_id)
        return "run-entrega"

    monkeypatch.setattr(workflows, "launch_delivery", fake_launch)
    return calls


def resend(client: TestClient, public_id: str, delivery_id: object, token: str) -> httpx.Response:
    return client.post(
        f"/admin/jobs/{public_id}/deliveries/{delivery_id}/resend",
        data={"csrf_token": token, "expected_config_version": 1, "expected_attempt": 1},
        headers=ORIGIN,
    )


def test_resend_returns_a_failed_delivery_to_pending_and_launches_it(
    client: TestClient, deps: Deps, launched: list[uuid.UUID]
) -> None:
    job = new_lead_job(deps, "Marina Alves", "marina@alves.example")
    delivery_id = failed_delivery(deps, job.id)
    login(client)
    token = csrf_token(client)
    before = section(client.get(f"/admin/jobs/{job.public_id}").text, "entregas")
    assert f"/deliveries/{delivery_id}/resend" in before

    response = resend(client, job.public_id, delivery_id, token)

    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/jobs/{job.public_id}"
    assert launched == [delivery_id]
    page = client.get(f"/admin/jobs/{job.public_id}").text
    assert "Reenvio disparado." in page
    deliveries = section(page, "entregas")
    assert "Pendente" in deliveries and "/resend" not in deliveries
    assert "Reenvio pedido por operadora" in timeline_events(page)
    with deps.sessions.begin() as session:  # sem run id: o reconciliador também a enxerga
        unlaunched = jobs.unlaunched_deliveries(session, older_than=timedelta(0), limit=10)
    assert [delivery.id for delivery in unlaunched] == [delivery_id]

    again = resend(client, job.public_id, delivery_id, token)  # já não está falha
    assert again.status_code == 303 and launched == [delivery_id]
    assert "A entrega precisa ter falhado" in client.get(again.headers["location"]).text


def test_resend_does_not_hold_the_operator_when_the_engine_does_not_answer(
    deps: Deps, clock: FakeClock, admin_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    deps.settings = Settings(  # type: ignore[call-arg]
        _env_file=None, HATCHET_TRIGGER_TIMEOUT_SECONDS="0.2", **admin_env
    )
    released = threading.Event()

    def stuck_launch(delivery_id: uuid.UUID) -> str | None:
        released.wait(timeout=10)  # o SDK insiste por dezenas de segundos sem engine
        return None

    monkeypatch.setattr(workflows, "launch_delivery", stuck_launch)
    job = new_lead_job(deps, "Marina Alves", "marina@alves.example")
    delivery_id = failed_delivery(deps, job.id)
    client = TestClient(create_app(trigger=None, clock=clock), follow_redirects=False)
    login(client)
    token = csrf_token(client)

    started = time.monotonic()
    response = resend(client, job.public_id, delivery_id, token)
    elapsed = time.monotonic() - started
    released.set()

    assert response.status_code == 303 and elapsed < 5
    page = client.get(response.headers["location"]).text
    assert "A entrega voltou para a fila" in page


def test_resend_is_refused_without_csrf_or_for_another_job(
    client: TestClient, deps: Deps, launched: list[uuid.UUID]
) -> None:
    job = new_lead_job(deps, "Marina Alves", "marina@alves.example")
    other = new_lead_job(deps, "Paula Nogueira", "contato@pn.example")
    delivery_id = failed_delivery(deps, job.id)
    login(client)
    token = csrf_token(client)

    assert resend(client, job.public_id, delivery_id, "token-errado").status_code == 403
    assert resend(client, other.public_id, delivery_id, token).status_code == 303
    assert resend(client, job.public_id, "nao-e-uuid", token).status_code == 404
    assert launched == []
    assert "Falhou" in section(client.get(f"/admin/jobs/{job.public_id}").text, "entregas")


def test_unknown_job_is_not_found(client: TestClient) -> None:
    login(client)
    response = client.get("/admin/jobs/nao-existe")
    assert response.status_code == 404 and "Pedido não encontrado" in response.text


def test_admin_downloads_the_pdf_of_each_attempt(client: TestClient, deps: Deps) -> None:
    job = new_lead_job(deps, "Marina Alves", "marina@alves.example")
    login(client)
    assert client.get(f"/admin/jobs/{job.public_id}/pdf").status_code == 404  # ainda sem PDF
    finish(deps, job.id)

    pdf = client.get(f"/admin/jobs/{job.public_id}/pdf")

    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")
    assert f'filename="plano-{job.public_id}-v1.pdf"' in pdf.headers["content-disposition"]
    assert pdf.headers["cache-control"] == "no-store"
    assert (
        client.get(f"/admin/jobs/{job.public_id}/pdf", params={"version": "2"}).status_code == 404
    )


def test_a_pdf_removed_by_the_retention_is_listed_without_a_download(
    client: TestClient, deps: Deps
) -> None:
    job = new_lead_job(deps, "Marina Alves", "marina@alves.example")
    finish(deps, job.id)
    retention.purge_expired(deps, now=utcnow() + timedelta(days=31))  # só o PDF saiu
    login(client)

    page = client.get(f"/admin/jobs/{job.public_id}").text
    download = client.get(f"/admin/jobs/{job.public_id}/pdf")

    pdfs = section(page, "plano")
    assert "PDF da tentativa 1" in pdfs and "removido em" in pdfs
    assert f"/admin/jobs/{job.public_id}/pdf" not in page
    assert download.status_code == 410 and "prazo de retenção" in download.text


def test_hatchet_link_appears_only_when_configured(
    deps: Deps, clock: FakeClock, admin_env: dict[str, str]
) -> None:
    job = new_lead_job(deps, "Marina Alves", "marina@alves.example")
    finish(deps, job.id)  # grava o run id "run-1"
    pages = {}
    for template in (None, "https://cloud.onhatchet.run/runs/{run_id}"):
        extra = {"HATCHET_RUN_URL_TEMPLATE": template} if template else {}
        deps.settings = Settings(_env_file=None, **admin_env, **extra)  # type: ignore[call-arg]
        client = TestClient(create_app(trigger=None, clock=clock), follow_redirects=False)
        login(client)
        pages[template] = client.get(f"/admin/jobs/{job.public_id}").text
    assert "Ver execução no Hatchet" not in pages[None]
    assert 'href="https://cloud.onhatchet.run/runs/run-1"' in pages[template]


def test_admin_search_terms_stay_out_of_the_access_log(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    access = logging.getLogger("uvicorn.access")
    line = '%s - "%s %s HTTP/%s" %d'  # o formato do log de acesso do uvicorn
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        access.info(line, "127.0.0.1:5000", "GET", "/admin?q=marina%40alves.example", "1.1", 200)
        access.info(line, "127.0.0.1:5000", "GET", "/admin/jobs/abc", "1.1", 200)
    assert "marina" not in caplog.text
    assert "GET /admin?[removido]" in caplog.text and "GET /admin/jobs/abc" in caplog.text


def test_htmx_requests_without_a_session_get_a_full_page_redirect(client: TestClient) -> None:
    response = client.get("/admin", params={"q": "x"}, headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/admin/login"


def test_session_cookie_is_secure_when_the_app_is_served_over_https(
    deps: Deps, clock: FakeClock, admin_env: dict[str, str]
) -> None:
    deps.settings = Settings(  # type: ignore[call-arg]
        _env_file=None, PUBLIC_BASE_URL="https://planos.example", **admin_env
    )
    client = TestClient(create_app(trigger=None, clock=clock), follow_redirects=False)
    response = client.post(
        "/admin/login",
        data={"username": "operadora", "password": PASSWORD},
        headers={"Origin": "https://planos.example"},
    )
    assert response.status_code == 303
    assert "secure" in response.headers["set-cookie"].lower()


SECRET = "s" * 40


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"ADMIN_USERNAME": "operadora"}, "ADMIN_PASSWORD_HASH"),
        (
            {
                "ADMIN_USERNAME": "operadora",
                "ADMIN_PASSWORD_HASH": PASSWORD_HASH,
                "ADMIN_SESSION_SECRET": "curto",
            },
            "ADMIN_SESSION_SECRET",
        ),
        (
            {
                "ADMIN_USERNAME": "operadora",
                "ADMIN_PASSWORD_HASH": "senha-em-texto-puro",
                "ADMIN_SESSION_SECRET": SECRET,
            },
            "argon2",
        ),
        ({"HATCHET_RUN_URL_TEMPLATE": "https://cloud.onhatchet.run/runs/"}, "{run_id}"),
    ],
)
def test_admin_settings_fail_at_startup_with_a_clear_message(
    env: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        Settings(_env_file=None, **env).check()  # type: ignore[call-arg]


def test_complete_admin_settings_are_accepted(admin_env: dict[str, str]) -> None:
    template = {"HATCHET_RUN_URL_TEMPLATE": "https://cloud.onhatchet.run/runs/{run_id}"}
    Settings(_env_file=None, **admin_env, **template).check()  # type: ignore[call-arg]
