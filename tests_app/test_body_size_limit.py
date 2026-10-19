"""O limite do corpo (`BodySizeLimit`, `app.api.main`): 256 KiB em todo lugar, menos no envio e na
troca de uma imagem, que levam um arquivo de até 8 MiB e uns campos. Cada caminho do envio vale para o
seu método, e nenhum outro caminho (nem outro método no mesmo caminho) ganha o limite maior.

Aqui o limite é provado sozinho, com um app de brinquedo que só lê o corpo; a prova de ponta a ponta,
com a autenticação e o `multipart` de verdade, está em `test_admin_api_assets.py` (a API) e em
`test_admin_assets.py` (a página).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.types import Receive, Scope, Send

from app.admin.api.errors import PREFIX
from app.api.main import MAX_BODY_BYTES, MAX_UPLOAD_BODY_BYTES, UPLOAD_ROUTES, BodySizeLimit

ASSET = "0b0a8f3e-6f0b-4c2e-9d57-3f1d1c1f2a10"
OVER_THE_SMALL_ONE = b"0" * (MAX_BODY_BYTES + 1)
OVER_THE_BIG_ONE = b"0" * (MAX_UPLOAD_BODY_BYTES + 1)
BIG_ONES = [
    ("POST", "/admin/assets"),
    ("POST", f"/admin/assets/{ASSET}/replace"),
    ("POST", f"{PREFIX}/assets"),
    ("PUT", f"{PREFIX}/assets/{ASSET}/content"),
]
SMALL_ONES = [
    ("PUT", "/admin/assets"),  # a página só envia por POST
    ("PUT", f"/admin/assets/{ASSET}/replace"),
    ("POST", f"/admin/assets/{ASSET}/remove"),
    ("POST", f"/admin/assets/{ASSET}"),
    ("POST", "/admin/assetsX"),
    ("POST", "/admin/forms/import"),
    ("PUT", f"{PREFIX}/assets"),  # a coleção não tem PUT: a troca é no conteúdo de uma imagem
    ("POST", f"{PREFIX}/assets/{ASSET}/content"),  # e o conteúdo, só por PUT
    ("PUT", f"{PREFIX}/assets/{ASSET}"),
    ("DELETE", f"{PREFIX}/assets/{ASSET}"),
    ("PUT", f"{PREFIX}/assets/{ASSET}/content/outra-coisa"),
    ("POST", f"{PREFIX}/assets/{ASSET}/replace"),
    ("POST", f"{PREFIX}/api-keys"),
    ("PUT", f"{PREFIX}/forms/demo/retention"),
]


async def reading_everything(scope: Scope, receive: Receive, send: Send) -> None:
    """O app de brinquedo: lê o corpo inteiro e diz que aceitou."""
    received = 0
    while True:
        message = await receive()
        received += len(message.get("body", b""))
        if not message.get("more_body", False):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": str(received).encode()})


@pytest.fixture
def client() -> TestClient:
    app = BodySizeLimit(
        reading_everything,
        MAX_BODY_BYTES,
        upload_routes=UPLOAD_ROUTES,
        upload_bytes=MAX_UPLOAD_BODY_BYTES,
    )
    return TestClient(app, raise_server_exceptions=False)


def send(client: TestClient, method: str, path: str, body: bytes) -> Any:
    return client.request(method, path, content=body)


@pytest.mark.parametrize(("method", "path"), BIG_ONES)
def test_the_upload_of_an_image_takes_a_body_over_the_default_limit(
    client: TestClient, method: str, path: str
) -> None:
    assert send(client, method, path, OVER_THE_SMALL_ONE).status_code == 200


@pytest.mark.parametrize(("method", "path"), BIG_ONES)
def test_the_upload_of_an_image_has_a_limit_of_its_own_too(
    client: TestClient, method: str, path: str
) -> None:
    just_under = b"0" * MAX_UPLOAD_BODY_BYTES

    assert send(client, method, path, just_under).status_code == 200
    assert send(client, method, path, OVER_THE_BIG_ONE).status_code == 413


@pytest.mark.parametrize(("method", "path"), SMALL_ONES)
def test_nothing_else_gets_the_limit_of_an_image(
    client: TestClient, method: str, path: str
) -> None:
    just_under = b"0" * MAX_BODY_BYTES

    assert send(client, method, path, just_under).status_code == 200
    assert send(client, method, path, OVER_THE_SMALL_ONE).status_code == 413


def test_the_refusal_is_in_the_format_of_the_api_under_it_and_in_the_one_of_the_pages_elsewhere(
    client: TestClient,
) -> None:
    api = send(client, "PUT", f"{PREFIX}/assets/{ASSET}/content", OVER_THE_BIG_ONE)
    page = send(client, "POST", "/admin/assets", OVER_THE_BIG_ONE)

    assert api.status_code == 413 and api.json()["code"] == "payload_too_large"
    assert page.status_code == 413 and page.json() == {"detail": "corpo muito grande"}


def test_a_declared_length_over_the_limit_is_refused_before_reading() -> None:
    """O `Content-Length` já diz que passa: a recusa sai sem ler o corpo."""
    read: list[bool] = []

    async def never_reads(scope: Scope, receive: Receive, send: Send) -> None:
        read.append(True)

    app = BodySizeLimit(
        never_reads,
        MAX_BODY_BYTES,
        upload_routes=UPLOAD_ROUTES,
        upload_bytes=MAX_UPLOAD_BODY_BYTES,
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.put(
        f"{PREFIX}/assets/{ASSET}/content",
        content=OVER_THE_BIG_ONE,
    )

    assert response.status_code == 413 and read == []
