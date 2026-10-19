"""A lista e o detalhe dos formulários (`app.admin.queries.forms`): o estado de cada formulário, sem
rótulo nem data de Brasília. A página o decora (`app.admin.views`, com os testes de
`test_admin_form_pages.py`) e a API o serializa. Os prazos de retenção têm os testes no fim."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from conftest import legacy_definition
from test_admin_forms import DEFINITION, SLUG, store_version
from test_intake import submit

from app import destinations
from app.admin.queries import forms as queries
from app.deps import Deps
from app.forms import drafts, stored
from app.forms import retention as form_retention
from app.integrations import config as integration_config
from app.models import Form
from app.retention import Due

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
BASE = "https://planos.example.test/"


def import_demo_definition(deps: Deps) -> None:
    stored.import_definition(deps, DEFINITION, actor="testes")


def summaries(deps: Deps) -> dict[str, queries.FormSummary]:
    with deps.sessions() as session:
        found = queries.form_summaries(session, deps.forms, public_base_url=BASE, now=NOW)
    return {row.slug: row for row in found}


def detail(deps: Deps, slug: str, **settings: int) -> queries.FormDetail | None:
    with deps.sessions() as session:
        return queries.form_detail(
            session,
            slug,
            deps.forms,
            public_base_url=BASE,
            retention_pdf_days=settings.get("pdf", 30),
            retention_days=settings.get("request", 60),
            now=NOW,
        )


def pending_draft(deps: Deps, slug: str, **changes: Any) -> None:
    """Um rascunho que difere da versão atual, gravado como o editor o grava."""
    with deps.sessions() as session:
        draft = drafts.load(session, slug)
    assert draft is not None
    texts = drafts.texts_of({**draft.document, **changes})
    drafts.save(deps, slug, texts, actor="testes", expected=draft.token)


# --- a lista ------------------------------------------------------------------------------------


def test_the_list_has_every_form_by_slug_with_the_codes_of_the_database(deps: Deps) -> None:
    import_demo_definition(deps)

    found = summaries(deps)

    assert list(found) == ["demo", SLUG, "mcc", "reforma-2027"]
    demo = found["demo"]
    assert (demo.title, demo.active, demo.version_number) == (
        "Projeto de demonstração",
        True,
        1,
    )
    assert demo.published_at is not None and demo.published_at.tzinfo is not None
    assert demo.compiles is True and demo.draft_pending is False
    assert demo.public_url == "https://planos.example.test/p/demo"
    assert demo.submissions == 0


def test_the_list_counts_the_submissions_of_each_form(deps: Deps) -> None:
    submit(deps, key="primeiro")
    submit(deps, key="segundo")

    assert summaries(deps)["demo"].submissions == 2
    assert summaries(deps)["mcc"].submissions == 0


def test_an_inactive_form_is_listed_as_inactive(deps: Deps) -> None:
    stored.set_active(deps.sessions, "mcc", False, actor="testes")

    found = summaries(deps)

    assert (found["mcc"].active, found["demo"].active) == (False, True)


def test_a_form_created_from_scratch_has_no_version_and_a_pending_draft(deps: Deps) -> None:
    drafts.create(deps, title="Gestão do escritório", slug="gestao", actor="testes")

    row = summaries(deps)["gestao"]

    assert (row.version_number, row.published_at, row.compiles) == (None, None, None)
    assert row.draft_pending is True and row.notices == ()


def test_a_form_whose_current_version_no_longer_compiles_says_so(deps: Deps) -> None:
    import_demo_definition(deps)
    store_version(deps, 2, {**json.loads(DEFINITION), "minutes": 0})

    row = summaries(deps)[SLUG]

    assert (row.version_number, row.compiles) == (2, False)


def test_a_form_with_a_pending_draft_is_flagged(deps: Deps) -> None:
    pending_draft(deps, "mcc", intro="Outra introdução.")

    found = summaries(deps)

    assert (found["mcc"].draft_pending, found["demo"].draft_pending) == (True, False)


def test_the_list_carries_the_notices_of_the_data_to_review(deps: Deps) -> None:
    after_both = datetime(
        2027, 6, 1, 12, tzinfo=UTC
    )  # as janelas de datas do reforma-2027 acabaram

    with deps.sessions() as session:
        rows = queries.form_summaries(session, deps.forms, public_base_url=BASE, now=after_both)

    notices = {row.slug: row.notices for row in rows}
    assert any("Inscrição no projeto" in item for item in notices["reforma-2027"])
    assert notices["demo"] == ()


# --- o detalhe ----------------------------------------------------------------------------------


def test_a_form_that_does_not_exist_has_no_detail(deps: Deps) -> None:
    assert detail(deps, "nao-existe") is None


def test_the_detail_gives_the_published_version_and_the_history_newest_first(deps: Deps) -> None:
    import_demo_definition(deps)
    store_version(deps, 2, {**json.loads(DEFINITION), "intro": "A segunda versão."})

    found = detail(deps, SLUG)

    assert found is not None
    assert (found.slug, found.title, found.active) == (
        SLUG,
        "Projeto de demonstração",
        True,
    )
    assert found.public_url == f"https://planos.example.test/p/{SLUG}"
    published = found.published
    assert published is not None and published.number == 2 and published.compiles is True
    assert published.text and published.text.endswith("v2")  # o rótulo do envio e do webhook
    assert (published.published_by, published.note) == ("operadora", "Versão 2")
    assert [entry.number for entry in found.history] == [2, 1]
    assert found.history[0].fingerprint == published.fingerprint
    assert found.unpublished is False and found.spec is not None and found.definition is not None


def test_the_detail_of_a_form_without_a_version_reads_its_draft(deps: Deps) -> None:
    drafts.create(deps, title="Gestão do escritório", slug="gestao", actor="testes")

    found = detail(deps, "gestao")

    assert found is not None and found.published is None and found.unpublished is True
    assert found.history == ()
    assert found.spec is not None and found.definition is not None  # a leitura é a do rascunho
    assert found.draft.pending is True and found.notices == ()


def test_the_detail_of_a_version_that_no_longer_compiles_has_no_contract(deps: Deps) -> None:
    import_demo_definition(deps)
    store_version(deps, 2, {**json.loads(DEFINITION), "minutes": 0})

    found = detail(deps, SLUG)

    assert found is not None and found.published is not None
    assert (found.published.number, found.published.compiles) == (2, False)
    assert found.published.text is None
    assert found.spec is None and found.definition is None


def test_the_detail_tells_which_destination_serves_the_form(deps: Deps) -> None:
    def save(slug: str | None, host: str) -> None:
        with deps.sessions.begin() as session:
            destinations.save(
                session,
                deps.cipher,
                form_slug=slug,
                url=f"https://{host}/entrada",
                actor="testes",
                expected_version=0,
                production=False,
                http_confirmed=False,
            )

    none = detail(deps, "demo")
    save(None, "padrao.example.test")
    default = detail(deps, "demo")
    save("demo", "proprio.example.test")
    own = detail(deps, "demo")

    assert none is not None and none.destination is None
    assert default is not None and default.destination == queries.Serving(
        "default", "padrao.example.test"
    )
    assert own is not None and own.destination == queries.Serving("form", "proprio.example.test")


def test_the_detail_gives_the_draft_as_it_is(deps: Deps) -> None:
    pending_draft(deps, "mcc", intro="Outra introdução.")

    found = detail(deps, "mcc")

    assert found is not None
    draft = found.draft
    assert draft.pending is True and draft.updated_by == "testes" and draft.base_version == 1
    assert draft.current is not None and draft.current.number == 1
    assert draft.token and draft.document["intro"] == "Outra introdução."


def test_the_detail_counts_only_the_submissions_of_the_form(deps: Deps) -> None:
    submit(deps, key="um")

    assert detail(deps, "demo").submissions == 1  # type: ignore[union-attr]
    assert detail(deps, "mcc").submissions == 0  # type: ignore[union-attr]


def test_the_detail_carries_the_notices_of_the_published_version(deps: Deps) -> None:
    after_both = datetime(2027, 6, 1, 12, tzinfo=UTC)

    with deps.sessions() as session:
        found = queries.form_detail(
            session,
            "reforma-2027",
            deps.forms,
            public_base_url=BASE,
            retention_pdf_days=30,
            retention_days=60,
            now=after_both,
        )

    assert found is not None and found.notices
    assert detail(deps, "reforma-2027").notices == ()  # type: ignore[union-attr]


def test_the_detail_gives_the_retention_of_the_form_and_the_defaults(deps: Deps) -> None:
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(Form)
            .where(Form.slug == "mcc")
            .values(retention_pdf_days=20, retention_days=45)
        )

    own = detail(deps, "mcc", pdf=30, request=60)
    default = detail(deps, "demo", pdf=30, request=60)

    assert own is not None and default is not None
    retention = own.retention
    assert (retention.own.pdf_days, retention.own.request_days) == (20, 45)
    assert (retention.default_pdf_days, retention.default_request_days) == (30, 60)
    assert tuple(retention.in_force) == (20, 45) and retention.version == "20:45"
    assert tuple(default.retention.in_force) == (30, 60) and default.retention.version == ":"


def test_the_detail_gives_the_integration_without_the_token(deps: Deps) -> None:
    none = detail(deps, "demo")
    with deps.sessions.begin() as session:
        integration_config.connect(
            session,
            deps.cipher,
            form_slug="demo",
            base_url="https://baserow.example.test",
            table_id=77,
            token="token-de-teste",
            actor="testes",
            expected_version=0,
            production=False,
        )

    found = detail(deps, "demo")

    assert none is not None and none.integration == queries.IntegrationSummary(
        configured=False, active=None, host=None, table_id=None, pending=0, failed=0
    )
    assert found is not None
    assert found.integration == queries.IntegrationSummary(
        configured=True,
        active=False,
        host="baserow.example.test",
        table_id=77,
        pending=0,
        failed=0,
    )
    assert "token-de-teste" not in repr(found.integration)


def test_the_definition_of_the_detail_is_the_one_the_database_keeps(deps: Deps) -> None:
    found = detail(deps, "demo")

    assert found is not None and found.definition is not None
    assert found.definition.model_dump(mode="json")["slug"] == legacy_definition("demo")["slug"]


# --- os prazos de retenção ----------------------------------------------------------------------


def retention_of(deps: Deps, slug: str) -> queries.FormRetention | None:
    with deps.sessions() as session:
        return queries.form_retention_of(session, slug, deps.settings)


def test_the_retention_of_a_form_that_does_not_exist_is_none(deps: Deps) -> None:
    assert retention_of(deps, "nao-existe") is None


def test_the_retention_of_a_form_gives_its_periods_the_defaults_and_the_limits(deps: Deps) -> None:
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(Form)
            .where(Form.slug == "mcc")
            .values(retention_pdf_days=20, retention_days=45)
        )

    own = retention_of(deps, "mcc")
    default = retention_of(deps, "demo")

    assert own is not None and default is not None
    assert (own.slug, own.title) == ("mcc", "Planejamento de atividades")
    assert (own.retention.own.pdf_days, own.retention.own.request_days) == (20, 45)
    assert own.retention.version == "20:45" and default.retention.version == ":"
    settings = deps.settings
    assert tuple(default.retention.in_force) == (
        settings.retention_pdf_days,
        settings.retention_days,
    )
    assert (own.link_ttl_days, own.max_days) == (settings.link_ttl_days, 3650)


def test_a_reduction_keeps_both_counts_and_the_token_that_comes_back_in_the_confirmation() -> None:
    reduction = queries.reduction_of(Due(requests=3, pdfs=5), Due(requests=1, pdfs=0))

    assert reduction.due == Due(3, 5) and reduction.today == Due(1, 0)
    assert reduction.seen == "3:5" and form_retention.parse_seen(reduction.seen) == Due(3, 5)
