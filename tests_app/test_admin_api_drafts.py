"""O rascunho e a criação de formulários na API de admin (`/admin/api/v1`): `create_form`,
`import_form`, `get_form_draft`, `save_form_draft` e `patch_form_draft`, pela costura HTTP.

Salvam pelos mesmos serviços das páginas (`app.forms.drafts`, `app.forms.stored`): a regra é uma só, e
a paridade com a página está no fim de cada operação que escreve. As 42 operações dos editores
estruturados que mudam o documento têm cada uma um patch que produz o mesmo documento que o editor
(`test_every_editor_operation_has_a_patch_that_makes_the_same_document`). As chaves destes testes são
sorteadas na execução.
"""

from __future__ import annotations

import copy
import json
import secrets
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from admin_api_editor_cases import BEYOND, CASES, Case, base
from admin_api_matrix import EDITOR, read
from admin_api_support import ApiClient, assert_same_effect, audited, make_api, url_of
from fastapi.testclient import TestClient
from test_admin import FakeClock, login
from test_admin_drafts import EDITOR_FORM, hidden, post
from test_admin_forms import (
    CONSENT_VERSION,
    CONTACT_PROMPT,
    DEFINITION,
    SLUG,
    import_form,
    store_version,
)
from test_public import form_fields

from app import destinations
from app.admin.form_editor import FormEdit
from app.api.main import MAX_BODY_BYTES
from app.deps import Deps
from app.forms import draft_patch, drafts, stored
from app.forms.definition import compile_definition, fingerprint
from app.forms.definition.normal import normalized
from app.forms.drafts import SCREEN_FIELDS
from app.models import AuditLog, Form, WebhookDestination

# --- as 42 operações do editor, como patch ------------------------------------------------------


def editor_result(case: Case) -> dict[str, Any]:
    """O documento que o editor deixa: o rascunho aberto no editor, a operação aplicada, o campo
    novo preenchido como a operadora o preencheria, e o que o editor cobre posto de volta no
    documento, como o salvar da página faz (`drafts.document_of`)."""
    document = base()
    if case.tweak is not None:
        case.tweak(document)
    edit = FormEdit.of_document(document)
    assert edit.apply(case.op) is not None, f"o editor não aplicou {case.op}"
    if case.fill is not None:
        case.fill(edit)
    screen = edit.screen()
    consent = {**screen["consent"], "version": document["consent"]["version"]}
    saved = {key: value for key, value in document.items() if key not in SCREEN_FIELDS}
    saved |= {**screen, "consent": consent}
    saved["examples"] = {**document["examples"], "answers": edit.example_answers()}
    # O que o editor não gera (o plano de exemplo), o patch que mantém o documento válido leva junto.
    outside = [op for op in case.patch if op["path"].startswith(BEYOND)]
    return draft_patch.apply(saved, outside)  # type: ignore[no-any-return]


def patch_result(case: Case) -> dict[str, Any]:
    document = base()
    if case.tweak is not None:
        case.tweak(document)
    return draft_patch.apply(document, case.patch)  # type: ignore[no-any-return]


def shown(document: dict[str, Any]) -> str:
    return json.dumps(normalized(document), indent=1, sort_keys=True, ensure_ascii=False)


def test_the_editor_reads_the_base_document_and_writes_it_back_the_same() -> None:
    """Sem isto, um caso passaria por uma diferença que o editor já tinha antes da operação."""
    document = base()
    edit = FormEdit.of_document(document)
    screen = edit.screen()
    saved = {key: value for key, value in document.items() if key not in SCREEN_FIELDS}
    saved |= {**screen, "consent": {**screen["consent"], "version": document["consent"]["version"]}}
    saved["examples"] = {**document["examples"], "answers": edit.example_answers()}

    assert shown(saved) == shown(document)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.op)
def test_every_editor_operation_has_a_patch_that_makes_the_same_document(case: Case) -> None:
    before = base()
    if case.tweak is not None:
        case.tweak(before)

    by_the_editor, by_the_patch = editor_result(case), patch_result(case)

    assert shown(by_the_patch) == shown(by_the_editor), case.op
    assert shown(by_the_patch) != shown(before), f"{case.op}: o patch não mudou nada"


def test_the_cases_cover_exactly_the_editor_operations_of_the_matrix() -> None:
    """A tabela do editor em `cobertura.md` manda: toda operação que muda o documento (as `refresh`
    são `fora (C)`) tem um caso, e nenhum caso é de uma operação que a matriz não lista."""
    rows = [row for row in read().of_kind(EDITOR) if row.status != "fora"]
    wanted = {(row.editor, row.editor_operation) for row in rows}
    cased = {(case.editor, case.operation) for case in CASES}

    assert len(wanted) == 42
    assert wanted == cased
    assert all(case.op.split(":")[0] == case.operation for case in CASES)


WRONG = {
    # a matriz resume o que o editor faz; o patch que só segue o resumo erra:
    "scoring-add-level:0": (
        "o nível novo entra antes do último, e não no fim",
        "/scoring/scores/0/bands/levels/-",
    ),
    "scoring-add-rule:priority": (
        "a regra nova entra antes do «senão»",
        "/scoring/priority/-",
    ),
}


@pytest.mark.parametrize("op", list(WRONG))
def test_a_patch_that_only_follows_the_summary_of_the_matrix_is_not_what_the_editor_does(
    op: str,
) -> None:
    why, path = WRONG[op]
    case = next(case for case in CASES if case.op == op)
    summary = [{**case.patch[0], "path": path}]

    wrong = Case(case.editor, case.operation, case.op, summary, case.fill)

    assert shown(patch_result(wrong)) != shown(editor_result(case)), why


def test_removing_a_step_without_the_rest_of_the_patch_leaves_the_identity_role() -> None:
    case = next(case for case in CASES if case.op == "remove-step:3")
    only_the_step = Case(case.editor, case.operation, case.op, case.patch[:1], tweak=case.tweak)

    assert shown(patch_result(only_the_step)) != shown(editor_result(case))
    assert len(case.patch) > 1


def test_a_wrong_patch_does_not_pass_the_equivalence() -> None:
    """O teste do teste: o patch que sobe a etapa errada não produz o documento do editor."""
    case = next(case for case in CASES if case.op == "up-step:2")
    wrong = Case(
        case.editor,
        case.operation,
        case.op,
        [{"op": "move", "from": "/steps/1", "path": "/steps/0"}],
    )

    assert shown(patch_result(wrong)) != shown(editor_result(case))
    assert copy.deepcopy(case.patch) == case.patch


# --- o mundo dos testes de HTTP ------------------------------------------------------------------

PAGE = "/admin"
SCRATCH = "gestao"  # o criado do zero, sem versão publicada
DRAFT = f"{PAGE}/forms/{SLUG}/draft"
# A chamada da API grava o último uso da chave que chama; a página, não. As colunas de data são do
# relógio da máquina (as da auditoria e do formulário), e não do relógio do app.
IGNORE = {"api_keys": {"last_used_at"}, "*": {"created_at", "updated_at", "published_at"}}
# O que nasce com o formulário novo: a auditoria o cita pelo id dele (um texto sorteado, e não um UUID).
IGNORE_NEW = {**IGNORE, "audit_log": {"target_id"}}
SENTINEL = "ZZ_SENT_" + secrets.token_hex(8).upper()
Client = Callable[..., ApiClient]


def document(**changes: Any) -> dict[str, Any]:
    return {**json.loads(DEFINITION), **changes}


def import_document(deps: Deps, definition: dict[str, Any]) -> None:
    stored.import_definition(deps, json.dumps(definition), actor="testes")


def draft_of(deps: Deps, slug: str = SLUG) -> drafts.Draft:
    with deps.sessions() as session:
        found = drafts.load(session, slug)
    assert found is not None
    return found


def by_a_key(deps: Deps, action: str) -> list[AuditLog]:
    return [entry for entry in audited(deps, action) if entry.actor.startswith("chave:")]


def forms_in_database(deps: Deps) -> list[str]:
    with deps.sessions() as session:
        return sorted(session.scalars(sa.select(Form.slug)))


@pytest.fixture
def world(deps: Deps) -> None:
    """Os formulários do teste, além do demo, do mcc e do reforma-2027 (ativos, na versão 1):
    `demo-definicao`, importado (versão 1, sem rascunho salvo), e `gestao`, criado do zero (só o
    rascunho)."""
    import_document(deps, document())
    drafts.create(deps, title="Gestão do escritório", slug=SCRATCH, actor="testes")


def draft_url(slug: str = SLUG) -> str:
    return url_of("get_form_draft", slug=slug)  # o mesmo caminho do PUT e do PATCH


def read_draft(client: ApiClient, slug: str = SLUG) -> dict[str, Any]:
    response = client.get(draft_url(slug))
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def put(client: ApiClient, new: Any, version: str, slug: str = SLUG, **more: Any) -> httpx.Response:
    body = {"expected_version": version, "document": new, **more}
    return client.put(url_of("save_form_draft", slug=slug), json=body)


def patch(
    client: ApiClient, operations: Any, version: str, slug: str = SLUG, **more: Any
) -> httpx.Response:
    body = {"expected_version": version, "patch": operations, **more}
    return client.patch(url_of("patch_form_draft", slug=slug), json=body)


def create(client: ApiClient, **body: Any) -> httpx.Response:
    return client.post(url_of("create_form"), json=body)


def import_it(client: ApiClient, definition: Any = None, **more: Any) -> httpx.Response:
    body = {"definition": document() if definition is None else definition, "confirm": True}
    return client.post(url_of("import_form"), json={**body, **more})


def fields_of(response: httpx.Response) -> dict[str, str]:
    """Os `errors` de um 422, por campo."""
    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    return {item["field"]: item["message"] for item in response.json()["errors"]}


# --- a chave e o papel --------------------------------------------------------------------------

REQUESTS = {
    "create_form": lambda: ("POST", url_of("create_form"), {"title": "Novo"}),
    "import_form": lambda: (
        "POST",
        url_of("import_form"),
        {"definition": document(slug="outro"), "confirm": True},
    ),
    "get_form_draft": lambda: ("GET", draft_url(), None),
    "save_form_draft": lambda: (
        "PUT",
        url_of("save_form_draft", slug=SLUG),
        {"expected_version": "x", "document": document()},
    ),
    "patch_form_draft": lambda: (
        "PATCH",
        url_of("patch_form_draft", slug=SLUG),
        {"expected_version": "x", "patch": [{"op": "test", "path": "/title", "value": "x"}]},
    ),
}
WRITES = ["create_form", "import_form", "save_form_draft", "patch_form_draft"]


def call(client: TestClient, operation_id: str) -> httpx.Response:
    method, url, body = REQUESTS[operation_id]()
    return client.request(method, url, json=body)


@pytest.mark.parametrize("operation_id", list(REQUESTS))
def test_every_draft_operation_needs_a_key(deps: Deps, world: None, operation_id: str) -> None:
    response = call(make_api(deps, FakeClock()), operation_id)

    assert response.status_code == 401 and response.json()["code"] == "invalid_credentials"


@pytest.mark.parametrize("operation_id", WRITES)
def test_a_read_key_cannot_write_the_draft_or_create_a_form(
    api_client: Client, deps: Deps, world: None, operation_id: str
) -> None:
    before = (forms_in_database(deps), draft_of(deps).token)

    response = call(api_client("leitura"), operation_id)

    assert response.status_code == 403 and response.json()["code"] == "forbidden_role"
    assert (forms_in_database(deps), draft_of(deps).token) == before


def test_a_read_key_reads_the_draft(api_client: Client, world: None) -> None:
    assert api_client("leitura").get(draft_url()).status_code == 200


# --- get_form_draft -----------------------------------------------------------------------------

STATE_FIELDS = {
    "pending",
    "saved_by",
    "saved_at",
    "base_version",
    "current_version",
    "version",
    "stale_assets",
}


def test_get_form_draft_gives_the_whole_document_and_the_version_of_the_page(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("leitura", ADMIN_USERNAME="pagina")
    assert login(client, "pagina").status_code == 303

    body = read_draft(client)

    published = draft_of(deps).document
    assert set(body) == {*STATE_FIELDS, "slug", "title", "document"}
    assert body["document"] == published and {"pdf", "preview", "examples"} <= set(body["document"])
    assert (body["slug"], body["title"]) == (SLUG, published["title"])
    assert body["version"] == fingerprint(published)
    assert (body["pending"], body["saved_by"], body["saved_at"]) == (False, None, None)
    assert (body["base_version"], body["current_version"], body["stale_assets"]) == (1, 1, [])
    # É a impressão digital que a página esconde no formulário do editor.
    assert hidden(client.get(DRAFT).text, "expected") == body["version"]


def test_get_form_draft_has_the_pdf_and_the_preview_the_pdf_page_shows(
    api_client: Client, world: None
) -> None:
    client = api_client("leitura", ADMIN_USERNAME="pagina")
    assert login(client, "pagina").status_code == 303

    body = read_draft(client)

    page = form_fields(client.get(f"{DRAFT}/pdf").text, "pdf-form")
    assert body["document"]["pdf"]["html"] == page["pdf_html"]
    assert body["document"]["pdf"].get("css", "") == page["pdf_css"]
    assert body["document"]["preview"] == page["preview"]
    assert hidden(client.get(f"{DRAFT}/pdf").text, "expected") == body["version"]


def test_get_form_draft_gives_the_document_as_the_database_keeps_it_not_with_the_defaults(
    api_client: Client, deps: Deps, world: None
) -> None:
    """O documento é o do banco: um padrão do formato escrito por extenso (`required: true`) fica
    como foi escrito, e uma chave ausente não aparece."""
    explicit = document()
    explicit["steps"][0]["questions"][0]["required"] = True
    store_version(deps, 2, explicit)

    body = read_draft(api_client("leitura"))

    assert body["document"] == explicit
    assert body["document"]["steps"][0]["questions"][0]["required"] is True
    assert "scoring" not in body["document"]


def test_get_form_draft_tells_a_pending_draft_from_the_published_version(
    api_client: Client, deps: Deps, world: None
) -> None:
    published = draft_of(deps)
    drafts.save(
        deps,
        SLUG,
        drafts.texts_of({**published.document, "intro": "Só no rascunho."}),
        actor="testes",
        expected=published.token,
    )

    body = read_draft(api_client("leitura"))

    assert body["document"]["intro"] == "Só no rascunho." and body["pending"] is True
    assert (body["saved_by"], body["base_version"], body["current_version"]) == ("testes", 1, 1)
    assert body["saved_at"].endswith("Z") or "+00:00" in body["saved_at"]
    assert body["version"] == fingerprint(body["document"]) != fingerprint(published.document)


def test_get_form_draft_of_a_form_without_a_published_version_is_all_pending(
    api_client: Client, world: None
) -> None:
    body = read_draft(api_client("leitura"), SCRATCH)

    assert (body["pending"], body["current_version"], body["base_version"]) == (True, None, None)
    assert body["slug"] == SCRATCH and body["document"]["slug"] == SCRATCH


def test_get_form_draft_of_a_form_that_does_not_exist_is_a_404(
    api_client: Client, world: None
) -> None:
    response = api_client("leitura").get(draft_url("nao-existe"))

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert "nao-existe" not in response.text


# --- save_form_draft ----------------------------------------------------------------------------


def test_the_document_of_get_form_draft_goes_back_unchanged_and_writes_the_same_document(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    before = read_draft(client)

    response = put(client, before["document"], before["version"])

    assert response.status_code == 200, response.text
    assert response.json() == before  # o mesmo documento, a mesma versão, nada pendente
    assert draft_of(deps).saved is False  # voltou ao que a versão publicada diz: sem rascunho


def test_save_form_draft_replaces_the_whole_document_audits_it_and_answers_with_the_new_one(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    before = read_draft(client)
    changed = {**before["document"], "intro": "Uma introdução nova.", "minutes": 7}

    response = put(client, changed, before["version"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document"] == changed == draft_of(deps).document
    assert body["pending"] is True and body["saved_by"] == client.actor
    assert body["version"] == fingerprint(changed) != before["version"]
    (entry,) = by_a_key(deps, "form.draft.save")
    assert entry.actor == client.actor and entry.details["form_slug"] == SLUG
    assert entry.details["fingerprint"] == body["version"]
    assert read_draft(client) == body


def test_an_old_version_is_a_409_with_the_current_one_and_stores_nothing(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    first = read_draft(client)
    assert put(client, {**first["document"], "minutes": 5}, first["version"]).status_code == 200
    current = draft_of(deps)

    response = put(client, {**first["document"], "minutes": 9}, first["version"])

    assert response.status_code == 409
    assert response.json()["code"] == "version_conflict"
    assert response.json()["current_version"] == current.token
    assert draft_of(deps).document["minutes"] == 5  # a segunda gravação não aconteceu
    assert len(by_a_key(deps, "form.draft.save")) == 1


def test_a_document_that_does_not_compile_is_a_422_with_every_path_and_the_line_and_stores_nothing(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    before = read_draft(client)
    broken = copy.deepcopy(before["document"])
    broken["minutes"] = f"x{SENTINEL}"
    broken["steps"][1]["questions"][0]["kind"] = SENTINEL
    broken["steps"][0]["title"] = ""

    response = put(client, broken, before["version"])

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    found = {item["field"]: item for item in response.json()["errors"]}
    assert set(found) == {"minutes", "steps.1.questions.0.kind", "steps.0.title"}
    assert all(set(item) == {"field", "message", "line"} for item in found.values())
    assert SENTINEL not in response.text  # nunca o valor recebido
    assert draft_of(deps).saved is False and by_a_key(deps, "form.draft.save") == []


def test_a_template_problem_comes_with_its_line(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    before = read_draft(client)
    broken = copy.deepcopy(before["document"])
    broken["pdf"]["html"] = "<p>{{ plan.titulo }}</p>\n<p>\n{% if %}</p>"

    response = put(client, broken, before["version"])

    assert response.status_code == 422
    (item,) = [i for i in response.json()["errors"] if i["field"].startswith("pdf")]
    assert item["field"] == "pdf.html" and item["line"] == 3


def test_the_pdf_cannot_ask_for_what_the_app_does_not_serve(
    api_client: Client, deps: Deps, world: None
) -> None:
    """As conferências das duas páginas de salvar valem juntas: a do PDF também."""
    client = api_client("total")
    before = read_draft(client)
    outside = copy.deepcopy(before["document"])
    outside["pdf"]["html"] = '<p>{{ plan.titulo }}</p>\n<img src="https://example.test/x.png">'

    response = put(client, outside, before["version"])

    (item,) = fields_of(response).items()
    assert item[0] == "pdf.html" and "carrega um recurso de fora do app" in item[1]
    assert response.json()["errors"][0]["line"] == 2
    assert draft_of(deps).saved is False


def test_the_address_changes_before_the_first_publication_and_takes_the_destination_along(
    api_client: Client, deps: Deps, world: None
) -> None:
    with deps.sessions.begin() as session:
        destinations.save(
            session,
            deps.cipher,
            form_slug=SCRATCH,
            url="https://receptor.example.test/gestao",
            actor="testes",
            expected_version=0,
            production=False,
            http_confirmed=False,
            purpose="internal",
        )
    client = api_client("total")
    before = read_draft(client, SCRATCH)

    response = put(
        client, {**before["document"], "slug": "gestao-2027"}, before["version"], SCRATCH
    )

    assert response.status_code == 200, response.text
    assert response.json()["slug"] == "gestao-2027"
    assert response.json()["document"]["consent"]["version"] == "gestao-2027-v1"
    assert client.get(draft_url(SCRATCH)).status_code == 404  # o endereço antigo saiu
    with deps.sessions() as session:
        moved = session.scalar(
            sa.select(WebhookDestination.id).where(WebhookDestination.form_slug == "gestao-2027")
        )
    assert moved is not None
    (entry,) = by_a_key(deps, "form.rename")
    assert entry.details == {"form_slug": "gestao-2027", "previous_slug": SCRATCH}


def test_after_the_first_publication_the_address_does_not_change(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    before = read_draft(client)

    response = put(client, {**before["document"], "slug": "outro-endereco"}, before["version"])

    assert "não muda depois da primeira publicação" in fields_of(response)["slug"]
    assert forms_in_database(deps).count(SLUG) == 1 and "outro-endereco" not in forms_in_database(
        deps
    )


@pytest.mark.parametrize("word", ["default", "import", "new", "demo"])
def test_a_reserved_or_taken_word_is_not_an_address_for_the_form(
    api_client: Client, deps: Deps, world: None, word: str
) -> None:
    client = api_client("total")
    before = read_draft(client, SCRATCH)

    response = put(client, {**before["document"], "slug": word}, before["version"], SCRATCH)

    assert fields_of(response).keys() == {"slug"}
    assert SCRATCH in forms_in_database(deps)


def test_the_version_of_the_consent_text_is_the_systems(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    before = read_draft(client)
    consent = {**before["document"]["consent"], "version": "inventada-v9"}

    response = put(client, {**before["document"], "consent": consent}, before["version"])

    assert response.status_code == 200
    assert response.json()["document"]["consent"]["version"] == CONSENT_VERSION


@pytest.mark.parametrize(
    ("shape", "field"),
    [([], ""), (None, ""), ("texto", ""), (7, "")],
    ids=["list", "null", "text", "number"],
)
def test_a_document_that_is_not_an_object_is_a_422_on_the_whole_document(
    api_client: Client, deps: Deps, world: None, shape: Any, field: str
) -> None:
    client = api_client("total")

    response = put(client, shape, read_draft(client)["version"])

    assert response.status_code == 422
    assert [item["field"] for item in response.json()["errors"]] == [field]
    assert draft_of(deps).saved is False


def test_saving_needs_the_version_and_the_document(api_client: Client, world: None) -> None:
    client = api_client("total")

    response = client.put(url_of("save_form_draft", slug=SLUG), json={})

    assert fields_of(response) == {
        "expected_version": "Campo obrigatório.",
        "document": "Campo obrigatório.",
    }


def test_saving_the_draft_of_a_form_that_does_not_exist_is_a_404(
    api_client: Client, world: None
) -> None:
    response = put(api_client("total"), document(), "x", "nao-existe")

    assert response.status_code == 404 and "nao-existe" not in response.text


def test_what_the_pdf_page_saved_comes_back_in_the_document_and_goes_back_the_same(
    api_client: Client, deps: Deps, world: None
) -> None:
    """O documento da API é o da página: o PDF e a prévia que a página do PDF salvou voltam no `GET`."""
    client = api_client("total", ADMIN_USERNAME="pagina")
    assert login(client, "pagina").status_code == 303
    page = form_fields(client.get(f"{DRAFT}/pdf").text, "pdf-form")
    page["pdf_html"] = f"{page['pdf_html']}<!-- da página -->"
    assert post(client, f"{DRAFT}/pdf", **page).status_code == 303

    body = read_draft(client)

    assert body["document"]["pdf"]["html"].endswith("<!-- da página -->")
    again = put(client, body["document"], body["version"]).json()
    assert (again["document"], again["version"]) == (body["document"], body["version"])


def page_post(
    client: ApiClient, path: str, change: Callable[[dict[str, Any]], Any], form: str
) -> Callable[[], httpx.Response]:
    """Salvar pela página: entra, lê o formulário do editor, aplica a mudança e o manda de volta."""

    def call_it() -> httpx.Response:
        assert login(client, "pagina").status_code == 303
        fields = form_fields(client.get(path).text, form)
        change(fields)
        return post(client, path, **fields)

    return call_it


def test_save_form_draft_leaves_the_same_state_and_audit_as_the_fields_page(
    api_client: Client, world: None
) -> None:
    client = api_client("total", ADMIN_USERNAME="pagina")
    new_prompt = "Considere também o momento do mercado contábil."

    def on_the_page(fields: dict[str, Any]) -> None:
        fields["prompt"] = f"{fields['prompt']}{new_prompt}\n"

    def through_the_api() -> httpx.Response:
        before = read_draft(client)
        return put(
            client,
            {**before["document"], "prompt": f"{before['document']['prompt']}{new_prompt}\n"},
            before["version"],
        )

    page, api = assert_same_effect(
        page_post(client, DRAFT, on_the_page, EDITOR_FORM),
        through_the_api,
        api_actor=client.actor,
        page_actor="pagina",
        ignore=IGNORE,
    )

    assert page.status_code == 303 and api.status_code == 200
    assert api.json()["document"]["prompt"].endswith(new_prompt + "\n")


def test_save_form_draft_leaves_the_same_state_and_audit_as_the_pdf_page(
    api_client: Client, world: None
) -> None:
    client = api_client("total", ADMIN_USERNAME="pagina")
    comment = "<!-- um comentário -->"

    def on_the_page(fields: dict[str, Any]) -> None:
        fields["pdf_html"] = f"{fields['pdf_html']}{comment}"

    def through_the_api() -> httpx.Response:
        before = read_draft(client)
        pdf = {**before["document"]["pdf"], "html": f"{before['document']['pdf']['html']}{comment}"}
        return put(client, {**before["document"], "pdf": pdf}, before["version"])

    page, api = assert_same_effect(
        page_post(client, f"{DRAFT}/pdf", on_the_page, "pdf-form"),
        through_the_api,
        api_actor=client.actor,
        page_actor="pagina",
        ignore=IGNORE,
    )

    assert page.status_code == 303 and api.status_code == 200


def test_a_rename_leaves_the_same_state_and_audit_as_the_fields_page(
    api_client: Client, world: None
) -> None:
    client = api_client("total", ADMIN_USERNAME="pagina")
    path = f"{PAGE}/forms/{SCRATCH}/draft"

    def on_the_page(fields: dict[str, Any]) -> None:
        fields["slug"] = "gestao-2027"

    def through_the_api() -> httpx.Response:
        before = read_draft(client, SCRATCH)
        return put(
            client, {**before["document"], "slug": "gestao-2027"}, before["version"], SCRATCH
        )

    page, api = assert_same_effect(
        page_post(client, path, on_the_page, EDITOR_FORM),
        through_the_api,
        api_actor=client.actor,
        page_actor="pagina",
        ignore=IGNORE,
    )

    assert page.status_code == 303 and api.status_code == 200


# --- patch_form_draft ---------------------------------------------------------------------------


def test_patch_form_draft_edits_the_draft_and_answers_with_the_new_document(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    before = read_draft(client)

    response = patch(
        client,
        [
            {"op": "replace", "path": "/intro", "value": "Pelo patch."},
            {"op": "add", "path": "/steps/-", "value": NEW_STEP},
            {"op": "move", "from": "/steps/1", "path": "/steps/0"},
        ],
        before["version"],
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document"]["intro"] == "Pelo patch." and body["pending"] is True
    titles = [step["title"] for step in body["document"]["steps"]]
    assert titles == [
        before["document"]["steps"][1]["title"],
        before["document"]["steps"][0]["title"],
        before["document"]["steps"][2]["title"],
        "Nova etapa",
    ]
    assert body["document"] == draft_of(deps).document
    assert body["version"] == fingerprint(body["document"]) != before["version"]
    (entry,) = by_a_key(deps, "form.draft.save")
    assert (entry.actor, entry.details["fingerprint"]) == (client.actor, body["version"])


NEW_STEP = {
    "title": "Nova etapa",
    "questions": [
        {
            "name": "observacoes",
            "kind": "text",
            "label": "Observações",
            "required": False,
            "max_length": 300,
        }
    ],
}


def test_a_patch_with_one_operation_that_does_not_apply_stores_not_even_the_valid_ones(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    before = read_draft(client)

    response = patch(
        client,
        [
            {"op": "replace", "path": "/intro", "value": "Esta valia."},
            {"op": "remove", "path": "/steps/9"},
            {"op": "replace", "path": "/minutes", "value": 9},
        ],
        before["version"],
    )

    assert fields_of(response) == {"patch.1": draft_patch.NOT_APPLICABLE}
    assert draft_of(deps).saved is False and by_a_key(deps, "form.draft.save") == []


def test_a_test_operation_that_fails_refuses_the_whole_patch(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    before = read_draft(client)

    response = patch(
        client,
        [
            {"op": "test", "path": "/minutes", "value": before["document"]["minutes"] + 1},
            {"op": "replace", "path": "/minutes", "value": 5},
        ],
        before["version"],
    )

    assert fields_of(response) == {"patch.0": draft_patch.TEST_FAILED}
    assert draft_of(deps).document["minutes"] == before["document"]["minutes"]


def test_a_test_operation_that_holds_lets_the_patch_through(
    api_client: Client, world: None
) -> None:
    client = api_client("total")
    before = read_draft(client)
    minutes = before["document"]["minutes"]

    response = patch(
        client,
        [
            {"op": "test", "path": "/minutes", "value": minutes},
            {"op": "replace", "path": "/minutes", "value": minutes + 1},
        ],
        before["version"],
    )

    assert response.status_code == 200 and response.json()["document"]["minutes"] == minutes + 1


def test_a_patch_whose_result_does_not_compile_names_the_field_of_the_document(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    before = read_draft(client)

    response = patch(
        client,
        [
            {"op": "replace", "path": "/minutes", "value": 0},
            {"op": "replace", "path": "/steps/0/questions/0/kind", "value": SENTINEL},
        ],
        before["version"],
    )

    assert fields_of(response).keys() == {"minutes", "steps.0.questions.0.kind"}
    assert SENTINEL not in response.text
    assert draft_of(deps).saved is False


def test_an_old_version_is_a_409_for_the_patch_too(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    first = read_draft(client)
    assert patch(client, [REPLACE_INTRO], first["version"]).status_code == 200
    current = draft_of(deps)

    response = patch(client, [{"op": "replace", "path": "/minutes", "value": 9}], first["version"])

    assert response.status_code == 409 and response.json()["code"] == "version_conflict"
    assert response.json()["current_version"] == current.token
    assert draft_of(deps).document["minutes"] == first["document"]["minutes"]


REPLACE_INTRO = {"op": "replace", "path": "/intro", "value": "Outra."}


def test_patching_the_draft_of_a_form_that_does_not_exist_is_a_404(
    api_client: Client, world: None
) -> None:
    response = patch(api_client("total"), [REPLACE_INTRO], "x", "nao-existe")

    assert response.status_code == 404 and "nao-existe" not in response.text


@pytest.mark.parametrize(
    ("operations", "field", "message"),
    [
        ([], "patch", "Informe pelo menos um item."),
        ([{"op": "apague", "path": "/intro"}], "patch.0.op", "Use um destes valores: 'add'"),
        ([{"path": "/intro"}], "patch.0.op", "Campo obrigatório."),
        ([{"op": "replace", "value": 1}], "patch.0.path", "Campo obrigatório."),
        ([{"op": "add", "path": "/intro"}], "patch.0", "Em `add`, informe `value`."),
        ([{"op": "remove", "path": "/intro", "value": 1}], "patch.0", "`remove` não leva `value`."),
        ([{"op": "move", "path": "/intro"}], "patch.0", "Em `move`, informe `from`."),
        (
            [{"op": "test", "path": "/intro", "from": "/x", "value": 1}],
            "patch.0",
            "`test` não leva `from`.",
        ),
        (
            [{"op": "remove", "path": "/intro", "motivo": "x"}],
            "patch.0.motivo",
            "Campo desconhecido.",
        ),
        ([{"op": "remove", "path": 7}], "patch.0.path", "Informe um texto."),
        ("replace", "patch", "Informe uma lista."),
        ([["replace"]], "patch.0", "Informe um objeto."),
    ],
)
def test_a_patch_that_is_not_in_the_shape_of_the_rfc_is_a_422_on_its_operation(
    api_client: Client, deps: Deps, world: None, operations: Any, field: str, message: str
) -> None:
    client = api_client("total")

    response = patch(client, operations, read_draft(client)["version"])

    errors = fields_of(response)
    assert field in errors and errors[field].startswith(message), errors
    assert draft_of(deps).saved is False


def test_a_null_value_is_a_value_in_a_patch(api_client: Client, world: None) -> None:
    client = api_client("total")
    before = read_draft(client)

    operations = [{"op": "replace", "path": "/instructions", "value": None}]

    response = patch(client, operations, before["version"])

    assert "instructions" in fields_of(response)  # o `null` chegou: é a compilação que o recusa


def test_a_patch_can_rename_the_form_until_its_first_publication(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    before = read_draft(client, SCRATCH)

    response = patch(
        client,
        [{"op": "replace", "path": "/slug", "value": "gestao-2027"}],
        before["version"],
        SCRATCH,
    )

    assert response.status_code == 200 and response.json()["slug"] == "gestao-2027"
    assert "gestao-2027" in forms_in_database(deps) and SCRATCH not in forms_in_database(deps)
    published = read_draft(client)
    refused = patch(
        client,
        [{"op": "replace", "path": "/slug", "value": "outro-endereco"}],
        published["version"],
    )
    assert "não muda depois da primeira publicação" in fields_of(refused)["slug"]


def test_a_patch_and_a_put_with_the_same_result_leave_the_same_state_and_audit(
    api_client: Client, world: None
) -> None:
    client = api_client("total")

    def with_the_patch() -> httpx.Response:
        before = read_draft(client)
        return patch(client, [REPLACE_INTRO], before["version"])

    def with_the_put() -> httpx.Response:
        before = read_draft(client)
        return put(client, {**before["document"], "intro": "Outra."}, before["version"])

    patched, replaced = assert_same_effect(
        with_the_patch, with_the_put, api_actor=client.actor, page_actor=client.actor, ignore=IGNORE
    )

    assert patched.status_code == replaced.status_code == 200
    assert patched.json()["document"] == replaced.json()["document"]


def test_a_patch_leaves_the_same_state_and_audit_as_editing_and_saving_on_the_page(
    api_client: Client, world: None
) -> None:
    client = api_client("total", ADMIN_USERNAME="pagina")

    def on_the_page(fields: dict[str, Any]) -> None:
        fields["intro"] = "Outra."

    page, api = assert_same_effect(
        page_post(client, DRAFT, on_the_page, EDITOR_FORM),
        lambda: patch(client, [REPLACE_INTRO], read_draft(client)["version"]),
        api_actor=client.actor,
        page_actor="pagina",
        ignore=IGNORE,
    )

    assert page.status_code == 303 and api.status_code == 200


def test_a_patch_that_would_grow_the_document_past_what_a_put_takes_is_the_problem_of_its_operation(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    before = read_draft(client)
    copies = [{"op": "copy", "from": "/steps", "path": "/caixa"}]
    copies += [
        {"op": "copy", "from": "/caixa", "path": "/caixa/-"}
    ] * 12  # a caixa dobra a cada uma

    response = patch(client, copies, before["version"])

    errors = fields_of(response)
    assert len(errors) == 1, errors
    (field,) = errors
    assert field.startswith("patch.") and draft_patch.TOO_BIG in errors[field]
    assert draft_of(deps).saved is False


def test_an_index_with_a_leading_zero_is_a_422_on_its_operation(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    before = read_draft(client)

    response = patch(
        client, [{"op": "add", "path": "/steps/01", "value": NEW_STEP}], before["version"]
    )

    assert fields_of(response) == {"patch.0": draft_patch.INVALID}
    assert draft_of(deps).saved is False


def test_a_patch_cannot_write_the_consent_version_but_the_put_ignores_it(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    before = read_draft(client)

    refused = patch(
        client,
        [{"op": "replace", "path": "/consent/version", "value": "inventada-v9"}],
        before["version"],
    )
    allowed = patch(
        client,
        [
            {"op": "test", "path": "/consent/version", "value": CONSENT_VERSION},
            {"op": "replace", "path": "/consent/notice", "value": "Outro aviso."},
        ],
        before["version"],
    )

    assert fields_of(refused) == {"patch.0": draft_patch.SYSTEM_PATH}
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["document"]["consent"]["version"] == CONSENT_VERSION
    ignored = put(
        client,
        {**before["document"], "consent": {**before["document"]["consent"], "version": "x-v9"}},
        allowed.json()["version"],
    )
    assert ignored.status_code == 200 and (
        ignored.json()["document"]["consent"]["version"] == CONSENT_VERSION
    )


def lists_inside_lists(levels: int) -> Any:
    value: Any = []
    for _ in range(levels - 1):
        value = [value]
    return value


def test_a_value_nested_past_64_levels_is_a_422_on_its_operation(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    version = read_draft(client)["version"]

    at_the_limit = patch(
        client, [{"op": "add", "path": "/x", "value": lists_inside_lists(64)}], version
    )
    past_it = patch(client, [{"op": "add", "path": "/x", "value": lists_inside_lists(65)}], version)

    assert "patch.0.value" not in fields_of(at_the_limit)  # o campo `x` é que a compilação recusa
    assert fields_of(past_it) == {
        "patch.0.value": "O valor passa de 64 níveis de listas e objetos."
    }
    assert draft_of(deps).saved is False


def deep_body(operation_id: str, version: str, depth: int) -> str:
    """O corpo de uma operação com listas dentro de listas, `depth` níveis, escrito à mão (o JSON
    do Python também não o escreve)."""
    deep = "[" * depth + "]" * depth
    return {
        "create_form": f'{{"title":"x","source":{deep}}}',
        "import_form": f'{{"confirm":true,"definition":{{"a":{deep}}}}}',
        "save_form_draft": f'{{"expected_version":"{version}","document":{{"a":{deep}}}}}',
        "patch_form_draft": (
            f'{{"expected_version":"{version}","patch":[{{"op":"add","path":"/a","value":{deep}}}]}}'
        ),
    }[operation_id]


@pytest.mark.parametrize("depth", [100, 700, 5000, 100_000])
@pytest.mark.parametrize("operation_id", WRITES)
def test_a_body_nested_to_any_depth_is_a_4xx_in_the_error_format_and_never_a_500(
    api_client: Client, deps: Deps, world: None, operation_id: str, depth: int
) -> None:
    """Listas dentro de listas passam do que o `json`, o Pydantic e o compilador percorrem: a
    recusa é sempre um `4xx` no formato da API, no caminho de cada operação (o `confirm` lê o corpo
    antes de todo mundo)."""
    client = api_client("total")
    method, url, _ = REQUESTS[operation_id]()
    body = deep_body(operation_id, read_draft(client)["version"], depth)
    assert len(body) < MAX_BODY_BYTES

    response = client.request(
        method, url, content=body, headers={"Content-Type": "application/json"}
    )

    assert 400 <= response.status_code < 500, (operation_id, depth, response.status_code)
    assert response.json()["code"] in {"validation_failed", "confirmation_required"}
    assert draft_of(deps).saved is False


def test_a_definition_the_json_module_cannot_walk_is_a_422_on_the_whole_document(
    api_client: Client, deps: Deps, world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entre o que o `json.loads` aceita e o que o `json.dumps` percorre há uma faixa de aninhamento
    (a pilha já tem os quadros do servidor): a importação reserializa a definição, e o estouro dela é
    uma recusa, e não um 500."""

    def overflow(*args: Any, **kwargs: Any) -> str:
        raise RecursionError

    monkeypatch.setattr("app.admin.api.forms.json", SimpleNamespace(dumps=overflow))

    response = import_it(api_client("total"), importable())

    assert response.status_code == 422 and response.json()["errors"][0]["field"] == ""
    assert "listas ou objetos demais" in response.json()["errors"][0]["message"]
    assert IMPORTED not in forms_in_database(deps)


def test_the_description_of_the_patch_says_what_a_removal_and_an_addition_need() -> None:
    from app.admin.api.openapi import build

    operation = build()["paths"]["/forms/{slug}/draft"]["patch"]
    description = operation["description"]

    assert operation["operationId"] == "patch_form_draft"
    for needed in (
        "add /examples/answers/<nome>",
        "remove` ou `replace` em `/examples/answers/<nome>",
        "add /examples/plan/<nome>",
        "1 == true",
        "/consent/version",
        "20 delas `copy`",
        "64 níveis",
        "255 KiB",
    ):
        assert needed in description, needed


def accented_document_of(size: int) -> dict[str, Any]:
    """O documento do demo com um comentário de `é` no prompt, de modo que o JSON do `json.dumps`
    padrão (acentos escapados, espaço depois de `,` e `:`) tem `size` bytes ou pouco menos."""
    plain = document()
    spare = (
        size - len(json.dumps(plain)) - len(json.dumps({"p": "{##}"})) + len(json.dumps({"p": ""}))
    )
    return {**plain, "prompt": plain["prompt"] + "{#" + "é" * (spare // 6) + "#}"}


def test_a_document_at_the_limit_of_a_patch_goes_back_in_a_put_written_by_json_dumps(
    api_client: Client, deps: Deps, world: None
) -> None:
    """O documento que um `PATCH` deixa precisa caber num `PUT`. O limite é medido com o `json.dumps`
    padrão (acentos escapados, espaços): o corpo de um `PUT` assim, com o envelope, cabe nos 256 KiB."""
    client = api_client("total")
    before = read_draft(client)
    at_the_limit = accented_document_of(draft_patch.MAX_DOCUMENT_BYTES)
    assert (
        draft_patch.MAX_DOCUMENT_BYTES - 10 * 6
        < len(json.dumps(at_the_limit))
        <= (draft_patch.MAX_DOCUMENT_BYTES)
    )
    body = json.dumps({"expected_version": before["version"], "document": at_the_limit})
    assert len(body.encode()) <= MAX_BODY_BYTES

    saved = client.put(
        url_of("save_form_draft", slug=SLUG),
        content=body,
        headers={"Content-Type": "application/json"},
    )
    grown = patch(
        client,
        [{"op": "replace", "path": "/prompt", "value": at_the_limit["prompt"] + "é" * 200}],
        saved.json()["version"],
    )

    assert saved.status_code == 200, saved.text
    assert grown.status_code == 422
    assert [e["field"] for e in grown.json()["errors"]] == ["patch"]
    assert draft_patch.TOO_BIG in grown.json()["errors"][0]["message"]
    assert draft_of(deps).document == at_the_limit  # o patch que passa do limite não gravou


def test_the_patch_is_limited_to_a_thousand_operations(api_client: Client, world: None) -> None:
    client = api_client("total")
    operations = [{"op": "test", "path": "/minutes", "value": 3}] * 1001

    response = patch(client, operations, read_draft(client)["version"])

    assert (
        "patch" in fields_of(response)
        and "Use no máximo 1000 itens." in fields_of(response)["patch"]
    )


# --- as 42 operações do editor, pela API --------------------------------------------------------


def complete_patch(case: Case, initial: dict[str, Any]) -> list[dict[str, Any]]:
    """O patch do caso e o que mais o documento precisa para compilar."""
    return [*case.patch, *(case.complete(initial) if case.complete is not None else [])]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.op)
def test_the_complete_patch_of_every_editor_operation_is_accepted_and_stores_that_document(
    api_client: Client, deps: Deps, case: Case
) -> None:
    """A conferência do `PATCH` é a do salvar: o patch de cada operação do editor, com o que o documento
    precisa para compilar (o prompt que citava a pergunta que sai, os mínimos de dois níveis trocados),
    dá `200` e grava exatamente o documento que sai dele. Os 53 casos são aceitos."""
    initial = base()
    if case.tweak is not None:
        case.tweak(initial)
    stored.import_definition(deps, json.dumps(initial), actor="testes", contact_confirmed=True)
    client = api_client("total")
    before = read_draft(client, initial["slug"])
    operations = complete_patch(case, initial)
    expected = draft_patch.apply(initial, operations)
    compile_definition(expected)  # o patch completo é um documento válido

    response = patch(client, operations, before["version"], initial["slug"])

    assert response.status_code == 200, response.text
    assert response.json()["document"] == expected


def test_a_patch_that_leaves_the_prompt_citing_a_question_that_left_is_a_422_with_the_line(
    api_client: Client, deps: Deps
) -> None:
    """O que o patch completo acrescenta é o que o `PATCH` cobra: sem ele, o documento não compila e
    nada é gravado."""
    case = next(case for case in CASES if case.op == "remove-step:2")
    initial = base()
    stored.import_definition(deps, json.dumps(initial), actor="testes", contact_confirmed=True)
    client = api_client("total")
    before = read_draft(client, initial["slug"])

    response = patch(client, case.patch, before["version"], initial["slug"])

    assert fields_of(response) == {"prompt": "labels.resultado não é uma pergunta de escolha"}
    assert response.json()["errors"][0]["line"] == 9
    assert draft_of(deps, initial["slug"]).saved is False


# --- create_form --------------------------------------------------------------------------------

SOURCE_FIELDS = {"slug", "title", "active", "published_version", "draft", "versions"}


def test_create_form_makes_the_form_without_a_version_and_its_draft_from_the_starter(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")

    response = create(client, title="Diagnóstico de gestão", slug="diagnostico-de-gestao")

    assert response.status_code == 201, response.text
    body = response.json()
    assert SOURCE_FIELDS <= set(body)
    assert (body["slug"], body["title"]) == ("diagnostico-de-gestao", "Diagnóstico de gestão")
    assert body["published_version"] is None and body["versions"] == []
    assert body["draft"]["pending"] is True and body["draft"]["current_version"] is None
    draft = draft_of(deps, "diagnostico-de-gestao")
    assert draft.current is None and draft.document["title"] == "Diagnóstico de gestão"
    assert draft.document["slug"] == "diagnostico-de-gestao"
    assert body["draft"]["version"] == draft.token
    # Sem versão publicada, a lead não vê nada.
    assert client.get("/p/diagnostico-de-gestao").status_code == 404
    (entry,) = by_a_key(deps, "form.create")
    assert entry.actor == client.actor and entry.details["form_slug"] == "diagnostico-de-gestao"


def test_a_blank_address_comes_from_the_title(api_client: Client, world: None) -> None:
    client = api_client("total")

    response = create(client, title="Diagnóstico: Gestão & Equipe do Escritório")

    assert response.status_code == 201
    assert response.json()["slug"] == "diagnostico-gestao-equipe-do-escritorio"


def test_create_form_copies_another_form_when_asked(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")

    response = create(client, title="Outra gestão", slug="outra-gestao", source=SLUG)

    assert response.status_code == 201, response.text
    copy_of = draft_of(deps, "outra-gestao").document
    original = draft_of(deps).document
    assert copy_of["title"] == "Outra gestão" and copy_of["slug"] == "outra-gestao"
    assert copy_of["consent"]["version"] == "outra-gestao-v1"
    assert copy_of["steps"] == original["steps"] and copy_of["prompt"] == original["prompt"]
    (entry,) = by_a_key(deps, "form.duplicate")
    assert entry.details["source"] == SLUG and entry.details["source_version"] == 1


@pytest.mark.parametrize(
    ("body", "field", "message"),
    [
        ({"title": "   "}, "title", "dê um título ao formulário"),
        ({"title": "Novo", "slug": "demo"}, "slug", "já existe um formulário com este endereço"),
        ({"title": "Novo", "slug": "default"}, "slug", "este endereço é reservado"),
        ({"title": "Novo", "slug": "import"}, "slug", "este endereço é reservado"),
        ({"title": "Novo", "slug": "Gestão 2027"}, "slug", "use letras minúsculas"),
        (
            {"title": "Novo", "source": "nao-existe"},
            "source",
            "formulário de origem não encontrado",
        ),
    ],
)
def test_create_form_refuses_with_the_field_and_creates_nothing(
    api_client: Client, deps: Deps, world: None, body: dict[str, Any], field: str, message: str
) -> None:
    before = forms_in_database(deps)

    response = create(api_client("total"), **body)

    assert message in fields_of(response)[field]
    assert forms_in_database(deps) == before and by_a_key(deps, "form.create") == []


def test_create_form_says_every_field_that_is_wrong(api_client: Client, world: None) -> None:
    response = create(api_client("total"), title=" ", slug="demo", source="nao-existe")

    assert fields_of(response).keys() == {"title", "slug", "source"}


def test_create_form_cannot_copy_a_form_that_no_longer_compiles(
    api_client: Client, deps: Deps, world: None
) -> None:
    store_version(deps, 2, document(minutes=0))

    response = create(api_client("total"), title="Cópia", slug="copia", source=SLUG)

    assert "não compila" in fields_of(response)["source"]


def test_create_form_has_no_confirm_and_no_unknown_field(api_client: Client, world: None) -> None:
    response = create(api_client("total"), title="Novo", confirm=True, motivo="x")

    assert fields_of(response).keys() == {"confirm", "motivo"}


def test_the_same_form_twice_is_a_422_on_the_second_call(api_client: Client, world: None) -> None:
    client = api_client("total")
    assert create(client, title="Novo", slug="novo-form").status_code == 201

    again = create(client, title="Novo", slug="novo-form")

    assert "já existe um formulário" in fields_of(again)["slug"]


def test_create_form_leaves_the_same_state_and_audit_as_the_new_form_page(
    api_client: Client, world: None
) -> None:
    client = api_client("total", ADMIN_USERNAME="pagina")
    form = {"title": "Diagnóstico de gestão", "slug": "diagnostico-de-gestao", "source": ""}

    def on_the_page() -> httpx.Response:
        assert login(client, "pagina").status_code == 303
        return post(client, f"{PAGE}/forms/new", **form)

    page, api = assert_same_effect(
        on_the_page,
        lambda: create(client, **form),
        api_actor=client.actor,
        page_actor="pagina",
        ignore=IGNORE_NEW,
    )

    assert page.status_code == 303 and api.status_code == 201


def test_duplicating_leaves_the_same_state_and_audit_as_the_new_form_page(
    api_client: Client, world: None
) -> None:
    client = api_client("total", ADMIN_USERNAME="pagina")
    form = {"title": "Outra gestão", "slug": "outra-gestao", "source": SLUG}

    def on_the_page() -> httpx.Response:
        assert login(client, "pagina").status_code == 303
        return post(client, f"{PAGE}/forms/new", **form)

    page, api = assert_same_effect(
        on_the_page,
        lambda: create(client, **form),
        api_actor=client.actor,
        page_actor="pagina",
        ignore=IGNORE_NEW,
    )

    assert page.status_code == 303 and api.status_code == 201


# --- import_form --------------------------------------------------------------------------------

IMPORTED = "importado-pela-api"


def importable(slug: str = IMPORTED, **changes: Any) -> dict[str, Any]:
    found = document(slug=slug, **changes)
    found["consent"] = {**found["consent"], "version": f"{slug}-v1"}
    return found


def test_import_form_without_confirm_is_a_422_and_nothing_happens(
    api_client: Client, deps: Deps, world: None
) -> None:
    before = forms_in_database(deps)

    response = api_client("total").post(url_of("import_form"), json={"definition": importable()})

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert "publica já a versão 1" in response.json()["detail"]
    assert forms_in_database(deps) == before and by_a_key(deps, "form.import") == []


def test_import_form_publishes_version_1_and_audits_the_import(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")

    response = import_it(client, importable(), note="Vinda de outro ambiente.")

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["slug"], body["published_version"], body["active"]) == (IMPORTED, 1, True)
    assert [version["note"] for version in body["versions"]] == ["Vinda de outro ambiente."]
    assert [version["published_by"] for version in body["versions"]] == [client.actor]
    assert client.get(f"/p/{IMPORTED}").status_code == 200  # no ar
    (entry,) = by_a_key(deps, "form.import")
    assert entry.actor == client.actor and entry.details["form_slug"] == IMPORTED


def test_the_default_note_of_an_import(api_client: Client, world: None) -> None:
    response = import_it(api_client("total"), importable())

    assert [v["note"] for v in response.json()["versions"]] == ["Importação da definição."]


def test_a_prompt_that_sends_contact_data_needs_confirm_contact(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    definition = importable(prompt=json.loads(CONTACT_PROMPT)["prompt"])

    asked = import_it(client, definition)

    assert asked.status_code == 422 and asked.json()["code"] == "confirmation_required"
    assert "o nome e o e-mail" in asked.json()["detail"]
    assert "confirm_contact" in asked.json()["detail"] and "errors" not in asked.json()
    assert IMPORTED not in forms_in_database(deps) and by_a_key(deps, "form.import") == []
    confirmed = import_it(client, definition, confirm_contact=True)
    assert confirmed.status_code == 201, confirmed.text
    (entry,) = by_a_key(deps, "form.import")
    assert entry.details["contact_in_prompt"] == ["name", "email"]


def test_confirm_contact_only_counts_when_the_prompt_cites_contact_data(
    api_client: Client, deps: Deps, world: None
) -> None:
    response = import_it(api_client("total"), importable(), confirm_contact=True)

    assert response.status_code == 201
    (entry,) = by_a_key(deps, "form.import")
    assert "contact_in_prompt" not in entry.details


def test_a_definition_that_does_not_compile_is_a_422_with_every_path_and_nothing_is_stored(
    api_client: Client, deps: Deps, world: None
) -> None:
    broken = importable(minutes=f"x{SENTINEL}")
    broken["steps"][0]["title"] = ""
    broken["pdf"] = {**broken["pdf"], "html": "<p>\n{% if %}</p>"}

    response = import_it(api_client("total"), broken)

    errors = response.json()["errors"]
    assert response.status_code == 422 and {e["field"] for e in errors} >= {
        "minutes",
        "steps.0.title",
    }
    assert SENTINEL not in response.text
    assert IMPORTED not in forms_in_database(deps) and by_a_key(deps, "form.import") == []


def test_a_definition_that_does_not_compile_is_refused_before_the_contact_question(
    api_client: Client, world: None
) -> None:
    definition = importable(minutes=0, prompt=json.loads(CONTACT_PROMPT)["prompt"])

    response = import_it(api_client("total"), definition)

    assert response.json()["code"] == "validation_failed"


@pytest.mark.parametrize("slug", ["default", "import", "new", "demo", SLUG])
def test_import_form_refuses_a_reserved_or_taken_address(
    api_client: Client, deps: Deps, world: None, slug: str
) -> None:
    before = forms_in_database(deps)

    response = import_it(api_client("total"), importable(slug=slug))

    assert fields_of(response).keys() == {"slug"}
    assert forms_in_database(deps) == before


@pytest.mark.parametrize("shape", [[], None, "texto", 7], ids=["list", "null", "text", "number"])
def test_import_form_needs_an_object(api_client: Client, world: None, shape: Any) -> None:
    response = api_client("total").post(
        url_of("import_form"), json={"definition": shape, "confirm": True}
    )

    assert response.status_code == 422
    assert [item["field"] for item in response.json()["errors"]] == [""]


def test_import_form_takes_the_note_up_to_300_characters(api_client: Client, world: None) -> None:
    response = import_it(api_client("total"), importable(), note="n" * 301)

    assert "Use no máximo 300 caracteres." in fields_of(response)["note"]


def test_import_form_leaves_the_same_state_and_audit_as_the_import_page(
    api_client: Client, world: None
) -> None:
    client = api_client("total", ADMIN_USERNAME="pagina")
    definition = importable()

    def on_the_page() -> httpx.Response:
        assert login(client, "pagina").status_code == 303
        return import_form(client, json.dumps(definition), note="Uma nota.")

    page, api = assert_same_effect(
        on_the_page,
        lambda: import_it(client, definition, note="Uma nota."),
        api_actor=client.actor,
        page_actor="pagina",
        ignore=IGNORE_NEW,
    )

    assert page.status_code == 303 and api.status_code == 201


def test_a_confirmed_contact_import_leaves_the_same_audit_as_the_page(
    api_client: Client, world: None
) -> None:
    client = api_client("total", ADMIN_USERNAME="pagina")
    definition = importable(prompt=json.loads(CONTACT_PROMPT)["prompt"])

    def on_the_page() -> httpx.Response:
        assert login(client, "pagina").status_code == 303
        return import_form(client, json.dumps(definition), confirm_contact=True)

    page, api = assert_same_effect(
        on_the_page,
        lambda: import_it(client, definition, confirm_contact=True),
        api_actor=client.actor,
        page_actor="pagina",
        ignore=IGNORE_NEW,
    )

    assert page.status_code == 303 and api.status_code == 201


# --- o limite do corpo --------------------------------------------------------------------------


def test_a_body_over_the_limit_is_a_413_and_the_mcc_of_100_kb_goes_through(
    api_client: Client, deps: Deps
) -> None:
    client = api_client("total")
    mcc = read_draft(client, "mcc")  # o MCC, de cerca de 100 KB, migrado como versão 1
    # Corpo grande sintético, mantendo o aceite do limite sem catálogo comercial.
    mcc["document"]["prompt"] += "\n{# " + "padding sintético " * 1600 + " #}"
    size = len(json.dumps(mcc["document"]).encode())
    assert 80_000 < size < MAX_BODY_BYTES * 0.75  # o arquivo tem 100 KB indentado; o corpo, 86 KB

    saved = put(client, mcc["document"], mcc["version"], "mcc")
    imported = import_it(client, {**mcc["document"], "slug": "mcc-copia"}, confirm_contact=True)
    too_big = put(client, {**mcc["document"], "intro": "x" * MAX_BODY_BYTES}, mcc["version"], "mcc")

    assert saved.status_code == 200, saved.text
    assert imported.status_code == 201, imported.text
    assert too_big.status_code == 413 and too_big.json()["code"] == "payload_too_large"
    assert draft_of(deps, "mcc").document == mcc["document"]


# --- o eco: o que se manda não volta num erro ---------------------------------------------------


def test_no_error_of_the_draft_operations_gives_back_the_free_text_that_was_sent(
    api_client: Client, world: None
) -> None:
    """O eco geral do contrato para no `422` do Pydantic, antes de o serviço rodar. Aqui o texto livre
    de cada operação vai com o sentinela e o resto válido, e a recusa vem do serviço."""
    client = api_client("total")
    before = read_draft(client)
    version = before["version"]
    doc = before["document"]
    sentinel_pdf = {**doc["pdf"], "html": f"<p>{SENTINEL}</p>\n{{% if %}}"}

    refused = [
        create(client, title=SENTINEL, slug=SENTINEL),
        create(client, title=SENTINEL, source=SENTINEL),
        create(client, title=SENTINEL, slug="demo"),
        import_it(client, {**doc, "slug": SENTINEL}),
        import_it(client, {**doc, "title": SENTINEL, "minutes": 0}),
        import_it(client, {**doc, "pdf": sentinel_pdf}),
        put(client, {**doc, "slug": SENTINEL}, version),
        put(client, {**doc, "title": SENTINEL, "minutes": 0}, version),
        put(client, {**doc, "pdf": sentinel_pdf}, version),
        put(client, doc, SENTINEL),
        put(client, doc, version, SENTINEL),
        patch(client, [{"op": "remove", "path": f"/{SENTINEL}"}], version),
        patch(client, [{"op": "replace", "path": f"/{SENTINEL}", "value": SENTINEL}], version),
        patch(client, [{"op": "test", "path": "/title", "value": SENTINEL}], version),
        patch(client, [{"op": "move", "from": f"/{SENTINEL}", "path": "/title"}], version),
        patch(client, [{"op": "replace", "path": "/title", "value": SENTINEL}], SENTINEL),
        patch(
            client,
            [{"op": "replace", "path": "/pdf/html", "value": f"{SENTINEL}{{% if %}}"}],
            version,
        ),
        patch(client, [{"op": SENTINEL, "path": "/title"}], version),
        patch(client, [{"op": "remove", "path": "/title", "value": SENTINEL}], version),
    ]

    assert all(response.status_code >= 400 for response in refused)
    assert not [r.request.url for r in refused if SENTINEL in r.text], "um erro devolveu o texto"
