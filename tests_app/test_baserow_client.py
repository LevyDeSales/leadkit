"""O cliente da API do Baserow contra o Baserow de mentira (`httpx.MockTransport`): os campos, o
filtro, a criação e a atualização de uma linha, e o que cada falha vira."""

from __future__ import annotations

import httpx
import pytest
from conftest import BASEROW_TABLE, BASEROW_TOKEN, BASEROW_URL, FakeBaserow

from app.errors import FailureCode, RetryableError, TerminalError
from app.integrations.baserow import Connection


def connect(fake: FakeBaserow) -> tuple[Connection, FakeBaserow]:
    return Connection(BASEROW_URL, BASEROW_TABLE, BASEROW_TOKEN), fake


def test_the_fields_come_with_their_types_and_select_options() -> None:
    fake = FakeBaserow()
    connection, _ = connect(fake)

    fields = {item.name: item for item in fake.transport().fields(connection)}

    assert fields["Nome"].primary and fields["Nome"].type == "text"
    assert fields["Criado em"].read_only
    team = fields["Equipe"]
    assert team.type == "single_select"
    assert [option.value for option in team.options] == [
        "Só eu",
        "De 2 a 5 pessoas",
        "De 6 a 15 pessoas",
        "Mais de 15 pessoas",
    ]
    assert fields["Nome"].options == ()
    assert fake.calls == [("GET", f"fields/table/{BASEROW_TABLE}/")]
    assert fake.requests[0].headers["Authorization"] == f"Token {BASEROW_TOKEN}"


def test_a_row_is_found_by_the_key_field_created_and_updated() -> None:
    fake = FakeBaserow()
    connection, _ = connect(fake)
    transport = fake.transport()

    assert transport.find_rows(connection, 104, "abc123") == []
    row_id = transport.create_row(connection, {"field_104": "abc123", "field_101": "Ana"})
    assert transport.find_rows(connection, 104, "abc123") == [row_id]
    assert transport.update_row(connection, row_id, {"field_101": "Ana Maria"}) == row_id

    assert fake.rows[row_id]["field_101"] == "Ana Maria"
    assert fake.rows[row_id]["field_104"] == "abc123"  # o PATCH só mexe no que foi enviado
    assert "filter__field_104__equal=abc123" in str(fake.requests[0].url)
    assert "size=2" in str(fake.requests[0].url)


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_token_is_a_definitive_failure_that_says_so(status: int) -> None:
    fake = FakeBaserow()
    fake.fail("GET", status)
    connection, _ = connect(fake)

    with pytest.raises(TerminalError) as raised:
        fake.transport().fields(connection)

    assert raised.value.code is FailureCode.INTEGRATION_REJECTED
    assert f"HTTP {status}" in str(raised.value) and "token" in raised.value.detail


def test_a_wrong_token_is_refused_by_the_server() -> None:
    fake = FakeBaserow(token="outro-token-do-servidor")
    connection, _ = connect(fake)

    with pytest.raises(TerminalError, match="HTTP 401"):
        fake.transport().fields(connection)


def test_a_table_that_does_not_exist_is_a_definitive_failure() -> None:
    fake = FakeBaserow(table_id=99)
    connection, _ = connect(fake)

    with pytest.raises(TerminalError, match="HTTP 404.*tabela"):
        fake.transport().fields(connection)


def test_a_validation_error_names_the_fields_and_never_the_values() -> None:
    fake = FakeBaserow()
    connection, _ = connect(fake)

    with pytest.raises(TerminalError) as raised:
        fake.transport().create_row(connection, {"field_102": "sem-arroba-fulano"})

    message = str(raised.value)
    assert "HTTP 400" in message and "ERROR_REQUEST_BODY_VALIDATION" in message
    assert "field_102" in message
    assert "fulano" not in message and "e-mail válido" not in message  # nem o corpo da resposta


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503])
def test_an_unavailable_baserow_is_retried(status: int) -> None:
    fake = FakeBaserow()
    fake.fail("POST", status)
    connection, _ = connect(fake)

    with pytest.raises(RetryableError) as raised:
        fake.transport().create_row(connection, {"field_101": "Ana"})

    assert raised.value.code is FailureCode.INTEGRATION_UNAVAILABLE
    assert f"HTTP {status}" in str(raised.value)


def test_a_connection_failure_is_retried_with_the_error_type_only() -> None:
    fake = FakeBaserow()
    fake.fail("GET", httpx.ConnectError("não resolveu baserow.example.test"))
    connection, _ = connect(fake)

    with pytest.raises(RetryableError) as raised:
        fake.transport().fields(connection)

    assert str(raised.value) == "[integration_unavailable] ler os campos: ConnectError"


def test_a_row_that_disappeared_before_the_update_is_retried_so_the_next_attempt_creates_it() -> (
    None
):
    fake = FakeBaserow()
    connection, _ = connect(fake)

    with pytest.raises(RetryableError, match="atualizar a linha: HTTP 404"):
        fake.transport().update_row(connection, 999, {"field_101": "Ana"})


def test_a_redirect_is_a_definitive_failure_and_is_not_followed() -> None:
    fake = FakeBaserow()
    fake.fail("GET", 302)
    connection, _ = connect(fake)

    with pytest.raises(TerminalError, match="HTTP 302"):
        fake.transport().fields(connection)

    assert len(fake.calls) == 1


def test_a_reply_that_is_not_the_expected_json_is_retried() -> None:
    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy</html>")

    from app.integrations.baserow import BaserowTransport

    transport = BaserowTransport(client=httpx.Client(transport=httpx.MockTransport(html)))

    with pytest.raises(RetryableError, match="resposta inesperada"):
        transport.fields(Connection(BASEROW_URL, BASEROW_TABLE, BASEROW_TOKEN))


def test_the_token_never_shows_in_the_connection_repr_or_in_an_error() -> None:
    connection = Connection(BASEROW_URL, BASEROW_TABLE, BASEROW_TOKEN)
    fake = FakeBaserow()
    fake.fail("GET", 500, body={"detail": f"Authorization: Token {BASEROW_TOKEN}"})

    with pytest.raises(RetryableError) as raised:
        fake.transport().fields(connection)

    assert BASEROW_TOKEN not in repr(connection)
    assert BASEROW_TOKEN not in str(raised.value)
