"""O aviso dos dados vencidos de um formulário do banco (issue #48): a regra, pura e sem banco; a
leitura da versão atual de cada formulário; e o log da subida da API e do worker. O dia é sempre
injetado, então o resultado não depende do dia em que a suíte roda.

O `reforma-2027` do banco vem da migration `0008` (a janela de opção termina em 30/10/2026 e a de
desistência, em 20/12/2026). Os testes fixam a data de publicação dele, que a migration grava com o
relógio do sistema."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from conftest import legacy_definition
from fastapi.testclient import TestClient
from test_admin import FakeClock

from app.api.main import create_app
from app.db import make_engine, make_session_factory
from app.deps import Deps
from app.forms import data_notices as notices
from app.forms.data_notices import REVIEW_MAX_AGE_DAYS, DataNotice, data_notices
from app.forms.definition import fingerprint
from app.models import Form, FormVersion
from app.worker import ready_deps

LOGGER = "app.forms.data_notices"
SLUG = "exemplo"
TITLE = "Formulário Exemplo"
# Meio-dia em Brasília (UTC-3): o dia local é o mesmo em UTC.
PUBLISHED = datetime(2026, 10, 14, 15, tzinfo=UTC)
WHILE_OPEN = datetime(2026, 10, 20, 15, tzinfo=UTC)
AFTER_OPTION = datetime(2026, 10, 31, 15, tzinfo=UTC)
AFTER_BOTH = datetime(2026, 12, 21, 15, tzinfo=UTC)

OPTION = {
    "name": "opcao",
    "label": "Inscrição no projeto",
    "start": "2026-09-25",
    "end": "2026-10-30",
}
WITHDRAWAL = {
    "name": "desistencia",
    "label": "Revisão da inscrição",
    "start": "2026-11-03",
    "end": "2026-12-20",
}


def definition(*windows: dict[str, str], **blocks: Any) -> dict[str, Any]:
    """A parte da definição que o aviso lê: o título e os blocos de dados. As janelas vão no bloco
    `janelas`; `blocks` acrescenta outros blocos."""
    data: dict[str, Any] = {**blocks}
    if windows:
        data["janelas"] = {"kind": "windows", "windows": list(windows)}
    return {"title": TITLE, "data": data}


def of(
    document: dict[str, Any], now: datetime, *, published_at: datetime = PUBLISHED
) -> list[DataNotice]:
    return data_notices(SLUG, document, published_at=published_at, now=now)


# --- a regra, sem banco ---------------------------------------------------------------------------


def test_a_window_that_ended_names_the_form_the_block_the_window_and_the_date() -> None:
    (notice,) = of(definition(OPTION, WITHDRAWAL), AFTER_OPTION)

    assert (notice.slug, notice.title) == (SLUG, TITLE)
    assert notice.message == (
        "No bloco «janelas», já terminou a janela «Inscrição no projeto» (em 30/10/2026). "
        "Confira a fonte e, se as datas mudaram, publique uma versão nova."
    )


def test_a_block_whose_windows_all_ended_says_no_future_date_is_left() -> None:
    (notice,) = of(definition(OPTION, WITHDRAWAL), AFTER_BOTH, published_at=AFTER_BOTH)

    assert notice.message == (
        "No bloco «janelas», já terminaram a janela «Inscrição no projeto» (em 30/10/2026) e "
        "a janela «Revisão da inscrição» (em 20/12/2026). Nenhuma janela do bloco tem mais data "
        "futura. Confira a fonte e, se as datas mudaram, publique uma versão nova."
    )


def test_a_single_window_block_that_ended_says_no_future_date_is_left() -> None:
    (notice,) = of(definition(OPTION), AFTER_OPTION)

    assert "já terminou a janela «Inscrição no projeto» (em 30/10/2026)" in notice.message
    assert "Nenhuma janela do bloco tem mais data futura" in notice.message


def test_windows_that_are_open_or_still_to_come_give_no_notice() -> None:
    assert of(definition(OPTION, WITHDRAWAL), WHILE_OPEN) == []


def test_the_last_day_of_a_window_still_counts_as_open() -> None:
    last_day = datetime(2026, 10, 30, 18, tzinfo=UTC)

    assert of(definition(OPTION), last_day) == []


def test_the_day_is_the_leads_day_in_sao_paulo_not_utc() -> None:
    # 30/10 às 23h em Brasília já é 31/10 às 02h em UTC: a janela ainda está aberta.
    late_on_the_last_day = datetime(2026, 10, 31, 2, tzinfo=UTC)
    # 31/10 à 0h em Brasília: a janela terminou.
    first_minute_after = datetime(2026, 10, 31, 3, tzinfo=UTC)

    assert of(definition(OPTION), late_on_the_last_day) == []
    assert len(of(definition(OPTION), first_minute_after)) == 1


def test_a_window_without_a_label_is_named_by_its_name() -> None:
    nameless = {key: value for key, value in OPTION.items() if key != "label"}

    (notice,) = of(definition(nameless), AFTER_OPTION)

    assert "«opcao»" in notice.message


def test_each_windows_block_gets_its_own_notice_in_the_order_of_their_names() -> None:
    document = definition(OPTION)
    document["data"]["campanha"] = {
        "kind": "windows",
        "windows": [{"name": "lote", "start": "2026-09-01", "end": "2026-09-30"}],
    }

    found = of(document, AFTER_OPTION)

    assert [
        ("«campanha»" in notice.message, "«janelas»" in notice.message) for notice in found
    ] == [
        (True, False),
        (False, True),
    ]


def test_blocks_that_are_not_windows_never_give_a_notice() -> None:
    document = definition(
        value={"kind": "value", "value": {"fim": "2020-01-01"}},
        catalogo={"kind": "list", "items": ["aula 1"]},
        edicao={"kind": "edition", "name": "Edição 2026", "ends": "2020-01-01"},
    )

    assert of(document, AFTER_BOTH, published_at=PUBLISHED - timedelta(days=500)) == []


def test_a_block_that_is_not_a_valid_windows_block_is_skipped() -> None:
    document = definition(
        OPTION,
        quebrado={"kind": "windows", "windows": [{"name": "x", "start": "ontem", "end": "hoje"}]},
    )

    (notice,) = of(document, AFTER_OPTION)

    assert "«janelas»" in notice.message


def test_a_version_published_more_than_the_limit_ago_gives_the_review_notice() -> None:
    document = definition(WITHDRAWAL)  # as datas estão no futuro: só a revisão avisa
    limit = PUBLISHED + timedelta(days=REVIEW_MAX_AGE_DAYS)

    assert REVIEW_MAX_AGE_DAYS == 30
    assert of(document, limit) == []  # 30 dias: sem aviso
    (notice,) = of(document, limit + timedelta(days=1))  # 31 dias: com aviso
    assert notice.message == (
        "A versão atual foi publicada em 14/10/2026, há 31 dias: confira se as datas do bloco "
        "«janelas» ainda valem."
    )


def test_the_review_notice_names_every_windows_block() -> None:
    document = definition(WITHDRAWAL)
    document["data"]["campanha"] = {
        "kind": "windows",
        "windows": [{"name": "lote", "start": "2026-12-01", "end": "2026-12-30"}],
    }

    (notice,) = of(document, PUBLISHED + timedelta(days=45))

    assert "confira se as datas dos blocos «campanha» e «janelas» ainda valem" in notice.message


def test_the_review_age_counts_calendar_days_in_the_leads_timezone() -> None:
    # Publicada às 23h de 14/10 em Brasília (02h de 15/10 em UTC): o dia dela é 14/10.
    published = datetime(2026, 10, 15, 2, tzinfo=UTC)
    document = definition(WITHDRAWAL)

    # 14/11 às 12h em Brasília: 31 dias de calendário depois de 14/10.
    (notice,) = of(document, datetime(2026, 11, 14, 15, tzinfo=UTC), published_at=published)
    assert "publicada em 14/10/2026, há 31 dias" in notice.message
    # 13/11 às 23h em Brasília (14/11 em UTC): 30 dias, sem aviso.
    assert of(document, datetime(2026, 11, 14, 2, tzinfo=UTC), published_at=published) == []


def test_a_form_without_a_windows_block_never_gets_the_review_notice() -> None:
    mcc_like = definition(edicao={"kind": "edition", "name": "Edição 2026", "ends": "2027-05-31"})

    assert of(mcc_like, PUBLISHED + timedelta(days=400)) == []
    assert of({"title": TITLE}, PUBLISHED + timedelta(days=400)) == []  # sem dados


def test_a_stale_version_with_an_ended_window_gets_both_notices_window_first() -> None:
    window, review = of(definition(OPTION, WITHDRAWAL), AFTER_BOTH)  # 68 dias depois da publicação

    assert window.message.startswith("No bloco «janelas», já terminaram")
    assert review.message.startswith("A versão atual foi publicada em 14/10/2026, há 68 dias")


# --- o reforma-2027 do banco, em três dias -------------------------------------------------------


def test_the_reforma_2027_definition_has_no_window_notice_while_the_option_window_is_open() -> None:
    assert of(legacy_definition("reforma-2027"), WHILE_OPEN) == []


def test_the_reforma_2027_definition_warns_about_the_option_window_from_the_day_after_it_closes() -> (
    None
):
    (notice,) = of(legacy_definition("reforma-2027"), AFTER_OPTION)

    assert "«janelas»" in notice.message
    assert "«Inscrição no projeto» (em 30/10/2026)" in notice.message
    assert "Desistência" not in notice.message  # a de desistência vai até 20/12


def test_the_reforma_2027_definition_gets_both_notices_after_the_withdrawal_window() -> None:
    window, review = of(legacy_definition("reforma-2027"), AFTER_BOTH)

    assert "Nenhuma janela do bloco tem mais data futura" in window.message
    assert "«Revisão da inscrição» (em 20/12/2026)" in window.message
    assert "há 68 dias" in review.message


# --- a leitura do banco ---------------------------------------------------------------------------


def publish_version(
    deps: Deps, slug: str, number: int, document: dict[str, Any], published_at: datetime
) -> None:
    """Grava uma versão publicada em `published_at`. O banco não deixa mudar uma versão (migration
    0005), e a `0008` grava a publicação da versão 1 com o relógio do sistema: o teste publica uma
    versão seguinte com a data que quer."""
    with deps.sessions.begin() as session:
        form_id = session.scalar(sa.select(Form.id).where(Form.slug == slug))
        session.add(
            FormVersion(
                form_id=form_id,
                number=number,
                definition=document,
                fingerprint=fingerprint(document),
                note=f"Versão {number}.",
                published_by="operadora",
                published_at=published_at,
            )
        )


def pin_reforma_2027(deps: Deps, published_at: datetime = PUBLISHED) -> None:
    """A versão atual do reforma-2027 passa a ser uma publicada em `published_at`, com as mesmas
    datas da versão 1 da migration."""
    publish_version(deps, "reforma-2027", 2, legacy_definition("reforma-2027"), published_at)


def test_the_notices_of_the_forms_in_the_database_come_from_the_current_version(
    deps: Deps,
) -> None:
    pin_reforma_2027(deps)

    with deps.sessions() as session:
        found = notices.current_notices(session, AFTER_OPTION)

    assert list(found) == ["reforma-2027"]  # o demo e o MCC não têm bloco de janelas
    (notice,) = found["reforma-2027"]
    assert notice.slug == "reforma-2027" and "«Inscrição no projeto»" in notice.message


def test_a_newer_version_with_future_dates_takes_the_notice_away(deps: Deps) -> None:
    pin_reforma_2027(deps)
    newer = legacy_definition("reforma-2027")
    for window in newer["data"]["janelas"]["windows"]:
        window["end"] = "2027-12-31"
    publish_version(deps, "reforma-2027", 3, newer, AFTER_OPTION)

    with deps.sessions() as session:
        assert notices.current_notices(session, AFTER_OPTION) == {}


def test_a_form_without_a_published_version_has_no_notice(deps: Deps) -> None:
    with deps.sessions.begin() as session:
        session.add(Form(slug="so-rascunho", title="Só rascunho", active=False, created_by="x"))

    with deps.sessions() as session:
        assert "so-rascunho" not in notices.current_notices(session, AFTER_BOTH)


# --- o log da subida ------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def logger_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # O `fileConfig` do alembic, que a suíte roda na subida, desliga os loggers que já existiam (a
    # API e o worker não rodam o alembic).
    for name in (LOGGER, "app.api.main", "app.worker"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)


def warnings_of(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and record.levelno == logging.WARNING
    ]


def start_api(moment: datetime) -> None:
    clock = FakeClock()
    clock.now = moment.timestamp()

    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return "run-fake"

    with TestClient(create_app(trigger=fake_trigger, clock=clock)):
        pass  # só a subida


def test_the_api_logs_one_warning_per_notice_on_startup(
    deps: Deps, caplog: pytest.LogCaptureFixture
) -> None:
    pin_reforma_2027(deps)

    with caplog.at_level(logging.INFO):
        start_api(AFTER_OPTION)

    (message,) = warnings_of(caplog)
    assert message == (
        "formulário reforma-2027: No bloco «janelas», já terminou a janela «Inscrição no "
        "projeto» (em 30/10/2026). Confira a fonte e, se as datas mudaram, publique uma versão "
        "nova."
    )


def test_the_api_logs_both_notices_when_the_window_ended_and_the_version_is_old(
    deps: Deps, caplog: pytest.LogCaptureFixture
) -> None:
    pin_reforma_2027(deps)

    with caplog.at_level(logging.INFO):
        start_api(AFTER_BOTH)

    window, review = warnings_of(caplog)
    assert "Nenhuma janela do bloco tem mais data futura" in window
    assert "há 68 dias" in review
    assert all(message.startswith("formulário reforma-2027: ") for message in (window, review))


def test_the_api_stays_quiet_on_startup_while_the_window_is_open(
    deps: Deps, caplog: pytest.LogCaptureFixture
) -> None:
    pin_reforma_2027(deps)

    with caplog.at_level(logging.INFO):
        start_api(WHILE_OPEN)

    assert warnings_of(caplog) == []


class DatabaseDown:
    """As sessões de um Postgres que não responde."""

    def __call__(self) -> Any:
        raise sa.exc.OperationalError("select 1", {}, Exception("connection refused"))

    begin = __call__


def skipped_notices_of(caplog: pytest.LogCaptureFixture, logger_name: str) -> list[str]:
    """Os WARNING do `logger_name` que dizem que os avisos ficaram de fora desta subida."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == logger_name
        and record.levelno == logging.WARNING
        and record.getMessage().startswith("avisos dos formulários: ")
    ]


@pytest.fixture
def before_the_migrations(
    deps: Deps, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """O banco do primeiro deploy da stack do Swarm: a API e o worker sobem antes das migrations,
    então o Postgres responde, mas sem as tabelas (um schema vazio)."""
    engine = make_engine(database_url)
    with engine.begin() as connection:
        connection.execute(sa.text("create schema if not exists t50_before_the_migrations"))
    empty = sa.create_engine(
        database_url,
        connect_args={
            "options": "-csearch_path=t50_before_the_migrations",
            "prepare_threshold": None,
        },
    )
    monkeypatch.setattr(deps, "sessions", make_session_factory(empty))
    try:
        yield
    finally:
        empty.dispose()
        with engine.begin() as connection:
            connection.execute(sa.text("drop schema t50_before_the_migrations cascade"))
        engine.dispose()


def test_the_api_starts_without_a_warning_when_the_database_is_down(
    deps: Deps, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deps, "sessions", DatabaseDown())

    with caplog.at_level(logging.INFO):
        start_api(AFTER_BOTH)  # sobe sem derrubar

    assert warnings_of(caplog) == []
    (skipped,) = skipped_notices_of(caplog, "app.api.main")
    assert "na subida da API" in skipped


def test_the_api_starts_before_the_migrations_ran(
    before_the_migrations: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        start_api(AFTER_BOTH)  # a tabela forms não existe: sobe sem derrubar

    assert warnings_of(caplog) == []
    (skipped,) = skipped_notices_of(caplog, "app.api.main")
    assert "na subida da API" in skipped


def test_the_worker_starts_before_the_migrations_ran(
    before_the_migrations: None, deps: Deps, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        assert ready_deps(now=AFTER_BOTH) is deps  # a tabela forms não existe: segue

    assert warnings_of(caplog) == []
    (skipped,) = skipped_notices_of(caplog, "app.worker")
    assert "na subida do worker" in skipped


def test_the_worker_starts_without_a_warning_when_the_database_drops_after_the_connection_check(
    deps: Deps, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def database_down(session: Any) -> Any:
        raise sa.exc.OperationalError("select 1", {}, Exception("connection refused"))

    # A conexão que `ready_deps` confere responde; a leitura dos formulários, logo depois, não.
    monkeypatch.setattr(notices.stored_forms, "current_versions", database_down)

    with caplog.at_level(logging.INFO):
        assert ready_deps(now=AFTER_BOTH) is deps

    assert warnings_of(caplog) == []
    (skipped,) = skipped_notices_of(caplog, "app.worker")
    assert "na subida do worker" in skipped


def test_the_worker_still_refuses_to_start_when_the_database_is_down(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deps, "sessions", DatabaseDown())

    with pytest.raises(sa.exc.OperationalError):  # a conexão é conferida antes dos avisos
        ready_deps(now=AFTER_BOTH)


def test_the_worker_logs_the_notices_before_taking_tasks(
    deps: Deps, caplog: pytest.LogCaptureFixture
) -> None:
    pin_reforma_2027(deps)

    with caplog.at_level(logging.INFO):
        assert ready_deps(now=AFTER_OPTION) is deps

    (message,) = warnings_of(caplog)
    assert message.startswith("formulário reforma-2027: ") and "30/10/2026" in message


def test_the_worker_stays_quiet_while_the_window_is_open(
    deps: Deps, caplog: pytest.LogCaptureFixture
) -> None:
    pin_reforma_2027(deps)

    with caplog.at_level(logging.INFO):
        ready_deps(now=WHILE_OPEN)

    assert warnings_of(caplog) == []
