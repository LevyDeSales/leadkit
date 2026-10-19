"""As imagens dos formulários na API de admin (`/admin/api/v1`): listar, ler, enviar, trocar e remover,
pela costura HTTP, com o `multipart/form-data` de verdade.

Elas chamam o serviço que a página chama (`app.forms.assets`) e leem o arquivo pela mesma função
(`app.admin.actions.assets`); a paridade com a página, **inclusive o que fica no storage**, está no
fim de cada operação que escreve. As chaves destes testes são sorteadas na execução.
"""

from __future__ import annotations

import io
import random
import re
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from admin_api_support import (
    ORIGIN,
    ApiClient,
    assert_same_effect,
    audited,
    built_openapi,
    make_api,
    url_of,
)
from PIL import Image
from starlette.requests import Request
from test_admin import FakeClock, csrf_token, login
from test_admin_api_contract import SENTINEL, echoed
from test_admin_assets import picture
from test_form_assets import pin

from app.adapters.assets import AssetStore, AssetUnavailable
from app.api.main import MAX_BODY_BYTES, MAX_UPLOAD_BODY_BYTES
from app.deps import Deps
from app.errors import FailureCode, RetryableError
from app.forms import assets
from app.forms.image_optimizer import MAX_UPLOAD_BYTES
from app.models import AuditLog, FormAsset

Client = Callable[..., ApiClient]
PAGE = "/admin/assets"
# Um id que não existe (fixo: os ids dos testes parametrizados têm de ser os mesmos em todo worker).
MISSING = "00000000-0000-4000-8000-000000000000"
# A chamada da API grava o último uso da chave que chama; a página, não: é o que a comparação tira.
CALLER_USE = {"api_keys": {"last_used_at"}}
# O que o envio sorteia ou lê do relógio da máquina: o id da imagem (também o alvo da auditoria) e o
# instante em que foi gravada.
NEW_ASSET = {
    "form_assets": {"created_at", "updated_at"},
    "audit_log": {"target_id"},
}
CHANGED_ASSET = {"form_assets": {"created_at", "updated_at"}}
ASSET_FIELDS = {
    "id",
    "name",
    "reference",
    "form_slug",
    "content_type",
    "width",
    "height",
    "byte_size",
    "source_size",
    "saved_percent",
    "version",
    "uses",
    "created_by",
    "created_at",
    "updated_by",
    "updated_at",
}


def merged(*ignores: Mapping[str, set[str]]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for ignore in ignores:
        for table, columns in ignore.items():
            found.setdefault(table, set()).update(columns)
    return found


def noise(size: int = 400) -> bytes:
    """Um PNG que não encolhe (ruído), maior que o limite comum de corpo."""
    rng = random.Random(3)
    image = Image.frombytes(
        "RGB", (size, size), bytes(rng.randrange(256) for _ in range(size * size * 3))
    )
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def seed(
    deps: Deps,
    name: str = "logo-black",
    scope: str = "",
    content: bytes | None = None,
) -> assets.Asset:
    """Uma imagem que já estava lá, enviada por quem não é a página nem a chave do teste (assim a
    comparação de autores da paridade não confunde o mundo do teste com a ação)."""
    return assets.upload(
        deps,
        scope=scope,
        name=name,
        content=content if content is not None else picture(),
        actor="testes",
    ).asset


def by_a_key(deps: Deps, action: str) -> list[AuditLog]:
    """As entradas da auditoria de uma ação feitas por uma chave de API (`chave:<nome>`)."""
    return [entry for entry in audited(deps, action) if entry.actor.startswith("chave:")]


def count(deps: Deps) -> int:
    with deps.sessions() as session:
        return session.scalar(sa.select(sa.func.count()).select_from(FormAsset)) or 0


def listing(deps: Deps) -> list[assets.Asset]:
    with deps.sessions() as session:
        return assets.listing(session)


# --- o storage -----------------------------------------------------------------------------------


def stored_files(deps: Deps) -> dict[str, bytes]:
    """Os arquivos das imagens no storage dos testes, pela chave."""
    root: Path = deps.storage.root  # type: ignore[attr-defined]
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.relative_to(root).parts[0] == "assets"
    }


def put_back(deps: Deps, files: dict[str, bytes]) -> None:
    """Devolve o storage ao que `stored_files` fotografou (o banco volta pelo `assert_same_effect`)."""
    root: Path = deps.storage.root  # type: ignore[attr-defined]
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.relative_to(root).parts[0] == "assets":
            path.unlink()
    for key, content in files.items():
        target = root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def same_effect_with_storage(
    deps: Deps,
    page_call: Callable[[], Any],
    api_call: Callable[[], Any],
    **options: Any,
) -> tuple[Any, Any, dict[str, bytes], dict[str, bytes]]:
    """A prova de paridade (`assert_same_effect`) com o efeito no storage: o que a página deixa lá e o
    que a API deixa lá, a partir do mesmo estado de antes, têm de ser iguais. Devolve o que as duas
    chamadas devolveram, o storage de antes e o de depois."""
    before = stored_files(deps)
    after: dict[str, dict[str, bytes]] = {}

    def page() -> Any:
        result = page_call()
        after["page"] = stored_files(deps)
        return result

    def api() -> Any:
        put_back(deps, before)  # o storage volta ao que era, como o banco volta
        result = api_call()
        after["api"] = stored_files(deps)
        return result

    page_result, api_result = assert_same_effect(page, api, **options)
    assert after["page"] == after["api"], "a página e a API não deixaram o mesmo storage"
    return page_result, api_result, before, after["api"]


class BrokenStorage:
    """O storage que não guarda: a falha que o adaptador já traduziu (`RetryableError`)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.root = inner.root

    def put(self, key: str, content: bytes, content_type: str) -> None:
        raise RetryableError(FailureCode.STORAGE_FAILED, "OSError")

    def get(self, key: str) -> bytes:
        return self.inner.get(key)  # type: ignore[no-any-return]

    def presigned_url(self, key: str, *, ttl_seconds: int, filename: str) -> str | None:
        return None

    def delete(self, key: str) -> None:
        self.inner.delete(key)


# --- as chamadas ---------------------------------------------------------------------------------


def upload(
    client: httpx.Client,
    name: str = "logo-black",
    content: bytes | None = None,
    *,
    scope: str | None = None,
    filename: str = "logo.png",
    **more: str,
) -> httpx.Response:
    data = {"name": name, **({"scope": scope} if scope is not None else {}), **more}
    files = {"file": (filename, content if content is not None else picture(), "image/png")}
    return client.post(url_of("upload_asset"), data=data, files=files)


def replace(
    client: httpx.Client,
    asset: assets.Asset,
    content: bytes,
    *,
    expected: str | None = None,
    asset_id: str | None = None,
) -> httpx.Response:
    return client.put(
        url_of("replace_asset", asset_id=asset_id or str(asset.id)),
        data={"expected_version": expected if expected is not None else asset.ref.sha256},
        files={"file": ("nova.png", content, "image/png")},
    )


def remove(
    client: httpx.Client,
    asset: assets.Asset,
    *,
    expected: str | None = None,
    confirm: bool = True,
    asset_id: str | None = None,
) -> httpx.Response:
    params = {"expected_version": expected if expected is not None else asset.ref.sha256}
    if confirm:
        params["confirm"] = "true"
    return client.delete(url_of("remove_asset", asset_id=asset_id or str(asset.id)), params=params)


def page_upload(
    client: ApiClient, name: str, content: bytes, scope: str = ""
) -> Callable[[], httpx.Response]:
    """O envio pela página, como o navegador: entra, lê o token e manda o formulário."""

    def call() -> httpx.Response:
        assert login(client).status_code == 303
        return client.post(
            PAGE,
            data={"csrf_token": csrf_token(client), "name": name, "scope": scope},
            files={"file": ("logo.png", content, "image/png")},
            headers=ORIGIN,
        )

    return call


def page_replace(
    client: ApiClient, asset: assets.Asset, content: bytes
) -> Callable[[], httpx.Response]:
    def call() -> httpx.Response:
        assert login(client).status_code == 303
        return client.post(
            f"{PAGE}/{asset.id}/replace",
            data={"csrf_token": csrf_token(client), "expected": asset.ref.sha256},
            files={"file": ("nova.png", content, "image/png")},
            headers=ORIGIN,
        )

    return call


def page_remove(client: ApiClient, asset: assets.Asset) -> Callable[[], httpx.Response]:
    def call() -> httpx.Response:
        assert login(client).status_code == 303
        data = {"csrf_token": csrf_token(client), "expected": asset.ref.sha256, "confirm": "on"}
        return client.post(f"{PAGE}/{asset.id}/remove", data=data, headers=ORIGIN)

    return call


# --- list_assets ---------------------------------------------------------------------------------


def test_list_assets_needs_a_key(deps: Deps) -> None:
    response = make_api(deps, FakeClock()).get(url_of("list_assets"))

    assert response.status_code == 401 and response.json()["code"] == "invalid_credentials"


def test_list_assets_is_an_envelope_with_the_library_first_and_then_each_form_by_name(
    api_client: Client, deps: Deps
) -> None:
    seed(deps, "capa", scope="mcc", content=picture((1, 2, 3)))
    seed(deps, "logo-black", content=picture((4, 5, 6)))
    seed(deps, "capa", scope="demo", content=picture((7, 8, 9)))
    seed(deps, "abacaxi", content=picture((9, 9, 9)))

    response = api_client("leitura").get(url_of("list_assets"))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "next_cursor"} and body["next_cursor"] is None
    assert [(item["form_slug"], item["name"]) for item in body["items"]] == [
        (None, "abacaxi"),
        (None, "logo-black"),
        ("demo", "capa"),
        ("mcc", "capa"),
    ]
    for item in body["items"]:
        assert set(item) == ASSET_FIELDS


def test_list_assets_of_an_empty_library_is_one_empty_page(api_client: Client) -> None:
    response = api_client("leitura").get(url_of("list_assets"))

    assert response.status_code == 200 and response.json() == {"items": [], "next_cursor": None}


def test_list_assets_says_the_numbers_of_the_stored_file_and_where_each_image_is_used(
    api_client: Client, deps: Deps
) -> None:
    content = picture(size=(3000, 1000))
    used = seed(deps, "usada", content=content)
    seed(deps, "parada", content=picture((9, 9, 9)))
    pin(deps, used, "mcc", 1, name="usada")
    pin(deps, used, "demo", 1, name="usada")

    items = {
        item["name"]: item
        for item in api_client("leitura").get(url_of("list_assets")).json()["items"]
    }

    item = items["usada"]
    assert item["reference"] == "asset:usada" and item["content_type"] == used.ref.content_type
    assert (item["width"], item["height"]) == (2000, 667)
    assert item["byte_size"] == len(stored_files(deps)[used.ref.key])
    assert item["source_size"] == len(content) and item["saved_percent"] is not None
    assert item["version"] == used.ref.sha256
    assert item["uses"] == [
        {"form_slug": "demo", "form_version": 1},
        {"form_slug": "mcc", "form_version": 1},
    ]
    assert items["parada"]["uses"] == []


def test_list_assets_pages_through_the_images_without_skipping_or_repeating(
    api_client: Client, deps: Deps
) -> None:
    for number in range(5):
        seed(deps, f"imagem-{number}", content=picture((number, 50, 50)))
    client = api_client("leitura")

    seen, cursor = [], None
    while True:
        params: dict[str, Any] = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = client.get(url_of("list_assets"), params=params).json()
        assert len(page["items"]) <= 2
        seen += [item["name"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen == [f"imagem-{number}" for number in range(5)]


def test_list_assets_refuses_a_limit_out_of_range_and_a_cursor_it_did_not_give(
    api_client: Client,
) -> None:
    client = api_client("leitura")

    low = client.get(url_of("list_assets"), params={"limit": 0})
    cursor = client.get(url_of("list_assets"), params={"cursor": "inventado"})

    assert low.status_code == 422 and low.json()["errors"][0]["field"] == "limit"
    assert cursor.status_code == 422 and cursor.json()["errors"][0]["field"] == "cursor"


# --- get_asset -----------------------------------------------------------------------------------


def test_get_asset_needs_a_key(deps: Deps) -> None:
    asset = seed(deps)

    response = make_api(deps, FakeClock()).get(url_of("get_asset", asset_id=str(asset.id)))

    assert response.status_code == 401


def test_get_asset_gives_the_image_to_a_read_key(api_client: Client, deps: Deps) -> None:
    content = picture(size=(3000, 1000))
    asset = seed(deps, "capa", scope="mcc", content=content)

    response = api_client("leitura").get(url_of("get_asset", asset_id=str(asset.id)))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == ASSET_FIELDS
    assert body["id"] == str(asset.id) and body["name"] == "capa" and body["form_slug"] == "mcc"
    assert body["reference"] == "asset:capa" and body["version"] == asset.ref.sha256
    assert body["content_type"] == asset.ref.content_type
    assert (body["width"], body["height"]) == (2000, 667)
    assert body["byte_size"] == asset.byte_size and body["source_size"] == len(content)
    assert body["created_by"] == body["updated_by"] == "testes"
    assert body["created_at"].endswith("Z") or body["created_at"].endswith("+00:00")
    assert body["uses"] == []


def test_get_asset_lists_the_published_versions_that_use_the_image(
    api_client: Client, deps: Deps
) -> None:
    asset = seed(deps)
    pin(deps, asset, "mcc", 1, name="logo-black")

    body = api_client("leitura").get(url_of("get_asset", asset_id=str(asset.id))).json()

    assert body["uses"] == [{"form_slug": "mcc", "form_version": 1}]


@pytest.mark.parametrize("asset_id", [MISSING, "nao-e-um-id"])
def test_get_asset_of_an_image_that_does_not_exist_is_a_404(
    api_client: Client, asset_id: str
) -> None:
    response = api_client("leitura").get(url_of("get_asset", asset_id=asset_id))

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert asset_id not in response.text


def test_the_images_carry_neither_the_bytes_nor_an_address_of_the_storage(
    api_client: Client, deps: Deps
) -> None:
    asset = seed(deps)

    text_of = api_client("leitura").get(url_of("get_asset", asset_id=str(asset.id))).text

    assert asset.ref.key not in text_of and "/img/" not in text_of and "storage" not in text_of


# --- upload_asset --------------------------------------------------------------------------------


def test_upload_asset_needs_a_key_and_a_total_one(api_client: Client, deps: Deps) -> None:
    anonymous = upload(make_api(deps, FakeClock()))
    reader = upload(api_client("leitura"))

    assert anonymous.status_code == 401
    assert reader.status_code == 403 and reader.json()["code"] == "forbidden_role"
    assert count(deps) == 0 and stored_files(deps) == {}


def test_upload_asset_optimizes_stores_and_answers_with_the_image_and_the_numbers(
    api_client: Client, deps: Deps
) -> None:
    client = api_client("total")
    content = picture(size=(3000, 1000))

    response = upload(client, content=content, scope="mcc")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == ASSET_FIELDS | {"optimization"}
    assert body["name"] == "logo-black" and body["form_slug"] == "mcc" and body["uses"] == []
    assert body["created_by"] == client.actor
    optimization = body["optimization"]
    assert optimization["before"] == {
        "size": len(content),
        "content_type": "image/png",
        "width": 3000,
        "height": 1000,
    }
    assert optimization["after"]["size"] == body["byte_size"]
    assert optimization["after"]["content_type"] == body["content_type"]
    assert (optimization["after"]["width"], optimization["after"]["height"]) == (2000, 667)
    assert optimization["saved_percent"] == body["saved_percent"]
    (asset,) = listing(deps)
    assert str(asset.id) == body["id"] and body["version"] == asset.ref.sha256
    assert AssetStore(deps.storage).read(asset.ref) == stored_files(deps)[asset.ref.key]
    assert Image.open(io.BytesIO(stored_files(deps)[asset.ref.key])).size == (2000, 667)
    (entry,) = by_a_key(deps, "asset.upload")
    assert entry.actor == client.actor and entry.details["name"] == "logo-black"


def test_upload_asset_without_scope_goes_to_the_library(api_client: Client, deps: Deps) -> None:
    for number, scope in enumerate((None, "")):
        upload(api_client("total"), f"imagem-{number}", picture((number, 1, 1)), scope=scope)

    assert [asset.form_slug for asset in listing(deps)] == [None, None]


def test_upload_asset_without_the_file_is_a_422_on_the_file(api_client: Client, deps: Deps) -> None:
    response = api_client("total").post(url_of("upload_asset"), data={"name": "logo-black"})

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    assert [item["field"] for item in response.json()["errors"]] == ["file"]
    assert count(deps) == 0


def test_upload_asset_with_a_file_part_without_a_name_is_a_422_that_asks_for_the_file(
    api_client: Client, deps: Deps
) -> None:
    """O que o navegador manda quando ninguém escolheu um arquivo: a parte existe, sem nome de
    arquivo, e o Starlette a lê como um campo de texto."""
    response = upload(api_client("total"), filename="")

    assert response.status_code == 422
    (item,) = response.json()["errors"]
    assert item["field"] == "file" and "Mande o arquivo" in item["message"]
    assert count(deps) == 0


@pytest.mark.parametrize(
    ("kwargs", "field", "reason"),
    [
        (
            {"content": b"<svg xmlns='http://www.w3.org/2000/svg'/>", "filename": "a.svg"},
            "file",
            "SVG",
        ),
        ({"content": b"nao e imagem"}, "file", "PNG, JPEG ou WebP"),
        ({"content": b""}, "file", "escolha o arquivo"),
        ({"name": "Logo Preto"}, "name", "minúsculas"),
        ({"name": ""}, "name", "dê um nome"),
        ({"scope": "nao-existe"}, "scope", "formulário não encontrado"),
    ],
)
def test_upload_asset_refuses_what_the_page_refuses_with_the_reason_on_the_field(
    api_client: Client, deps: Deps, kwargs: dict[str, Any], field: str, reason: str
) -> None:
    response = upload(api_client("total"), **kwargs)

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    (item,) = response.json()["errors"]
    assert item["field"] == field and reason in item["message"]
    assert count(deps) == 0 and by_a_key(deps, "asset.upload") == [] and stored_files(deps) == {}


def test_upload_asset_refuses_a_field_it_does_not_know(api_client: Client, deps: Deps) -> None:
    response = upload(api_client("total"), intruso="1")

    assert response.status_code == 422
    assert [item["field"] for item in response.json()["errors"]] == ["intruso"]
    assert count(deps) == 0


def test_upload_asset_with_a_taken_name_is_a_422_that_points_to_the_replacement(
    api_client: Client, deps: Deps
) -> None:
    client = api_client("total")
    assert upload(client).status_code == 201

    again = upload(client, content=picture((9, 9, 9)))

    assert again.status_code == 422
    (item,) = again.json()["errors"]
    assert item["field"] == "name" and "troque a imagem que existe" in item["message"]
    assert count(deps) == 1 and len(stored_files(deps)) == 1


def test_upload_asset_does_not_store_the_same_image_twice(api_client: Client, deps: Deps) -> None:
    """O arquivo é guardado pelo conteúdo: a mesma imagem sob outro nome é outra imagem para os
    templates, mas é um arquivo só no storage. Sob o mesmo nome, nem isso: é um `422`."""
    client = api_client("total")
    content = picture()

    first = upload(client, "logo-black", content)
    second = upload(client, "logo-white", content)
    same_name = upload(client, "logo-black", content)

    assert (first.status_code, second.status_code, same_name.status_code) == (201, 201, 422)
    assert first.json()["version"] == second.json()["version"]
    assert count(deps) == 2 and len(stored_files(deps)) == 1


def test_upload_asset_over_the_file_limit_but_inside_the_body_limit_is_a_422_on_the_file(
    api_client: Client, deps: Deps
) -> None:
    big = b"0" * (
        MAX_UPLOAD_BYTES + 100 * 1024
    )  # cabe no corpo (há 256 KiB de folga), não no arquivo

    response = upload(api_client("total"), content=big)

    assert response.status_code == 422
    (item,) = response.json()["errors"]
    assert item["field"] == "file" and "passa de 8 MB" in item["message"]
    assert count(deps) == 0


def test_upload_asset_takes_a_body_over_the_default_limit_and_refuses_one_over_its_own(
    api_client: Client, deps: Deps
) -> None:
    client = api_client("total")
    content = noise()
    assert len(content) > MAX_BODY_BYTES

    accepted = upload(client, content=content)
    too_big = client.post(
        url_of("upload_asset"),
        data={"name": "enorme"},
        files={"file": ("a.png", b"0" * (MAX_UPLOAD_BODY_BYTES + 1), "image/png")},
    )

    assert accepted.status_code == 201 and count(deps) == 1
    assert too_big.status_code == 413 and too_big.json()["code"] == "payload_too_large"


def counting_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Conta as vezes que alguém lê o corpo da requisição (`Request.form` e `Request.stream`, por onde
    o FastAPI e o Starlette o consomem), como o teste de `test_admin_api_operations.py`."""
    reads: list[str] = []
    form = Request.form
    stream = Request.stream

    def counting_form(self: Request, **kwargs: Any) -> Any:
        reads.append("form")
        return form(self, **kwargs)

    async def counting_stream(self: Request) -> AsyncIterator[bytes]:
        reads.append("stream")
        async for chunk in stream(self):
            yield chunk

    monkeypatch.setattr(Request, "form", counting_form)
    monkeypatch.setattr(Request, "stream", counting_stream)
    return reads


def test_upload_asset_does_not_read_the_body_of_who_cannot_send_it(
    api_client: Client, deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Um arquivo de 8 MiB de verdade: sem chave (`401`) e com chave `leitura` (`403`) o corpo não é
    lido nem em parte, e com a chave `total` o mesmo corpo é lido (a prova de que a contagem conta)."""
    reads = counting_reads(monkeypatch)
    big = b"0" * MAX_UPLOAD_BYTES

    anonymous = upload(make_api(deps, FakeClock()), "grande", big)
    reader = upload(api_client("leitura"), "grande", big)

    assert anonymous.status_code == 401 and reader.status_code == 403
    assert reads == []
    allowed = upload(api_client("total"), "grande", big)
    assert allowed.status_code == 422  # não é uma imagem, mas o corpo foi lido para saber
    assert "form" in reads and "stream" in reads
    assert count(deps) == 0 and stored_files(deps) == {}


def test_upload_asset_with_a_storage_that_fails_is_a_502_and_nothing_is_recorded(
    api_client: Client, deps: Deps
) -> None:
    client = api_client("total")
    deps.storage = BrokenStorage(deps.storage)  # type: ignore[assignment]

    response = upload(client)

    assert response.status_code == 502 and response.json()["code"] == "upstream_failed"
    assert (
        "Nada foi gravado" in response.json()["detail"] and "OSError" in response.json()["detail"]
    )
    assert count(deps) == 0 and by_a_key(deps, "asset.upload") == [] and stored_files(deps) == {}


def test_upload_asset_leaves_the_same_state_audit_and_storage_as_the_page(
    api_client: Client, deps: Deps
) -> None:
    client = api_client("total")
    content = picture(size=(3000, 1000))

    page, api, before, after = same_effect_with_storage(
        deps,
        page_upload(client, "logo-black", content, "mcc"),
        lambda: upload(client, "logo-black", content, scope="mcc"),
        api_actor=client.actor,
        ignore=merged(CALLER_USE, NEW_ASSET),
    )

    assert page.status_code == 303 and api.status_code == 201
    assert before == {} and len(after) == 1  # o storage mudou, e do mesmo jeito


def test_a_refused_upload_leaves_the_same_state_on_the_page_and_in_the_api(
    api_client: Client, deps: Deps
) -> None:
    client = api_client("total")

    page, api, _, after = same_effect_with_storage(
        deps,
        page_upload(client, "Logo Preto", picture()),
        lambda: upload(client, "Logo Preto"),
        api_actor=client.actor,
        ignore=CALLER_USE,
        expect_no_change=True,
    )

    assert page.status_code == 422 and api.status_code == 422 and after == {}


# --- replace_asset -------------------------------------------------------------------------------


def test_replace_asset_needs_a_key_and_a_total_one(api_client: Client, deps: Deps) -> None:
    asset = seed(deps)

    anonymous = replace(make_api(deps, FakeClock()), asset, picture((9, 9, 9)))
    reader = replace(api_client("leitura"), asset, picture((9, 9, 9)))

    assert anonymous.status_code == 401
    assert reader.status_code == 403 and reader.json()["code"] == "forbidden_role"
    assert listing(deps) == [asset]


def test_replace_asset_changes_the_content_and_keeps_the_name_and_the_id(
    api_client: Client, deps: Deps
) -> None:
    client = api_client("total")
    old = seed(deps)

    response = replace(client, old, picture((9, 9, 9), size=(800, 400)))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == ASSET_FIELDS | {"optimization", "changed"}
    assert body["changed"] is True and body["id"] == str(old.id) and body["name"] == "logo-black"
    assert (body["width"], body["height"]) == (800, 400)
    assert body["version"] != old.ref.sha256 and body["updated_by"] == client.actor
    assert body["optimization"]["before"]["width"] == 800
    (now,) = listing(deps)
    assert now.id == old.id and now.ref != old.ref and body["version"] == now.ref.sha256
    (entry,) = by_a_key(deps, "asset.replace")
    assert entry.details["previous_sha256"] == old.ref.sha256


def test_replace_asset_with_the_same_image_changes_nothing(api_client: Client, deps: Deps) -> None:
    content = picture()
    old = seed(deps, content=content)

    response = replace(api_client("total"), old, content)

    assert response.status_code == 200 and response.json()["changed"] is False
    assert response.json()["version"] == old.ref.sha256
    assert audited(deps, "asset.replace") == [] and listing(deps) == [old]


def test_replace_asset_does_not_touch_what_the_published_versions_use(
    api_client: Client, deps: Deps
) -> None:
    """Trocar muda a imagem para as versões novas; a versão já publicada segue com o conteúdo que
    fixou, e o arquivo antigo fica no storage para o PDF dela sair igual."""
    old = seed(deps)
    pin(deps, old, "mcc", 1, name="logo-black")

    response = replace(api_client("total"), old, picture((9, 9, 9)))

    assert response.status_code == 200
    assert response.json()["uses"] == [{"form_slug": "mcc", "form_version": 1}]
    with deps.sessions() as session:
        pinned = assets.version_refs(session, "mcc", 1)
    assert pinned == {"logo-black": old.ref}
    assert AssetStore(deps.storage).read(old.ref)  # o arquivo que a versão fixou segue lá
    assert old.ref.key in stored_files(deps)


@pytest.mark.parametrize("asset_id", [MISSING, "nao-e-um-id"])
def test_replace_asset_of_an_image_that_does_not_exist_is_a_404_even_with_a_bad_file(
    api_client: Client, deps: Deps, asset_id: str
) -> None:
    ghost = seed(deps)
    client = api_client("total")

    fine = replace(client, ghost, picture((9, 9, 9)), asset_id=asset_id)
    bad = replace(client, ghost, b"<svg/>", asset_id=asset_id)

    for response in (fine, bad):
        assert response.status_code == 404 and response.json()["code"] == "not_found"
        assert asset_id not in response.text
    assert listing(deps) == [ghost]


def test_replace_asset_without_the_expected_version_or_the_file_is_a_422_on_each(
    api_client: Client, deps: Deps
) -> None:
    asset = seed(deps)

    response = api_client("total").put(url_of("replace_asset", asset_id=str(asset.id)), data={})

    assert response.status_code == 422
    assert {item["field"] for item in response.json()["errors"]} == {"expected_version", "file"}


def test_replace_asset_from_a_stale_version_is_a_409_with_the_version_of_now(
    api_client: Client, deps: Deps
) -> None:
    client = api_client("total")
    old = seed(deps)
    assert replace(client, old, picture((9, 9, 9))).status_code == 200
    (now,) = listing(deps)

    response = replace(client, old, picture((7, 7, 7)))  # `old` ainda traz o sha256 de antes

    assert response.status_code == 409 and response.json()["code"] == "version_conflict"
    assert response.json()["current_version"] == now.ref.sha256
    assert listing(deps) == [now] and len(by_a_key(deps, "asset.replace")) == 1


def test_replace_asset_with_a_bad_file_is_a_422_on_the_file_and_the_image_stays(
    api_client: Client, deps: Deps
) -> None:
    old = seed(deps)

    response = replace(api_client("total"), old, b"<svg/>")

    assert response.status_code == 422
    (item,) = response.json()["errors"]
    assert item["field"] == "file" and "SVG" in item["message"]
    assert listing(deps) == [old] and audited(deps, "asset.replace") == []


def test_replace_asset_takes_a_body_over_the_default_limit_and_refuses_one_over_its_own(
    api_client: Client, deps: Deps
) -> None:
    client = api_client("total")
    old = seed(deps)
    content = noise()
    assert len(content) > MAX_BODY_BYTES

    accepted = replace(client, old, content)
    (now,) = listing(deps)
    too_big = client.put(
        url_of("replace_asset", asset_id=str(old.id)),
        data={"expected_version": now.ref.sha256},
        files={"file": ("a.png", b"0" * (MAX_UPLOAD_BODY_BYTES + 1), "image/png")},
    )

    assert accepted.status_code == 200 and accepted.json()["changed"] is True
    assert too_big.status_code == 413 and too_big.json()["code"] == "payload_too_large"
    assert listing(deps) == [now]


def test_replace_asset_does_not_read_the_body_of_who_cannot_send_it(
    api_client: Client, deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = seed(deps)
    reads = counting_reads(monkeypatch)
    big = b"0" * MAX_UPLOAD_BYTES

    anonymous = replace(make_api(deps, FakeClock()), asset, big)
    reader = replace(api_client("leitura"), asset, big)

    assert anonymous.status_code == 401 and reader.status_code == 403
    assert reads == []
    allowed = replace(api_client("total"), asset, big)
    assert allowed.status_code == 422  # não é uma imagem, mas o corpo foi lido para saber
    assert "form" in reads and "stream" in reads
    assert listing(deps) == [asset]


def test_replace_asset_with_a_storage_that_fails_is_a_502_and_the_image_stays(
    api_client: Client, deps: Deps
) -> None:
    old = seed(deps)
    client = api_client("total")
    deps.storage = BrokenStorage(deps.storage)  # type: ignore[assignment]

    response = replace(client, old, picture((9, 9, 9)))

    assert response.status_code == 502 and response.json()["code"] == "upstream_failed"
    assert listing(deps) == [old] and audited(deps, "asset.replace") == []
    assert set(stored_files(deps)) == {old.ref.key}


def test_replace_asset_leaves_the_same_state_audit_and_storage_as_the_page(
    api_client: Client, deps: Deps
) -> None:
    client = api_client("total")
    old = seed(deps)
    new = picture((9, 9, 9), size=(800, 400))

    page, api, before, after = same_effect_with_storage(
        deps,
        page_replace(client, old, new),
        lambda: replace(client, old, new),
        api_actor=client.actor,
        ignore=merged(CALLER_USE, CHANGED_ASSET),
    )

    assert page.status_code == 303 and api.status_code == 200
    assert set(before) == {old.ref.key} and set(after) != set(before)  # trocou o arquivo


def test_replacing_an_image_a_version_pinned_leaves_the_same_storage_as_the_page(
    api_client: Client, deps: Deps
) -> None:
    client = api_client("total")
    old = seed(deps)
    pin(deps, old, "mcc", 1, name="logo-black")
    new = picture((9, 9, 9))

    _, _, before, after = same_effect_with_storage(
        deps,
        page_replace(client, old, new),
        lambda: replace(client, old, new),
        api_actor=client.actor,
        ignore=merged(CALLER_USE, CHANGED_ASSET),
    )

    assert set(before) <= set(after) and len(after) == 2  # o antigo fica, o novo entra


# --- remove_asset --------------------------------------------------------------------------------


def test_remove_asset_needs_a_key_and_a_total_one(api_client: Client, deps: Deps) -> None:
    asset = seed(deps)

    anonymous = remove(make_api(deps, FakeClock()), asset)
    reader = remove(api_client("leitura"), asset)

    assert anonymous.status_code == 401
    assert reader.status_code == 403 and reader.json()["code"] == "forbidden_role"
    assert listing(deps) == [asset]


def test_remove_asset_without_the_confirm_does_nothing(api_client: Client, deps: Deps) -> None:
    asset = seed(deps)
    client = api_client("total")

    for params in (
        {"expected_version": asset.ref.sha256},
        {"expected_version": asset.ref.sha256, "confirm": "false"},
    ):
        response = client.delete(url_of("remove_asset", asset_id=str(asset.id)), params=params)
        assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
        assert "na query" in response.json()["detail"]

    assert listing(deps) == [asset] and audited(deps, "asset.remove") == []
    assert asset.ref.key in stored_files(deps)


def test_remove_asset_asks_for_the_confirm_before_it_looks_for_the_image(
    api_client: Client,
) -> None:
    response = api_client("total").delete(
        url_of("remove_asset", asset_id=str(uuid.uuid4())), params={"expected_version": "x"}
    )

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"


def test_remove_asset_deletes_the_image_and_its_file_and_answers_with_what_went(
    api_client: Client, deps: Deps
) -> None:
    client = api_client("total")
    asset = seed(deps)

    response = remove(client, asset)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == ASSET_FIELDS and body["id"] == str(asset.id)
    assert body["name"] == "logo-black" and body["version"] == asset.ref.sha256
    assert listing(deps) == [] and stored_files(deps) == {}
    with pytest.raises(AssetUnavailable):
        AssetStore(deps.storage).read(asset.ref)
    (entry,) = by_a_key(deps, "asset.remove")
    assert entry.actor == client.actor and entry.details["sha256"] == asset.ref.sha256


def test_remove_asset_that_a_published_version_uses_is_a_409_that_says_which(
    api_client: Client, deps: Deps
) -> None:
    asset = seed(deps)
    pin(deps, asset, "demo", 1, name="logo-black")
    pin(deps, asset, "mcc", 1, name="logo-black")

    response = remove(api_client("total"), asset)

    assert response.status_code == 409 and response.json()["code"] == "state_conflict"
    detail = response.json()["detail"]
    assert "demo v1" in detail and "mcc v1" in detail and "não pode ser removida" in detail
    assert listing(deps) == [asset] and audited(deps, "asset.remove") == []
    assert asset.ref.key in stored_files(deps)


def test_remove_asset_from_a_stale_version_is_a_409_with_the_version_of_now(
    api_client: Client, deps: Deps
) -> None:
    client = api_client("total")
    old = seed(deps)
    assert replace(client, old, picture((9, 9, 9))).status_code == 200
    (now,) = listing(deps)

    response = remove(client, old)  # `old` ainda traz o sha256 de antes

    assert response.status_code == 409 and response.json()["code"] == "version_conflict"
    assert response.json()["current_version"] == now.ref.sha256
    assert listing(deps) == [now]


@pytest.mark.parametrize("asset_id", [MISSING, "nao-e-um-id"])
def test_remove_asset_of_an_image_that_does_not_exist_is_a_404(
    api_client: Client, deps: Deps, asset_id: str
) -> None:
    other = seed(deps)

    response = remove(api_client("total"), other, asset_id=asset_id)

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert asset_id not in response.text and listing(deps) == [other]


def test_remove_asset_without_the_expected_version_is_a_422_on_it(
    api_client: Client, deps: Deps
) -> None:
    asset = seed(deps)

    response = api_client("total").delete(
        url_of("remove_asset", asset_id=str(asset.id)), params={"confirm": "true"}
    )

    assert response.status_code == 422
    assert [item["field"] for item in response.json()["errors"]] == ["expected_version"]
    assert listing(deps) == [asset]


def test_remove_asset_leaves_the_same_state_audit_and_storage_as_the_page(
    api_client: Client, deps: Deps
) -> None:
    client = api_client("total")
    asset = seed(deps)

    page, api, before, after = same_effect_with_storage(
        deps,
        page_remove(client, asset),
        lambda: remove(client, asset),
        api_actor=client.actor,
        ignore=CALLER_USE,
    )

    assert page.status_code == 303 and api.status_code == 200
    assert set(before) == {asset.ref.key} and after == {}  # o arquivo saiu do storage


def test_removing_an_image_in_use_leaves_the_same_state_and_storage_on_the_page_and_in_the_api(
    api_client: Client, deps: Deps
) -> None:
    client = api_client("total")
    asset = seed(deps)
    pin(deps, asset, "mcc", 1, name="logo-black")

    page, api, before, after = same_effect_with_storage(
        deps,
        page_remove(client, asset),
        lambda: remove(client, asset),
        api_actor=client.actor,
        ignore=CALLER_USE,
        expect_no_change=True,
    )

    assert page.status_code == 409 and api.status_code == 409 and before == after


# --- o corpo que não é um formulário multipart ---------------------------------------------------

NOT_MULTIPART = [
    pytest.param(
        {"Content-Type": "multipart/form-data"}, b"--fronteira--", id="multipart-without-boundary"
    ),
    pytest.param(
        {"Content-Type": "multipart/form-data; boundary=fronteira"}, b"lixo", id="garbage-body"
    ),
    pytest.param({"Content-Type": "application/json"}, b'{"name": "logo-black"}', id="json"),
    pytest.param({}, b"", id="no-body-at-all"),
]


@pytest.mark.parametrize(("headers", "content"), NOT_MULTIPART)
def test_upload_asset_with_a_body_that_is_not_multipart_is_a_422_on_the_file(
    api_client: Client, deps: Deps, headers: dict[str, str], content: bytes
) -> None:
    response = api_client("total").post(url_of("upload_asset"), content=content, headers=headers)

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    assert "file" in [item["field"] for item in response.json()["errors"]]
    assert count(deps) == 0 and stored_files(deps) == {}


@pytest.mark.parametrize(("headers", "content"), NOT_MULTIPART)
def test_replace_asset_with_a_body_that_is_not_multipart_is_a_422_on_the_file(
    api_client: Client, deps: Deps, headers: dict[str, str], content: bytes
) -> None:
    asset = seed(deps)

    response = api_client("total").put(
        url_of("replace_asset", asset_id=str(asset.id)), content=content, headers=headers
    )

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    assert "file" in [item["field"] for item in response.json()["errors"]]
    assert listing(deps) == [asset]


def test_a_multipart_without_boundary_is_a_422_that_says_only_the_file_is_wrong(
    api_client: Client,
) -> None:
    response = api_client("total").post(
        url_of("upload_asset"),
        content=b"--fronteira--",
        headers={"Content-Type": "multipart/form-data"},
    )

    (item,) = response.json()["errors"]
    assert item["field"] == "file" and "multipart" in item["message"]


# --- o custo das operações pesadas ---------------------------------------------------------------


def test_upload_asset_costs_five_tokens_so_a_bucket_of_ten_takes_two(
    api_client: Client, deps: Deps
) -> None:
    """O balde de 10 fichas com o relógio parado: a imagem é decodificada e recodificada, e custa 5
    (`heavy`), então a terceira chamada já é `429`. Uma leitura custaria 1."""
    client = api_client("total", API_RATE_LIMIT_PER_MINUTE="10")

    answers = [
        upload(client, name, picture((number, 40, 40)))
        for number, name in enumerate(("primeira", "segunda", "terceira"))
    ]

    assert [answer.status_code for answer in answers] == [201, 201, 429]
    assert answers[2].json()["code"] == "rate_limited" and "Retry-After" in answers[2].headers
    assert [asset.name for asset in listing(deps)] == ["primeira", "segunda"]


def test_replace_asset_costs_five_tokens_so_a_bucket_of_ten_takes_two(
    api_client: Client, deps: Deps
) -> None:
    client = api_client("total", API_RATE_LIMIT_PER_MINUTE="10")
    asset = seed(deps)

    first = replace(client, asset, picture((1, 40, 40)))
    second = replace(client, asset, picture((2, 40, 40)), expected=first.json()["version"])
    third = replace(client, asset, picture((3, 40, 40)), expected=second.json()["version"])

    assert [first.status_code, second.status_code, third.status_code] == [200, 200, 429]
    assert third.json()["code"] == "rate_limited" and "Retry-After" in third.headers
    (now,) = listing(deps)
    assert now.ref.sha256 == second.json()["version"]


def test_a_read_costs_one_token_where_the_upload_costs_five(api_client: Client) -> None:
    client = api_client("total", API_RATE_LIMIT_PER_MINUTE="10")

    answers = [client.get(url_of("list_assets")).status_code for _ in range(11)]

    assert answers == [200] * 10 + [429]


# --- o eco ---------------------------------------------------------------------------------------


def test_the_free_text_of_the_image_operations_never_comes_back_in_an_error(
    api_client: Client, deps: Deps
) -> None:
    """O eco geral (`test_admin_api_contract.py`) põe o sentinela em todo campo e para no `422` do
    Pydantic. Aqui cada texto livre vai sozinho, com os outros campos válidos, para a recusa vir do
    serviço: o nome, o escopo, o nome do arquivo e a versão esperada."""
    client = api_client("total")
    asset = seed(deps)

    answers = {
        "name": upload(client, SENTINEL),
        "scope": upload(client, "outra", scope=SENTINEL),
        "filename": upload(client, "outra", b"nao e imagem", filename=f"{SENTINEL}.png"),
        "replace version": replace(client, asset, picture((1, 1, 1)), expected=SENTINEL),
        "remove version": remove(client, asset, expected=SENTINEL),
    }

    assert {name: answer.status_code for name, answer in answers.items()} == {
        "name": 422,
        "scope": 422,
        "filename": 422,
        "replace version": 409,
        "remove version": 409,
    }
    for name, answer in answers.items():
        assert not echoed(answer), f"{name} devolve o valor"
    assert listing(deps) == [asset]


# --- o contrato ----------------------------------------------------------------------------------


def test_the_operations_are_declared_with_their_roles_marks_and_multipart_bodies() -> None:
    document = built_openapi()
    found = {
        operation["operationId"]: (method, path, operation)
        for path, methods in document["paths"].items()
        for method, operation in methods.items()
    }

    assert {
        name: found[name][:2] for name in found if name.endswith("_asset") or name == "list_assets"
    } == {
        "list_assets": ("get", "/assets"),
        "get_asset": ("get", "/assets/{asset_id}"),
        "upload_asset": ("post", "/assets"),
        "replace_asset": ("put", "/assets/{asset_id}/content"),
        "remove_asset": ("delete", "/assets/{asset_id}"),
    }
    roles = {name: found[name][2]["x-required-role"] for name in found if name.endswith("_asset")}
    assert roles == {
        "get_asset": "leitura",
        "upload_asset": "total",
        "replace_asset": "total",
        "remove_asset": "total",
    }
    assert found["list_assets"][2]["x-required-role"] == "leitura"
    marked = {name for name, (_, _, op) in found.items() if op.get("x-destructive")}
    assert "remove_asset" in marked and "upload_asset" not in marked
    assert "replace_asset" not in marked
    for name, model in (
        ("upload_asset", "UploadAssetRequest"),
        ("replace_asset", "ReplaceAssetRequest"),
    ):
        content = found[name][2]["requestBody"]["content"]
        assert content == {
            "multipart/form-data": {"schema": {"$ref": f"#/components/schemas/{model}"}}
        }
    for name in ("upload_asset", "replace_asset"):
        assert "413" in found[name][2]["responses"] and "502" in found[name][2]["responses"]
    assert re.fullmatch(r"1\.\d+\.0", document["info"]["version"])
