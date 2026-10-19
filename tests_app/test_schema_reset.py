"""O reset destrutivo da suíte só alcança o banco de teste que a conexão confirma."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from conftest import reset_test_schema


@pytest.mark.parametrize(
    "database", ["leadkit", "leadkit_test_backup", "other-test", "name-with-dash_test"]
)
def test_non_test_database_is_refused_before_connecting(
    monkeypatch: pytest.MonkeyPatch, database: str
) -> None:
    engine = MagicMock()
    monkeypatch.setattr("conftest.make_engine", engine)
    with pytest.raises(RuntimeError, match="banco de teste"):
        reset_test_schema(f"postgresql+psycopg://synthetic@127.0.0.1:1/{database}")
    engine.assert_not_called()


def test_connected_database_must_match_the_test_url_before_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value
    connection.exec_driver_sql.return_value.scalar_one.return_value = "another_test"
    monkeypatch.setattr("conftest.make_engine", lambda _: engine)
    with pytest.raises(RuntimeError, match="banco conectado"):
        reset_test_schema("postgresql+psycopg://synthetic@127.0.0.1:1/intended_test")
    assert [call.args[0] for call in connection.exec_driver_sql.call_args_list] == [
        "SELECT current_database()"
    ]
    engine.dispose.assert_called_once()
