"""A origem de uma URL (`app.web.origin.origin_of`): a conferência de origem dos POST do admin e o
`<base>` das prévias da API usam a mesma conta."""

from __future__ import annotations

import pytest

from app.web.origin import origin_of


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://planos.exemplo.test", "https://planos.exemplo.test"),
        ("HTTPS://Planos.Exemplo.Test/um/caminho?x=1#y", "https://planos.exemplo.test"),
        ("  http://localhost:8000  ", "http://localhost:8000"),
        ("https://planos.exemplo.test:443", "https://planos.exemplo.test"),  # a porta padrão some
        ("http://planos.exemplo.test:80/", "http://planos.exemplo.test"),
        ("https://planos.exemplo.test:8443", "https://planos.exemplo.test:8443"),
        ("https://usuario:senha@planos.exemplo.test/", "https://planos.exemplo.test"),  # sem login
        ("http://[::1]:8000/", "http://[::1]:8000"),  # o IPv6 volta entre colchetes
    ],
)
def test_the_origin_is_the_scheme_the_host_and_a_port_that_is_not_the_default(
    url: str, origin: str
) -> None:
    assert origin_of(url) == origin


@pytest.mark.parametrize(
    "url",
    ["", "não é uma url", "ftp://exemplo.test", "javascript:alert(1)", "//exemplo.test", "http://"],
)
def test_what_is_not_an_http_url_has_no_origin(url: str) -> None:
    assert origin_of(url) == ""


def test_a_port_that_is_not_a_number_has_no_origin() -> None:
    assert origin_of("http://exemplo.test:abc/") == ""
