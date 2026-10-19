"""O banco de teste de cada worker do pytest-xdist (`worker_database_url` e
`create_database_if_missing`, em `tests_app/conftest.py`): `<nome>_test` vira
`<nome>_<worker>_test`, o banco nasce na primeira vez e a suíte serial segue com o da URL."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from conftest import BASE_DATABASE_URL, create_database_if_missing, worker_database_url


def url_of(database: str, *, query: dict[str, str] | None = None) -> str:
    return sa.engine.URL.create(
        "postgresql+psycopg",
        username="mcc",
        password="nao-e-senha",
        host="db.example.test",
        port=5432,
        database=database,
        query=query or {},
    ).render_as_string(hide_password=False)


def test_without_a_worker_the_url_stays_as_it_came() -> None:
    url = url_of("mcc_app_test")

    assert worker_database_url(url, None) == url
    assert worker_database_url(url, "") == url


def test_each_worker_gets_its_own_database_with_the_test_suffix() -> None:
    url = url_of("mcc_app_test")

    assert worker_database_url(url, "gw0") == url_of("mcc_app_gw0_test")
    assert worker_database_url(url, "gw12") == url_of("mcc_app_gw12_test")


def test_the_worker_database_keeps_the_user_the_password_and_the_server_options() -> None:
    url = url_of("mcc_app_test", query={"sslmode": "disable"})

    renamed = sa.engine.make_url(worker_database_url(url, "gw1"))

    assert renamed.database == "mcc_app_gw1_test"
    assert (renamed.username, renamed.password, renamed.host, renamed.port) == (
        "mcc",
        "nao-e-senha",
        "db.example.test",
        5432,
    )
    assert renamed.query == {"sslmode": "disable"}


def test_a_database_without_the_test_suffix_is_left_for_the_safeguard_to_refuse() -> None:
    url = url_of("producao")

    assert worker_database_url(url, "gw0") == url


def test_a_database_name_that_is_not_a_plain_identifier_is_refused_before_connecting() -> None:
    with pytest.raises(RuntimeError, match="nome de banco de teste inesperado"):
        create_database_if_missing("postgresql+psycopg://mcc@127.0.0.1:1/com-hifen_test")


@pytest.fixture
def scratch_database() -> Iterator[str]:
    """A URL de um banco que não existe, ao lado do de teste; se existir ao fim, é apagado."""
    base = sa.engine.make_url(BASE_DATABASE_URL)
    maintenance = sa.create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = f"{(base.database or '').removesuffix('_test')}_pt{uuid.uuid4().hex[:8]}_test"
    try:
        with maintenance.connect() as connection:
            allowed = connection.execute(
                sa.text("select rolcreatedb or rolsuper from pg_roles where rolname = current_user")
            ).scalar()
        if not allowed:
            pytest.skip("o usuário de TEST_DATABASE_URL não pode criar bancos (CREATEDB)")
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        with maintenance.connect() as connection:
            connection.execute(sa.text(f'drop database if exists "{name}" with (force)'))
        maintenance.dispose()


def exists(url: str) -> bool:
    target = sa.engine.make_url(url)
    maintenance = sa.create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with maintenance.connect() as connection:
            return bool(
                connection.execute(
                    sa.text("select 1 from pg_database where datname = :name"),
                    {"name": target.database},
                ).scalar()
            )
    finally:
        maintenance.dispose()


def test_the_database_of_a_worker_is_created_the_first_time_and_reused_after(
    scratch_database: str,
) -> None:
    assert not exists(scratch_database)

    create_database_if_missing(scratch_database)
    create_database_if_missing(scratch_database)  # o banco já existe: nada a fazer

    assert exists(scratch_database)
    engine = sa.create_engine(scratch_database)
    try:
        with engine.connect() as connection:
            assert connection.execute(sa.text("select 1")).scalar() == 1
    finally:
        engine.dispose()
