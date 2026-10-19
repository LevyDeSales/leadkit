"""Os prazos de retenção de cada formulário: a retenção diária que os usa (`app.retention`), pelo
Postgres de teste e o storage local com o relógio injetado, e a página do admin onde a operadora os
define, pela costura HTTP (TestClient)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from test_admin import ORIGIN, FakeClock, csrf_token, finish, login, section
from test_admin_destinations import ADMIN_ENV
from test_admin_forms import CONSENT_VERSION, DEFINITION, SLUG
from test_intake import submit
from test_public import form_fields, tags, text
from test_retention import days_later, pdf_keys

from app import retention
from app import settings as settings_module
from app.api.main import create_app
from app.deps import Deps
from app.forms import retention as stored_retention
from app.forms import stored
from app.models import Artifact, AuditLog, Form, FormDraft, FormVersion, Job, Submission, utcnow
from app.settings import Settings

# --- os prazos de cada formulário ---------------------------------------------------------------


def set_periods(
    deps: Deps, slug: str, *, pdf: int | None = None, request: int | None = None
) -> None:
    """Os prazos próprios do formulário, como o admin os guarda (None: o padrão do ambiente)."""
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(Form)
            .where(Form.slug == slug)
            .values(retention_pdf_days=pdf, retention_days=request)
        )


def order_form(deps: Deps) -> None:
    """O segundo formulário: a definição do demo importada com outro endereço."""
    if SLUG not in [form.slug for form in deps.forms.current_forms()]:
        stored.import_definition(deps, DEFINITION, actor="testes")


def order(deps: Deps, slug: str, key: str, *, delivered: bool = False) -> uuid.UUID:
    """Um pedido do demo ou do segundo formulário (o demo importado com outro endereço); com
    `delivered`, ele vai até o PDF no storage."""
    if slug == SLUG:
        order_form(deps)
        accepted = submit(
            deps,
            key=key,
            form=deps.forms.current(SLUG),
            consent={"privacy": True, "text_version": CONSENT_VERSION},
        )
    else:
        accepted = submit(deps, key=key)
    if delivered:
        finish(deps, accepted.job.id)
    return accepted.job.id


def jobs_left(deps: Deps) -> list[str]:
    """O endereço do formulário de cada pedido que ainda está no banco, em ordem."""
    with deps.sessions() as session:
        return sorted(session.scalars(sa.select(Submission.form_slug).join(Job)))


def test_a_form_with_its_own_request_period_is_purged_on_that_period_not_on_the_default(
    deps: Deps,
) -> None:
    order(deps, "demo", "pedido-do-demo")
    order(deps, SLUG, "pedido-do-outro")
    set_periods(deps, SLUG, request=90)

    retention.purge_expired(deps, now=days_later(70))  # o padrão (60) já venceu; os 90, não

    assert jobs_left(deps) == [SLUG]
    assert retention.purge_expired(deps, now=days_later(89)).requests_deleted == 0
    assert retention.purge_expired(deps, now=days_later(91)).requests_deleted == 1
    assert jobs_left(deps) == []


def test_a_form_with_a_shorter_request_period_is_purged_before_the_default(deps: Deps) -> None:
    order(deps, "demo", "pedido-do-demo")
    order(deps, SLUG, "pedido-do-outro")
    set_periods(deps, SLUG, request=40)

    report = retention.purge_expired(deps, now=days_later(45))

    assert report.requests_deleted == 1 and jobs_left(deps) == ["demo"]


def test_a_period_set_after_the_requests_exist_applies_to_them(deps: Deps) -> None:
    order(deps, "demo", "pedido-antigo")
    assert retention.purge_expired(deps, now=days_later(40)).requests_deleted == 0

    set_periods(deps, "demo", pdf=35, request=36)

    assert retention.purge_expired(deps, now=days_later(40)).requests_deleted == 1


def test_the_pdf_period_of_a_form_removes_the_file_of_its_requests_only(deps: Deps) -> None:
    demo = order(deps, "demo", "pedido-do-demo", delivered=True)
    other = order(deps, SLUG, "pedido-do-outro", delivered=True)
    set_periods(deps, SLUG, pdf=45)
    (demo_key,), (other_key,) = pdf_keys(deps, demo), pdf_keys(deps, other)

    report = retention.purge_expired(deps, now=days_later(35))  # o padrão do PDF (30) venceu

    assert (report.pdfs_deleted, report.requests_deleted) == (1, 0)
    with pytest.raises(FileNotFoundError):
        deps.storage.get(demo_key)
    assert deps.storage.get(other_key)
    assert retention.purge_expired(deps, now=days_later(46)).pdfs_deleted == 1
    with pytest.raises(FileNotFoundError):
        deps.storage.get(other_key)


def test_a_form_that_is_gone_from_the_registry_follows_the_environment_default(
    deps: Deps,
) -> None:
    """Um pedido de um endereço que não está em `forms` (um banco que a migration 0008 não levou)
    segue os prazos do ambiente."""
    order(deps, "demo", "pedido-sem-formulario")
    with deps.sessions.begin() as session:
        session.execute(sa.update(Submission).values(form_slug="sumiu"))

    assert retention.purge_expired(deps, now=days_later(59)).requests_deleted == 0
    assert retention.purge_expired(deps, now=days_later(61)).requests_deleted == 1


def test_a_stored_period_never_outruns_the_lead_link(deps: Deps) -> None:
    """O admin recusa um prazo menor que a validade do link, mas o `LINK_TTL_DAYS` pode subir
    depois: enquanto o link vale, nada sai, qualquer que seja o prazo guardado."""
    job_id = order(deps, "demo", "pedido-com-prazo-curto", delivered=True)
    set_periods(deps, "demo", pdf=1, request=2)
    (key,) = pdf_keys(deps, job_id)

    report = retention.purge_expired(deps, now=days_later(10))

    assert (report.pdfs_deleted, report.requests_deleted) == (0, 0)
    assert deps.storage.get(key)
    assert retention.purge_expired(deps, now=days_later(31)).requests_deleted == 1


# --- a página do admin --------------------------------------------------------------------------


@pytest.fixture
def clock() -> FakeClock:
    """O relógio do admin, no dia de hoje: os pedidos do teste nascem agora, e a contagem do que a
    retenção apagaria olha o relógio do app."""
    current = FakeClock()
    current.now = utcnow().timestamp()
    return current


@pytest.fixture
def client(deps: Deps, clock: FakeClock) -> TestClient:
    """O navegador da operadora, já no admin."""
    deps.settings = Settings(_env_file=None, **ADMIN_ENV)  # type: ignore[call-arg]
    current = TestClient(create_app(trigger=None, clock=clock), follow_redirects=False)
    assert login(current).status_code == 303
    return current


PATH = f"/admin/forms/{SLUG}/retention"


def periods(deps: Deps, slug: str = SLUG) -> tuple[int | None, int | None]:
    """Os prazos próprios do formulário no banco: (PDF, pedido); None é o padrão do ambiente."""
    with deps.sessions() as session:
        row = session.execute(
            sa.select(Form.retention_pdf_days, Form.retention_days).where(Form.slug == slug)
        ).one()
    return row[0], row[1]


def fields(page: str, **typed: str) -> dict[str, str | list[str]]:
    """Os campos do formulário dos prazos como o navegador os mandaria, com o que a operadora
    digitou ou marcou por cima."""
    return {**form_fields(page, "form-prazos"), **typed}


def send(client: TestClient, page: str, **typed: str) -> httpx.Response:
    """Manda o formulário dos prazos da página `page`, como o navegador."""
    return client.post(PATH, data=fields(page, **typed), headers=ORIGIN)


def open_page(client: TestClient, deps: Deps) -> str:
    """A página dos prazos do segundo formulário, que o teste importa antes."""
    order_form(deps)
    response = client.get(PATH)
    assert response.status_code == 200, response.text
    return response.text


def set_via_admin(client: TestClient, deps: Deps, pdf: str, request: str) -> None:
    """Define os prazos do formulário pela página, sem redução (o ponto de partida do teste)."""
    response = send(client, open_page(client, deps), pdf_days=pdf, request_days=request)
    assert response.status_code == 303, response.text


def aged(deps: Deps, job_id: uuid.UUID, days: int) -> None:
    """O pedido terminou `days` dias atrás, com o PDF gerado nesse dia e o link de 30 dias."""
    moment = utcnow() - timedelta(days=days)
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(Job)
            .where(Job.id == job_id)
            .values(created_at=moment, updated_at=moment, expires_at=moment + timedelta(days=30))
        )
        session.execute(
            sa.update(Artifact).where(Artifact.job_id == job_id).values(created_at=moment)
        )


def audit_of(deps: Deps) -> list[AuditLog]:
    with deps.sessions() as session:
        statement = (
            sa.select(AuditLog).where(AuditLog.action == "form.retention").order_by(AuditLog.at)
        )
        return list(session.scalars(statement))


def test_the_page_needs_the_session_and_the_csrf_token(client: TestClient, deps: Deps) -> None:
    open_page(client, deps)
    anonymous = TestClient(client.app, follow_redirects=False)
    data = {"pdf_days": "45", "request_days": "90", "expected": ":"}

    refused = [
        anonymous.get(PATH),
        anonymous.post(PATH, data={"csrf_token": "x", **data}, headers=ORIGIN),
        client.post(PATH, data={"csrf_token": "errado", **data}, headers=ORIGIN),
        client.post(PATH, data={"csrf_token": csrf_token(client), **data}),  # sem a origem
    ]

    assert [response.status_code for response in refused] == [303, 303, 403, 403]
    assert periods(deps) == (None, None) and audit_of(deps) == []


def test_a_form_that_does_not_exist_has_no_retention_page(client: TestClient) -> None:
    data = {"csrf_token": csrf_token(client), "expected": ":"}

    assert client.get("/admin/forms/nao-existe/retention").status_code == 404
    post = client.post("/admin/forms/nao-existe/retention", data=data, headers=ORIGIN)
    assert post.status_code == 404


def test_the_page_shows_the_periods_in_force_and_where_each_comes_from(
    client: TestClient, deps: Deps
) -> None:
    page = open_page(client, deps)

    shown = text(section(page, "prazos"))
    assert "PDF 30 dias depois de gerado · padrão do ambiente" in shown
    assert "Pedido inteiro 60 dias depois da última mudança · padrão do ambiente" in shown
    typed = fields(page)
    assert (typed["pdf_days"], typed["request_days"]) == ("", "")
    assert "validade do link da lead (30 dias)" in text(section(page, "mudar"))
    assert tags(page, "a", href=f"/admin/forms/{SLUG}")  # volta para a leitura do formulário


def test_the_operator_sets_the_periods_of_one_form_without_publishing_a_version(
    client: TestClient, deps: Deps
) -> None:
    page = open_page(client, deps)

    response = send(client, page, pdf_days="45", request_days="90")

    assert response.status_code == 303 and response.headers["location"] == f"/admin/forms/{SLUG}"
    assert periods(deps) == (45, 90) and periods(deps, "demo") == (None, None)
    reading = client.get(response.headers["location"]).text
    assert "Prazos de retenção salvos" in text(reading)
    shown = text(section(reading, "retencao"))
    assert "PDF 45 dias depois de gerado · prazo deste formulário" in shown
    assert "Pedido inteiro 90 dias depois da última mudança · prazo deste formulário" in shown
    assert tags(section(reading, "retencao"), "a", href=PATH)
    other = text(section(client.get("/admin/forms/demo").text, "retencao"))
    assert "PDF 30 dias depois de gerado · padrão do ambiente" in other
    with deps.sessions() as session:  # nada de rascunho nem de versão nova
        assert session.scalar(sa.select(sa.func.count()).select_from(FormDraft)) == 0
        numbers = session.scalars(sa.select(FormVersion.number).join(Form).where(Form.slug == SLUG))
        assert list(numbers) == [1]
    again = text(section(client.get(PATH).text, "prazos"))
    assert "PDF 45 dias depois de gerado · prazo deste formulário" in again


def test_a_blank_field_goes_back_to_the_environment_default(client: TestClient, deps: Deps) -> None:
    set_via_admin(client, deps, "30", "60")  # os mesmos números do padrão, agora próprios
    assert periods(deps) == (30, 60)

    response = send(client, client.get(PATH).text, pdf_days="", request_days="")

    assert response.status_code == 303 and periods(deps) == (None, None)


def test_saving_the_same_periods_changes_nothing_and_says_so(
    client: TestClient, deps: Deps
) -> None:
    set_via_admin(client, deps, "45", "90")

    response = send(client, client.get(PATH).text)

    assert response.status_code == 303
    assert "nada mudou" in text(client.get(response.headers["location"]).text)
    assert len(audit_of(deps)) == 1


@pytest.mark.parametrize(
    ("typed", "field", "message"),
    [
        (
            {"pdf_days": "29", "request_days": ""},
            "pdf_days",
            "O prazo do PDF (29 dias) não pode ser menor que a validade do link da lead (30 dias)",
        ),
        (
            {"pdf_days": "40", "request_days": "39"},
            "request_days",
            "O prazo do pedido (39 dias) não pode ser menor que o prazo do PDF (40 dias)",
        ),
        (
            {"pdf_days": "90", "request_days": ""},
            "request_days",
            "O prazo do pedido (60 dias) não pode ser menor que o prazo do PDF (90 dias)",
        ),
        ({"pdf_days": "", "request_days": "29"}, "request_days", "O prazo do pedido (29 dias)"),
        ({"pdf_days": "abc", "request_days": ""}, "pdf_days", "inteiro de dias, de 1 a 3650"),
        ({"pdf_days": "", "request_days": "0"}, "request_days", "de 1 a 3650"),
        ({"pdf_days": "-5", "request_days": ""}, "pdf_days", "de 1 a 3650"),
        ({"pdf_days": "4.5", "request_days": ""}, "pdf_days", "de 1 a 3650"),
        ({"pdf_days": "", "request_days": "3651"}, "request_days", "de 1 a 3650"),
    ],
)
def test_a_period_that_breaks_the_rule_is_refused_where_it_was_typed_and_nothing_is_saved(
    client: TestClient, deps: Deps, typed: dict[str, str], field: str, message: str
) -> None:
    page = open_page(client, deps)

    response = send(client, page, **typed)

    assert response.status_code == 422
    (error,) = tags(response.text, "p", id=f"{field.replace('_', '-')}-erro")
    assert error["class"] == "error"
    assert message in text(section(response.text, "mudar"))
    assert fields(response.text)[field] == typed[field]  # o que ela digitou volta ao campo
    assert periods(deps) == (None, None) and audit_of(deps) == []


def test_a_pdf_period_alone_above_the_default_request_period_says_to_set_the_other(
    client: TestClient, deps: Deps
) -> None:
    response = send(client, open_page(client, deps), pdf_days="90", request_days="")

    assert "Esse é o padrão do ambiente: defina também um prazo próprio para o pedido" in text(
        response.text
    )


def test_the_environment_and_the_form_share_one_margin_over_the_lead_link(
    client: TestClient, deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings_module, "RETENTION_PDF_MARGIN_DAYS", 7)
    page = open_page(client, deps)

    refused = send(client, page, pdf_days="36", request_days="60")
    saved = send(client, page, pdf_days="37", request_days="60")

    assert refused.status_code == 422
    assert "a validade do link da lead + 7 (37 dias)" in text(refused.text)
    assert saved.status_code == 303 and periods(deps) == (37, 60)


def test_a_page_opened_before_another_change_is_refused(client: TestClient, deps: Deps) -> None:
    old = open_page(client, deps)
    set_via_admin(client, deps, "40", "80")  # outra aba, ou outra pessoa

    response = send(client, old, pdf_days="45", request_days="90")

    assert response.status_code == 409
    assert "mudaram desde que você abriu esta página" in text(response.text)
    assert fields(response.text)["pdf_days"] == "40"  # a página nova mostra como está agora
    assert periods(deps) == (40, 80) and len(audit_of(deps)) == 1


def test_every_change_is_audited_in_the_operator_name_with_the_periods_and_no_lead_data(
    client: TestClient, deps: Deps
) -> None:
    order(deps, SLUG, "pedido-da-lead")
    set_via_admin(client, deps, "45", "90")

    (entry,) = audit_of(deps)

    assert (entry.actor, entry.target_type) == ("operadora", "form")
    assert entry.details == {
        "form_slug": SLUG,
        "pdf_days": 45,
        "request_days": 90,
        "previous_pdf_days": None,
        "previous_request_days": None,
        "in_force_pdf_days": 45,
        "in_force_request_days": 90,
    }
    with deps.sessions() as session:
        assert entry.target_id == str(session.scalar(sa.select(Form.id).where(Form.slug == SLUG)))


# --- reduzir um prazo ---------------------------------------------------------------------------


def old_requests(deps: Deps) -> None:
    """Os pedidos do teste da redução: dois do segundo formulário com 70 dias, um com 10, e um do
    demo com 65 (que o padrão de 60 dias apaga, mas não conta para o segundo formulário)."""
    for key, slug, days in (
        ("a1", SLUG, 70),
        ("a2", SLUG, 70),
        ("a3", SLUG, 10),
        ("d1", "demo", 65),
    ):
        aged(deps, order(deps, slug, key), days)


def test_reducing_a_period_shows_how_many_requests_the_next_retention_deletes_and_asks(
    client: TestClient, deps: Deps
) -> None:
    order_form(deps)
    old_requests(deps)
    set_via_admin(client, deps, "45", "90")
    page = client.get(PATH).text

    preview = send(client, page, request_days="60")

    assert preview.status_code == 422 and periods(deps) == (45, 90)  # ainda não gravou
    shown = text(section(preview.text, "reducao"))
    assert "vai apagar 2 pedidos inteiros" in shown  # os do demo e o de 10 dias não contam
    assert "seriam 0 pedidos e 0 arquivos de PDF" in shown  # com os prazos de hoje
    assert fields(preview.text)["request_days"] == "60"  # o que ela digitou segue no campo
    assert len(audit_of(deps)) == 1

    confirmed = send(client, preview.text, confirm="on")

    assert confirmed.status_code == 303 and periods(deps) == (45, 60)
    _, second = audit_of(deps)
    assert second.details["confirmed_requests"] == 2 and second.details["confirmed_pdfs"] == 0
    assert (second.details["previous_request_days"], second.details["request_days"]) == (90, 60)
    report = retention.purge_expired(deps)
    # Os dois da conta, mais o do demo, que o padrão de 60 dias apagaria de qualquer jeito.
    assert report.requests_deleted == 3 and jobs_left(deps) == [SLUG]


def test_reducing_the_pdf_period_counts_the_files_of_the_requests_that_stay(
    client: TestClient, deps: Deps
) -> None:
    set_via_admin(client, deps, "45", "90")
    aged(deps, order(deps, SLUG, "a1", delivered=True), 40)
    aged(deps, order(deps, SLUG, "a2", delivered=True), 20)
    page = client.get(PATH).text

    preview = send(client, page, pdf_days="35")

    shown = text(section(preview.text, "reducao"))
    assert "vai apagar 1 arquivo de PDF dos pedidos que ficam" in shown
    assert "seriam 0 pedidos e 0 arquivos de PDF" in shown
    assert send(client, preview.text, confirm="on").status_code == 303
    report = retention.purge_expired(deps)
    assert (report.pdfs_deleted, report.requests_deleted) == (1, 0)


def test_a_reduction_that_deletes_nothing_still_asks_for_the_confirmation(
    client: TestClient, deps: Deps
) -> None:
    set_via_admin(client, deps, "45", "90")

    preview = send(client, client.get(PATH).text, request_days="80")

    assert preview.status_code == 422 and periods(deps) == (45, 90)
    assert "não vai apagar nada deste formulário" in text(section(preview.text, "reducao"))
    assert send(client, preview.text, confirm="on").status_code == 303
    assert periods(deps) == (45, 80)


def test_going_back_to_a_shorter_default_is_a_reduction_too(client: TestClient, deps: Deps) -> None:
    set_via_admin(client, deps, "45", "90")
    aged(deps, order(deps, SLUG, "a1"), 70)

    preview = send(client, client.get(PATH).text, pdf_days="", request_days="")

    assert preview.status_code == 422 and periods(deps) == (45, 90)
    assert "vai apagar 1 pedido inteiro" in text(section(preview.text, "reducao"))
    assert send(client, preview.text, confirm="on").status_code == 303
    assert periods(deps) == (None, None)


def test_a_confirmation_without_the_count_the_operator_saw_is_not_enough(
    client: TestClient, deps: Deps
) -> None:
    set_via_admin(client, deps, "45", "90")

    response = send(client, client.get(PATH).text, request_days="60", confirm="on")

    assert response.status_code == 422 and periods(deps) == (45, 90)
    assert 'id="reducao"' in response.text


def test_a_count_that_grew_since_the_screen_asks_again(client: TestClient, deps: Deps) -> None:
    set_via_admin(client, deps, "45", "90")
    aged(deps, order(deps, SLUG, "a1"), 70)
    preview = send(client, client.get(PATH).text, request_days="60")
    assert "vai apagar 1 pedido inteiro" in text(section(preview.text, "reducao"))
    aged(deps, order(deps, SLUG, "a2"), 70)  # outro pedido vence enquanto ela lê a tela

    again = send(client, preview.text, confirm="on")

    assert again.status_code == 422 and periods(deps) == (45, 90)
    assert "vai apagar 2 pedidos inteiros" in text(section(again.text, "reducao"))
    assert send(client, again.text, confirm="on").status_code == 303
    assert periods(deps) == (45, 60)


def test_raising_a_period_needs_no_confirmation(client: TestClient, deps: Deps) -> None:
    set_via_admin(client, deps, "45", "90")
    aged(deps, order(deps, SLUG, "a1"), 70)

    response = send(client, client.get(PATH).text, pdf_days="50", request_days="120")

    assert response.status_code == 303 and periods(deps) == (50, 120)


# --- a frase do que a retenção apagaria ---------------------------------------------------------


@pytest.mark.parametrize(
    ("due", "sentence"),
    [
        (retention.Due(2, 0), "A próxima retenção vai apagar 2 pedidos inteiros."),
        (retention.Due(1, 0), "A próxima retenção vai apagar 1 pedido inteiro."),
        (
            retention.Due(0, 1),
            "A próxima retenção vai apagar 1 arquivo de PDF dos pedidos que ficam.",
        ),
        (
            retention.Due(3, 5),
            (
                "A próxima retenção vai apagar 3 pedidos inteiros e 5 arquivos de PDF dos "
                "pedidos que ficam."
            ),
        ),
        (retention.Due(0, 0), "A próxima retenção não vai apagar nada deste formulário."),
    ],
)
def test_the_sentence_says_what_the_next_retention_deletes(
    due: retention.Due, sentence: str
) -> None:
    assert stored_retention.deletion_sentence(due) == sentence
