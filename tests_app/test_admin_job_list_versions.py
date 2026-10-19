"""A lista de pedidos do admin e a versão do formulário: o select "Versão" (`form_version`), que só
aparece com um formulário escolhido e oferece as versões dos pedidos dele. A consulta é a de
`test_admin_queries_jobs.py` e a API, a de `test_admin_api_jobs.py`; aqui é a página."""

from __future__ import annotations

import re

import pytest
from jobs_world import DEMO_VERSIONS, VERSION_EXPECTED, VERSION_OF, jobs_by_version
from test_admin import codes_on_the_page, select_choices
from test_admin_destinations import admin_client

from app.admin import views
from app.deps import Deps
from app.forms import FormSpec


def test_job_list_offers_the_versions_of_the_chosen_form_only_with_a_form(
    deps: Deps, newer_demo: FormSpec
) -> None:
    jobs_by_version(deps, newer_demo)
    client = admin_client(deps)

    plain = client.get("/admin").text
    demo = client.get("/admin", params={"form": "demo"}).text
    mcc = client.get("/admin", params={"form": "mcc"}).text

    # Sem formulário escolhido o select não existe, mas o lugar dele está na barra, vazio, para o
    # HTMX o encher quando o formulário mudar.
    assert 'id="f-versao"' not in plain and 'id="campo-versao"' in plain
    assert '<label for="f-versao">Versão</label>' in demo  # o rótulo, visível
    assert select_choices(demo, "f-versao") == [
        ("", False, "Todas"),  # sem `selected`: o navegador mostra a primeira
        *((version, False, version) for version in DEMO_VERSIONS),  # da mais nova à mais antiga
    ]
    assert select_choices(mcc, "f-versao") == [("", False, "Todas"), ("mcc-v1", False, "mcc-v1")]


def test_job_list_wires_the_form_and_the_version_for_htmx(deps: Deps, newer_demo: FormSpec) -> None:
    jobs_by_version(deps, newer_demo)
    client = admin_client(deps)

    html = client.get("/admin", params={"form": "demo"}).text

    # Escolher a versão dispara a busca; o #campo-versao é estável (só o que há dentro dele é trocado).
    assert "change from:#campo-versao" in html
    assert 'hx-select-oob="#limpar-filtros,#campo-versao:innerHTML"' in html
    # Trocar o formulário busca sem a versão antiga, e as opções voltam em "Todas".
    (form_select,) = re.findall(r'<select class="select" id="f-form"[^>]*>', html)
    assert 'hx-params="not form_version"' in form_select and 'hx-trigger="change"' in form_select
    assert "change from:#f-form" not in html  # a busca do formulário é a do próprio select


def test_job_list_filters_by_the_version_and_the_address_keeps_it(
    deps: Deps, newer_demo: FormSpec
) -> None:
    made = jobs_by_version(deps, newer_demo)
    client = admin_client(deps)
    names = {job.public_id: name for name, job in made.items()}

    def shown(**params: str) -> list[str]:
        return [names[code] for code in codes_on_the_page(client.get("/admin", params=params).text)]

    for (form, version), expected in VERSION_EXPECTED.items():
        params = {"form": form, **({"form_version": version} if version else {})}
        assert shown(**params) == [
            name for name in reversed(list(VERSION_OF)) if name in expected
        ], (form, version)
    # O texto legado e o `<endereço>-v<número>` são duas versões do mesmo formulário.
    assert shown(form="demo", form_version="demo-2026.10") == ["legacy"]
    assert shown(form="demo", form_version="demo-v1") == ["v1"]
    # Combina por E com a busca e o status.
    assert shown(form="demo", form_version="demo-v2", q="v2_again") == ["v2_again"]
    assert shown(form="demo", form_version="demo-v2", status="failed") == []

    html = client.get("/admin", params={"form": "demo", "form_version": "demo-v2"}).text
    assert "2 pedidos com estes filtros" in html
    assert [(v, chosen) for v, chosen, _ in select_choices(html, "f-versao") if chosen] == [
        ("demo-v2", True)
    ]
    assert 'href="/admin">Limpar filtros</a>' in html  # a versão ativa conta como filtro


def test_job_list_ignores_a_version_without_a_form_and_shows_the_one_it_does_not_have(
    deps: Deps, newer_demo: FormSpec
) -> None:
    made = jobs_by_version(deps, newer_demo)
    client = admin_client(deps)

    # Sem formulário a versão não filtra: o filtro escondido, sem o select, deixaria a lista sem
    # explicação. Também não vale um `form` que não é um endereço.
    for params in ({"form_version": "demo-v2"}, {"form": "Nope", "form_version": "demo-v2"}):
        ignored = client.get("/admin", params=params).text
        assert len(codes_on_the_page(ignored)) == len(made), params
        assert "com estes filtros" not in ignored and 'id="f-versao"' not in ignored, params
    # Uma versão que o formulário não tem (ou que é de outro) dá a lista vazia, sem erro, e o select
    # diz o que filtra, em vez de mostrar "Todas" sobre uma lista vazia.
    for version in ("mcc-v1", "demo-v9"):
        empty = client.get("/admin", params={"form": "demo", "form_version": version})
        assert empty.status_code == 200 and codes_on_the_page(empty.text) == [], version
        assert "Nenhum pedido encontrado com estes filtros." in empty.text
        assert [(v, chosen, label) for v, chosen, label in select_choices(empty.text, "f-versao")][
            -1
        ] == (version, True, f"{version} (não encontrada)")
    # A versão com mais de 64 caracteres não é versão de ninguém: ignorada.
    long = client.get("/admin", params={"form": "demo", "form_version": "v" * 65}).text
    assert len(codes_on_the_page(long)) == len(VERSION_EXPECTED[("demo", "")])
    assert "(não encontrada)" not in long


def test_job_list_pages_keep_the_version_and_htmx_pushes_it_to_the_address(
    deps: Deps, newer_demo: FormSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(views, "PAGE_SIZE", 1)
    made = jobs_by_version(deps, newer_demo)
    client = admin_client(deps)
    params = {"form": "demo", "form_version": "demo-v2"}

    first = client.get("/admin", params=params).text
    second = client.get("/admin", params={**params, "page": "2"}).text

    assert codes_on_the_page(first) == [made["v2_again"].public_id]
    assert codes_on_the_page(second) == [made["v2"].public_id]
    assert "2 pedidos com estes filtros" in first and "Página 1 de 2" in first
    assert 'href="/admin?form=demo&amp;form_version=demo-v2&amp;page=2"' in first
    assert (
        "Página 2 de 2" in second and 'href="/admin?form=demo&amp;form_version=demo-v2"' in second
    )
    # O formulário manda os campos vazios; o endereço guarda só o que filtra.
    fields = {"q": "", "form": "demo", "form_version": "demo-v2", "period": "", "status": ""}
    htmx = client.get("/admin", params=fields, headers={"HX-Request": "true"})
    assert htmx.headers["HX-Push-Url"] == "/admin?form=demo&form_version=demo-v2"
    all_of_them = client.get(
        "/admin", params={**fields, "form_version": ""}, headers={"HX-Request": "true"}
    )
    assert (
        all_of_them.headers["HX-Push-Url"] == "/admin?form=demo"
    )  # "Todas" não escreve o parâmetro


def test_changing_the_form_over_htmx_remakes_the_versions_and_goes_back_to_all(
    deps: Deps, newer_demo: FormSpec
) -> None:
    made = jobs_by_version(deps, newer_demo)
    client = admin_client(deps)
    htmx = {"HX-Request": "true"}

    # O que o select de formulário manda ao trocar para o MCC: os campos do formulário, menos a versão.
    changed = client.get("/admin", params={"q": "", "form": "mcc", "period": ""}, headers=htmx)
    # E ao voltar para "Todos": o campo da versão se esvazia, e a lista é de todos os formulários.
    cleared = client.get("/admin", params={"q": "", "form": "", "period": ""}, headers=htmx)

    assert select_choices(changed.text, "f-versao") == [
        ("", False, "Todas"),
        ("mcc-v1", False, "mcc-v1"),
    ]
    assert codes_on_the_page(changed.text) == [made["mcc"].public_id]
    assert changed.headers["HX-Push-Url"] == "/admin?form=mcc"
    assert 'id="f-versao"' not in cleared.text and 'id="campo-versao"' in cleared.text
    assert len(codes_on_the_page(cleared.text)) == len(made)
    assert cleared.headers["HX-Push-Url"] == "/admin"
