"""A lista e a leitura dos formulários no admin, pela costura HTTP (TestClient): todo formulário,
com o endereço público, a situação, a versão publicada e os envios; a leitura da versão publicada;
e a exportação da definição de uma versão."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime

import httpx
import pytest
import sqlalchemy as sa
from conftest import TABLES, Receiver, legacy_definition
from fastapi.testclient import TestClient
from test_admin import FakeClock, login, section
from test_admin_destinations import ADMIN_ENV, create
from test_admin_forms import (
    DEFINITION,
    SLUG,
    import_form,
    section_of,
    send_form,
    store_version,
)
from test_form_data_notices import (
    AFTER_BOTH,
    AFTER_OPTION,
    PUBLISHED,
    WHILE_OPEN,
    pin_reforma_2027,
    publish_version,
)
from test_intake import submit
from test_public import tags, text

from app.api.main import create_app
from app.deps import Deps
from app.settings import Settings


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(deps: Deps, clock: FakeClock) -> TestClient:
    """O navegador da operadora, já no admin, e o da lead: o mesmo app, sem o Hatchet."""
    deps.settings = Settings(_env_file=None, **ADMIN_ENV)  # type: ignore[call-arg]

    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return "run-fake"

    current = TestClient(create_app(trigger=fake_trigger, clock=clock), follow_redirects=False)
    assert login(current).status_code == 303
    return current


def rows_of(response: httpx.Response) -> dict[str, str]:
    """As linhas da tabela de formulários, pelo slug, como a operadora as lê."""
    body = re.search(r"<tbody>(.*?)</tbody>", response.text, re.DOTALL)
    assert body is not None, "a página não mostrou a tabela de formulários"
    rows = re.findall(r"<tr>(.*?)</tr>", body.group(1), re.DOTALL)
    found = {}
    for row in rows:
        slug = re.search(r'<span class="mono">([a-z0-9-]+)</span>', row)
        assert slug is not None, row
        found[slug.group(1)] = row
    return found


def test_the_list_shows_every_form_with_its_public_address(
    client: TestClient,
) -> None:
    assert import_form(client, DEFINITION).status_code == 303

    page = client.get("/admin/forms")

    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    rows = rows_of(page)
    assert list(rows) == ["demo", SLUG, "mcc", "reforma-2027"]
    titles = {
        "demo": "Projeto de demonstração",
        "mcc": "Planejamento de atividades",
        "reforma-2027": "Calendário de atividades",
        SLUG: "Projeto de demonstração",
    }
    for slug, title in titles.items():
        assert title in text(rows[slug]), slug
        assert f"http://testserver/p/{slug}" in text(rows[slug])  # o endereço inteiro, à vista
        assert tags(rows[slug], "a", href=f"http://testserver/p/{slug}")
        assert tags(rows[slug], "a", href=f"/admin/forms/{slug}")
    assert tags(page.text, "a", href="/admin/forms/import")
    (menu,) = [link for link in tags(page.text, "a") if link.get("aria-current") == "page"]
    assert menu["href"] == "/admin/forms"


def test_each_row_tells_the_status_the_published_version_and_the_submissions(
    client: TestClient, deps: Deps
) -> None:
    import_form(client, DEFINITION)
    store_version(deps, 2, {**json.loads(DEFINITION), "intro": "A segunda versão."})
    send_form(client)
    send_form(client, _idem="outro-envio")
    submit(deps)  # um envio ao demo

    rows = {slug: text(row) for slug, row in rows_of(client.get("/admin/forms")).items()}

    assert all("Ativo" in row for row in rows.values())
    assert "Versão 2" in rows[SLUG] and "2 respostas" in rows[SLUG]
    assert (
        "Versão 1" in rows["demo"] and "1 resposta" in rows["demo"] and "0 respostas" in rows["mcc"]
    )
    # O total abre os dois históricos, preservando o formulário.
    raw = rows_of(client.get("/admin/forms"))
    assert tags(raw["demo"], "a", href="/admin/responses?form=demo")
    assert tags(raw[SLUG], "a", href=f"/admin/responses?form={SLUG}")


def test_without_javascript_the_address_is_a_link_and_the_copy_button_stays_hidden(
    client: TestClient,
) -> None:
    page = client.get("/admin/forms").text

    buttons = [button for button in tags(page, "button") if "data-copy" in button]
    assert len(buttons) == 3 and all("hidden" in button for button in buttons)
    for button in buttons:  # cada botão copia o endereço da linha dele
        (address,) = tags(page, "a", id=str(button["data-copy"]))
        assert str(address["href"]).startswith("http://testserver/p/")
    (admin_script,) = [
        s for s in tags(page, "script") if str(s.get("src")).startswith("/static/js/admin.js")
    ]
    script = client.get(str(admin_script["src"]))  # o endereço com a versão serve o arquivo
    assert script.status_code == 200 and "clipboard" in script.text


def in_order(shown: str, *parts: str) -> None:
    """Cada trecho aparece depois do anterior."""
    position = 0
    for part in parts:
        found = shown.find(part, position)
        assert found >= 0, f"{part!r} não aparece depois do trecho anterior"
        position = found + len(part)


def reading_definition() -> str:
    """A definição do demo com o que a leitura também precisa mostrar: dados do formulário, lidos
    pelas instruções, e um campo interno no plano."""
    document = json.loads(DEFINITION)
    document["data"] = {"edicao": {"kind": "value", "value": {"nome": "Ciclo demonstrativo 2027"}}}
    document["instructions"] += "Cite a edição {{ data.edicao.nome }} quando fizer sentido.\n"
    document["plan"]["fields"].insert(
        0, {"name": "rascunho_do_time", "kind": "text", "internal": True, "max_length": 300}
    )
    document["examples"]["plan"]["rascunho_do_time"] = "Anotação interna de exemplo."
    return json.dumps(document, ensure_ascii=False)


def test_the_reading_shows_everything_in_the_published_version_of_an_imported_form(
    client: TestClient,
) -> None:
    assert import_form(client, reading_definition()).status_code == 303

    page = client.get(f"/admin/forms/{SLUG}")

    assert page.status_code == 200
    html = page.text
    in_order(
        text(section(html, "pagina")),
        "Conte como você organiza seu projeto fictício",
        "Vamos começar por você",
        "Nome completo",
        "nome",
        "texto · obrigatória · entre 2 e 120 caracteres · dado de contato",
        "Nome do escritório",
        "opcional",
        "Seu escritório hoje",
        "Quantas pessoas trabalham no escritório, contando com você?",
        "escolha única",
        "2_a_5",
        "De 2 a 5 pessoas",
        "Seus próximos 90 dias",
        "texto longo",
    )
    in_order(text(section(html, "identidade")), "E-mail", "email", "Nome", "Nome completo", "nome")
    assert "Telefone não coletado" in text(section(html, "identidade"))
    in_order(
        text(section(html, "consentimento")),
        "demo-definicao-v1",
        "Concordo com o uso das minhas respostas para gerar o meu plano de ação.",
        "Usamos suas respostas para montar o seu plano.",
        "Quero receber dicas e novidades da Leadkit por e-mail.",
    )
    assert "Cite a edição Ciclo demonstrativo 2027 quando fizer sentido." in text(
        section(html, "instrucoes")
    )
    assert "{{ answers.tamanho_equipe }}" in text(section(html, "prompt"))
    in_order(
        text(section(html, "modelo-do-plano")),
        "rascunho_do_time",
        "interno",
        "titulo",
        "Entre 3 e 120 caracteres",
        "acoes",
        "lista de textos",
        "Entre 3 e 5 itens",
        "proximo_passo",
    )
    assert "Ciclo demonstrativo 2027" in text(section(html, "dados"))
    in_order(
        text(section(html, "templates")),
        "<h1>{{ plan.titulo }}</h1>",
        "ol.actions li",
        "Para começar esta semana",
    )
    in_order(
        text(section(html, "exemplos")),
        "Contadora Exemplo",
        "Organizar atividades do projeto",
    )


def test_the_admin_names_a_form_alike_and_the_reading_shows_the_title_the_lead_sees(
    client: TestClient, deps: Deps
) -> None:
    import_form(client, DEFINITION)
    # Gravada por fora da publicação, que mantém o nome do formulário igual ao título da versão.
    store_version(deps, 2, {**json.loads(DEFINITION), "title": "Outro título na versão 2"})

    row = text(rows_of(client.get("/admin/forms"))[SLUG])
    page = client.get(f"/admin/forms/{SLUG}").text

    (heading,) = re.findall(r"<h1>(.*?)</h1>", page)
    assert heading == "Projeto de demonstração"
    assert "Projeto de demonstração" in row and "Outro título" not in row
    assert "Título Outro título na versão 2" in text(section(page, "pagina"))


def test_the_reading_takes_a_definition_without_the_optional_parts(client: TestClient) -> None:
    document = json.loads(DEFINITION)
    del document["data"]  # opcionais: os dados e o CSS do PDF
    del document["pdf"]["css"]
    assert import_form(client, json.dumps(document, ensure_ascii=False)).status_code == 303

    page = client.get(f"/admin/forms/{SLUG}")

    assert page.status_code == 200
    assert "Nenhum." in text(section(page.text, "dados"))
    assert "PDF (CSS)" not in text(section(page.text, "templates"))


def first_line(text_of_template: str) -> str:
    return text_of_template.splitlines()[0]


def test_the_reading_of_a_migrated_form_shows_its_published_version_one(
    client: TestClient,
) -> None:
    """O reforma-2027 viveu em código e hoje é a versão 1 de um formulário do banco."""
    definition = legacy_definition("reforma-2027")

    page = client.get("/admin/forms/reforma-2027")

    assert page.status_code == 200
    html = page.text
    in_order(
        text(section(html, "pagina")),
        "Conte como você organiza seu projeto fictício",
        "Vamos começar por você",
        "WhatsApp",
        "whatsapp",
        "telefone",
        "opcional",
        "dado de contato",
        "No campo vazio: (11) 91234-5678",
    )
    in_order(
        text(section(html, "identidade")),
        "E-mail",
        "E-mail (email)",
        "Nome",
        "Nome completo (nome)",
        "Telefone",
        "WhatsApp (whatsapp)",
    )
    assert "reforma-2027-v1" in text(section(html, "consentimento"))
    assert first_line(definition["instructions"]) in text(section(html, "instrucoes"))
    prompt = text(section(html, "prompt"))
    assert first_line(definition["prompt"]) in prompt and "reforma-2027-v1" in prompt
    in_order(
        text(section(html, "modelo-do-plano")),
        "uso_da_janela",
        "interno",
        "fases",
        "lista de grupos",
        "Exatamente 3 itens",
        "acoes",
        "tipo",
        "opções fixas",
        "decisao_da_opcao",
        "Conferência do plano",
        "garantia",
        "As janelas vêm do bloco janelas",
    )
    # O JSONB guarda as chaves por tamanho e depois em ordem alfabética: `opcao` vem antes de `janelas`.
    in_order(text(section(html, "dados")), "opcao", "referencia", "janelas", "Revisão da inscrição")
    in_order(
        text(section(html, "templates")),
        first_line(definition["pdf"]["html"]),
        first_line(definition["pdf"]["css"]),
        first_line(definition["preview"]),
    )
    examples = text(section(html, "exemplos"))
    assert "Respostas de exemplo" in examples and "Plano de exemplo" in examples
    version = text(section(html, "versao"))
    assert "Versão 1 · reforma-2027-v1" in version
    assert "fixture" in version and "Cenário fictício de teste" in version
    assert tags(section(html, "versao"), "a", href=export_path(1, "reforma-2027"))


def test_the_reading_tells_where_the_plans_go_how_long_the_data_stays_and_the_submissions(
    client: TestClient, deps: Deps, receiver: Receiver
) -> None:
    deps.settings = Settings(  # type: ignore[call-arg]
        _env_file=None, RETENTION_PDF_DAYS="45", RETENTION_DAYS="90", **ADMIN_ENV
    )
    import_form(client, DEFINITION)
    send_form(client)

    page = client.get(f"/admin/forms/{SLUG}").text

    destination = section(page, "destino")
    assert "Sem destino do webhook: nenhum evento é enviado por webhook" in text(destination)
    assert tags(destination, "a", href=f"/admin/destinations/forms/{SLUG}")
    in_order(
        text(section(page, "retencao")),
        "PDF 45 dias depois de gerado · padrão do ambiente",
        "Pedido inteiro 90 dias depois da última mudança · padrão do ambiente",
    )
    assert "Envios 1 envio" in text(section(page, "situacao"))
    create(client, f"/admin/destinations/forms/{SLUG}", receiver.url("/proprio"))
    destination = text(section(client.get(f"/admin/forms/{SLUG}").text, "destino"))
    assert f"Destino efetivo: o destino próprio ({receiver.host})" in destination


def test_a_form_that_does_not_exist_has_no_page(client: TestClient) -> None:
    response = client.get("/admin/forms/nao-existe")

    assert response.status_code == 404 and "Formulário não encontrado" in text(response.text)


def export_path(number: int, slug: str = SLUG) -> str:
    return f"/admin/forms/{slug}/versions/{number}/export"


def test_a_stored_version_is_exported_as_a_json_file_that_imports_in_another_environment(
    client: TestClient, deps: Deps
) -> None:
    import_form(client, DEFINITION)
    store_version(deps, 2, {**json.loads(DEFINITION), "intro": "A segunda versão."})
    assert tags(
        section(client.get(f"/admin/forms/{SLUG}").text, "versao"), "a", href=export_path(2)
    )

    first = client.get(export_path(1))

    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    assert first.headers["content-type"] == "application/json"
    assert first.headers["content-disposition"] == f'attachment; filename="{SLUG}-v1.json"'
    assert json.loads(first.content) == json.loads(DEFINITION)  # a ordem das chaves não conta
    assert json.loads(client.get(export_path(2)).content)["intro"] == "A segunda versão."
    with deps.sessions.begin() as session:  # outro ambiente: um banco sem o formulário
        session.execute(sa.text(f"TRUNCATE {TABLES} CASCADE"))
    assert import_form(client, first.text).status_code == 303
    assert "Conte como você organiza seu projeto fictício" in text(
        client.get(f"/admin/forms/{SLUG}").text
    )


@pytest.mark.parametrize(
    "path",
    [
        export_path(3),
        export_path(1, "nao-existe"),
        f"/admin/forms/{SLUG}/versions/um/export",
    ],
    ids=["unknown-version", "unknown-form", "not-a-number"],
)
def test_only_a_published_version_has_a_definition_to_export(client: TestClient, path: str) -> None:
    import_form(client, DEFINITION)

    response = client.get(path)

    assert response.status_code == 404 and "definição" in text(response.text)


def test_the_form_pages_and_the_export_need_the_session(client: TestClient) -> None:
    import_form(client, DEFINITION)
    anonymous = TestClient(client.app, follow_redirects=False)

    responses = [
        anonymous.get(path)
        for path in ("/admin/forms", f"/admin/forms/{SLUG}", "/admin/forms/demo", export_path(1))
    ]

    for response in responses:
        assert response.status_code == 303, response.url
        assert response.headers["location"].startswith("/admin/login"), response.url


def test_a_published_version_that_no_longer_compiles_is_flagged_and_still_exports(
    client: TestClient, deps: Deps
) -> None:
    import_form(client, DEFINITION)
    # Uma versão 2 que o compilador de um deploy seguinte não aceita mais.
    store_version(deps, 2, {**json.loads(DEFINITION), "minutes": 0})

    row = text(rows_of(client.get("/admin/forms"))[SLUG])
    page = client.get(f"/admin/forms/{SLUG}")

    assert "Não compila" in row and "Versão 2" in row
    assert page.status_code == 200
    assert "A versão publicada não compila" in text(section_of(page, "nao-compila"))
    assert 'id="pagina"' not in page.text
    assert json.loads(client.get(export_path(2)).content)["minutes"] == 0


# --- aviso dos dados vencidos ---------------------------------------------------------------------

REFORMA_URL = "/admin/forms/reforma-2027"
REFORMA_TITLE = "Calendário de atividades"
OPTION_ENDED = "já terminou a janela «Inscrição no projeto» (em 30/10/2026)"
ALL_ENDED = "Nenhuma janela do bloco tem mais data futura"
BADGE = "Dados a revisar"


def on_day(clock: FakeClock, moment: datetime) -> None:
    """O admin com o relógio parado em `moment`: o resultado não depende do dia em que a suíte
    roda."""
    clock.now = moment.timestamp()


def test_the_list_warns_about_a_form_whose_window_ended(
    client: TestClient, clock: FakeClock, deps: Deps
) -> None:
    pin_reforma_2027(deps)
    on_day(clock, AFTER_OPTION)

    page = client.get("/admin/forms")

    assert page.status_code == 200
    warning = section_of(page, "avisos-dos-formularios")
    assert REFORMA_TITLE in text(warning) and OPTION_ENDED in text(warning)
    assert tags(warning, "a", href=REFORMA_URL)
    rows = rows_of(page)
    assert BADGE in text(rows["reforma-2027"])
    assert all(BADGE not in text(row) for slug, row in rows.items() if slug != "reforma-2027")


def test_the_list_stays_quiet_while_the_window_is_open_and_the_version_is_recent(
    client: TestClient, clock: FakeClock, deps: Deps
) -> None:
    pin_reforma_2027(deps)
    on_day(clock, WHILE_OPEN)

    page = client.get("/admin/forms")

    assert tags(page.text, "section", id="avisos-dos-formularios") == []
    assert BADGE not in text(page.text)


def test_the_list_warns_about_an_old_version_even_with_the_window_open(
    client: TestClient, clock: FakeClock, deps: Deps
) -> None:
    pin_reforma_2027(deps, datetime(2026, 9, 1, 15, tzinfo=UTC))
    on_day(clock, WHILE_OPEN)

    warning = text(section_of(client.get("/admin/forms"), "avisos-dos-formularios"))

    assert "publicada em 01/09/2026, há 49 dias" in warning and "terminou" not in warning


def test_the_list_follows_the_current_version_of_each_form(
    client: TestClient, clock: FakeClock, deps: Deps
) -> None:
    pin_reforma_2027(deps)
    newer = legacy_definition("reforma-2027")
    for window in newer["data"]["janelas"]["windows"]:
        window["end"] = "2027-12-31"
    publish_version(deps, "reforma-2027", 3, newer, AFTER_OPTION)
    on_day(clock, AFTER_OPTION)

    assert BADGE not in text(client.get("/admin/forms").text)


def test_any_database_form_with_a_dated_window_gets_the_warning(
    client: TestClient, clock: FakeClock, deps: Deps
) -> None:
    assert import_form(client, DEFINITION).status_code == 303
    document = json.loads(DEFINITION)
    document["data"] = {
        "prazos": {
            "kind": "windows",
            "windows": [
                {
                    "name": "lote",
                    "label": "Lote de outubro",
                    "start": "2026-10-01",
                    "end": "2026-10-15",
                }
            ],
        }
    }
    publish_version(deps, SLUG, 2, document, PUBLISHED)
    on_day(clock, WHILE_OPEN)

    page = client.get("/admin/forms")

    warning = text(section_of(page, "avisos-dos-formularios"))
    assert "No bloco «prazos», já terminou a janela «Lote de outubro» (em 15/10/2026)" in warning
    assert BADGE in text(rows_of(page)[SLUG]) and BADGE not in text(rows_of(page)["mcc"])


def test_the_reading_of_the_form_shows_the_warning_at_the_top(
    client: TestClient, clock: FakeClock, deps: Deps
) -> None:
    pin_reforma_2027(deps)
    on_day(clock, AFTER_BOTH)

    page = client.get(REFORMA_URL)

    assert page.status_code == 200
    warning = text(section_of(page, "aviso-dos-dados"))
    assert ALL_ENDED in warning and "«Revisão da inscrição» (em 20/12/2026)" in warning
    assert "publicada em 14/10/2026, há 68 dias" in warning
    assert page.text.index('id="aviso-dos-dados"') < page.text.index('class="cols')


def test_the_reading_of_a_form_without_a_warning_does_not_change(
    client: TestClient, clock: FakeClock, deps: Deps
) -> None:
    pin_reforma_2027(deps)
    on_day(clock, AFTER_BOTH)
    assert tags(client.get("/admin/forms/demo").text, "section", id="aviso-dos-dados") == []
    assert tags(client.get("/admin/forms/mcc").text, "section", id="aviso-dos-dados") == []
    on_day(clock, WHILE_OPEN)
    assert tags(client.get(REFORMA_URL).text, "section", id="aviso-dos-dados") == []
