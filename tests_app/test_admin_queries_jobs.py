"""A consulta dos pedidos (`app.admin.queries.jobs`): a que a página e a API de admin dividem, sem
rótulo e com o estado do pedido separado do dado da lead.

A página decora o resultado (rótulos em português, data de Brasília) e a API o serializa. O que prova
que a página não mudou são os testes dela (`test_admin*.py`); o que prova que a leitura de `leitura`
não leva dado pessoal é a separação que estes testes cobram.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from conftest import record_webhook
from jobs_world import (
    DEMO_VERSIONS,
    PERSONAL,
    SYNC_CASES,
    SYNC_EXPECTED,
    SYNC_SHOWN,
    VERSION_EXPECTED,
    VERSION_OF,
    field_names,
    jobs_by_sync,
    jobs_by_version,
)
from test_admin import (
    ARRIVALS,
    BRT,
    DELIVERY_EXPECTED,
    PERIOD_EXPECTED,
    PERIOD_NOW,
    failed_delivery,
    finish,
    jobs_by_arrival,
    jobs_by_delivery,
    new_lead_job,
    new_mcc_job,
)

from app import jobs
from app.admin.queries import jobs as queries
from app.deps import Deps
from app.forms import FormSpec
from app.models import Job


def three_jobs(deps: Deps) -> dict[str, Job]:
    return {
        "ana": new_lead_job(deps, "Ana Souza", "ana@example.test"),
        "beatriz": new_lead_job(deps, "Beatriz Lima", "beatriz@example.test"),
        "carla": new_lead_job(deps, "Carla Dias", "carla@example.test"),
    }


def page_of(deps: Deps, conditions: list[Any], **options: Any) -> list[queries.JobRow]:
    with deps.sessions() as session:
        return queries.jobs_by_page(
            session,
            conditions,
            forms=deps.forms,
            **{"limit": 25, "offset": 0, "with_lead": True, **options},
        )


def public_ids(rows: list[queries.JobRow]) -> list[str]:
    return [row.state.public_id for row in rows]


# --- o filtro, que a página e a API dividem -----------------------------------------------------


def test_the_filter_searches_the_name_and_the_email_only_when_the_caller_may(deps: Deps) -> None:
    made = three_jobs(deps)

    def found(q: str, *, search_lead: bool) -> list[str]:
        return public_ids(
            page_of(deps, queries.JobFilters(q=q).conditions(search_lead=search_lead))
        )

    assert found("souza", search_lead=True) == [made["ana"].public_id]  # parte do nome
    assert found("BEATRIZ@", search_lead=True) == [made["beatriz"].public_id]  # parte do e-mail
    assert found("souza", search_lead=False) == []
    assert found("beatriz@example.test", search_lead=False) == []
    code = made["carla"].public_id
    assert found(code, search_lead=True) == found(code, search_lead=False) == [code]
    assert found(code.lower(), search_lead=False) == []  # o código é exato


def test_the_filter_takes_the_wildcards_of_like_as_text_and_ignores_an_unknown_status(
    deps: Deps,
) -> None:
    three_jobs(deps)

    assert page_of(deps, queries.JobFilters(q="100%_").conditions()) == []
    assert len(page_of(deps, queries.JobFilters(status="inexistente").conditions())) == 3


def test_the_status_filter_and_the_total_follow_the_same_conditions(deps: Deps) -> None:
    made = three_jobs(deps)
    with deps.sessions.begin() as session:
        jobs.claim(session, made["beatriz"].id, 1, "run-1")
        jobs.fail(session, made["beatriz"].id, 1, "llm_unavailable", "HTTP 503")
    failed = queries.JobFilters(status="failed").conditions()

    with deps.sessions() as session:
        total_failed = queries.count_jobs(session, failed)
        total = queries.count_jobs(session, queries.JobFilters().conditions())

    rows = page_of(deps, failed)
    assert public_ids(rows) == [made["beatriz"].public_id]
    assert (total_failed, total) == (1, 3)
    assert rows[0].state.failure_code == "llm_unavailable"


def test_the_form_filter_keeps_only_the_orders_of_that_form_and_the_total_follows(
    deps: Deps,
) -> None:
    made = three_jobs(deps)  # do demo
    fernanda = new_mcc_job(deps, "Fernanda Rocha", "fernanda@example.test")  # do MCC
    with deps.sessions.begin() as session:
        jobs.claim(session, made["beatriz"].id, 1, "run-1")
        jobs.fail(session, made["beatriz"].id, 1, "llm_unavailable", "HTTP 503")

    def found(**filters: str) -> list[str]:
        return public_ids(page_of(deps, queries.JobFilters(**filters).conditions()))

    def total(**filters: str) -> int:
        with deps.sessions() as session:
            return queries.count_jobs(session, queries.JobFilters(**filters).conditions())

    demo = [made[name].public_id for name in ("carla", "beatriz", "ana")]  # os mais novos primeiro
    assert found(form="mcc") == [fernanda.public_id]
    assert found(form="demo") == demo
    assert found() == [fernanda.public_id, *demo]
    assert found(form="demo", status="failed") == [made["beatriz"].public_id]
    assert found(form="mcc", status="failed") == []
    assert found(form="demo", q="souza") == [made["ana"].public_id]  # E com a busca
    assert found(form="mcc", q="souza") == []
    assert found(form="nao-existe") == [] and total(form="nao-existe") == 0
    assert (total(form="mcc"), total(form="demo"), total()) == (1, 3, 4)


def test_a_form_that_is_not_an_address_is_no_filter_and_the_url_writes_only_what_filters() -> None:
    for form in ("Maiúscula", "com espaço", "-hifen", "x" * 65, "a\nb", "mcc\n"):
        filters = queries.JobFilters(form=form)
        assert filters.form == "" and not filters.active and filters.conditions() == [], form
    assert queries.JobFilters(form="x" * 64).form == "x" * 64

    assert queries.JobFilters().url() == "/admin"
    assert queries.JobFilters(status="inexistente", form="Nope").url(1) == "/admin"
    assert queries.JobFilters(form="mcc").url() == "/admin?form=mcc"
    full = queries.JobFilters(q=" ana souza ", status="failed", form="mcc")
    assert full.url() == "/admin?q=ana+souza&form=mcc&status=failed"
    assert full.url(3) == "/admin?q=ana+souza&form=mcc&status=failed&page=3"


def test_the_period_filter_keeps_the_calendar_days_of_brasilia_counting_today(
    deps: Deps,
) -> None:
    made = jobs_by_arrival(deps)

    def found(period: str, now: datetime = PERIOD_NOW) -> set[str]:
        conditions = queries.JobFilters(period=period, now=now).conditions()
        shown = {job.public_id: name for name, job in made.items()}
        with deps.sessions() as session:
            total = queries.count_jobs(session, conditions)
        names = {shown[code] for code in public_ids(page_of(deps, conditions))}
        assert total == len(names)  # o total conta o mesmo filtro
        return names

    for period, names in PERIOD_EXPECTED.items():
        assert found(period) == names, period
        assert found(period, PERIOD_NOW.astimezone(UTC)) == names, (
            period
        )  # o mesmo instante, em UTC
    # À 00:30 de 16/01 em Brasília (03:30 UTC) o "hoje" já é 16/01: ninguém chegou ainda, e a
    # semana perdeu o dia 9.
    after_midnight = datetime(2027, 1, 16, 0, 30, tzinfo=BRT)
    assert found("today", after_midnight) == set()
    assert found("7d", after_midnight) == PERIOD_EXPECTED["7d"] - {"start_of_7_days"}


def test_the_period_bounds_are_the_midnights_of_brasilia_and_the_limits_of_the_api_add_up(
    deps: Deps,
) -> None:
    now = PERIOD_NOW.astimezone(UTC)  # 02:00 UTC de 16/01, 23:00 de 15/01 em Brasília

    assert queries.JobFilters(period="today", now=now).created_from == datetime(
        2027, 1, 15, 3, 0, tzinfo=UTC
    )
    assert queries.JobFilters(period="7d", now=now).created_from == datetime(
        2027, 1, 9, 3, 0, tzinfo=UTC
    )
    assert queries.JobFilters(period="30d", now=now).created_from == datetime(
        2026, 12, 17, 3, 0, tzinfo=UTC
    )
    assert queries.JobFilters(period="30d", now=now).created_to is None  # até agora, sem teto
    assert queries.JobFilters(now=now).created_from is None  # "Tudo"

    jobs_by_arrival(deps)
    start, end = ARRIVALS["start_of_today"], ARRIVALS["tonight"]
    limits = queries.JobFilters(created_from=start, created_to=end).conditions()
    assert len(page_of(deps, limits)) == 1  # [de, até): o começo entra, o fim fica fora
    backwards = queries.JobFilters(created_from=end, created_to=start).conditions()
    assert page_of(deps, backwards) == []


def test_an_unknown_period_is_no_filter_and_the_url_writes_only_a_period_that_filters() -> None:
    for period in ("1y", "Today", "7", "7d ", "", "today\n"):
        filters = queries.JobFilters(period=period, now=PERIOD_NOW)
        assert filters.period == "" and not filters.active, period
        assert filters.conditions() == [] and filters.url() == "/admin", period
    assert queries.JobFilters(period="bogus").period == ""  # sem `now`: nada a resolver

    week = queries.JobFilters(period="7d", now=PERIOD_NOW)
    assert week.active and len(week.conditions()) == 1
    assert week.url() == "/admin?period=7d"
    full = queries.JobFilters(q="ana", form="mcc", period="30d", status="failed", now=PERIOD_NOW)
    assert full.url(2) == "/admin?q=ana&form=mcc&period=30d&status=failed&page=2"
    with pytest.raises(ValueError):  # um período sem "agora" não tem como virar limites
        queries.JobFilters(period="7d")
    with pytest.raises(ValueError):  # o período e os limites da API são dois caminhos
        queries.JobFilters(period="7d", now=PERIOD_NOW, created_from=PERIOD_NOW)


def test_the_delivery_filter_looks_only_at_the_deliveries_of_the_current_attempt(
    deps: Deps,
) -> None:
    made = jobs_by_delivery(deps)
    names = {job.public_id: name for name, job in made.items()}

    for delivery, expected in DELIVERY_EXPECTED.items():
        conditions = queries.JobFilters(delivery=delivery).conditions()
        rows = page_of(deps, conditions)
        with deps.sessions() as session:
            total = queries.count_jobs(session, conditions)
        assert {names[code] for code in public_ids(rows)} == expected, delivery
        assert total == len(expected), delivery  # o total conta o mesmo filtro
        # A mesma definição da coluna "Entregas": a linha que o filtro acha mostra uma entrega com
        # o estado pedido, nunca a de uma tentativa antiga.
        if delivery:
            for row in rows:
                assert delivery in {d.status for d in row.state.deliveries}

    # Falhou na tentativa 1 e foi recebida na 2: a coluna diz "succeeded", e "failed" não o acha.
    (retried,) = (r for r in page_of(deps, []) if r.state.public_id == made["retried_ok"].public_id)
    assert retried.state.attempt == 2
    assert [d.status for d in retried.state.deliveries] == ["succeeded"]
    # O inverso: recebida na 1 e falha na 2 é um pedido com entrega que falhou.
    failed_ids = public_ids(page_of(deps, queries.JobFilters(delivery="failed").conditions()))
    assert made["regressed"].public_id in failed_ids
    assert made["retried_ok"].public_id not in failed_ids


def test_the_delivery_filter_combines_with_the_others_and_never_matches_an_order_without_one(
    deps: Deps,
) -> None:
    made = jobs_by_delivery(deps)

    def found(**filters: str) -> list[str]:
        return public_ids(page_of(deps, queries.JobFilters(**filters).conditions()))

    regressed, failed_now = made["regressed"].public_id, made["failed_now"].public_id
    assert found(delivery="failed") == [regressed, failed_now]  # os mais novos primeiro
    assert found(delivery="failed", status="completed") == [regressed, failed_now]
    assert found(delivery="failed", status="queued") == []
    assert found(delivery="failed", form="mcc") == []
    assert found(delivery="failed", form="demo") == [regressed, failed_now]
    assert found(delivery="failed", q="regressed") == [regressed]
    assert found(delivery="pending", q="regressed") == []
    # O pedido na fila, sem PDF nem entrega, não casa com nenhuma das duas opções.
    for delivery in ("failed", "pending"):
        assert made["no_delivery"].public_id not in found(delivery=delivery)


def test_an_unknown_delivery_is_no_filter_and_the_url_writes_only_a_delivery_that_filters() -> None:
    for delivery in ("succeeded", "Failed", "all", "failed ", "", "pending\n"):
        filters = queries.JobFilters(delivery=delivery)
        assert filters.delivery == "" and not filters.active, repr(delivery)
        assert filters.conditions() == [] and filters.url() == "/admin", repr(delivery)

    failed = queries.JobFilters(delivery="failed")
    assert failed.active and len(failed.conditions()) == 1
    assert failed.url() == "/admin?delivery=failed"
    full = queries.JobFilters(
        q="ana", form="mcc", period="30d", status="failed", delivery="pending", now=PERIOD_NOW
    )
    assert full.url(2) == "/admin?q=ana&form=mcc&period=30d&status=failed&delivery=pending&page=2"


def test_the_sync_filter_looks_only_at_the_syncs_of_the_current_attempt(deps: Deps) -> None:
    made = jobs_by_sync(deps)
    names = {job.public_id: name for name, job in made.items()}

    for sync, expected in SYNC_EXPECTED.items():
        conditions = queries.JobFilters(sync=sync).conditions()
        rows = page_of(deps, conditions)
        with deps.sessions() as session:
            total = queries.count_jobs(session, conditions)
        assert {names[code] for code in public_ids(rows)} == expected, sync
        assert total == len(expected), sync  # o total conta o mesmo filtro
        # A mesma definição da coluna "Entregas e planilha": a linha que o filtro acha mostra uma
        # sincronização com o estado pedido, nunca a de uma tentativa antiga.
        if sync:
            for row in rows:
                assert sync in {s.status for s in row.state.syncs}

    failed_ids = public_ids(page_of(deps, queries.JobFilters(sync="failed").conditions()))
    # Falhou na tentativa 1 e deu certo na 2: a lista diz "succeeded", e "failed" não o acha.
    assert made["retried_ok"].public_id not in failed_ids
    # O inverso: deu certo na 1 e falhou na 2 é um pedido com sincronização que falhou.
    assert made["regressed"].public_id in failed_ids


def test_the_sync_filter_combines_with_the_others_and_never_matches_an_order_without_one(
    deps: Deps,
) -> None:
    made = jobs_by_sync(deps)

    def found(**filters: str) -> list[str]:
        return public_ids(page_of(deps, queries.JobFilters(**filters).conditions()))

    regressed, failed_now = made["regressed"].public_id, made["failed_now"].public_id
    assert found(sync="failed") == [regressed, failed_now]  # os mais novos primeiro
    assert found(sync="failed", status="completed") == [regressed, failed_now]
    assert found(sync="failed", status="queued") == []
    assert found(sync="failed", form="mcc") == []
    assert found(sync="failed", form="demo") == [regressed, failed_now]
    assert found(sync="failed", q="regressed") == [regressed]
    assert found(sync="pending", q="regressed") == []
    # E com a entrega: o webhook dos pedidos concluídos está pendente, e o da fila nem existe.
    assert found(sync="failed", delivery="pending") == [regressed, failed_now]
    assert found(sync="failed", delivery="failed") == []
    # O pedido na fila e o do formulário sem integração não casam com nenhuma das duas opções.
    for sync in ("failed", "pending"):
        assert made["queued"].public_id not in found(sync=sync)
        assert made["no_integration"].public_id not in found(sync=sync)


def test_an_unknown_sync_is_no_filter_and_the_url_writes_only_a_sync_that_filters() -> None:
    for sync in ("succeeded", "Failed", "all", "failed ", "", "pending\n"):
        filters = queries.JobFilters(sync=sync)
        assert filters.sync == "" and not filters.active, repr(sync)
        assert filters.conditions() == [] and filters.url() == "/admin", repr(sync)

    failed = queries.JobFilters(sync="failed")
    assert failed.active and len(failed.conditions()) == 1
    assert failed.url() == "/admin?sync=failed"
    full = queries.JobFilters(
        q="ana",
        form="mcc",
        period="30d",
        status="failed",
        delivery="pending",
        sync="pending",
        now=PERIOD_NOW,
    )
    assert full.url(2) == (
        "/admin?q=ana&form=mcc&period=30d&status=failed&delivery=pending&sync=pending&page=2"
    )


def test_the_form_version_filter_keeps_the_orders_with_that_text_inside_the_form(
    deps: Deps, newer_demo: FormSpec
) -> None:
    made = jobs_by_version(deps, newer_demo)
    names = {job.public_id: name for name, job in made.items()}

    def found(**filters: str) -> list[str]:
        return [
            names[code]
            for code in public_ids(page_of(deps, queries.JobFilters(**filters).conditions()))
        ]

    def total(**filters: str) -> int:
        with deps.sessions() as session:
            return queries.count_jobs(session, queries.JobFilters(**filters).conditions())

    for (form, version), expected in VERSION_EXPECTED.items():
        filters = {"form": form, **({"form_version": version} if version else {})}
        newest_first = [name for name in reversed(list(VERSION_OF)) if name in expected]
        assert found(**filters) == newest_first, (form, version)
        assert total(**filters) == len(expected), (form, version)  # o total conta o mesmo filtro
    # O texto legado e o `<endereço>-v<número>` do mesmo formulário são duas versões.
    assert found(form="demo", form_version="demo-2026.10") == ["legacy"]
    assert found(form="demo", form_version="demo-v1") == ["v1"]
    # Combina por E com os outros filtros.
    assert found(form="demo", form_version="demo-v2", q=made["v2"].public_id) == ["v2"]
    assert found(form="demo", form_version="demo-v2", status="failed") == []


def test_a_form_version_needs_a_form_and_the_url_writes_it_only_with_one() -> None:
    # Uma versão existe dentro de um formulário: sozinha, a página a ignora (e a API a recusa).
    alone = queries.JobFilters(form_version="demo-v2")
    assert alone.form_version == "" and not alone.active
    assert alone.conditions() == [] and alone.url() == "/admin"
    # Um `form` que não é endereço some, e leva a versão junto.
    assert queries.JobFilters(form="Nope", form_version="demo-v2").form_version == ""
    # Mais de 64 caracteres (a coluna `submissions.form_version`) não é versão de ninguém.
    assert queries.JobFilters(form="demo", form_version="v" * 65).form_version == ""
    assert queries.JobFilters(form="demo", form_version="v" * 64).form_version == "v" * 64

    chosen = queries.JobFilters(form="demo", form_version="demo-2026.10")
    assert chosen.active and len(chosen.conditions()) == 2
    assert chosen.url() == "/admin?form=demo&form_version=demo-2026.10"
    full = queries.JobFilters(q="ana", form="demo", form_version="demo-v2", status="failed")
    assert full.url(2) == "/admin?q=ana&form=demo&form_version=demo-v2&status=failed&page=2"


def test_the_versions_of_a_form_come_newest_first_from_its_own_orders(
    deps: Deps, newer_demo: FormSpec
) -> None:
    jobs_by_version(deps, newer_demo)

    with deps.sessions() as session:
        demo = queries.form_versions(session, "demo")
        mcc = queries.form_versions(session, "mcc")
        nothing = queries.form_versions(session, "nao-existe")

    # A de maior número primeiro; na mesma, a que chegou por último: `demo-v1` depois do legado.
    assert demo == DEMO_VERSIONS and mcc == ["mcc-v1"] and nothing == []


# --- a linha: o estado de um lado, o dado da lead do outro --------------------------------------


def test_a_row_keeps_the_state_apart_from_the_lead_and_can_leave_the_lead_out(deps: Deps) -> None:
    made = three_jobs(deps)
    conditions = queries.JobFilters().conditions()

    with_lead = page_of(deps, conditions)
    without = page_of(deps, conditions, with_lead=False)

    assert public_ids(with_lead) == public_ids(without)
    assert public_ids(with_lead)[0] == made["carla"].public_id  # os mais novos primeiro
    newest = with_lead[0]
    assert newest.lead == queries.SummaryLead(
        name="Carla Dias", email="carla@example.test", priority="P2"
    )
    assert all(row.lead is None for row in without)
    assert without[0].state == newest.state  # o estado é o mesmo com ou sem a lead


def test_the_state_of_a_row_has_no_personal_field() -> None:
    assert field_names(queries.JobSummary).isdisjoint(PERSONAL)
    assert field_names(queries.SummaryLead) <= PERSONAL


def test_the_deliveries_of_a_row_are_those_of_the_current_attempt(deps: Deps) -> None:
    job = new_lead_job(deps, "Ana Souza", "ana@example.test")
    failed_delivery(deps, job.id)
    (before,) = page_of(deps, queries.JobFilters().conditions())
    with deps.sessions.begin() as session:
        assert jobs.reprocess(
            session, job.id, actor="operadora", reason="Prompt corrigido", expected_attempt=1
        )
    record_webhook(deps)
    finish(deps, job.id, attempt=2)
    (after,) = page_of(deps, queries.JobFilters().conditions())

    assert [(d.destination, d.status) for d in before.state.deliveries] == [("webhook", "failed")]
    assert after.state.attempt == 2
    assert [(d.destination, d.status) for d in after.state.deliveries] == [("webhook", "pending")]


def test_the_syncs_of_a_row_are_those_of_the_current_attempt_and_say_only_the_kind_and_the_state(
    deps: Deps,
) -> None:
    made = jobs_by_sync(deps)
    names = {job.public_id: name for name, job in made.items()}

    rows = page_of(deps, queries.JobFilters().conditions())

    assert len(rows) == len(SYNC_CASES)
    for row in rows:
        name = names[row.state.public_id]
        assert [s.status for s in row.state.syncs] == SYNC_SHOWN[name], name
        assert all(s == queries.SyncBrief("baserow", s.status) for s in row.state.syncs), name
    # A falha da tentativa 1 da `retried_ok` foi refeita: a lista mostra a sincronização da 2.
    (retried,) = (r for r in rows if names[r.state.public_id] == "retried_ok")
    assert retried.state.attempt == 2
    # Só tipo e estado: o texto do erro, a tabela e a linha da planilha não saem na lista.
    assert field_names(queries.SyncBrief) == {"integration", "status"}


def test_the_syncs_of_a_whole_page_are_read_in_one_query(deps: Deps) -> None:
    jobs_by_sync(deps)
    seen: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        seen.append(statement)

    with deps.sessions() as session:
        engine = session.get_bind()
        sa.event.listen(engine, "before_cursor_execute", record)
        try:
            rows = queries.jobs_by_page(
                session, [], forms=deps.forms, limit=25, offset=0, with_lead=False
            )
        finally:
            sa.event.remove(engine, "before_cursor_execute", record)

    assert len(rows) == len(SYNC_CASES)
    # Uma consulta às sincronizações e uma às entregas, quantos forem os pedidos da página.
    assert sum("FROM integration_syncs" in statement for statement in seen) == 1
    assert sum("FROM deliveries" in statement for statement in seen) == 1


def test_a_row_says_the_form_the_attempt_and_the_status(deps: Deps) -> None:
    new_lead_job(deps, "Ana Souza", "ana@example.test")

    (row,) = page_of(deps, queries.JobFilters().conditions())

    assert (row.state.form_slug, row.state.attempt, row.state.status) == ("demo", 1, "queued")
    assert row.state.form_version and row.state.deliveries == () and row.state.syncs == ()


# --- a paginação por número (a página) e por chave (a API) --------------------------------------


def test_the_page_pagination_offsets_the_newest_first(deps: Deps) -> None:
    for number in range(5):
        new_lead_job(deps, f"Lead {number}", f"lead{number}@example.test")
    conditions = queries.JobFilters().conditions()

    rows = [
        *page_of(deps, conditions, limit=2, offset=0),
        *page_of(deps, conditions, limit=2, offset=2),
    ]

    assert [row.lead.name for row in rows if row.lead] == ["Lead 4", "Lead 3", "Lead 2", "Lead 1"]


def after(row: queries.JobRow) -> queries.JobCursor:
    return queries.JobCursor(row.state.created_at, row.state.id)


def test_the_keyset_pagination_neither_skips_nor_repeats_a_row_when_a_job_arrives(
    deps: Deps,
) -> None:
    for number in range(5):
        new_lead_job(deps, f"Lead {number}", f"lead{number}@example.test")
    conditions = queries.JobFilters().conditions()

    with deps.sessions() as session:
        first, more_1 = queries.jobs_after(
            session, conditions, forms=deps.forms, limit=2, with_lead=True
        )
    new_lead_job(deps, "Lead novo", "novo@example.test")  # entra no meio da leitura
    with deps.sessions() as session:
        second, more_2 = queries.jobs_after(
            session, conditions, forms=deps.forms, limit=2, after=after(first[-1]), with_lead=True
        )
    with deps.sessions() as session:
        third, more_3 = queries.jobs_after(
            session, conditions, forms=deps.forms, limit=2, after=after(second[-1]), with_lead=True
        )

    assert (more_1, more_2, more_3) == (True, True, False)
    rows = [*first, *second, *third]
    assert [row.lead.name for row in rows if row.lead] == [
        "Lead 4",
        "Lead 3",
        "Lead 2",
        "Lead 1",
        "Lead 0",
    ]  # o pedido novo ficou de fora, e nenhum se repetiu
    assert len({row.state.id for row in rows}) == 5


def test_jobs_with_the_same_instant_are_ordered_by_id_and_still_paginate(deps: Deps) -> None:
    made = three_jobs(deps)
    with deps.sessions.begin() as session:
        session.execute(sa.update(Job).values(created_at=datetime(2027, 1, 2, 3, 4, 5, tzinfo=UTC)))
    conditions = queries.JobFilters().conditions()

    seen: list[uuid.UUID] = []
    cursor = None
    while True:
        with deps.sessions() as session:
            rows, more = queries.jobs_after(
                session, conditions, forms=deps.forms, limit=1, after=cursor, with_lead=False
            )
        seen += [row.state.id for row in rows]
        if not more:
            break
        cursor = after(rows[-1])

    assert seen == sorted(seen, reverse=True)  # o `id` desempata, do maior para o menor
    assert set(seen) == {job.id for job in made.values()}
