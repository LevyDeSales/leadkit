"""O `DatabaseReset` do `conftest.py`, que o `deps` usa para deixar o banco como todo teste o espera.

Ele só esvazia as tabelas em que o teste anterior deixou linhas, e mantém os três formulários
migrados enquanto eles são os da última semeadura. Estes testes provam as duas metades: o banco sai
sempre igual (vazio, com os três formulários na versão 1, qualquer que seja a tabela que o teste
sujou) e os formulários só são semeados de novo quando mudaram.
"""

from __future__ import annotations

import threading
import time

import conftest
import pytest
import sqlalchemy as sa
from conftest import LEGACY_SLUGS, TABLE_NAMES, DatabaseReset
from sqlalchemy.orm import Session
from test_intake import submit

from app.deps import Deps

AUDIT = (
    "insert into audit_log (id, at, actor, action, target_type, target_id, details) "
    "values (gen_random_uuid(), now(), 'testes', 'teste', 'teste', 'x', '{}')"
)
EXTRA_FORM = (
    "insert into forms (id, slug, title, active, created_by, created_at) "
    "values (gen_random_uuid(), 'extra', 'Extra', true, 'testes', now())"
)
DRAFT_OF_DEMO = (
    "insert into form_drafts (form_id, definition, fingerprint, base_version, updated_by, updated_at) "
    "select id, '{}'::jsonb, 'f', 1, 'testes', now() from forms where slug = 'demo'"
)


def counts(engine: sa.Engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            name: connection.execute(sa.text(f"select count(*) from {name}")).scalar_one()
            for name in TABLE_NAMES
        }


def form_ids(engine: sa.Engine) -> dict[str, object]:
    with engine.connect() as connection:
        return dict(connection.execute(sa.text("select slug, id from forms")).all())


def run(engine: sa.Engine, *statements: str) -> None:
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(sa.text(statement))


def only_the_migrated_forms() -> dict[str, int]:
    return {name: 3 if name in ("forms", "form_versions") else 0 for name in TABLE_NAMES}


def test_a_new_reset_leaves_only_the_three_migrated_forms_and_their_versions(
    shared_engine: sa.Engine, deps: Deps
) -> None:
    run(shared_engine, AUDIT, EXTRA_FORM)
    submit(deps)

    DatabaseReset().reset(shared_engine)

    assert counts(shared_engine) == only_the_migrated_forms()
    assert set(form_ids(shared_engine)) == set(LEGACY_SLUGS)


def test_rows_in_other_tables_are_emptied_and_the_migrated_forms_are_not_touched(
    shared_engine: sa.Engine, database_reset: DatabaseReset, deps: Deps
) -> None:
    before = form_ids(shared_engine)
    submit(deps)  # a lead, a submission and a job
    run(shared_engine, AUDIT)
    assert counts(shared_engine)["jobs"] == 1

    database_reset.reset(shared_engine)

    assert counts(shared_engine) == only_the_migrated_forms()
    assert form_ids(shared_engine) == before  # as mesmas linhas: ninguém os semeou de novo


def test_a_table_that_points_to_the_forms_is_emptied_without_seeding_them_again(
    shared_engine: sa.Engine, database_reset: DatabaseReset, deps: Deps
) -> None:
    before = form_ids(shared_engine)
    run(shared_engine, DRAFT_OF_DEMO)
    assert counts(shared_engine)["form_drafts"] == 1

    database_reset.reset(shared_engine)

    assert counts(shared_engine) == only_the_migrated_forms()
    assert form_ids(shared_engine) == before


@pytest.mark.parametrize(
    "change",
    [
        EXTRA_FORM,  # um formulário a mais
        "update forms set active = false where slug = 'mcc'",  # um formulário mudado
        "update forms set title = title || '!' where slug = 'demo'",
        "truncate " + ", ".join(TABLE_NAMES) + " cascade",  # um teste que esvaziou tudo
    ],
    ids=["extra form", "changed form", "changed title", "everything emptied"],
)
def test_a_change_to_the_migrated_forms_brings_them_back_as_they_were(
    shared_engine: sa.Engine, database_reset: DatabaseReset, deps: Deps, change: str
) -> None:
    run(shared_engine, change)

    database_reset.reset(shared_engine)

    assert counts(shared_engine) == only_the_migrated_forms()
    with shared_engine.connect() as connection:
        active = dict(connection.execute(sa.text("select slug, active from forms")).all())
        titles = dict(connection.execute(sa.text("select slug, title from forms")).all())
    assert active == dict.fromkeys(LEGACY_SLUGS, True)
    assert not any(title.endswith("!") for title in titles.values())


def test_a_database_somebody_else_rebuilt_is_seeded_again(
    shared_engine: sa.Engine, database_reset: DatabaseReset, deps: Deps
) -> None:
    """Um teste que refez o banco (as migrations) deixa nele outros formulários: o reset não confia
    no que viu da última vez."""
    before = form_ids(shared_engine)
    run(shared_engine, "truncate forms cascade", EXTRA_FORM)

    database_reset.reset(shared_engine)

    assert counts(shared_engine) == only_the_migrated_forms()
    assert set(form_ids(shared_engine)) == set(before)
    assert form_ids(shared_engine) != before  # outras linhas: foram semeadas de novo


def wait_until_running(engine: sa.Engine, fragment: str) -> None:
    """Espera uma sessão estar rodando uma consulta com este trecho (ativa, e não parada)."""
    for _ in range(200):
        with engine.connect() as watcher:
            running = watcher.execute(
                sa.text(
                    "select count(*) from pg_stat_activity where state = 'active' "
                    "and pid <> pg_backend_pid() and query like :fragment"
                ),
                {"fragment": f"%{fragment}%"},
            ).scalar_one()
        if running:
            return
        time.sleep(0.02)
    raise AssertionError(f"nenhuma sessão rodando {fragment!r}")


@pytest.mark.parametrize("kind", ["connection", "orm session"])
def test_a_session_left_open_makes_the_reset_fail_with_its_last_query(
    shared_engine: sa.Engine,
    database_reset: DatabaseReset,
    deps: Deps,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Como o `TRUNCATE` das 18 tabelas fazia: a sessão esquecida do teste anterior reprova o seguinte,
    mesmo que ela não segure lock nenhum e que nenhuma tabela esteja suja."""
    monkeypatch.setattr(conftest, "LOCK_WAIT", 0.3)
    forgotten: sa.Connection | Session = (
        shared_engine.connect() if kind == "connection" else deps.sessions()
    )
    try:
        forgotten.execute(sa.text("select 'consulta esquecida'"))  # abre a transação e não fecha

        with pytest.raises(RuntimeError, match="deixou uma sessão aberta") as error:
            database_reset.reset(shared_engine)

        assert "consulta esquecida" in str(error.value)
    finally:
        forgotten.rollback()
        forgotten.close()
    database_reset.reset(shared_engine)
    assert counts(shared_engine) == only_the_migrated_forms()


def test_a_session_that_closes_within_the_wait_is_not_an_error(
    shared_engine: sa.Engine,
    database_reset: DatabaseReset,
    deps: Deps,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uma thread do teste anterior que ainda termina a transação dela tem o tempo da espera."""
    monkeypatch.setattr(conftest, "LOCK_WAIT", 5.0)
    finishing = shared_engine.connect()
    finishing.execute(sa.text("select 1"))
    closer = threading.Timer(0.3, finishing.rollback)
    closer.start()
    try:
        database_reset.reset(shared_engine)
    finally:
        closer.join()
        finishing.close()

    assert counts(shared_engine) == only_the_migrated_forms()


def test_a_lock_held_by_a_running_query_stops_the_truncate_and_names_the_wait(
    shared_engine: sa.Engine,
    database_reset: DatabaseReset,
    deps: Deps,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uma sessão que não está parada (está numa consulta) e segura o lock de uma tabela suja não
    deixa o `TRUNCATE` esperar para sempre: ele desiste depois da espera e diz por quê."""
    monkeypatch.setattr(conftest, "LOCK_WAIT", 0.3)
    run(shared_engine, AUDIT)
    holding = shared_engine.connect()
    holding.execute(sa.text("lock table audit_log in share mode"))
    busy = threading.Thread(target=holding.execute, args=(sa.text("select pg_sleep(1.5)"),))
    busy.start()
    try:
        wait_until_running(shared_engine, "pg_sleep")  # a sessão deixa de estar parada
        with pytest.raises(RuntimeError, match="esperou .* por um lock"):
            database_reset.reset(shared_engine)
    finally:
        busy.join()
        holding.rollback()
        holding.close()
    database_reset.reset(shared_engine)
    assert counts(shared_engine) == only_the_migrated_forms()
