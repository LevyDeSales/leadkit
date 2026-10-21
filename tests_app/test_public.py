"""Jornada pública pelo navegador: formulário (/p/{slug}) -> espera e resultado (/d/{public_id})."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from copy import copy
from dataclasses import replace
from datetime import timedelta
from html.parser import HTMLParser
from unittest.mock import Mock

import h11
import httpx
import pytest
import sqlalchemy as sa
from conftest import (
    DEMO_ANSWERS,
    DEMO_CONSENT,
    NEWER_DEMO_INTRO,
    NEWER_DEMO_PREVIEW,
    legacy_definition,
    publish,
)
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import jobs, pipeline
from app.api.main import create_app
from app.deps import Deps
from app.forms import FormSpec
from app.forms.definition import compile_definition
from app.forms.sandbox import SandboxTemplate
from app.models import Job, Plan, Submission, utcnow
from app.web.public import hide_link_tokens_in_access_log


@pytest.mark.parametrize("path", ["/d/fictitious", "/v1/jobs/fictitious"])
@pytest.mark.parametrize(
    ("query", "tokens"),
    [
        (b"", []),
        (b"t=fictitious-first", ["fictitious-first"]),
        (b"%74=fictitious-first", ["fictitious-first"]),
        (b"t=fictitious-first&t=fictitious-second", ["fictitious-first", "fictitious-second"]),
        (
            b"x=extra&%74=fictitious-first&t=fictitious-second&y=last",
            ["fictitious-first", "fictitious-second"],
        ),
        (b"%74=fictitious%2Dfirst%26extra%3Dvalue", ["fictitious-first&extra=value"]),
        (b"t=fictitious%3Bfirst%3Fmore%23tail", ["fictitious;first?more#tail"]),
        (b"x=before%26t%3Ddecoy&%74=fictitious-first", ["fictitious-first"]),
        (b"x=extra&t=&%74=fictitious-first", ["", "fictitious-first"]),
        (b"x=%ZZ&%74=fictitious-first", ["fictitious-first"]),
    ],
)
def test_access_log_omits_query_without_changing_request(
    path: str, query: bytes, tokens: list[str], database_url: str, monkeypatch
) -> None:
    """O emissor real do Uvicorn mantém a credencial só no request, nunca no log final."""
    # O Alembic configura o logging na preparação do banco, antes de importar o Uvicorn.
    from uvicorn.logging import AccessFormatter
    from uvicorn.protocols.http.flow_control import FlowControl
    from uvicorn.protocols.http.h11_impl import RequestResponseCycle

    access_logger = logging.getLogger("uvicorn.access")
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "root_path": "",
        "query_string": query,
        "http_version": "1.1",
        "headers": [],
        "client": ("127.0.0.1", 12345),
    }
    request = Request(scope)
    assert request.query_params.getlist("t") == tokens
    original_scope = scope.copy()
    transport = Mock(spec=asyncio.Transport)

    async def respond() -> None:
        cycle = RequestResponseCycle(
            scope=scope,
            conn=h11.Connection(h11.SERVER),
            transport=transport,
            flow=FlowControl(transport),
            logger=logging.getLogger("uvicorn.error"),
            access_logger=access_logger,
            access_log=True,
            default_headers=[],
            message_event=asyncio.Event(),
            on_response=lambda: None,
        )
        await cycle.send({"type": "http.response.start", "status": 303, "headers": []})

    records: list[logging.LogRecord] = []
    formatted: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            # Handlers MCP podem zerar args no mesmo LogRecord; confira o Uvicorn aqui.
            records.append(copy(record))
            formatted.append(self.format(record))

    handler = Capture()
    handler.setFormatter(
        AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False)
    )
    settings = {
        "filters": [],
        "handlers": [handler],
        "propagate": False,
        "disabled": False,
        "level": logging.INFO,
        "_cache": {},  # isEnabledFor não reutiliza nem altera o cache da configuração anterior.
    }
    previous = {name: getattr(access_logger, name) for name in settings}
    try:
        with monkeypatch.context() as isolated:
            for name, value in settings.items():
                isolated.setattr(access_logger, name, value)
            hide_link_tokens_in_access_log()
            asyncio.run(respond())
    finally:
        handler.close()
        assert all(getattr(access_logger, name) is value for name, value in previous.items())
    (record,) = records
    (logged,) = formatted
    target = f"{path}?[removido]" if query else path
    assert record.getMessage() == f'127.0.0.1:12345 - "GET {target} HTTP/1.1" 303'
    assert logged == f'127.0.0.1:12345 - "GET {target} HTTP/1.1" 303 See Other'
    for token in tokens:
        if token:
            assert token not in record.getMessage() and token not in logged
    if query:
        assert query.decode("ascii") not in logged
    assert scope == original_scope
    assert Request(scope).query_params.getlist("t") == tokens


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data: str) -> None:
        self.chunks.append(data)


def _parse(html: str) -> _Page:
    page = _Page()
    page.feed(html)
    return page


def tags(html: str, tag: str, **attrs: str) -> list[dict[str, str | None]]:
    """Elementos `tag` com os atributos pedidos (`class_` vira `class`)."""
    wanted = {key.rstrip("_"): value for key, value in attrs.items()}
    return [
        found
        for name, found in _parse(html).elements
        if name == tag and all(found.get(key) == value for key, value in wanted.items())
    ]


def text(html: str) -> str:
    return re.sub(r"\s+", " ", " ".join(_parse(html).chunks))


class _Form(HTMLParser):
    """Os campos de um formulário como o navegador os manda: os textos, os campos ocultos, as
    caixas e os rádios marcados, a opção escolhida de cada lista e o texto de cada textarea, sem os
    botões nem os campos desligados."""

    SKIPPED = frozenset({"submit", "button", "image", "reset", "file"})

    def __init__(self, form_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.form_id = form_id
        self.inside = False
        self.pairs: list[tuple[str, str]] = []
        self.textarea: list[str] | None = None
        self.textarea_name = ""
        self.select_name: str | None = None
        self.options: list[tuple[str, bool]] = []
        self.option: dict[str, str | None] | None = None
        self.option_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        if tag == "form":
            self.inside = found.get("id") == self.form_id
        if not self.inside or "disabled" in found:
            return
        name = found.get("name")
        if tag == "input" and name and found.get("type", "text") not in self.SKIPPED:
            if found.get("type") in ("checkbox", "radio") and "checked" not in found:
                return
            default = "on" if found.get("type") in ("checkbox", "radio") else ""
            self.pairs.append((name, found.get("value") or default))
        elif tag == "textarea" and name:
            self.textarea, self.textarea_name = [], name
        elif tag == "select" and name:
            self.select_name, self.options = name, []
        elif tag == "option" and self.select_name is not None:
            self.option, self.option_text = found, []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self.textarea is not None:
            self.textarea.append(data)
        elif self.option is not None:
            self.option_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.inside = False
        elif tag == "textarea" and self.textarea is not None:
            # A quebra de linha logo depois da abertura da tag não faz parte do texto (HTML).
            self.pairs.append((self.textarea_name, "".join(self.textarea).removeprefix("\n")))
            self.textarea = None
        elif tag == "option" and self.option is not None:
            value = self.option.get("value")
            chosen = "selected" in self.option
            self.options.append((value if value is not None else "".join(self.option_text), chosen))
            self.option = None
        elif tag == "select" and self.select_name is not None:
            chosen = [value for value, selected in self.options if selected]
            if not chosen and self.options:
                chosen = [self.options[0][0]]  # sem `selected`, vale a primeira opção
            self.pairs += [(self.select_name, value) for value in chosen]
            self.select_name = None


def form_fields(html: str, form_id: str) -> dict[str, str | list[str]]:
    """Os campos do formulário com este `id`, pelo nome, como o navegador os mandaria sem mudança
    nenhuma: um nome repetido (caixas marcadas) vira uma lista."""
    parser = _Form(form_id)
    parser.feed(html)
    found: dict[str, str | list[str]] = {}
    for name, value in parser.pairs:
        before = found.get(name)
        if before is None:
            found[name] = value
        else:
            found[name] = [*before, value] if isinstance(before, list) else [before, value]
    return found


def assert_private(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "noindex" in response.headers["x-robots-tag"]
    assert "script-src 'self'" in response.headers["content-security-policy"]


@pytest.fixture
def triggered() -> list[tuple[uuid.UUID, int]]:
    return []


@pytest.fixture
def client(deps: Deps, triggered: list[tuple[uuid.UUID, int]]) -> TestClient:
    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        triggered.append((job_id, attempt))
        return "run-fake"

    return TestClient(create_app(trigger=fake_trigger))


def test_form_page_shows_every_question_and_option_of_the_demo(client: TestClient) -> None:
    response = client.get("/p/demo")

    assert response.status_code == 200
    page = text(response.text)
    for label in (
        "Nome completo",
        "E-mail",
        "Nome do escritório (opcional)",
        "Quantas pessoas trabalham no escritório, contando com você?",
        "Só eu",
        "Mais de 15 pessoas",
        "Qual é o seu principal desafio hoje?",
        "Atrair novos clientes",
        "O que você quer alcançar nos próximos 90 dias?",
    ):
        assert label in page
    assert_private(response)


def test_processing_consent_is_required_and_apart_from_optional_marketing(
    client: TestClient,
) -> None:
    html = client.get("/p/demo").text

    (privacy,) = tags(html, "input", name="_privacy")
    (marketing,) = tags(html, "input", name="_mkt_email")
    assert "required" in privacy and "checked" not in privacy
    assert "required" not in marketing and "checked" not in marketing
    assert not tags(html, "input", name="_mkt_whatsapp")  # o demo não pede telefone
    assert tags(html, "input", name="_consent_version")[0]["value"] == "demo-v1"


def test_without_javascript_the_form_is_one_page_with_every_question(client: TestClient) -> None:
    html = client.get("/p/demo").text

    (form,) = tags(html, "form")
    assert form["method"] == "post" and form["action"] == "/p/demo"
    names = {field["name"] for field in tags(html, "input") + tags(html, "textarea")}
    assert {"nome", "email", "escritorio", "tamanho_equipe", "objetivo_90_dias"} <= names
    (submit,) = tags(html, "button", type="submit")
    assert "hidden" not in submit
    step_buttons = tags(html, "button", type="button")
    assert all("hidden" in button for button in step_buttons if "data-clear" not in button)


def test_contact_answers_never_go_to_the_device_draft(client: TestClient) -> None:
    html = client.get("/p/demo").text

    drafted = {f["name"] for f in tags(html, "input") + tags(html, "textarea") if "data-draft" in f}
    assert drafted == {"tamanho_equipe", "principal_desafio", "objetivo_90_dias"}


def test_ad_tracking_parameters_are_carried_into_the_form(client: TestClient) -> None:
    html = client.get("/p/demo", params={"utm_source": "instagram", "outro": "x"}).text

    assert tags(html, "input", name="_t_utm_source")[0]["value"] == "instagram"
    assert not tags(html, "input", name="_t_outro")


def test_form_page_shows_the_current_version_of_the_form(
    client: TestClient, deps: Deps, newer_demo: FormSpec
) -> None:
    publish(deps, newer_demo)

    page = text(client.get("/p/demo").text)
    assert NEWER_DEMO_INTRO in page and "Entre 2 e 5 pessoas" in page


def test_unknown_form_is_a_private_not_found_page(client: TestClient) -> None:
    response = client.get("/p/inexistente")

    assert response.status_code == 404
    assert "Não encontramos esta página" in text(response.text)
    assert_private(response)


VALID_FORM = {
    "nome": "Contadora Exemplo",
    "email": " Contadora@Example.test ",
    "escritorio": "Escritório Exemplo",
    "tamanho_equipe": "2_a_5",
    "principal_desafio": "captacao",
    "objetivo_90_dias": "Organizar a captação de clientes e medir a conversão.",
    "_privacy": "1",
    "_consent_version": "demo-v1",
    "_idem": "chave-da-renderizacao",
}


def submit(client: TestClient, **changes: str | None) -> httpx.Response:
    data = {key: value for key, value in {**VALID_FORM, **changes}.items() if value is not None}
    return client.post("/p/demo", data=data, follow_redirects=False)


def test_valid_submission_redirects_to_a_clean_job_url_with_an_access_cookie(
    client: TestClient, triggered: list
) -> None:
    response = submit(client)

    assert response.status_code == 303
    location = response.headers["location"]
    assert re.fullmatch(r"/d/[A-Za-z0-9_-]+", location)  # nenhum token na URL
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert f"Path={location}" in cookie and "Secure" not in cookie
    assert len(triggered) == 1
    assert_private(response)


def test_double_submit_or_refresh_does_not_create_a_second_request(
    client: TestClient, triggered: list
) -> None:
    first = submit(client)
    again = submit(client)

    assert again.status_code == 303
    assert again.headers["location"] == first.headers["location"]
    assert len(triggered) == 1


def test_access_cookie_is_secure_over_https(client: TestClient) -> None:
    https = TestClient(client.app, base_url="https://testserver")

    assert "Secure" in submit(https).headers["set-cookie"]


def test_invalid_answers_come_back_with_field_errors_without_echoing_them(
    client: TestClient, triggered: list
) -> None:
    response = submit(
        client, email="segredo-nao-ecoar", objetivo_90_dias="xyz12", principal_desafio=None
    )

    assert response.status_code == 422
    html = response.text
    assert "segredo-nao-ecoar" not in html and "xyz12" not in html
    page = text(html)
    assert "Falta corrigir 3 itens para continuar" in page
    assert "Informe um e-mail válido, por exemplo nome@example.test" in page
    assert "Escreva pelo menos 10 caracteres." in page
    assert "Escolha uma opção para continuar." in page
    (email,) = tags(html, "input", name="email")
    assert email["aria-invalid"] == "true" and email["value"] == ""
    assert tags(html, "input", name="nome")[0]["value"] == "Contadora Exemplo"
    assert "checked" in tags(html, "input", name="tamanho_equipe", value="2_a_5")[0]
    assert triggered == []
    assert_private(response)


def test_nothing_is_sent_without_the_processing_consent(
    client: TestClient, triggered: list
) -> None:
    response = submit(client, _privacy=None)

    assert response.status_code == 422
    assert "Marque esta opção para gerarmos o seu plano." in text(response.text)
    assert tags(response.text, "input", name="_privacy")[0]["aria-invalid"] == "true"
    assert triggered == []
    assert submit(client).status_code == 303 and len(triggered) == 1  # a chave não foi gasta


def test_marketing_channel_the_form_does_not_ask_for_shows_up_at_the_consent(
    client: TestClient, triggered: list
) -> None:
    response = submit(client, _mkt_whatsapp="1")  # o demo não pede o canal WhatsApp

    assert response.status_code == 422
    assert "Este formulário não solicita autorização de marketing por este canal." in text(
        response.text
    )
    assert tags(response.text, "input", name="_privacy")[0]["aria-invalid"] == "true"
    assert triggered == []
    assert submit(client, _mkt_email="1").status_code == 303 and len(triggered) == 1


def test_honeypot_submission_is_refused_without_creating_a_request(
    client: TestClient, triggered: list
) -> None:
    response = submit(client, _website="https://spam.example")

    assert response.status_code == 400
    assert tags(response.text, "input", name="_website")[0].get("value") is None
    assert triggered == []
    assert submit(client).status_code == 303 and len(triggered) == 1


def test_outdated_consent_text_asks_to_agree_again(client: TestClient, triggered: list) -> None:
    response = submit(client, _consent_version="demo-v0")

    assert response.status_code == 409
    assert "Atualizamos o texto sobre o uso dos seus dados" in text(response.text)
    assert "checked" not in tags(response.text, "input", name="_privacy")[0]
    assert tags(response.text, "input", name="_consent_version")[0]["value"] == "demo-v1"
    assert triggered == []


def test_same_form_sent_again_with_other_answers_needs_a_new_confirmation(
    client: TestClient, triggered: list
) -> None:
    submit(client)
    response = submit(client, principal_desafio="vendas")

    assert response.status_code == 409
    (key,) = tags(response.text, "input", name="_idem")
    assert key["value"] != VALID_FORM["_idem"]
    assert submit(client, principal_desafio="vendas", _idem=key["value"]).status_code == 303
    assert len(triggered) == 2


def count_rows(deps: Deps, model: type) -> int:
    with deps.sessions() as session:
        return session.scalar(sa.select(sa.func.count()).select_from(model)) or 0


def test_fourth_form_from_the_same_email_comes_back_with_a_friendly_message(
    client: TestClient, deps: Deps, triggered: list
) -> None:
    for number in range(3):
        assert submit(client, _idem=f"envio-{number}").status_code == 303

    response = submit(client, _idem="envio-3")

    assert response.status_code == 429
    assert 0 < int(response.headers["retry-after"]) <= 24 * 3600
    page = text(response.text)
    assert "Você já enviou este formulário 3 vezes nas últimas 24 horas" in page
    assert "o máximo por e-mail" in page
    assert "Tente de novo daqui a cerca de 24 horas" in page
    assert "Falta corrigir" not in page  # não é erro nas respostas
    assert tags(response.text, "input", name="nome")[0]["value"] == "Contadora Exemplo"
    assert tags(response.text, "input", name="_idem")[0]["value"] == "envio-3"
    assert len(triggered) == 3 and count_rows(deps, Job) == 3
    assert_private(response)


def test_the_message_follows_the_configured_window_in_the_singular(
    client: TestClient, deps: Deps
) -> None:
    deps.settings = deps.settings.model_copy(
        update={"submission_limit_per_email": 1, "submission_limit_window_hours": 1}
    )
    submit(client, _idem="envio-0")

    page = text(submit(client, _idem="envio-1").text)

    assert "Você já enviou este formulário 1 vez na última hora" in page
    assert "Tente de novo daqui a cerca de 1 hora" in page


def test_double_submit_at_the_limit_still_goes_to_the_same_request(
    client: TestClient, triggered: list
) -> None:
    first = submit(client, _idem="envio-0")
    submit(client, _idem="envio-1")
    submit(client, _idem="envio-2")

    again = submit(client, _idem="envio-0")  # o clique duplo da primeira

    assert again.status_code == 303 and again.headers["location"] == first.headers["location"]
    assert len(triggered) == 3
    assert submit(client, _idem="envio-3").status_code == 429


def test_form_asks_to_wait_while_the_queue_is_full(
    client: TestClient, deps: Deps, triggered: list
) -> None:
    deps.settings = deps.settings.model_copy(update={"submission_limit_queue": 1})
    first = submit(client, _idem="envio-0")

    response = submit(client, email="lead1@example.test", _idem="envio-1")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "300"
    page = text(response.text)
    assert "Estamos com muitos pedidos agora" in page
    assert "Suas respostas não foram enviadas" in page
    assert "Tente de novo daqui a cerca de 5 minutos" in page
    assert tags(response.text, "input", name="nome")[0]["value"] == "Contadora Exemplo"
    assert len(triggered) == 1 and count_rows(deps, Job) == 1
    assert_private(response)
    replay = submit(client, _idem="envio-0")  # quem já enviou segue para o pedido dela
    assert replay.status_code == 303 and replay.headers["location"] == first.headers["location"]


def enter(client: TestClient) -> str:
    """Envia o formulário e devolve o caminho da página do pedido (o cliente guarda o cookie)."""
    return submit(client).headers["location"]


def job_id_of(deps: Deps, page: str) -> uuid.UUID:
    with deps.sessions() as session:
        return session.scalar(sa.select(Job.id).where(Job.public_id == page.rsplit("/", 1)[1]))


def test_after_sending_the_lead_lands_on_the_waiting_page(client: TestClient) -> None:
    response = client.post("/p/demo", data=VALID_FORM)  # segue o 303 levando o cookie

    assert response.status_code == 200 and response.url.path.startswith("/d/")
    page = text(response.text)
    assert "Estamos montando seu plano, Contadora" in page
    for stage in ("Recebemos suas respostas", "Montando seu plano de ação", "Gerando o seu PDF"):
        assert stage in page
    (poll,) = tags(response.text, "div", id="progresso")
    assert poll["hx-get"].startswith(f"{response.url.path}/progresso")
    assert poll["hx-trigger"].startswith("every ")
    assert client.cookies["acesso"] not in response.text
    assert_private(response)


def test_waiting_page_follows_the_pipeline_stage(client: TestClient, deps: Deps) -> None:
    page = enter(client)
    job_id = job_id_of(deps, page)
    with deps.sessions.begin() as session:
        jobs.claim(session, job_id, 1, "run-fake")
        jobs.set_stage(session, job_id, 1, "generating")
    assert "Etapa 2 de 3: montando seu plano de ação." in text(client.get(page).text)

    with deps.sessions.begin() as session:
        jobs.set_stage(session, job_id, 1, "rendering")
    assert "Etapa 3 de 3: gerando o seu PDF." in text(client.get(page).text)


def test_job_page_without_valid_access_is_not_found(client: TestClient) -> None:
    page = enter(client)
    stranger = TestClient(client.app)

    response = stranger.get(page)
    assert response.status_code == 404
    assert "Não encontramos este plano" in text(response.text)
    assert_private(response)
    stranger.cookies.set("acesso", f"{page.rsplit('/', 1)[1]}.adulterado", path=page)
    assert stranger.get(page).status_code == 404
    assert stranger.get("/d/naoexiste").status_code == 404


def test_progress_fragment_keeps_polling_and_announces_only_stage_changes(
    client: TestClient, deps: Deps
) -> None:
    page = enter(client)

    same = client.get(f"{page}/progresso", params={"etapa": "queued"})
    assert same.status_code == 200 and "<html" not in same.text
    assert tags(same.text, "div", id="progresso")[0]["hx-trigger"] == "every 3s"
    assert not tags(same.text, "p", id="anuncio")
    assert_private(same)

    job_id = job_id_of(deps, page)
    with deps.sessions.begin() as session:
        jobs.claim(session, job_id, 1, "run-fake")
        jobs.set_stage(session, job_id, 1, "generating")
    moved = client.get(f"{page}/progresso", params={"etapa": "queued"})
    (announcement,) = tags(moved.text, "p", id="anuncio")
    assert announcement["hx-swap-oob"] == "innerHTML"
    assert "Etapa 2 de 3: montando seu plano de ação." in text(moved.text)


def test_progress_fragment_stops_polling_and_reloads_when_the_request_ends(
    client: TestClient, deps: Deps
) -> None:
    page = enter(client)
    job_id = job_id_of(deps, page)
    with deps.sessions.begin() as session:
        jobs.fail(session, job_id, 1, "llm_unavailable", "fora do ar")

    response = client.get(f"{page}/progresso")
    assert response.status_code == 286  # o htmx para o polling
    assert response.headers["hx-refresh"] == "true"
    assert TestClient(client.app).get(f"{page}/progresso").status_code == 286


def test_slow_request_is_told_in_plain_language_and_polls_less(
    client: TestClient, deps: Deps
) -> None:
    page = enter(client)
    with deps.sessions.begin() as session:
        session.execute(sa.update(Job).values(updated_at=utcnow() - timedelta(minutes=10)))

    html = client.get(page).text
    assert "Está demorando mais que o normal" in text(html)
    assert "nenhuma resposta foi perdida" in text(html)
    assert tags(html, "div", id="progresso")[0]["hx-trigger"] == "every 10s"


def test_failed_request_is_explained_without_technical_details(
    client: TestClient, deps: Deps
) -> None:
    page = enter(client)
    with deps.sessions.begin() as session:
        jobs.fail(session, job_id_of(deps, page), 1, "llm_unavailable", "OpenAI HTTP 503")

    response = client.get(page)
    assert response.status_code == 200
    assert "Não conseguimos montar seu plano agora" in text(response.text)
    assert "llm_unavailable" not in response.text and "OpenAI" not in response.text
    assert page.rsplit("/", 1)[1] in text(response.text)  # código de atendimento
    assert not tags(response.text, "div", id="progresso")


def test_expired_link_says_so_instead_of_not_found(client: TestClient, deps: Deps) -> None:
    page = enter(client)
    with deps.sessions.begin() as session:
        session.execute(sa.update(Job).values(expires_at=utcnow() - timedelta(days=1)))

    response = client.get(page)
    assert response.status_code == 410
    assert "Este link expirou" in text(response.text)
    assert "30 dias" in text(response.text)
    assert_private(response)


def complete(deps: Deps, page: str) -> None:
    job_id = job_id_of(deps, page)
    with deps.sessions.begin() as session:
        jobs.claim(session, job_id, 1, "run-fake")
    plan_id, _ = pipeline.generate_plan(deps, job_id, 1)
    artifact_id, _ = pipeline.render_pdf(deps, job_id, 1, plan_id)
    pipeline.finalize(deps, job_id, 1, artifact_id)


def test_result_page_previews_the_plan_and_offers_the_download(
    client: TestClient, deps: Deps
) -> None:
    page = enter(client)
    complete(deps, page)

    response = client.get(page)
    assert response.status_code == 200
    shown = text(response.text)
    assert "Seu plano está pronto, Contadora" in shown
    assert "Organizar atividades do projeto" in shown  # EXAMPLE_PLAN do demo
    assert "A participante combina uma atividade simples com a equipe e." in shown
    assert "não substitui consultoria" in shown
    assert len(tags(response.text, "li")) >= 3  # prévia em HTML de verdade, não escapada
    assert tags(response.text, "a", href=f"{page}/pdf")
    assert not tags(response.text, "div", id="progresso")
    assert client.cookies["acesso"] not in response.text
    assert_private(response)


def test_each_result_page_previews_the_plan_with_the_version_its_lead_answered(
    client: TestClient, deps: Deps, newer_demo: FormSpec
) -> None:
    before = enter(client)  # versão 1
    complete(deps, before)
    first = text(client.get(before).text)  # a prévia da versão 1 já fica em cache
    publish(deps, newer_demo)
    after = submit(client, _idem="outra-renderizacao").headers["location"]  # versão 2
    complete(deps, after)

    second = text(client.get(after).text)
    assert text(client.get(before).text) == first
    assert "Organizar atividades do projeto" in first
    assert NEWER_DEMO_PREVIEW not in first
    assert f"{NEWER_DEMO_PREVIEW}: Plano da versão 2 (até o fim do trimestre)" in second


def test_preview_never_outlives_the_form_contract_it_was_compiled_from(
    client: TestClient, deps: Deps, newer_demo: FormSpec
) -> None:
    publish(deps, newer_demo)
    page = enter(client)  # versão 2
    complete(deps, page)
    assert NEWER_DEMO_PREVIEW in text(client.get(page).text)  # compilada e em cache

    # Outro contrato com o mesmo slug e a mesma versão (um rascunho compilado antes de ser
    # publicado, outro registro): a página segue o contrato que o registro tem agora.
    revised = SandboxTemplate(
        "<p>Prévia revisada: {{ plan.titulo }}</p>", name="preview", html=True
    )
    publish(deps, replace(newer_demo, templates=replace(newer_demo.templates, preview=revised)))

    shown = text(client.get(page).text)
    assert "Prévia revisada: Plano da versão 2" in shown and NEWER_DEMO_PREVIEW not in shown


@pytest.mark.parametrize(
    "gone",
    [{"form_slug": "formulario-removido"}, {"form_version_number": 7}],
    ids=["slug", "versao"],
)
def test_result_page_with_unresolvable_historical_policy_protects_the_pdf(
    client: TestClient, deps: Deps, gone: dict[str, object]
) -> None:
    page = enter(client)
    complete(deps, page)
    with deps.sessions.begin() as session:  # o registro já não tem o formulário, ou a versão dele
        session.execute(sa.update(Submission).values(**gone))

    response = client.get(page)
    assert response.status_code == 200
    shown = text(response.text)
    assert "Seu plano aguarda liberação" in shown
    assert "Contadora" not in shown  # sem a versão exata, não presume o nome do cadastro
    assert "Organizar atividades do projeto" not in shown  # sem a prévia
    assert not tags(response.text, "a", href=f"{page}/pdf")
    assert_private(response)


def test_plan_text_from_the_llm_is_escaped_in_the_preview(client: TestClient, deps: Deps) -> None:
    page = enter(client)
    complete(deps, page)
    hostile = {**deps.forms.current("demo").example_plan, "titulo": "<script>alert(1)</script>"}
    with deps.sessions.begin() as session:
        session.execute(sa.update(Plan).values(document=hostile))

    html = client.get(page).text
    assert "<script>alert" not in html and "&lt;script&gt;alert(1)" in html


def api_submission(client: TestClient) -> dict:
    body = {"answers": DEMO_ANSWERS, "consent": DEMO_CONSENT}
    return client.post("/v1/forms/demo/submissions", json=body).json()


def test_link_with_token_is_exchanged_for_a_cookie_and_a_clean_url(client: TestClient) -> None:
    submitted = api_submission(client)
    link = submitted["page_url"]  # o link que vai para o lead (webhook -> e-mail/WhatsApp)
    assert link.startswith("http://testserver/d/") and "?t=" in link

    phone = TestClient(client.app)
    response = phone.get(link, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/d/{submitted['public_id']}"
    assert "HttpOnly" in response.headers["set-cookie"]
    assert_private(response)
    assert "Estamos montando seu plano" in text(phone.get(response.headers["location"]).text)


def test_link_with_a_wrong_token_grants_nothing(client: TestClient) -> None:
    public_id = api_submission(client)["public_id"]
    phone = TestClient(client.app)

    response = phone.get(
        f"/d/{public_id}", params={"t": f"{public_id}.errado"}, follow_redirects=False
    )
    assert response.status_code == 303 and "set-cookie" not in response.headers
    assert phone.get(f"/d/{public_id}").status_code == 404


def test_expired_link_from_an_old_message_says_it_expired(client: TestClient, deps: Deps) -> None:
    link = api_submission(client)["page_url"]
    with deps.sessions.begin() as session:
        session.execute(sa.update(Job).values(expires_at=utcnow() - timedelta(days=1)))

    assert TestClient(client.app).get(link).status_code == 410


def test_download_uses_the_access_cookie_and_sends_the_pdf_as_attachment(
    client: TestClient, deps: Deps
) -> None:
    page = enter(client)
    early = client.get(f"{page}/pdf", follow_redirects=False)
    assert early.status_code == 303 and early.headers["location"] == page  # ainda não está pronto

    complete(deps, page)
    pdf = client.get(f"{page}/pdf")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")
    assert pdf.headers["content-disposition"].startswith("attachment")
    assert pdf.headers["cache-control"] == "no-store"
    assert TestClient(client.app).get(f"{page}/pdf").status_code == 404


def test_a_label_that_already_says_optional_does_not_get_the_mark_twice(
    client: TestClient, deps: Deps
) -> None:
    document = legacy_definition("demo")
    for step in document["steps"]:
        for question in step["questions"]:
            if question["name"] == "escritorio":
                question["label"] = "Nome do projeto (Opcional)"
    publish(deps, compile_definition(document, version_number=2))
    page = text(client.get("/p/demo").text)
    assert page.count("Nome do projeto (Opcional)") == 1
    assert "(Opcional) (opcional)" not in page
