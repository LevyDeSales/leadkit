"""Revisar, simular, publicar e voltar a uma versão na API de admin (`/admin/api/v1`):
`review_form_draft_publication`, `publish_form_draft` (com `dry_run`) e `revert_form_draft_to_version`,
pela costura HTTP.

Agem pelos mesmos serviços das páginas (`app.forms.drafts`: `review`, `publish` e `revert`): a revisão,
o que falta e a gravação são uma regra só, e a API as traduz em JSON e status. A paridade com a página
(o mesmo estado, a mesma auditoria, as imagens fixadas em `form_version_assets`) está em cada operação
que escreve. **Publicar muda o que a lead vê**: a operação é marcada, exige `confirm: true` e não
afrouxa nenhuma conferência; o `dry_run` roda todas, o PDF de exemplo incluído, e não grava nem audita.
As chaves destes testes são sorteadas na execução.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from admin_api_support import (
    API,
    ApiClient,
    assert_same_effect,
    audited,
    built_openapi,
    make_api,
    snapshot,
    url_of,
)
from test_admin import FakeClock, login
from test_admin_drafts import post
from test_admin_forms import CONTACT_PROMPT, DEFINITION, SLUG, store_version
from test_form_images_publication import BLUE, PDF_HTML, PREVIEW, RED, upload
from test_public import text

from app.adapters.assets import AssetStore
from app.adapters.weasyprint_pdf import WeasyPrintRenderer
from app.deps import Deps
from app.errors import FailureCode, TerminalError
from app.forms import assets, draft_tests, drafts, stored
from app.forms.definition import fingerprint
from app.models import FormDraft, FormVersionAsset

Client = Callable[..., ApiClient]
PAGE = f"/admin/forms/{SLUG}/draft/publish"
SENTINEL = "ZZ_SENT_" + secrets.token_hex(8).upper()
NOTE = "Prompt pede o momento do mercado."
NEW_LINE = "\nConsidere também o momento do mercado contábil."
START = datetime(2027, 1, 15, 12, 0, tzinfo=UTC)
BROKEN = "quebrado"  # o formulário cuja versão 2 não compila mais
# O que a chamada da API muda e a página não (o último uso da chave que chama) e o que a ação carimba
# com o relógio da máquina.
IGNORE = {
    "api_keys": {"last_used_at"},
    "form_versions": {"published_at"},
    "*": {"created_at", "updated_at"},
}
CONTACT_API = "confirm_contact"


class SpyPdf:
    """O adaptador do PDF, trocado: conta as renderizações e, se `refuses`, recusa o exemplo."""

    def __init__(self, inner: Any, *, refuses: bool = False) -> None:
        self.inner = inner
        self.refuses = refuses
        self.calls = 0

    def render(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.refuses:
            raise TerminalError(FailureCode.RENDER_FAILED, "o exemplo não renderiza")
        return self.inner.render(*args, **kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def world(deps: Deps) -> None:
    """O formulário demo-definicao, importado (versão 1)."""
    stored.import_definition(deps, DEFINITION, actor="testes")


@pytest.fixture
def pdf(deps: Deps) -> SpyPdf:
    spy = SpyPdf(deps.pdf)
    deps.pdf = spy
    return spy


@pytest.fixture
def writer(api_client: Client, clock: FakeClock, world: None) -> ApiClient:
    return api_client("total", clock=clock)


@pytest.fixture
def reader(api_client: Client, clock: FakeClock, world: None) -> ApiClient:
    return api_client("leitura", clock=clock)


@pytest.fixture
def both(api_client: Client, clock: FakeClock, world: None) -> ApiClient:
    """Um cliente que é a operadora na página (`pagina`) e uma chave `total` na API: o autor da página
    é diferente do `operadora` do mundo de teste, para as linhas que o mundo gravou não se confundirem."""
    return api_client("total", clock=clock, ADMIN_USERNAME="pagina")


@pytest.fixture
def tick() -> list[int]:
    """Quantos testes do rascunho o teste já rodou (ver `run_test`)."""
    return [0]


# --- o mundo de cada teste ----------------------------------------------------------------------


def draft_of(deps: Deps, slug: str = SLUG) -> drafts.Draft:
    with deps.sessions() as session:
        found = drafts.load(session, slug)
    assert found is not None
    return found


def edit(deps: Deps, slug: str = SLUG, **changes: Any) -> None:
    """Salva o rascunho com as mudanças, como a API salva."""
    document = {**draft_of(deps, slug).document, **changes}
    drafts.replace_document(
        deps, slug, document, actor="chave:testes", expected=draft_of(deps, slug).token
    )


def edit_prompt(deps: Deps) -> None:
    edit(deps, prompt=draft_of(deps).document["prompt"] + NEW_LINE)


def run_test(deps: Deps, tick: list[int]) -> None:
    """Um teste do rascunho com sucesso, como agora; cada um começa 1 minuto depois do anterior."""
    tick[0] += 1
    moment = START + timedelta(minutes=tick[0])
    test_id = draft_tests.start(deps, SLUG, actor="chave:testes", now=moment)
    assert draft_tests.run(deps, test_id, now=lambda: moment) == "succeeded"


def ready_to_publish(deps: Deps, tick: list[int]) -> str:
    """O rascunho com o prompt mudado e o teste com sucesso: devolve a `version` dele."""
    edit_prompt(deps)
    run_test(deps, tick)
    return draft_of(deps).token


def unchanged(deps: Deps) -> Callable[[], bool]:
    """Um fotógrafo do banco que diz se algo mudou, menos o último uso da chave que chama
    (`api_keys.last_used_at`, que a própria chamada grava): o resto da tabela das chaves conta."""

    def without_last_use() -> dict[str, Any]:
        state = snapshot()
        state["api_keys"] = [
            {name: value for name, value in row.items() if name != "last_used_at"}
            for row in state["api_keys"]
        ]
        return state

    before = without_last_use()
    return lambda: without_last_use() == before


def stored_files(deps: Deps) -> dict[str, bytes]:
    """Todos os arquivos do storage dos testes, pela chave."""
    root: Path = deps.storage.root  # type: ignore[attr-defined]
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def review_url(slug: str = SLUG) -> str:
    return url_of("review_form_draft_publication", slug=slug)


def publish_url(slug: str = SLUG) -> str:
    return url_of("publish_form_draft", slug=slug)


def revert_url(number: Any, slug: str = SLUG) -> str:
    return url_of("revert_form_draft_to_version", slug=slug, number=str(number))


def publish_body(version: str, **changes: Any) -> dict[str, Any]:
    return {"expected_version": version, "note": NOTE, "confirm": True, **changes}


def publish_via_api(client: ApiClient, version: str, **changes: Any) -> httpx.Response:
    return client.post(publish_url(), json=publish_body(version, **changes))


def revert_via_api(client: ApiClient, number: Any, version: str, **changes: Any) -> httpx.Response:
    return client.post(
        revert_url(number), json={"expected_version": version, "confirm": True, **changes}
    )


def versions_of(deps: Deps, slug: str = SLUG) -> list[int]:
    with deps.sessions() as session:
        return [entry.number for entry in stored.history(session, slug)]


# --- a chave e o papel --------------------------------------------------------------------------

REQUESTS: dict[str, Callable[[], tuple[str, str, Any]]] = {
    "review_form_draft_publication": lambda: ("GET", review_url(), None),
    "publish_form_draft": lambda: ("POST", publish_url(), publish_body("x")),
    "revert_form_draft_to_version": lambda: (
        "POST",
        revert_url(1),
        {"expected_version": "x", "confirm": True},
    ),
}


def call(client: Any, operation_id: str) -> httpx.Response:
    method, url, body = REQUESTS[operation_id]()
    return client.request(method, url, json=body)


@pytest.mark.parametrize("operation_id", list(REQUESTS))
def test_every_publication_operation_needs_a_key(
    deps: Deps, clock: FakeClock, world: None, operation_id: str
) -> None:
    response = call(make_api(deps, clock), operation_id)

    assert response.status_code == 401 and response.json()["code"] == "invalid_credentials"
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_read_key_reads_the_review_but_cannot_simulate_publish_or_go_back(
    reader: ApiClient, deps: Deps, tick: list[int]
) -> None:
    version = ready_to_publish(deps, tick)
    nothing_changed = unchanged(deps)

    review = reader.get(review_url())
    simulated = publish_via_api(reader, version, dry_run=True, confirm=False)
    published = publish_via_api(reader, version)
    reverted = revert_via_api(reader, 1, version)

    assert review.status_code == 200 and review.headers["cache-control"] == "no-store"
    for refused in (simulated, published, reverted):
        assert refused.status_code == 403 and refused.json()["code"] == "forbidden_role"
    assert nothing_changed()


# --- review_form_draft_publication --------------------------------------------------------------


def test_review_form_draft_publication_says_what_the_publication_would_do_and_what_is_missing(
    writer: ApiClient, deps: Deps
) -> None:
    edit_prompt(deps)

    response = writer.get(review_url())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["slug"] == SLUG and body["version"] == draft_of(deps).token
    assert (body["current_version"], body["number"]) == (1, 2)
    assert body["first_publication"] is False and body["nothing_to_publish"] is False
    assert [change["key"] for change in body["changes"]] == ["prompt"]
    (change,) = body["changes"]
    assert change["changed"] is True and any(line["kind"] == "add" for line in change["lines"])
    assert body["test"] == {
        "status": "missing",
        "llm_changes": ["o prompt"],
        "tested_at": None,
    }
    assert body["missing"] == [drafts.TEST_REQUIRED.format(changes="mudou o prompt")]
    assert body["problems"] == [] and body["contact"] == []
    assert body["consent_version"] is None and body["dropped_values"] == []
    assert body["stale_assets"] == []


def test_review_form_draft_publication_says_the_test_covers_the_change_once_one_succeeds(
    writer: ApiClient, deps: Deps, tick: list[int]
) -> None:
    ready_to_publish(deps, tick)

    body = writer.get(review_url()).json()

    assert body["test"]["status"] == "covered" and body["test"]["tested_at"] is not None
    assert body["test"]["llm_changes"] == ["o prompt"] and body["missing"] == []


def test_review_form_draft_publication_needs_no_test_for_what_the_llm_does_not_read(
    writer: ApiClient, deps: Deps
) -> None:
    edit(deps, intro="Uma introdução nova, que o LLM não lê.")

    body = writer.get(review_url()).json()

    assert body["test"] == {"status": "not_needed", "llm_changes": [], "tested_at": None}
    assert body["missing"] == [] and [c["key"] for c in body["changes"]] == ["pagina"]


def test_review_form_draft_publication_of_nothing_pending_says_there_is_nothing_to_publish(
    writer: ApiClient,
) -> None:
    body = writer.get(review_url()).json()

    assert body["nothing_to_publish"] is True and body["changes"] == []
    assert body["missing"] == [drafts.NOTHING_TO_PUBLISH]


def test_review_form_draft_publication_of_a_first_publication_lists_no_section_as_changed(
    writer: ApiClient, deps: Deps
) -> None:
    drafts.create(deps, title="Gestão do escritório", slug="gestao", actor="testes")

    body = writer.get(review_url("gestao")).json()

    assert body["first_publication"] is True and body["current_version"] is None
    assert body["number"] == 1 and body["changes"] == []  # tudo é novo; a página também não lista
    assert body["consent_version"] is None


def test_review_form_draft_publication_names_the_contact_data_the_prompt_sends_to_the_llm(
    writer: ApiClient, deps: Deps
) -> None:
    edit(deps, prompt=json.loads(CONTACT_PROMPT)["prompt"])

    body = writer.get(review_url()).json()

    assert body["contact"] == ["name", "email"]


def test_review_form_draft_publication_says_the_new_version_of_the_consent_text(
    writer: ApiClient, deps: Deps
) -> None:
    consent = {**draft_of(deps).document["consent"], "notice": "Um aviso de privacidade novo."}
    edit(deps, consent=consent)

    body = writer.get(review_url()).json()

    assert body["consent_version"] == "demo-definicao-v2"


def test_review_form_draft_publication_lists_the_option_values_the_new_version_drops(
    writer: ApiClient, deps: Deps
) -> None:
    steps = draft_of(deps).document["steps"]
    team = next(q for step in steps for q in step["questions"] if q["name"] == "tamanho_equipe")
    team["options"] = [option for option in team["options"] if option["value"] != "mais_de_15"]
    edit(deps, steps=steps)

    body = writer.get(review_url()).json()

    (dropped,) = body["dropped_values"]
    assert (dropped["question"], dropped["value"]) == ("tamanho_equipe", "mais_de_15")
    assert dropped["label"] and dropped["option"]


def test_review_form_draft_publication_says_the_images_that_changed_since_the_version(
    writer: ApiClient, deps: Deps
) -> None:
    deps.pdf = WeasyPrintRenderer(AssetStore(deps.storage))
    logo = upload(deps, RED)
    pdf = {**draft_of(deps).document["pdf"], "html": PDF_HTML}
    edit(deps, pdf=pdf, preview=PREVIEW)
    drafts.publish(deps, SLUG, actor="testes", note="Logo.", expected=draft_of(deps).token)
    assets.replace(deps, logo.id, BLUE, expected=logo.ref.sha256, actor="testes")

    body = writer.get(review_url()).json()

    assert body["stale_assets"] == ["logo-black"] and body["nothing_to_publish"] is False


def test_review_form_draft_publication_does_not_render_the_pdf_and_writes_nothing(
    writer: ApiClient, deps: Deps, pdf: SpyPdf, tick: list[int]
) -> None:
    ready_to_publish(deps, tick)
    calls = pdf.calls
    nothing_changed = unchanged(deps)

    response = writer.get(review_url())

    assert response.status_code == 200 and pdf.calls == calls
    assert nothing_changed() and audited(deps, "form.publish") == []


def test_review_form_draft_publication_gives_the_version_the_other_operations_ask_for(
    writer: ApiClient, deps: Deps
) -> None:
    edit_prompt(deps)

    review = writer.get(review_url()).json()
    draft = writer.get(url_of("get_form_draft", slug=SLUG)).json()

    assert review["version"] == draft["version"]


def test_review_form_draft_publication_says_what_the_publication_page_says(
    both: ApiClient, deps: Deps, tick: list[int]
) -> None:
    edit_prompt(deps)
    assert login(both, "pagina").status_code == 303
    api = both.get(review_url()).json()

    untested = text(both.get(PAGE).text)
    run_test(deps, tick)
    tested = text(both.get(PAGE).text)

    assert api["missing"] and "Falta um teste com sucesso: mudou o prompt" in untested
    assert f"Publicar a versão {api['number']}" in untested and "Prompt" in untested
    assert "O teste com sucesso de" in tested and "cobre o que vai ao LLM" in tested
    assert both.get(review_url()).json()["missing"] == []


def test_review_form_draft_publication_of_a_form_that_does_not_exist_is_not_found(
    writer: ApiClient,
) -> None:
    response = writer.get(review_url("nao-existe"))

    assert response.status_code == 404 and response.json()["code"] == "not_found"


# --- publish_form_draft: a confirmação ----------------------------------------------------------


def test_publish_form_draft_without_a_confirm_true_is_refused_and_nothing_happens(
    writer: ApiClient, deps: Deps, tick: list[int]
) -> None:
    version = ready_to_publish(deps, tick)
    nothing_changed = unchanged(deps)

    for changes in (
        {},
        {"confirm": False},
        {"confirm": "true"},
        {"dry_run": False},
        {"dry_run": "true"},  # só o booleano `true` dispensa
        {"dry_run": 1},
    ):
        sent = {"expected_version": version, "note": NOTE, **changes}
        response = writer.post(publish_url(), json=sent)

        assert response.status_code == 422, changes
        assert response.json()["code"] == "confirmation_required"
        assert "dry_run: true" in response.json()["detail"]  # a recusa diz como só simular
    assert nothing_changed() and versions_of(deps) == [1]
    assert audited(deps, "form.publish") == []


def test_publish_form_draft_asks_for_the_confirmation_before_it_looks_at_the_body(
    writer: ApiClient,
) -> None:
    response = writer.post(publish_url(), json={"intruso": 1})

    assert response.json()["code"] == "confirmation_required"


def test_publish_form_draft_is_marked_external_and_dispenses_the_confirm_only_for_a_dry_run() -> (
    None
):
    document = built_openapi()
    operation = document["paths"]["/forms/{slug}/draft/publish"]["post"]

    assert operation["operationId"] == "publish_form_draft"
    assert operation["x-required-role"] == "total"
    assert operation["x-external-effect"] is True and "x-destructive" not in operation
    assert operation["x-confirm-unless"] == "dry_run"
    schema = document["components"]["schemas"]["PublishFormDraftRequest"]["properties"]
    assert schema["dry_run"]["type"] == "boolean" and schema["dry_run"]["default"] is False
    assert {"confirm", "confirm_contact", "note", "expected_version"} <= set(schema)


# --- publish_form_draft: o dry_run --------------------------------------------------------------


def test_a_dry_run_needs_no_confirm_and_creates_no_version_changes_no_draft_and_audits_nothing(
    writer: ApiClient, deps: Deps, tick: list[int]
) -> None:
    """Com o PDF de verdade e uma imagem: a simulação o renderiza (lê a imagem do storage) e deixa o
    banco **e o storage** como estavam."""
    pdf = SpyPdf(WeasyPrintRenderer(AssetStore(deps.storage)))
    deps.pdf = pdf
    upload(deps, RED)
    edit(deps, pdf={**draft_of(deps).document["pdf"], "html": PDF_HTML}, preview=PREVIEW)
    version = ready_to_publish(deps, tick)
    nothing_changed = unchanged(deps)
    files = stored_files(deps)
    rendered = pdf.calls
    assert any(key.startswith("assets/") for key in files)  # a imagem está lá

    for confirm in (None, False):
        sent = {"expected_version": version, "note": NOTE, "dry_run": True}
        if confirm is not None:
            sent["confirm"] = confirm
        response = writer.post(publish_url(), json=sent)

        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["dry_run"], body["published"], body["ready"]) == (True, False, True)
        assert body["published_version"] is None
        assert body["review"]["number"] == 2 and body["review"]["missing"] == []
        assert body["draft"]["pending"] is True and body["draft"]["version"] == version
    assert pdf.calls == rendered + 2  # uma renderização por simulação
    assert nothing_changed() and versions_of(deps) == [1]
    assert stored_files(deps) == files  # nenhum arquivo novo, nenhum trocado
    assert audited(deps, "form.publish") == []
    assert draft_of(deps).token == version  # o rascunho ficou onde estava
    # O que a simulação disse que estava pronto, a publicação de verdade faz.
    published = publish_via_api(writer, version)
    assert published.status_code == 200 and published.json()["published_version"]["number"] == 2


def test_a_dry_run_renders_the_example_pdf_in_the_adapter(
    writer: ApiClient, deps: Deps, pdf: SpyPdf, tick: list[int]
) -> None:
    version = ready_to_publish(deps, tick)
    calls = pdf.calls

    response = publish_via_api(writer, version, dry_run=True)

    assert response.status_code == 200 and pdf.calls == calls + 1


def test_a_dry_run_says_everything_that_is_missing_at_once_in_a_200(
    writer: ApiClient, deps: Deps
) -> None:
    edit_prompt(deps)  # sem teste
    version = draft_of(deps).token

    response = publish_via_api(writer, version, dry_run=True, note="  ")

    body = response.json()
    assert response.status_code == 200 and body["ready"] is False
    assert body["review"]["missing"] == [
        drafts.TEST_REQUIRED.format(changes="mudou o prompt"),
        drafts.NOTE_REQUIRED,
    ]
    assert body["review"]["test"]["status"] == "missing"
    # A publicação de verdade recusa com as mesmas razões.
    refused = publish_via_api(writer, version, note="  ")
    assert refused.status_code == 422
    assert refused.json()["reasons"] == body["review"]["missing"]


def test_a_dry_run_names_the_contact_data_and_says_the_confirmation_publishing_will_ask_for(
    writer: ApiClient, deps: Deps, tick: list[int]
) -> None:
    edit(deps, prompt=json.loads(CONTACT_PROMPT)["prompt"])
    run_test(deps, tick)
    version = draft_of(deps).token

    unconfirmed = publish_via_api(writer, version, dry_run=True).json()
    confirmed = publish_via_api(writer, version, dry_run=True, confirm_contact=True).json()

    assert unconfirmed["review"]["contact"] == ["name", "email"] and unconfirmed["ready"] is False
    (reason,) = unconfirmed["review"]["missing"]
    assert CONTACT_API in reason and "o nome e o e-mail" in reason
    assert confirmed["ready"] is True and confirmed["review"]["missing"] == []
    assert confirmed["review"]["contact"] == ["name", "email"]


def test_a_dry_run_reports_the_example_pdf_the_adapter_refuses(
    writer: ApiClient, deps: Deps, tick: list[int]
) -> None:
    version = ready_to_publish(deps, tick)
    deps.pdf = SpyPdf(deps.pdf, refuses=True)

    response = publish_via_api(writer, version, dry_run=True)

    body = response.json()
    assert response.status_code == 200 and body["ready"] is False
    assert body["review"]["missing"] == [drafts.PDF_FAILED]
    (problem,) = body["review"]["problems"]
    assert problem["field"] == "pdf" and "o PDF de exemplo não foi gerado" in problem["message"]


def test_a_dry_run_of_a_draft_that_changed_is_a_version_conflict(
    writer: ApiClient, deps: Deps
) -> None:
    edit_prompt(deps)
    stale = draft_of(deps).token
    edit(deps, intro="Outra mudança, salva depois.")

    response = publish_via_api(writer, stale, dry_run=True)

    assert response.status_code == 409 and response.json()["code"] == "version_conflict"


def test_a_dry_run_costs_five_tokens_of_the_usage_limit(
    api_client: Client, world: None, deps: Deps, clock: FakeClock
) -> None:
    client = api_client("total", clock=clock, API_RATE_LIMIT_PER_MINUTE="5")
    version = draft_of(deps).token

    first = publish_via_api(client, version, dry_run=True)
    second = publish_via_api(client, version, dry_run=True)

    assert first.status_code == 200
    assert second.status_code == 429 and second.json()["code"] == "rate_limited"


# --- publish_form_draft: publicar ---------------------------------------------------------------


def test_publish_form_draft_creates_the_next_version_and_answers_with_the_new_state(
    writer: ApiClient, deps: Deps, tick: list[int]
) -> None:
    version = ready_to_publish(deps, tick)

    response = publish_via_api(writer, version)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["dry_run"], body["published"], body["ready"]) == (False, True, True)
    published = body["published_version"]
    assert (published["slug"], published["number"], published["note"]) == (SLUG, 2, NOTE)
    assert published["published_by"] == writer.actor
    current = draft_of(deps).current
    assert current is not None and published["fingerprint"] == current.fingerprint
    assert body["review"]["number"] == 2 and body["review"]["missing"] == []
    assert body["review"]["test"]["llm_changes"] == ["o prompt"]
    assert body["draft"]["pending"] is False and body["draft"]["current_version"] == 2
    assert versions_of(deps) == [2, 1]
    assert deps.forms.current(SLUG).prompt_version == "demo-definicao-v2"  # sem reiniciar
    (entry,) = audited(deps, "form.publish")
    assert entry.actor == writer.actor and entry.details["version"] == 2
    assert "tested_at" in entry.details and NOTE not in str(entry.details)


def test_publish_form_draft_leaves_the_same_state_and_audit_as_the_page(
    both: ApiClient, deps: Deps, tick: list[int]
) -> None:
    version = ready_to_publish(deps, tick)

    def by_the_page() -> httpx.Response:
        assert login(both, "pagina").status_code == 303
        return post(both, PAGE, note=NOTE, expected=version)

    page, api = assert_same_effect(
        by_the_page,
        lambda: publish_via_api(both, version),
        api_actor=both.actor,
        page_actor="pagina",
        ignore=IGNORE,
    )

    assert (page.status_code, api.status_code) == (303, 200)
    assert versions_of(deps) == [2, 1]


def test_publish_form_draft_pins_the_images_in_the_version_like_the_page(
    both: ApiClient, deps: Deps
) -> None:
    deps.pdf = WeasyPrintRenderer(AssetStore(deps.storage))
    logo = upload(deps, RED)
    edit(deps, pdf={**draft_of(deps).document["pdf"], "html": PDF_HTML}, preview=PREVIEW)
    version = draft_of(deps).token

    def by_the_page() -> httpx.Response:
        assert login(both, "pagina").status_code == 303
        return post(both, PAGE, note=NOTE, expected=version)

    page, api = assert_same_effect(
        by_the_page,
        lambda: publish_via_api(both, version),
        api_actor=both.actor,
        page_actor="pagina",
        ignore=IGNORE,
    )

    assert (page.status_code, api.status_code) == (303, 200)
    with deps.sessions() as session:
        pinned = {pin.name: pin.sha256 for pin in session.scalars(sa.select(FormVersionAsset))}
    (entry,) = audited(deps, "form.publish")
    assert pinned == {"logo-black": logo.ref.sha256}  # a imagem, fixada na versão 2
    assert entry.details["images"] == {"logo-black": logo.ref.sha256}


def test_publish_form_draft_needs_a_note_and_says_so_in_the_reasons(
    writer: ApiClient, deps: Deps, tick: list[int]
) -> None:
    version = ready_to_publish(deps, tick)
    nothing_changed = unchanged(deps)

    response = publish_via_api(writer, version, note="   ")

    body = response.json()
    assert response.status_code == 422 and body["code"] == "validation_failed"
    assert body["reasons"] == [drafts.NOTE_REQUIRED]
    assert body["errors"] == [{"field": "note", "message": drafts.NOTE_REQUIRED, "line": None}]
    assert nothing_changed() and versions_of(deps) == [1]


def test_publish_form_draft_still_asks_for_a_test_of_what_goes_to_the_llm(
    writer: ApiClient, deps: Deps, tick: list[int]
) -> None:
    edit_prompt(deps)
    nothing_changed = unchanged(deps)

    untested = publish_via_api(writer, draft_of(deps).token)

    assert untested.status_code == 422
    assert untested.json()["reasons"] == [drafts.TEST_REQUIRED.format(changes="mudou o prompt")]
    assert nothing_changed() and versions_of(deps) == [1]
    run_test(deps, tick)
    assert publish_via_api(writer, draft_of(deps).token).status_code == 200


def test_publish_form_draft_does_not_accept_a_test_older_than_a_change_to_what_goes_to_the_llm(
    writer: ApiClient, deps: Deps, tick: list[int]
) -> None:
    ready_to_publish(deps, tick)
    instructions = draft_of(deps).document["instructions"] + "\nUse frases curtas."
    edit(deps, instructions=instructions)  # `gate_of` diz `stale` para o teste de antes

    response = publish_via_api(writer, draft_of(deps).token)

    assert response.status_code == 422
    assert response.json()["reasons"] == [
        drafts.TEST_REQUIRED.format(changes="mudaram o prompt e as instruções")
    ]
    assert versions_of(deps) == [1]


def test_publish_form_draft_accepts_a_test_that_still_unlocks_the_publication(
    writer: ApiClient, deps: Deps, tick: list[int]
) -> None:
    ready_to_publish(deps, tick)
    edit(deps, intro="Uma introdução nova, que o LLM não lê.")  # `gate_of` diz `unlocks`

    response = publish_via_api(writer, draft_of(deps).token)

    assert response.status_code == 200 and versions_of(deps) == [2, 1]


def test_publish_form_draft_asks_for_confirm_contact_when_it_is_the_only_thing_missing(
    writer: ApiClient, deps: Deps, tick: list[int]
) -> None:
    edit(deps, prompt=json.loads(CONTACT_PROMPT)["prompt"])
    run_test(deps, tick)
    version = draft_of(deps).token
    nothing_changed = unchanged(deps)

    asked = publish_via_api(writer, version)

    body = asked.json()
    assert asked.status_code == 422 and body["code"] == "confirmation_required"
    assert "o nome e o e-mail" in body["detail"] and "`confirm_contact: true`" in body["detail"]
    assert "reasons" not in body and "errors" not in body
    assert nothing_changed() and versions_of(deps) == [1]

    confirmed = publish_via_api(writer, version, confirm_contact=True)

    assert confirmed.status_code == 200
    (entry,) = audited(deps, "form.publish")
    assert entry.details["contact_in_prompt"] == ["name", "email"]


def test_publish_form_draft_lists_the_contact_confirmation_among_the_other_reasons(
    writer: ApiClient, deps: Deps, tick: list[int]
) -> None:
    edit(deps, prompt=json.loads(CONTACT_PROMPT)["prompt"])
    run_test(deps, tick)

    response = publish_via_api(writer, draft_of(deps).token, note="")

    body = response.json()
    assert response.status_code == 422 and body["code"] == "validation_failed"
    assert len(body["reasons"]) == 2 and CONTACT_API in body["reasons"][0]
    assert body["reasons"][1] == drafts.NOTE_REQUIRED
    assert [item["field"] for item in body["errors"]] == ["note"]  # a nota é um campo do corpo


def test_publish_form_draft_of_a_draft_that_changed_is_a_version_conflict_and_writes_nothing(
    writer: ApiClient, deps: Deps, tick: list[int]
) -> None:
    stale = ready_to_publish(deps, tick)
    edit(deps, intro="Outra mudança, salva depois.")
    current = draft_of(deps).token
    nothing_changed = unchanged(deps)

    response = publish_via_api(writer, stale)

    body = response.json()
    assert response.status_code == 409 and body["code"] == "version_conflict"
    assert body["current_version"] == current
    assert nothing_changed() and versions_of(deps) == [1]


def test_publish_form_draft_of_a_draft_that_changes_while_the_pdf_renders_is_a_version_conflict(
    writer: ApiClient, deps: Deps, tick: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A publicação concorrente: a conferência passou, mas outra mudança entrou antes da gravação. A
    gravação confere o rascunho sob o bloqueio do formulário e não grava."""
    version = ready_to_publish(deps, tick)
    real = drafts.publication_problems

    def then_someone_saves(*args: Any, **kwargs: Any) -> Any:
        found = real(*args, **kwargs)
        edit(deps, intro="Outra pessoa salvou enquanto o PDF era gerado.")
        return found

    monkeypatch.setattr(drafts, "publication_problems", then_someone_saves)

    response = publish_via_api(writer, version)

    body = response.json()
    assert response.status_code == 409 and body["code"] == "version_conflict"
    assert body["current_version"] == draft_of(deps).token != version
    assert versions_of(deps) == [1] and audited(deps, "form.publish") == []


def test_publish_form_draft_refuses_for_the_same_reasons_as_the_page_and_changes_nothing(
    both: ApiClient, deps: Deps
) -> None:
    edit_prompt(deps)  # sem o teste
    version = draft_of(deps).token

    def by_the_page() -> httpx.Response:
        assert login(both, "pagina").status_code == 303
        return post(both, PAGE, note=NOTE, expected=version)

    page, api = assert_same_effect(
        by_the_page,
        lambda: publish_via_api(both, version),
        api_actor=both.actor,
        page_actor="pagina",
        ignore=IGNORE,
        expect_no_change=True,
    )

    assert (page.status_code, api.status_code) == (422, 422)
    (reason,) = api.json()["reasons"]
    assert reason in text(page.text)


def test_publish_form_draft_twice_in_a_row_refuses_the_second_for_having_nothing_to_publish(
    writer: ApiClient, deps: Deps, tick: list[int]
) -> None:
    version = ready_to_publish(deps, tick)
    assert publish_via_api(writer, version).status_code == 200
    nothing_changed = unchanged(deps)

    again = publish_via_api(writer, version)

    assert again.status_code == 422
    assert again.json()["reasons"] == [drafts.NOTHING_TO_PUBLISH]
    assert nothing_changed() and versions_of(deps) == [2, 1]


def test_publish_form_draft_brings_the_two_lists_when_the_example_pdf_is_refused(
    writer: ApiClient, deps: Deps, tick: list[int]
) -> None:
    version = ready_to_publish(deps, tick)
    deps.pdf = SpyPdf(deps.pdf, refuses=True)
    nothing_changed = unchanged(deps)

    response = publish_via_api(writer, version)

    body = response.json()
    assert response.status_code == 422 and body["code"] == "validation_failed"
    assert body["reasons"] == [drafts.PDF_FAILED]
    (problem,) = body["errors"]
    assert problem["field"] == "pdf" and "o PDF de exemplo não foi gerado" in problem["message"]
    assert nothing_changed() and versions_of(deps) == [1]


def broken_draft(deps: Deps) -> str:
    """Um rascunho gravado antes de o compilador deste deploy mudar: ele não compila mais (o salvar
    de hoje não o aceitaria). Devolve a `version` dele."""
    document = {**draft_of(deps).document, "minutes": 0}
    with deps.sessions.begin() as session:
        form_id = draft_of(deps).form_id
        session.add(
            FormDraft(
                form_id=form_id,
                definition=document,
                fingerprint=fingerprint(document),
                base_version=1,
                updated_by="testes",
                updated_at=START,
            )
        )
    return draft_of(deps).token


def test_publish_form_draft_of_a_draft_that_does_not_compile_brings_the_problems_with_the_path(
    writer: ApiClient, deps: Deps
) -> None:
    version = broken_draft(deps)
    nothing_changed = unchanged(deps)

    review = writer.get(review_url()).json()
    response = publish_via_api(writer, version)

    body = response.json()
    assert response.status_code == 422 and body["code"] == "validation_failed"
    assert body["reasons"] == [drafts.DOES_NOT_COMPILE]
    assert [item["field"] for item in body["errors"]] == ["minutes"]
    assert review["missing"] == body["reasons"] and review["problems"] == body["errors"]
    assert nothing_changed() and versions_of(deps) == [1]


def test_a_dry_run_of_a_draft_that_does_not_compile_reports_the_problems_without_the_pdf(
    writer: ApiClient, deps: Deps, pdf: SpyPdf
) -> None:
    version = broken_draft(deps)

    response = publish_via_api(writer, version, dry_run=True)

    body = response.json()
    assert response.status_code == 200 and body["ready"] is False
    assert body["review"]["missing"] == [drafts.DOES_NOT_COMPILE]
    assert [item["field"] for item in body["review"]["problems"]] == ["minutes"]
    assert pdf.calls == 0  # sem definição que compile, não há PDF de exemplo


def test_publish_form_draft_validates_the_body_with_the_path_of_the_field(
    writer: ApiClient,
) -> None:
    response = writer.post(
        publish_url(), json={"confirm": True, "note": "x" * 301, "dry_run": "talvez"}
    )

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    fields = {item["field"]: item["message"] for item in response.json()["errors"]}
    assert set(fields) == {"expected_version", "note", "dry_run"}
    assert "Campo obrigatório." == fields["expected_version"]


def test_publish_form_draft_refuses_a_field_it_does_not_know(writer: ApiClient) -> None:
    response = writer.post(
        publish_url(), json={"confirm": True, "expected_version": "x", "nota": "x"}
    )

    assert response.status_code == 422
    assert {item["field"] for item in response.json()["errors"]} == {"nota"}


def test_publish_form_draft_of_a_form_that_does_not_exist_is_not_found(
    writer: ApiClient,
) -> None:
    response = writer.post(publish_url("nao-existe"), json=publish_body("x"))

    assert response.status_code == 404 and response.json()["code"] == "not_found"


def test_publish_form_draft_echoes_no_free_text_it_was_sent(writer: ApiClient, deps: Deps) -> None:
    """O eco geral do contrato para no `422` do Pydantic antes do serviço rodar: aqui os outros campos
    são válidos e o texto livre (a nota, a versão que veio) leva o sentinela."""
    edit_prompt(deps)
    stale = draft_of(deps).token
    edit(deps, intro="Outra mudança, salva depois.")
    current = draft_of(deps).token

    for response in (
        publish_via_api(writer, stale, note=SENTINEL),  # 409
        publish_via_api(writer, SENTINEL, note=SENTINEL),  # 409
        publish_via_api(writer, current, note=SENTINEL),  # 422 do serviço: falta o teste
        publish_via_api(writer, current, note=SENTINEL, dry_run=SENTINEL),  # 422 do Pydantic
        publish_via_api(writer, current, note=SENTINEL, confirm_contact=SENTINEL),  # 422
    ):
        assert response.status_code in (409, 422), response.text
        assert SENTINEL not in response.text and SENTINEL not in str(response.headers)
    assert versions_of(deps) == [1]


# --- revert_form_draft_to_version ---------------------------------------------------------------


def second_version(deps: Deps) -> None:
    """A versão 2 publicada, com a introdução trocada."""
    edit(deps, intro="A segunda versão.")
    drafts.publish(deps, SLUG, actor="testes", note="Segunda.", expected=draft_of(deps).token)


def test_revert_form_draft_to_version_copies_the_version_into_the_draft_and_answers_with_it(
    writer: ApiClient, deps: Deps
) -> None:
    second_version(deps)
    with deps.sessions() as session:
        first = stored.stored_version(session, SLUG, 1)
    assert first is not None

    response = revert_via_api(writer, 1, draft_of(deps).token)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document"]["intro"] == first.definition["intro"]
    assert body["pending"] is True and body["base_version"] == 1 and body["current_version"] == 2
    assert body["version"] == draft_of(deps).token
    (entry,) = audited(deps, "form.draft.revert")
    assert entry.actor == writer.actor and entry.details["version"] == 1
    assert versions_of(deps) == [2, 1]  # voltar não publica: a publicação cria a versão 3


def test_revert_form_draft_to_version_leaves_the_same_state_and_audit_as_the_page(
    both: ApiClient, deps: Deps
) -> None:
    second_version(deps)
    version = draft_of(deps).token

    def by_the_page() -> httpx.Response:
        assert login(both, "pagina").status_code == 303
        return post(both, f"/admin/forms/{SLUG}/versions/1/revert", expected=version, confirm="on")

    page, api = assert_same_effect(
        by_the_page,
        lambda: revert_via_api(both, 1, version),
        api_actor=both.actor,
        page_actor="pagina",
        ignore=IGNORE,
    )

    assert (page.status_code, api.status_code) == (303, 200)


def test_revert_form_draft_to_version_without_a_confirm_true_is_refused_and_nothing_happens(
    writer: ApiClient, deps: Deps
) -> None:
    second_version(deps)
    version = draft_of(deps).token
    nothing_changed = unchanged(deps)

    for sent in ({}, {"confirm": False}, {"confirm": "true"}):
        response = writer.post(revert_url(1), json={"expected_version": version, **sent})

        assert response.status_code == 422, sent
        assert response.json()["code"] == "confirmation_required"
        assert "Nada foi feito" in response.json()["detail"]
    assert nothing_changed() and audited(deps, "form.draft.revert") == []


def test_revert_form_draft_to_version_of_a_draft_that_changed_is_a_version_conflict(
    writer: ApiClient, deps: Deps
) -> None:
    second_version(deps)
    stale = draft_of(deps).token
    edit(deps, intro="Outra mudança, salva depois.")
    current = draft_of(deps).token
    nothing_changed = unchanged(deps)

    response = revert_via_api(writer, 1, stale)

    body = response.json()
    assert response.status_code == 409 and body["code"] == "version_conflict"
    assert body["current_version"] == current and nothing_changed()


def test_revert_form_draft_to_version_of_a_version_that_does_not_exist_is_not_found(
    writer: ApiClient, deps: Deps
) -> None:
    nothing_changed = unchanged(deps)
    version = draft_of(deps).token

    missing_version = revert_via_api(writer, 9, version)
    missing_form = writer.post(
        revert_url(1, "nao-existe"), json={"expected_version": version, "confirm": True}
    )

    assert missing_version.status_code == 404 and missing_form.status_code == 404
    assert missing_version.json()["code"] == "not_found" and nothing_changed()


def test_revert_form_draft_to_version_of_a_version_that_no_longer_compiles_says_the_problems(
    writer: ApiClient, deps: Deps
) -> None:
    broken = {**json.loads(DEFINITION), "slug": BROKEN}
    stored.import_definition(deps, json.dumps(broken), actor="testes")
    store_version(deps, 2, {**broken, "minutes": 0})
    version = draft_of(deps, BROKEN).token
    nothing_changed = unchanged(deps)

    response = writer.post(
        revert_url(2, BROKEN), json={"expected_version": version, "confirm": True}
    )

    body = response.json()
    assert response.status_code == 422 and body["code"] == "validation_failed"
    assert [item["field"] for item in body["errors"]] == ["minutes"]
    assert nothing_changed() and audited(deps, "form.draft.revert") == []
    # A versão 1 do mesmo formulário compila, e volta.
    assert (
        writer.post(
            revert_url(1, BROKEN), json={"expected_version": version, "confirm": True}
        ).status_code
        == 200
    )


def test_revert_form_draft_to_version_validates_the_number_in_the_path(
    writer: ApiClient, deps: Deps
) -> None:
    version = draft_of(deps).token

    for number in ("0", "abc", "-1", "9" * 12):
        response = writer.post(
            f"{API}/forms/{SLUG}/versions/{number}/revert",
            json={"expected_version": version, "confirm": True},
        )

        assert response.status_code == 422, number
        assert {item["field"] for item in response.json()["errors"]} == {"number"}


def test_revert_form_draft_to_version_validates_the_body_with_the_path_of_the_field(
    writer: ApiClient,
) -> None:
    response = writer.post(revert_url(1), json={"confirm": True, "nota": 1})

    fields = {item["field"] for item in response.json()["errors"]}
    assert response.status_code == 422 and fields == {"expected_version", "nota"}


def test_revert_form_draft_to_version_echoes_no_value_it_was_sent(
    writer: ApiClient, deps: Deps
) -> None:
    second_version(deps)

    for response in (
        revert_via_api(writer, 1, SENTINEL),
        writer.post(
            f"{API}/forms/{SLUG}/versions/{SENTINEL}/revert",
            json={"expected_version": SENTINEL, "confirm": True},
        ),
        writer.post(revert_url(1), json={"expected_version": SENTINEL, "confirm": SENTINEL}),
    ):
        assert response.status_code in (409, 422)
        assert SENTINEL not in response.text and SENTINEL not in str(response.headers)


def test_revert_form_draft_to_version_is_marked_destructive_and_never_dispenses_the_confirm() -> (
    None
):
    operation = built_openapi()["paths"]["/forms/{slug}/versions/{number}/revert"]["post"]

    assert operation["operationId"] == "revert_form_draft_to_version"
    assert operation["x-required-role"] == "total" and operation["x-destructive"] is True
    assert "x-external-effect" not in operation and "x-confirm-unless" not in operation
