"""A lista de pedidos do admin e a planilha: o selo "Planilha …" da sincronização da tentativa atual
na coluna "Entregas e planilha" e o filtro "Planilha" (`sync`). A consulta é a de
`test_admin_queries_jobs.py` e a API, a de `test_admin_api_jobs.py`; aqui é a página."""

from __future__ import annotations

import re

import pytest
from jobs_world import SYNC_CASES, SYNC_EXPECTED, jobs_by_sync
from test_admin import codes_on_the_page, select_choices
from test_admin_destinations import admin_client

from app.admin import views
from app.deps import Deps

# O selo que a lista mostra para cada pedido de `jobs_by_sync`: a sincronização da tentativa atual.
SYNC_BADGES = {
    "failed_now": ["Planilha falhou"],
    "pending_now": ["Planilha pendente"],
    "synced": ["Planilha sincronizada"],
    "retried_ok": ["Planilha sincronizada"],  # a falha da tentativa 1 já foi refeita
    "regressed": ["Planilha falhou"],  # sincronizou na tentativa 1, mas a atual falhou
    "queued": [],
    "no_integration": [],
}


def row_of(html: str, public_id: str) -> str:
    """A linha da tabela do pedido de código `public_id`."""
    rows = re.findall(r"<tr>.*?</tr>", html, re.DOTALL)
    (found,) = (row for row in rows if f'<span class="mono">{public_id}</span>' in row)
    return found


def last_cell(row: str) -> str:
    """A célula "Entregas e planilha" da linha."""
    match = re.search(r'<td data-label="Entregas e planilha">(.*?)</td>', row, re.DOTALL)
    assert match is not None
    return match.group(1)


def badges_in(cell: str) -> list[str]:
    """Os textos dos selos de uma célula, na ordem em que aparecem."""
    return [text.strip() for text in re.findall(r"</svg>([^<]+)</span>", cell)]


def test_job_list_shows_the_sync_of_the_current_attempt_below_the_delivery(deps: Deps) -> None:
    made = jobs_by_sync(deps)
    client = admin_client(deps)

    html = client.get("/admin").text

    assert '<th scope="col">Entregas e planilha</th>' in html
    assert codes_on_the_page(html) == [made[name].public_id for name in reversed(SYNC_CASES)]
    for name, planilha in SYNC_BADGES.items():
        cell = last_cell(row_of(html, made[name].public_id))
        shown = badges_in(cell)
        if name == "queued":  # sem PDF, sem entrega nem sincronização: o traço de sempre
            assert shown == [] and "—" in cell, name
        elif name == "no_integration":  # o formulário não tem planilha: só a entrega
            assert shown == ["Webhook pendente"], name
        else:  # a entrega do webhook em cima e a planilha embaixo, cada uma com ícone e texto
            assert shown == ["Webhook pendente", *planilha], (name, shown)
    # Um estado que não é "Falhou" também diz o que é, sem depender da cor.
    assert "Planilha sincronizada" in html and "Planilha pendente" in html


def test_job_list_never_shows_the_sync_error_nor_where_the_spreadsheet_is(deps: Deps) -> None:
    jobs_by_sync(deps)
    client = admin_client(deps)

    html = client.get("/admin").text

    for text in ("integration_unavailable", "HTTP 503", "baserow.exemplo"):
        assert text not in html, text


def test_job_list_filters_by_the_sync_of_the_current_attempt(deps: Deps) -> None:
    made = jobs_by_sync(deps)
    client = admin_client(deps)

    for sync, names in SYNC_EXPECTED.items():
        html = client.get("/admin", params={"sync": sync} if sync else {}).text
        # Os mais novos primeiro (a ordem de criação, ao contrário), e o total acompanha o filtro.
        assert codes_on_the_page(html) == [
            made[name].public_id for name in reversed(SYNC_CASES) if name in names
        ], sync
        assert f'role="status">{len(names)} pedido' in html, sync
        assert ("com estes filtros" in html) is bool(sync), sync
        assert ('href="/admin">Limpar filtros</a>' in html) is bool(sync), sync

    # A linha filtrada mostra a sincronização que casou: a mesma que a coluna sempre mostrou.
    failed = client.get("/admin", params={"sync": "failed"}).text
    assert failed.count("Planilha falhou") == 2 and "Planilha sincronizada" not in failed
    pending = client.get("/admin", params={"sync": "pending"}).text
    assert pending.count("Planilha pendente") == 1 and "Planilha falhou" not in pending
    # Falhou na tentativa 1 e sincronizou na 2: a coluna mostra "sincronizada", e o filtro não o acha.
    retried = made["retried_ok"].public_id
    assert retried not in failed and retried in client.get("/admin").text


def test_job_list_offers_the_sync_choices_and_marks_the_chosen_one(deps: Deps) -> None:
    client = admin_client(deps)

    plain = client.get("/admin").text
    failed = client.get("/admin", params={"sync": "failed"}).text

    assert '<label for="f-sync">Planilha</label>' in plain  # o rótulo, visível
    assert select_choices(plain, "f-sync") == [
        ("", False, "Todas"),  # sem `selected`: o navegador mostra a primeira
        ("failed", False, "Falhou"),
        ("pending", False, "Pendente"),
    ]
    assert [(value, chosen) for value, chosen, _ in select_choices(failed, "f-sync") if chosen] == [
        ("failed", True)
    ]
    assert "change from:#f-sync" in plain  # escolher dispara a busca, como o Status


def test_job_list_ignores_an_unknown_sync_and_combines_it_with_the_other_filters(
    deps: Deps,
) -> None:
    made = jobs_by_sync(deps)
    client = admin_client(deps)

    def shown(**params: str) -> list[str]:
        return codes_on_the_page(client.get("/admin", params=params).text)

    ignored = client.get("/admin", params={"sync": "succeeded"}).text
    assert len(codes_on_the_page(ignored)) == len(SYNC_CASES)
    assert "com estes filtros" not in ignored
    assert all(chosen is False for _, chosen, _ in select_choices(ignored, "f-sync")[1:])
    regressed, failed_now = made["regressed"].public_id, made["failed_now"].public_id
    assert shown(sync="failed", status="completed") == [regressed, failed_now]  # E com o status
    assert shown(sync="failed", status="queued") == []
    assert shown(sync="failed", form="demo") == [regressed, failed_now]  # e com o formulário
    assert shown(sync="failed", form="mcc") == []
    assert shown(sync="failed", q="failed_now") == [failed_now]  # e com a busca
    assert shown(sync="failed", q=made["pending_now"].public_id) == []
    assert shown(sync="failed", delivery="pending") == [regressed, failed_now]  # e com a entrega
    assert shown(sync="failed", delivery="failed") == []
    assert shown(sync="failed") == [regressed, failed_now]


def test_job_list_says_when_no_sync_failed_or_is_pending(deps: Deps) -> None:
    jobs_by_sync(deps)
    client = admin_client(deps)

    nothing_failed = client.get("/admin", params={"sync": "failed", "status": "queued"}).text
    assert "Nenhuma sincronização falhou com estes filtros." in nothing_failed
    assert "Nenhum pedido encontrado" not in nothing_failed
    assert 'href="/admin">Limpar filtros</a>' in nothing_failed
    nothing_pending = client.get("/admin", params={"sync": "pending", "q": "failed_now"}).text
    assert "Nenhuma sincronização pendente com estes filtros." in nothing_pending
    # Com a entrega e a planilha juntas, a frase não aponta uma das duas: é a de sempre.
    both = client.get("/admin", params={"sync": "failed", "delivery": "failed"}).text
    assert "Nenhum pedido encontrado com estes filtros." in both
    # E a entrega sozinha continua com a sua frase.
    delivery = client.get("/admin", params={"delivery": "failed"}).text
    assert "Nenhuma entrega falhou com estes filtros." in delivery


def test_job_list_pages_keep_the_sync_and_htmx_pushes_it_to_the_address(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(views, "PAGE_SIZE", 1)
    made = jobs_by_sync(deps)
    client = admin_client(deps)

    first = client.get("/admin", params={"sync": "failed"}).text
    second = client.get("/admin", params={"sync": "failed", "page": "2"}).text

    assert codes_on_the_page(first) == [made["regressed"].public_id]
    assert codes_on_the_page(second) == [made["failed_now"].public_id]
    assert "2 pedidos com estes filtros" in first and "Página 1 de 2" in first
    assert 'href="/admin?sync=failed&amp;page=2"' in first
    assert "Página 2 de 2" in second and 'href="/admin?sync=failed"' in second  # volta para a 1
    # O formulário manda os campos vazios; o endereço guarda só o que filtra.
    fields = {"q": "", "form": "", "period": "", "status": "", "delivery": "", "sync": "failed"}
    htmx = client.get("/admin", params=fields, headers={"HX-Request": "true"})
    assert htmx.headers["HX-Push-Url"] == "/admin?sync=failed"
    everything = client.get("/admin", params={**fields, "sync": ""}, headers={"HX-Request": "true"})
    assert everything.headers["HX-Push-Url"] == "/admin"  # "Todas" não escreve o parâmetro
