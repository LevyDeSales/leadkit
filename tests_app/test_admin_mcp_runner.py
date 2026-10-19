"""A chamada de uma ferramenta do MCP de admin (`app.admin_mcp.runner`, `client` e `results`), contra
uma API de mentira em memória: a requisição que sai, a confirmação antes das operações marcadas, o que
volta (JSON, erro, arquivo) e a chave que nunca aparece.

Sem rede e sem o SDK do MCP: o `Prompter` é de mentira (`Asker`), e a API é um `httpx.MockTransport`
(`admin_mcp_support.FakeApi`). A conversa com o SDK de verdade está em `test_admin_mcp_server.py`.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import stat
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from admin_mcp_support import (
    FakeApi,
    answer,
    body_of,
    error,
    html,
    make_config,
    new_key,
    pdf,
    query_of,
)
from admin_mcp_world import Asker, World

from app.admin_mcp import results
from app.admin_mcp.client import ApiClient, ApiUnavailable, InputError, prepare
from app.admin_mcp.config import Config
from app.admin_mcp.redaction import MASK, RedactingFilter, Redactor
from app.admin_mcp.results import ToolOutput
from app.admin_mcp.runner import NeverAsks, ToolRunner

PNG = b"\x89PNG\r\n\x1a\n" + b"bytes sinteticos de uma imagem"
FORM = {"slug": "diagnostico-demo", "name": "Diagnóstico demo"}


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    found = World(tmp_path)
    yield found
    found.close()


def make_world(tmp_path: Path, role: str = "total", **options: Any) -> World:
    return World(tmp_path, role, **options)


# --- a requisição --------------------------------------------------------------------------------


def test_a_read_goes_to_the_operation_path_with_the_key_in_the_header_only(world: World) -> None:
    world.api.when("GET", "/forms/{slug}", answer(200, FORM))

    output = world.call("get_form", {"slug": "diagnostico-demo"})

    assert not output.is_error and json.loads(output.text) == FORM
    (request,) = world.api.calls()
    assert (request.method, request.url.path) == ("GET", "/admin/api/v1/forms/diagnostico-demo")
    assert request.headers["authorization"] == f"Bearer {world.key}"
    assert world.key not in str(request.url) and world.key not in request.content.decode()


@pytest.mark.parametrize(
    "evil",
    [
        "..",
        ".",
        "",
        "x/draft",  # o servidor da API decodifica o `%2F`: chegaria a `get_form_draft`
        "x%2Fdraft",
        "../api-keys",
        "a/b%c",
        "a b",
        "a\nb",
        "a\x00b",
        "a?b=1",
        "a#b",
        "a\\b",
        "ação",
        "a;b",
        "a@b",
        " ",
        "/",
        "x/",
        "%2e%2e",
    ],
)
def test_a_path_argument_outside_the_safe_alphabet_is_refused_before_any_request(
    world: World, evil: str
) -> None:
    """Escapar o `/` não bastaria, porque o uvicorn o decodifica antes de rotear (veja os testes
    contra o app de verdade, em `test_admin_mcp_live_api.py`): o argumento de caminho só pode ter
    `[A-Za-z0-9._~-]`, e `.` e `..` sozinhos não valem."""
    world.api.when("GET", "/forms/{slug}", answer(200, FORM))

    output = world.call("get_form", {"slug": evil})

    assert output.is_error and "`slug` só pode ter" in output.text
    assert "não enviei nada" in output.text and world.api.calls() == []


def test_a_path_argument_is_refused_before_the_person_is_asked_anything(world: World) -> None:
    asker = Asker(True)

    output = world.call("activate_form", {"slug": "x/draft"}, asker)

    assert output.is_error and asker.messages == [] and world.api.calls() == []


def test_an_over_long_path_argument_is_refused_by_the_alphabet_check_not_blamed_on_the_address(
    world: World,
) -> None:
    world.api.when("GET", "/forms/{slug}", answer(200, FORM))

    for length in (201, 5000, 100_000):
        output = world.call("get_form", {"slug": "a" * length})
        assert output.is_error and "`slug` só pode ter" in output.text, length
        assert "até 200 caracteres" in output.text and "GERADOR_API_URL" not in output.text

    world.call("get_form", {"slug": "a" * 200})
    assert [len(r.url.path.rsplit("/", 1)[1]) for r in world.api.calls()] == [200]


def test_a_url_the_http_client_rejects_does_not_blame_only_the_address(world: World) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.InvalidURL("URL component 'query' too long")

    world.api.when("GET", "/forms", refuse)

    output = world.call("list_forms")

    assert output.is_error and "nada foi enviado" in output.text
    assert "tamanho dos argumentos" in output.text and "GERADOR_API_URL" in output.text


@pytest.mark.parametrize("good", ["diagnostico-demo", "a.b_c~d-1", "A1", "...", "0", "a..b"])
def test_a_path_argument_inside_the_safe_alphabet_goes_through_unchanged(
    world: World, good: str
) -> None:
    world.api.when("GET", "/forms/{slug}", answer(200, FORM))

    world.call("get_form", {"slug": good})

    (request,) = world.api.calls()
    assert request.url.raw_path.decode() == f"/admin/api/v1/forms/{good}"


def test_a_numeric_path_argument_is_text_and_a_bool_or_an_object_is_not_one(world: World) -> None:
    world.api.when("GET", "/forms/{slug}/versions/{number}", answer(200, {"number": 3}))

    ok = world.call("get_form_version", {"slug": "demo", "number": 3})
    bad = world.call("get_form_version", {"slug": "demo", "number": True})

    assert not ok.is_error and bad.is_error
    assert [r.url.path for r in world.api.calls()] == ["/admin/api/v1/forms/demo/versions/3"]


def test_query_arguments_go_in_the_query_with_booleans_in_lowercase(world: World) -> None:
    world.api.when("GET", "/jobs", answer(200, {"items": [], "next_cursor": None}))

    world.call("list_jobs", {"status": "failed", "include_total": True, "limit": 10})

    (request,) = world.api.calls()
    assert query_of(request) == {
        "status": "failed",
        "include_total": "true",
        "limit": "10",
        "include_lead_data": "false",  # o servidor fixa: nome, e-mail e respostas não vêm
    }


def test_a_body_field_goes_in_the_json_and_the_path_stays_in_the_path(world: World) -> None:
    world.api.when("PUT", "/forms/{slug}/draft", answer(200, {"version": "2"}))

    world.call("save_form_draft", {"slug": "demo", "expected_version": "1", "document": {"a": 1}})

    (request,) = world.api.calls()
    assert request.url.path.endswith("/forms/demo/draft")
    assert body_of(request) == {"expected_version": "1", "document": {"a": 1}}


def test_an_optional_body_is_left_out_when_nothing_is_sent(world: World) -> None:
    world.api.when("POST", "/forms/{slug}/draft/pdf-preview", pdf())

    world.call("render_form_draft_pdf_preview", {"slug": "demo"})
    world.call("render_form_draft_pdf_preview", {"slug": "demo", "source": "test"})

    first, second = world.api.calls()
    assert first.content == b""
    assert body_of(second) == {"source": "test"}


def test_arguments_the_tool_does_not_have_and_the_missing_required_are_refused(
    world: World,
) -> None:
    unknown = world.call("get_form", {"slug": "demo", "extra": 1})
    missing = world.call("save_form_draft", {"slug": "demo"})

    assert unknown.is_error and "`extra`" in unknown.text
    assert (
        missing.is_error and "`expected_version`" in missing.text and "`document`" in missing.text
    )
    assert world.api.calls() == []


def test_the_model_cannot_turn_the_lead_data_on_by_passing_the_argument(world: World) -> None:
    for name in ("list_jobs", "get_job"):
        arguments = {"include_lead_data": True} | ({"public_id": "x"} if name == "get_job" else {})
        output = world.call(name, arguments)
        assert output.is_error and "`include_lead_data`" in output.text
    assert world.call("list_jobs", {"q": "maria@example.test"}).is_error
    assert world.api.calls() == []


def test_with_the_option_on_the_lead_data_argument_and_the_search_work(tmp_path: Path) -> None:
    option = make_world(tmp_path, lead_data=True)
    option.api.when("GET", "/jobs", answer(200, {"items": []}))
    option.api.when("GET", "/jobs/{public_id}", answer(200, {"public_id": "x"}))

    option.call("list_jobs", {"q": "maria", "include_lead_data": True})
    option.call("get_job", {"public_id": "x"})

    listing, one = option.api.calls()
    assert query_of(listing) == {"q": "maria", "include_lead_data": "true"}
    assert query_of(one) == {}
    option.close()


def test_a_leitura_key_cannot_even_name_a_total_tool(tmp_path: Path) -> None:
    reader = make_world(tmp_path, "leitura")

    assert "publish_form_draft" not in reader.tools and "download_job_pdf" not in reader.tools
    assert "list_forms" in reader.tools
    reader.close()


# --- os arquivos enviados ------------------------------------------------------------------------


def test_an_image_goes_as_multipart_without_the_name_of_the_local_file(world: World) -> None:
    picture = _write(world.uploads / "foto-da-cliente-maria.png", PNG)
    world.api.when("POST", "/assets", answer(201, {"id": "a1", "version": "1"}))

    output = world.call(
        "upload_asset",
        {"name": "logo-demo", "scope": "demo", "file": str(picture)},
        Asker(True),
    )

    assert not output.is_error
    (request,) = world.api.calls()
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    sent = request.content
    assert PNG in sent
    assert b'name="name"\r\n\r\nlogo-demo' in sent and b'name="scope"\r\n\r\ndemo' in sent
    assert b'name="file"; filename="upload.png"' in sent
    assert b"foto-da-cliente" not in sent
    assert b"Content-Type: image/png" in sent
    assert b'name="confirm"' not in sent  # o `confirm` do arquivo é só do MCP


def test_replace_asset_sends_the_version_and_the_file_in_the_same_form(world: World) -> None:
    picture = _write(world.uploads / "x.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 dados")
    world.api.when("PUT", "/assets/{asset_id}/content", answer(200, {"version": "2"}))

    world.call(
        "replace_asset",
        {"asset_id": "a1", "expected_version": "1", "file": str(picture)},
        Asker(True),
    )

    (request,) = world.api.calls()
    assert b'name="expected_version"\r\n\r\n1' in request.content
    assert b'filename="upload.webp"' in request.content and b"image/webp" in request.content


def test_a_relative_path_is_taken_from_the_upload_folder(world: World) -> None:
    _write(world.uploads / "logos" / "azul.png", PNG)
    world.api.when("POST", "/assets", answer(201, {}))

    output = world.call(
        "upload_asset", {"name": "logo-demo", "file": "logos/azul.png"}, Asker(True)
    )

    assert not output.is_error and PNG in world.api.calls()[0].content


@pytest.mark.parametrize(
    ("make", "fragment"),
    [
        (lambda p: p / "nao-existe.png", "não existe"),
        (lambda p: _dir(p / "pasta.png"), "não é um arquivo comum"),
        (lambda p: _write(p / "segredo.txt", b"senha=1234"), "não é uma imagem"),
        (lambda p: _write(p / "gif.gif", b"GIF89a....."), "não é uma imagem"),
        (lambda p: _write(p / "vazio.png", b""), "não é uma imagem"),
    ],
)
def test_a_path_that_is_not_an_image_is_refused_before_anything_is_sent(
    world: World, make: Any, fragment: str
) -> None:
    target = make(world.uploads)
    asker = Asker(True)

    output = world.call("upload_asset", {"name": "logo-demo", "file": str(target)}, asker)

    assert output.is_error and fragment in output.text
    assert b"senha" not in output.text.encode()
    assert world.api.calls() == [] and asker.messages == []


def test_a_file_over_the_ceiling_is_refused(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.admin_mcp import client

    monkeypatch.setattr(client, "MAX_UPLOAD_BYTES", 10)
    big = _write(world.uploads / "grande.png", PNG)

    output = world.call("upload_asset", {"name": "logo-demo", "file": str(big)}, Asker(True))

    assert output.is_error and "10 MB" in output.text and world.api.calls() == []


# --- os arquivos enviados: só de dentro da pasta de envio ----------------------------------------


def test_without_the_upload_folder_the_upload_tools_explain_how_to_set_it(tmp_path: Path) -> None:
    bare = make_world(tmp_path, upload_dir=None)
    picture = _write(tmp_path / "foto.png", PNG)
    asker = Asker(True)

    for name, arguments in (
        ("upload_asset", {"name": "logo-demo", "file": str(picture)}),
        ("replace_asset", {"asset_id": "a", "expected_version": "1", "file": str(picture)}),
    ):
        output = bare.call(name, arguments, asker)
        assert output.is_error and "defina GERADOR_MCP_UPLOAD_DIR" in output.text
        assert "bloco `env`" in output.text and "servida sem login" in output.text

    assert bare.api.calls() == [] and asker.messages == []
    bare.close()


def test_a_file_outside_the_upload_folder_is_never_read_even_if_it_is_an_image(
    world: World, tmp_path: Path
) -> None:
    outside = _write(tmp_path / "particular" / "foto-de-familia.png", PNG)
    asker = Asker(True)

    for given in (
        str(outside),
        "../particular/foto-de-familia.png",
        f"{world.uploads}/../particular/foto-de-familia.png",
    ):
        output = world.call("upload_asset", {"name": "logo-demo", "file": given}, asker)
        assert output.is_error and "fora da pasta de envio" in output.text, given

    assert world.api.calls() == [] and asker.messages == []


def test_a_symlink_inside_the_folder_that_points_outside_is_refused(
    world: World, tmp_path: Path
) -> None:
    if os.name != "posix":
        pytest.skip("symlink do POSIX")
    outside = _write(tmp_path / "particular" / "foto.png", PNG)
    (world.uploads / "atalho.png").symlink_to(outside)
    (world.uploads / "pasta-atalho").symlink_to(outside.parent, target_is_directory=True)
    asker = Asker(True)

    for given in ("atalho.png", "pasta-atalho/foto.png", str(world.uploads / "atalho.png")):
        output = world.call("upload_asset", {"name": "logo-demo", "file": given}, asker)
        assert output.is_error and "fora da pasta de envio" in output.text, given

    assert world.api.calls() == [] and asker.messages == []


def test_a_symlink_that_stays_inside_the_folder_is_shown_with_its_real_path(
    world: World,
) -> None:
    if os.name != "posix":
        pytest.skip("symlink do POSIX")
    real = _write(world.uploads / "reais" / "logo.png", PNG)
    (world.uploads / "atalho.png").symlink_to(real)
    asker = Asker(False)

    world.call("upload_asset", {"name": "logo-demo", "file": "atalho.png"}, asker)

    assert f"Arquivo (file): {real.resolve()} ({len(PNG)} bytes)" in asker.messages[0]


def test_the_upload_folder_may_itself_be_a_symlink(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("symlink do POSIX")
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    linked = make_world(tmp_path, upload_dir=link)
    picture = _write(real / "x.png", PNG)
    linked.api.when("POST", "/assets", answer(201, {}))

    output = linked.call("upload_asset", {"name": "logo-demo", "file": str(picture)}, Asker(True))

    assert not output.is_error
    linked.close()


def test_a_path_outside_the_folder_gets_the_same_answer_whether_it_exists_or_not(
    world: World, tmp_path: Path
) -> None:
    """Sem oráculo de existência: o modelo não usa a ferramenta para descobrir o que há no disco."""
    present = _write(tmp_path / "particular" / "existe.png", PNG)
    (tmp_path / "vazia").mkdir()
    asker = Asker(True)
    pairs = [
        (str(present), str(tmp_path / "particular" / "nao-existe.png")),
        (str(tmp_path / "particular"), str(tmp_path / "nenhuma-pasta")),  # uma pasta que existe
        ("../particular/existe.png", "../particular/nao-existe.png"),
        ("../vazia", "../nao-existe-pasta/x"),
        ("/etc/hosts", "/etc/nao-existe-mesmo"),
        ("/", "/nao-existe-mesmo/x/y"),
    ]

    for exists, missing in pairs:
        a = world.call("upload_asset", {"name": "logo-demo", "file": exists}, asker)
        b = world.call("upload_asset", {"name": "logo-demo", "file": missing}, asker)
        assert a.is_error and a.text == b.text, (exists, missing)
        assert "fora da pasta de envio" in a.text

    assert world.api.calls() == [] and asker.messages == []


def test_a_dangling_symlink_to_the_outside_is_also_just_outside(
    world: World, tmp_path: Path
) -> None:
    if os.name != "posix":
        pytest.skip("symlink do POSIX")
    (world.uploads / "pendente.png").symlink_to(tmp_path / "particular" / "nao-existe.png")
    real = _write(tmp_path / "particular" / "existe.png", PNG)
    (world.uploads / "valido.png").symlink_to(real)

    dangling = world.call("upload_asset", {"name": "logo-demo", "file": "pendente.png"})
    valid = world.call("upload_asset", {"name": "logo-demo", "file": "valido.png"})

    assert "fora da pasta de envio" in dangling.text and "fora da pasta de envio" in valid.text


def test_inside_the_folder_a_missing_file_is_said_to_be_missing(world: World) -> None:
    output = world.call("upload_asset", {"name": "logo-demo", "file": "nao-existe.png"})

    assert "não existe dentro da pasta de envio" in output.text


def test_the_file_is_read_without_following_a_symlink_on_the_last_name(tmp_path: Path) -> None:
    """Entre a conferência e a leitura alguém troca o arquivo por um atalho: `O_NOFOLLOW` recusa."""
    from app.admin_mcp import client

    if os.name != "posix":
        pytest.skip("O_NOFOLLOW é do POSIX")
    real = _write(tmp_path / "real.png", PNG)
    (tmp_path / "troca.png").symlink_to(real)

    assert client._read_regular(real, 1000) == PNG
    with pytest.raises(InputError, match="atalho"):
        client._read_regular(tmp_path / "troca.png", 1000)


def test_a_fifo_in_the_folder_does_not_hang_the_server(tmp_path: Path) -> None:
    from app.admin_mcp import client

    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO é do POSIX")
    fifo = tmp_path / "pipe.png"
    os.mkfifo(fifo)

    with pytest.raises(InputError, match="não é um arquivo comum"):
        client._read_regular(fifo, 1000)


def test_a_file_over_the_ceiling_is_refused_before_it_is_read_into_memory(tmp_path: Path) -> None:
    from app.admin_mcp import client

    big = _write(tmp_path / "x.png", PNG + b"." * 50)

    with pytest.raises(InputError, match="10 MB"):
        client._read_regular(big, 20)


def test_an_upload_folder_that_does_not_exist_says_so(tmp_path: Path) -> None:
    gone = make_world(tmp_path, upload_dir=tmp_path / "nao-existe")
    asker = Asker(True)

    output = gone.call("upload_asset", {"name": "logo-demo", "file": "x.png"}, asker)

    assert output.is_error and "A pasta GERADOR_MCP_UPLOAD_DIR não existe" in output.text
    assert (
        "não existe (confira o caminho)" not in output.text
    )  # a culpa não é do caminho do arquivo
    assert gone.api.calls() == [] and asker.messages == []
    gone.close()


def test_an_upload_folder_that_is_a_file_says_so(tmp_path: Path) -> None:
    not_a_folder = _write(tmp_path / "arquivo.txt", b"x")
    broken = make_world(tmp_path, upload_dir=not_a_folder)

    output = broken.call("upload_asset", {"name": "logo-demo", "file": "x.png"})

    assert "GERADOR_MCP_UPLOAD_DIR não é uma pasta" in output.text
    broken.close()


# --- os arquivos enviados: a pessoa confirma o arquivo, sempre -----------------------------------


def test_the_person_is_asked_about_the_file_with_its_resolved_path_and_size(world: World) -> None:
    picture = _write(world.uploads / "logo.png", PNG)
    world.api.when("POST", "/assets", answer(201, {}))
    asker = Asker(True)

    output = world.call("upload_asset", {"name": "logo-demo", "file": str(picture)}, asker)

    assert not output.is_error
    (question,) = asker.messages
    assert f"Arquivo (file): {picture.resolve()} ({len(PNG)} bytes)" in question
    assert "servida sem login" in question
    assert "Campos enviados: name, file" in question


def test_a_no_to_the_file_sends_nothing(world: World) -> None:
    picture = _write(world.uploads / "logo.png", PNG)

    output = world.call("upload_asset", {"name": "logo-demo", "file": str(picture)}, Asker(False))

    assert output.is_error and "não confirmou" in output.text and world.api.calls() == []


def test_the_confirm_the_model_wrote_does_not_skip_the_question_about_a_file(world: World) -> None:
    picture = _write(world.uploads / "logo.png", PNG)
    asker = Asker(False)

    output = world.call(
        "upload_asset", {"name": "logo-demo", "file": str(picture), "confirm": True}, asker
    )

    assert output.is_error and len(asker.messages) == 1 and world.api.calls() == []


def test_without_elicitation_the_first_call_shows_the_file_and_the_next_one_sends_it(
    world: World,
) -> None:
    picture = _write(world.uploads / "logo.png", PNG)
    world.api.when("POST", "/assets", answer(201, {"id": "a1"}))
    arguments = {"name": "logo-demo", "file": str(picture)}

    first = world.call("upload_asset", arguments)
    second = world.call("upload_asset", {**arguments, "confirm": True})

    assert first.text.startswith("CONFIRMAÇÃO NECESSÁRIA: nada foi executado.")
    assert f"Arquivo (file): {picture.resolve()} ({len(PNG)} bytes)" in first.text
    assert not second.is_error
    (request,) = world.api.calls()
    assert PNG in request.content


def test_a_confirmation_is_for_that_file_not_for_that_name(world: World) -> None:
    """Trocar o conteúdo do arquivo entre a primeira chamada e a segunda (ou apontar o mesmo nome
    para outra imagem) exige mostrar de novo: a impressão digital leva o sha256."""
    picture = _write(world.uploads / "logo.png", PNG)
    world.api.when("POST", "/assets", answer(201, {}))
    arguments = {"name": "logo-demo", "file": str(picture)}
    world.call("upload_asset", arguments)

    _write(picture, PNG + b"outra imagem, outro conteudo")
    swapped = world.call("upload_asset", {**arguments, "confirm": True})

    assert swapped.text.startswith("CONFIRMAÇÃO NECESSÁRIA") and world.api.calls() == []
    assert not world.call("upload_asset", {**arguments, "confirm": True}).is_error


def test_the_bytes_sent_are_the_bytes_the_person_confirmed(world: World) -> None:
    class Swap(Asker):
        async def ask(self, message: str) -> bool | None:
            _write(picture, PNG + b"trocado depois da pergunta")
            return await super().ask(message)

    picture = _write(world.uploads / "logo.png", PNG)
    world.api.when("POST", "/assets", answer(201, {}))

    world.call("upload_asset", {"name": "logo-demo", "file": str(picture)}, Swap(True))

    (request,) = world.api.calls()
    assert PNG in request.content and b"trocado depois" not in request.content


def test_a_file_tool_asks_even_though_the_api_does_not_mark_it(world: World) -> None:
    assert not world.tools["upload_asset"].marked
    picture = _write(world.uploads / "logo.png", PNG)
    asker = Asker(True)

    world.call("upload_asset", {"name": "logo-demo", "file": str(picture)}, asker)

    assert len(asker.messages) == 1


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- a confirmação -------------------------------------------------------------------------------


def test_with_elicitation_the_person_is_asked_and_only_the_yes_sends_confirm(world: World) -> None:
    world.api.when("POST", "/forms/{slug}/activate", answer(200, {"active": True}))
    asker = Asker(True)

    output = world.call("activate_form", {"slug": "demo"}, asker)

    assert not output.is_error
    (question,) = asker.messages
    assert f"«{world.tools['activate_form'].summary}» (`activate_form`)" in question
    assert "Efeito: muda o que a lead vê" in question
    assert "Alvo: slug=demo" in question
    (request,) = world.api.calls()
    assert request.url.path.endswith("/forms/demo/activate")
    assert body_of(request) == {"confirm": True}


def test_a_no_from_the_person_sends_nothing(world: World) -> None:
    asker = Asker(False)

    output = world.call("activate_form", {"slug": "demo"}, asker)

    assert output.is_error and "não confirmou" in output.text
    assert world.api.calls() == []


def test_with_elicitation_the_confirm_the_model_wrote_is_ignored(world: World) -> None:
    """Nada que o modelo escreva pula a pergunta: o `confirm: true` dele não vale, e o `não` da
    pessoa vence."""
    asker = Asker(False)

    output = world.call("activate_form", {"slug": "demo", "confirm": True}, asker)

    assert output.is_error and world.api.calls() == [] and len(asker.messages) == 1


def test_the_question_names_the_fields_but_never_their_values(world: World) -> None:
    """O corpo pode levar segredo (o token de uma integração) ou texto longo: a pergunta lista os
    nomes dos campos do corpo, e só os valores curtos do caminho e da query."""
    world.api.when("PUT", "/forms/{slug}/integrations/baserow/connection", answer(200, {}))
    secret = "token-" + new_key()
    asker = Asker(True)

    world.call(
        "connect_form_integration",
        {
            "slug": "demo",
            "expected_version": "0",
            "base_url": "https://baserow.exemplo.test",
            "table_id": 7,
            "token": secret,
        },
        asker,
    )

    (question,) = asker.messages
    assert secret not in question and "baserow.exemplo.test" not in question
    assert "Alvo: slug=demo" in question
    assert "Campos enviados: expected_version, base_url, table_id, token" in question
    assert body_of(world.api.calls()[0])["token"] == secret  # a API recebe; a pergunta não mostra


def test_without_elicitation_the_first_call_only_describes_the_effect(world: World) -> None:
    output = world.call(
        "reprocess_job", {"public_id": "abc", "reason": "x" * 12, "expected_attempt": 1}
    )

    assert not output.is_error
    assert output.text.startswith("CONFIRMAÇÃO NECESSÁRIA: nada foi executado.")
    assert "`confirm: true`" in output.text and "`reprocess_job`" in output.text
    assert "public_id=abc" in output.text
    assert world.api.calls() == []


def test_without_elicitation_the_next_call_with_confirm_true_executes(world: World) -> None:
    world.api.when("POST", "/jobs/{public_id}/reprocess", answer(200, {"attempt": 2}))
    arguments = {"public_id": "abc", "reason": "Prompt corrigido", "expected_attempt": 1}

    world.call("reprocess_job", arguments)
    second = world.call("reprocess_job", {**arguments, "confirm": True})

    assert not second.is_error and json.loads(second.text) == {"attempt": 2}
    (request,) = world.api.calls()
    assert body_of(request) == {
        "reason": "Prompt corrigido",
        "expected_attempt": 1,
        "confirm": True,
    }


def test_a_confirm_true_before_the_server_showed_the_call_does_not_execute(world: World) -> None:
    arguments = {"public_id": "abc", "reason": "Prompt corrigido", "expected_attempt": 1}

    first = world.call("reprocess_job", {**arguments, "confirm": True})

    assert first.text.startswith("CONFIRMAÇÃO NECESSÁRIA") and world.api.calls() == []


def test_the_confirmation_belongs_to_the_exact_call(world: World) -> None:
    world.api.when("POST", "/jobs/{public_id}/reprocess", answer(200, {}))
    arguments = {"public_id": "abc", "reason": "Prompt corrigido", "expected_attempt": 1}
    world.call("reprocess_job", arguments)

    other_job = world.call("reprocess_job", {**arguments, "public_id": "outro", "confirm": True})
    other_reason = world.call(
        "reprocess_job", {**arguments, "reason": "Outro motivo!", "confirm": True}
    )

    assert other_job.text.startswith("CONFIRMAÇÃO NECESSÁRIA")
    assert other_reason.text.startswith("CONFIRMAÇÃO NECESSÁRIA")
    assert world.api.calls() == []


def test_a_confirmation_expires_and_is_used_once(world: World) -> None:
    world.api.when("POST", "/jobs/{public_id}/reprocess", answer(200, {}))
    arguments = {"public_id": "abc", "reason": "Prompt corrigido", "expected_attempt": 1}

    world.call("reprocess_job", arguments)
    world.clock.now += 901  # a pessoa não voltou
    expired = world.call("reprocess_job", {**arguments, "confirm": True})
    done = world.call(
        "reprocess_job", {**arguments, "confirm": True}
    )  # mostrou de novo na anterior
    again = world.call("reprocess_job", {**arguments, "confirm": True})  # e a confirmação foi gasta

    assert expired.text.startswith("CONFIRMAÇÃO NECESSÁRIA")
    assert not done.is_error
    assert again.text.startswith("CONFIRMAÇÃO NECESSÁRIA")
    assert len(world.api.calls()) == 1


def test_a_delete_confirms_through_the_query(world: World) -> None:
    world.api.when("DELETE", "/assets/{asset_id}", answer(200, {"removed": True}))

    world.call("remove_asset", {"asset_id": "a1", "expected_version": "4"}, Asker(True))

    (request,) = world.api.calls()
    assert request.method == "DELETE"
    assert query_of(request) == {"expected_version": "4", "confirm": "true"}
    assert request.content == b""


def test_a_dry_run_publication_asks_nothing_and_sends_no_confirm(world: World) -> None:
    world.api.when("POST", "/forms/{slug}/draft/publish", answer(200, {"missing": []}))
    asker = Asker()

    output = world.call(
        "publish_form_draft", {"slug": "demo", "expected_version": "3", "dry_run": True}, asker
    )

    assert not output.is_error and asker.messages == []
    (request,) = world.api.calls()
    assert body_of(request) == {"expected_version": "3", "dry_run": True}


def test_a_real_publication_asks_even_when_dry_run_is_false(world: World) -> None:
    world.api.when("POST", "/forms/{slug}/draft/publish", answer(200, {"version": 4}))
    asker = Asker(True)

    world.call(
        "publish_form_draft",
        {"slug": "demo", "expected_version": "3", "dry_run": False, "note": "Mudou o prompt"},
        asker,
    )

    assert len(asker.messages) == 1
    (request,) = world.api.calls()
    assert body_of(request)["confirm"] is True and body_of(request)["dry_run"] is False


def test_an_unmarked_operation_never_asks(world: World) -> None:
    world.api.when("PUT", "/forms/{slug}/draft", answer(200, {"version": "2"}))
    asker = Asker()

    world.call("save_form_draft", {"slug": "demo", "expected_version": "1", "document": {}}, asker)

    assert asker.messages == []


def test_when_the_question_fails_the_server_falls_back_to_the_next_call(world: World) -> None:
    """O cliente declarou a *elicitation* mas a pergunta caiu (`None`): vale o caminho da
    chamada seguinte, e nada é executado."""
    output = world.call("activate_form", {"slug": "demo"}, Asker(None))

    assert output.text.startswith("CONFIRMAÇÃO NECESSÁRIA") and world.api.calls() == []


# --- as confirmações além do `confirm` -----------------------------------------------------------

CONTACT = (
    "O prompt do formulário manda o nome e o e-mail da lead ao provedor do modelo em todo pedido; "
    "confirme com confirm_contact."
)


def _contact_gate(world: World) -> None:
    """A publicação recusa até receber `confirm_contact`, como a API."""

    def reply(request: httpx.Request) -> httpx.Response:
        if body_of(request).get("confirm_contact") is True:
            return answer(200, {"version": 4})
        return error(422, "confirmation_required", CONTACT)

    world.api.when("POST", "/forms/{slug}/draft/publish", reply)


PUBLISH = {"slug": "demo", "expected_version": "3", "note": "Mudou o prompt"}


def test_with_elicitation_a_contact_refusal_is_put_to_the_person_with_the_detail(
    world: World,
) -> None:
    _contact_gate(world)
    asker = Asker(True, True)

    output = world.call("publish_form_draft", PUBLISH, asker)

    assert not output.is_error and json.loads(output.text) == {"version": 4}
    assert CONTACT in asker.messages[1]
    first, second = world.api.calls()
    assert "confirm_contact" not in body_of(first) and body_of(first)["confirm"] is True
    assert body_of(second)["confirm_contact"] is True and body_of(second)["confirm"] is True


def test_with_elicitation_a_no_to_the_contact_question_stops_there(world: World) -> None:
    _contact_gate(world)

    output = world.call("publish_form_draft", PUBLISH, Asker(True, False))

    assert output.is_error and "não confirmou" in output.text
    assert len(world.api.calls()) == 1  # a segunda tentativa não saiu


def test_the_contact_flag_the_model_wrote_is_not_forwarded_before_the_person_saw_the_detail(
    world: World,
) -> None:
    _contact_gate(world)

    first = world.call("publish_form_draft", {**PUBLISH, "confirm": True, "confirm_contact": True})

    assert first.text.startswith("CONFIRMAÇÃO NECESSÁRIA")  # primeiro mostra o efeito
    second = world.call("publish_form_draft", {**PUBLISH, "confirm": True, "confirm_contact": True})
    # Agora o `confirm` vale, mas o `confirm_contact` ainda não foi mostrado: a API recusa e o
    # servidor devolve o `detail` e o campo certo para reenviar.
    assert second.text.startswith("CONFIRMAÇÃO NECESSÁRIA: nada foi gravado.")
    assert CONTACT in second.text and "`confirm: true`" in second.text
    assert "`confirm_contact: true`" in second.text
    assert all("confirm_contact" not in body_of(r) for r in world.api.calls())

    third = world.call("publish_form_draft", {**PUBLISH, "confirm": True, "confirm_contact": True})

    assert not third.is_error and json.loads(third.text) == {"version": 4}
    assert body_of(world.api.calls()[-1])["confirm_contact"] is True


def test_http_in_production_asks_for_confirm_http_the_same_way(world: World) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        if body_of(request).get("confirm_http") is True:
            return answer(200, {"version": "1"})
        return error(
            422, "confirmation_required", "Em produção, http manda os dados sem criptografia."
        )

    world.api.when("PUT", "/destinations/{scope}", reply)
    arguments = {"scope": "default", "expected_version": "0", "url": "http://hook.exemplo.test/x"}

    asker = Asker(True, True)
    output = world.call("save_destination", arguments, asker)

    assert not output.is_error
    assert "sem criptografia" in asker.messages[1]
    assert body_of(world.api.calls()[-1])["confirm_http"] is True


def test_a_refusal_that_is_not_a_contact_confirmation_is_just_an_error(world: World) -> None:
    world.api.when(
        "POST",
        "/forms/{slug}/draft/publish",
        error(
            422,
            "validation_failed",
            "Não vale.",
            errors=[{"field": "note", "message": "Escreva a nota."}],
        ),
    )

    output = world.call("publish_form_draft", PUBLISH, Asker(True))

    assert (
        output.is_error
        and "422 validation_failed" in output.text
        and "note: Escreva a nota." in output.text
    )


# --- o que volta ---------------------------------------------------------------------------------


def test_the_api_error_becomes_a_portuguese_text_with_the_fields_and_the_reasons(
    world: World,
) -> None:
    world.api.when(
        "POST",
        "/forms/{slug}/draft/publish",
        error(
            422,
            "validation_failed",
            "A publicação foi recusada.",
            errors=[
                {"field": "pdf.html", "message": "Falta fechar o bloco.", "line": 12},
                {"field": "", "message": "Nada para publicar."},
            ],
            reasons=["Falta a nota.", "Falta o teste do que vai ao LLM."],
        ),
    )

    output = world.call("publish_form_draft", {**PUBLISH, "dry_run": True})

    assert output.is_error
    assert output.text.splitlines() == [
        "A API recusou (422 validation_failed): A publicação foi recusada.",
        "- pdf.html (linha 12): Falta fechar o bloco.",
        "- corpo: Nada para publicar.",
        "O que falta para publicar:",
        "- Falta a nota.",
        "- Falta o teste do que vai ao LLM.",
    ]


def test_a_version_conflict_tells_the_current_version(world: World) -> None:
    world.api.when(
        "PUT",
        "/forms/{slug}/draft",
        error(409, "version_conflict", "O rascunho mudou.", current_version="7"),
    )

    output = world.call(
        "save_form_draft", {"slug": "demo", "expected_version": "1", "document": {}}
    )

    assert output.is_error
    assert "409 version_conflict" in output.text and "A versão de agora é 7" in output.text


def test_rate_limits_say_when_to_try_again(world: World) -> None:
    world.api.when(
        "GET",
        "/assets",
        httpx.Response(
            429,
            json={"code": "rate_limited", "detail": "Muitas chamadas."},
            headers={"Retry-After": "12"},
        ),
    )

    output = world.call("list_assets")

    assert output.is_error
    assert output.text.splitlines() == [
        "A API recusou (429 rate_limited): Muitas chamadas.",
        "Tente de novo depois de 12 s.",
    ]


def test_a_reply_that_is_not_the_api_error_is_reported_without_its_body(world: World) -> None:
    world.api.when(
        "GET",
        "/forms",
        httpx.Response(
            502,
            text="<html>Bad gateway: dados internos do proxy</html>",
            headers={"content-type": "text/html"},
        ),
    )

    output = world.call("list_forms")

    assert output.is_error
    assert output.text == "A API respondeu 502, sem o corpo de erro esperado."


def test_a_network_failure_says_what_to_check_and_not_the_library_message(world: World) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"conexão recusada para {request.url} com {world.key}")

    world.api.when("GET", "/forms", refuse)
    output = world.call("list_forms")

    assert output.is_error
    assert output.text.startswith("Não consegui falar com a API (ConnectError).")
    assert "GERADOR_API_URL" in output.text and world.key not in output.text
    assert "recusada" not in output.text


def test_a_timeout_is_reported(world: World) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("devagar", request=request)

    world.api.when("GET", "/forms", slow)

    assert "não respondeu a tempo" in world.call("list_forms").text


def _times_out(
    world: World, method: str, path: str, exception: type[httpx.TimeoutException]
) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise exception("devagar", request=request)

    world.api.when(method, path, slow)


def test_a_write_that_timed_out_may_have_run_so_the_text_says_to_check_before_repeating(
    world: World,
) -> None:
    _times_out(world, "POST", "/jobs/{public_id}/reprocess", httpx.ReadTimeout)
    arguments = {"public_id": "abc", "reason": "Prompt corrigido", "expected_attempt": 1}

    output = world.call("reprocess_job", arguments, Asker(True))

    assert output.is_error
    assert "PODE ter sido executada" in output.text and "Não repita ainda" in output.text
    assert "`get_...` ou `list_...`" in output.text
    assert "Tente de novo" not in output.text and "ReadTimeout" in output.text


def test_a_write_that_dropped_after_going_out_gets_the_same_warning(world: World) -> None:
    def dropped(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("caiu", request=request)

    world.api.when("PUT", "/forms/{slug}/draft", dropped)

    output = world.call(
        "save_form_draft", {"slug": "demo", "expected_version": "1", "document": {}}
    )

    assert "PODE ter sido executada" in output.text and "Tente de novo" not in output.text


def test_a_write_that_never_connected_is_safe_to_repeat(world: World) -> None:
    _times_out(world, "POST", "/jobs/{public_id}/reprocess", httpx.ConnectTimeout)
    arguments = {"public_id": "abc", "reason": "Prompt corrigido", "expected_attempt": 1}

    output = world.call("reprocess_job", arguments, Asker(True))

    assert "PODE ter sido executada" not in output.text and "Tente de novo" in output.text


def test_a_read_that_timed_out_can_just_be_tried_again(world: World) -> None:
    _times_out(world, "GET", "/forms", httpx.ReadTimeout)

    output = world.call("list_forms")

    assert "Tente de novo" in output.text and "PODE ter sido executada" not in output.text


def test_a_timeout_on_a_write_clears_the_confirmation_so_the_retry_asks_again(world: World) -> None:
    _times_out(world, "POST", "/jobs/{public_id}/reprocess", httpx.ReadTimeout)
    arguments = {"public_id": "abc", "reason": "Prompt corrigido", "expected_attempt": 1}
    world.call("reprocess_job", arguments)  # mostra o efeito

    timed_out = world.call("reprocess_job", {**arguments, "confirm": True})
    retry = world.call("reprocess_job", {**arguments, "confirm": True})  # às cegas, sem conferir

    assert "PODE ter sido executada" in timed_out.text
    assert retry.text.startswith("CONFIRMAÇÃO NECESSÁRIA") and len(world.api.calls()) == 1


def test_a_timeout_on_a_write_makes_the_retry_ask_the_person_again(world: World) -> None:
    _times_out(world, "POST", "/jobs/{public_id}/reprocess", httpx.ReadTimeout)
    arguments = {"public_id": "abc", "reason": "Prompt corrigido", "expected_attempt": 1}
    asker = Asker(True, True)

    world.call("reprocess_job", arguments, asker)
    world.call("reprocess_job", arguments, asker)

    assert len(asker.messages) == 2 and len(world.api.calls()) == 2


def test_a_json_reply_is_returned_as_the_api_wrote_it(world: World) -> None:
    world.api.when(
        "GET",
        "/forms",
        httpx.Response(
            200, text='{"items": [], "ação": "ç"}', headers={"content-type": "application/json"}
        ),
    )

    assert world.call("list_forms").text == '{"items": [], "ação": "ç"}'


def test_a_huge_json_reply_is_cut_and_the_cut_is_said(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(results, "MAX_TEXT", 50)
    world.api.when("GET", "/forms", answer(200, {"items": ["x" * 100]}))

    output = world.call("list_forms")

    assert output.text.startswith('{"items":["xxx') and "resposta cortada" in output.text
    assert not output.is_error


def test_a_reply_without_a_body_is_a_plain_success(world: World) -> None:
    world.api.when("GET", "/forms", httpx.Response(204))

    assert world.call("list_forms") == ToolOutput("Pronto: a API respondeu sem corpo.")


# --- os arquivos devolvidos ----------------------------------------------------------------------


def test_a_pdf_is_saved_to_a_private_file_and_the_tool_returns_only_its_path(
    world: World, tmp_path: Path
) -> None:
    content = b"%PDF-1.7 " + b"plano sintetico " * 50
    world.api.when("POST", "/forms/{slug}/draft/pdf-preview", pdf(content))

    output = world.call("render_form_draft_pdf_preview", {"slug": "demo"})

    assert not output.is_error
    fields = dict(line.split(": ", 1) for line in output.text.splitlines()[1:])
    saved = Path(fields["caminho"])
    assert saved.parent == tmp_path / "downloads" and saved.suffix == ".pdf"
    assert saved.name.startswith("render_form_draft_pdf_preview-")
    assert saved.read_bytes() == content
    assert fields["tipo"] == "application/pdf" and fields["tamanho"] == f"{len(content)} bytes"
    assert len(fields["sha256"]) == 64
    assert "plano sintetico" not in output.text and "%PDF" not in output.text
    if os.name == "posix":
        assert stat.S_IMODE(saved.stat().st_mode) == 0o600
        assert stat.S_IMODE(saved.parent.stat().st_mode) == 0o700


def test_an_html_preview_is_saved_too(world: World) -> None:
    world.api.when("GET", "/forms/{slug}/draft/public-page", html("<html>prévia</html>"))

    output = world.call("render_form_draft_public_page", {"slug": "demo"})

    path = Path(output.text.split("caminho: ", 1)[1].splitlines()[0])
    assert path.suffix == ".html" and path.read_text(encoding="utf-8") == "<html>prévia</html>"
    assert "prévia" not in output.text


def test_the_file_name_never_carries_the_order_code_or_the_form_address(
    tmp_path: Path,
) -> None:
    lead = make_world(tmp_path, lead_data=True)
    lead.api.when("GET", "/jobs/{public_id}/pdf", pdf())

    output = lead.call("download_job_pdf", {"public_id": "codigo-do-pedido-123"})

    assert "codigo-do-pedido-123" not in output.text
    assert Path(output.text.split("caminho: ", 1)[1].splitlines()[0]).name.startswith(
        "download_job_pdf-"
    )
    lead.close()


def test_the_same_content_lands_on_the_same_file(world: World) -> None:
    world.api.when("POST", "/forms/{slug}/draft/pdf-preview", pdf())

    first = world.call("render_form_draft_pdf_preview", {"slug": "a"})
    second = world.call("render_form_draft_pdf_preview", {"slug": "b"})

    assert first.text == second.text
    assert len(list((Path(world.config.download_dir or ".")).iterdir())) == 1


def test_the_default_folder_is_private_to_the_user_and_a_symlink_is_not_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "posix":
        pytest.skip("permissões de pasta são do POSIX")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    plain = World(tmp_path)
    plain.config = make_config(plain.key)  # sem GERADOR_MCP_DOWNLOAD_DIR
    plain.runner = ToolRunner(plain.client, plain.config)
    plain.api.when("POST", "/forms/{slug}/draft/pdf-preview", pdf())

    ok = plain.call("render_form_draft_pdf_preview", {"slug": "demo"})

    folder = tmp_path / f"gerador-mcp-{os.getuid()}"
    assert folder.is_dir() and stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert Path(ok.text.split("caminho: ", 1)[1].splitlines()[0]).parent == folder

    folder_target = tmp_path / "alheia"
    folder_target.mkdir()
    for item in folder.iterdir():
        item.unlink()
    folder.rmdir()
    folder.symlink_to(folder_target)
    refused = plain.call("render_form_draft_pdf_preview", {"slug": "demo"})

    assert refused.is_error and "Não consegui ler ou gravar o arquivo" in refused.text
    assert list(folder_target.iterdir()) == []
    plain.close()


# --- a chave nunca aparece -----------------------------------------------------------------------


def test_the_key_is_redacted_from_anything_the_api_sends_back(world: World) -> None:
    world.api.when(
        "GET", "/forms", answer(200, {"eco": world.key, "segredo": world.key.split("_", 2)[2]})
    )
    world.api.when("GET", "/assets", error(500, "internal_error", f"falhou com {world.key}"))

    ok = world.call("list_forms")
    failed = world.call("list_assets")

    for output in (ok, failed):
        assert world.key not in output.text and world.key.split("_", 2)[2] not in output.text
        assert MASK in output.text


def test_an_unexpected_failure_returns_only_the_type_and_logs_without_the_key(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter(Redactor(world.key)))
    logger = logging.getLogger("gerador_mcp")
    level, disabled = logger.level, logger.disabled
    # O `fileConfig` das migrations (outro teste do mesmo processo) desliga os loggers que já existem.
    logger.disabled = False
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    def explode(*_: Any, **__: Any) -> None:
        raise RuntimeError(f"estourou com {world.key}")

    monkeypatch.setattr("app.admin_mcp.runner.prepare", explode)
    try:
        output = world.call("list_forms")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
        logger.disabled = disabled

    assert output.is_error and "RuntimeError" in output.text
    assert world.key not in output.text and "estourou" not in output.text
    logged = stream.getvalue()
    assert "Falha inesperada em list_forms" in logged and "Traceback" in logged
    assert world.key not in logged and MASK in logged


def test_the_key_only_ever_travels_in_the_authorization_header(world: World) -> None:
    picture = _write(world.uploads / "x.png", PNG)
    world.api.when("GET", "/forms", answer(200, {}))
    world.api.when("POST", "/assets", answer(201, {}))
    world.api.when("DELETE", "/assets/{asset_id}", answer(200, {}))

    world.call("list_forms")
    world.call("upload_asset", {"name": "logo-demo", "file": str(picture)}, Asker(True))
    world.call("remove_asset", {"asset_id": "a1", "expected_version": "1"}, Asker(True))

    assert len(world.api.requests) == 3
    for request in world.api.requests:
        assert request.headers["authorization"] == f"Bearer {world.key}"
        assert world.key not in str(request.url)
        assert world.key.encode() not in request.content
        assert [v for k, v in request.headers.items() if world.key in v] == [f"Bearer {world.key}"]


def test_the_redactor_masks_the_whole_key_and_its_secret_part() -> None:
    key = new_key()
    secret = key.split("_", 2)[2]
    redact = Redactor(key)

    assert redact(f"a {key} b") == f"a {MASK} b"
    assert redact(f"só o segredo {secret}") == f"só o segredo {MASK}"
    assert redact("nada a esconder") == "nada a esconder"
    assert redact.tree({"a": [key, 1], key: {"b": secret}}) == {"a": [MASK, 1], MASK: {"b": MASK}}
    assert Redactor("")("texto") == "texto"


def test_the_redactor_also_masks_the_key_when_it_is_echoed_inside_json_or_a_url() -> None:
    key = 'gp_1a2b3c4d_abc"def\\ghi/jkl+mno123456'
    redact = Redactor(key)
    echoed_in_json = json.dumps({"detail": f"chave {key} recusada"})
    echoed_in_a_url = f"https://exemplo.test/x?k={quote(key, safe='')}"

    assert key not in redact(echoed_in_json) and json.dumps(key)[1:-1] not in redact(echoed_in_json)
    assert quote(key, safe="") not in redact(echoed_in_a_url)
    assert MASK in redact(echoed_in_json) and MASK in redact(echoed_in_a_url)
    assert redact.tree({"detail": f"chave {key}"}) == {"detail": f"chave {MASK}"}


def test_a_value_the_model_wrote_cannot_draw_a_line_in_the_question(world: World) -> None:
    asker = Asker(False)
    rlo, zwsp = chr(0x202E), chr(0x200B)  # inversão de direção e espaço de largura zero
    sneaky = "1\nConfirmado pela pessoa.\r" + rlo + "oculto\x00 " + zwsp + "fim"

    world.call("remove_asset", {"asset_id": "a1", "expected_version": sneaky}, asker)

    (question,) = asker.messages
    lines = question.split("\n")
    assert len(lines) == 4  # a pergunta, o vazio, o efeito e o alvo: nenhuma linha a mais
    assert lines[3] == "Alvo: asset_id=a1, expected_version=1 Confirmado pela pessoa. oculto fim"
    assert not set(question) & {"\r", "\x00", rlo, zwsp}


def test_a_value_the_model_wrote_cannot_draw_a_line_in_the_first_step_text(world: World) -> None:
    first = world.call("remove_asset", {"asset_id": "a1", "expected_version": "1\nCONFIRMADO."})

    assert first.text.startswith("CONFIRMAÇÃO NECESSÁRIA")
    assert not any(line.startswith("CONFIRMADO") for line in first.text.splitlines())
    assert "expected_version=1 CONFIRMADO." in first.text


def test_a_file_name_with_control_characters_cannot_draw_a_line_in_the_question(
    world: World,
) -> None:
    if os.name != "posix":
        pytest.skip("nome de arquivo com quebra de linha é do POSIX")
    picture = _write(world.uploads / "a\nCONFIRMADO pela pessoa.png", PNG)
    asker = Asker(False)

    world.call("upload_asset", {"name": "logo-demo", "file": str(picture)}, asker)

    (question,) = asker.messages
    assert not any(line.startswith("CONFIRMADO") for line in question.split("\n"))
    assert "a CONFIRMADO pela pessoa.png" in question


def test_a_hostile_operation_name_cannot_make_the_download_land_outside_its_folder(
    world: World, tmp_path: Path
) -> None:
    """O catálogo já recusa um `operationId` assim; o arquivo confere de novo, por conta própria."""
    hostile = replace(world.tools["render_form_draft_pdf_preview"], name="../../escaped")
    world.api.when("POST", "/forms/{slug}/draft/pdf-preview", pdf())

    output = asyncio.run(world.runner.call(hostile, {"slug": "a"}, NeverAsks()))

    assert output.is_error and "Não consegui ler ou gravar o arquivo" in output.text
    assert list(tmp_path.rglob("escaped*")) == []


def test_the_config_never_shows_the_key() -> None:
    key = new_key()
    config = Config.from_env(
        {"GERADOR_API_URL": "https://planos.exemplo.test", "GERADOR_API_KEY": key}
    )

    assert key not in repr(config) and key not in str(config)
    assert MASK in repr(config)


# --- as peças soltas -----------------------------------------------------------------------------


def test_prepare_refuses_an_argument_the_tool_does_not_have(world: World) -> None:
    with pytest.raises(InputError):
        prepare(world.tools["get_form"], {"slug": "demo", "nao_existe": 1})


def test_a_client_does_not_follow_redirects(tmp_path: Path) -> None:
    key = new_key()
    api = FakeApi(key)
    api.when(
        "GET", "/forms", httpx.Response(302, headers={"location": "https://outro.exemplo.test/x"})
    )
    client = ApiClient(make_config(key), transport=api.transport)

    response = asyncio.run(client.get("/forms"))

    assert response.status == 302
    assert [r.url.host for r in api.requests] == ["planos.exemplo.test"]
    asyncio.run(client.aclose())


def test_the_api_unavailable_error_is_a_plain_exception_with_a_portuguese_message(
    tmp_path: Path,
) -> None:
    key = new_key()

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("x")

    client = ApiClient(make_config(key), transport=httpx.MockTransport(boom))

    with pytest.raises(ApiUnavailable, match="ConnectError"):
        asyncio.run(client.get("/forms"))
    asyncio.run(client.aclose())


# --- o que uma composta lê do retorno (`status`, `data`, `awaiting`) e a chamada seguinte dela ---


def test_the_output_carries_the_status_and_the_json_so_a_composite_need_not_read_the_text(
    world: World,
) -> None:
    world.api.when("GET", "/forms/{slug}", answer(200, FORM))
    world.api.when("GET", "/forms", error(429, "rate_limited", "Devagar.", reasons=["x"]))

    ok = world.call("get_form", {"slug": "diagnostico-demo"})
    refused = world.call("list_forms")

    assert (ok.status, ok.data, ok.awaiting) == (200, FORM, False)
    assert (refused.status, refused.is_error) == (429, True)
    assert refused.data == {"code": "rate_limited", "detail": "Devagar.", "reasons": ["x"]}


def test_nothing_sent_means_no_status_and_the_extra_fields_stay_out_of_the_comparison(
    world: World,
) -> None:
    refused = world.call("get_form", {"slug": "a/b"})  # recusado antes de qualquer requisição
    cancelled = world.call("activate_form", {"slug": "demo"}, Asker(False))

    assert refused.is_error and refused.status is None and refused.data is None
    assert cancelled.is_error and cancelled.status is None and not cancelled.awaiting
    assert ToolOutput("a") == ToolOutput("a", status=200, data={"x": 1}, awaiting=True)


def test_a_json_with_the_key_in_it_is_given_to_the_composite_without_the_key(world: World) -> None:
    world.api.when("GET", "/forms/{slug}", answer(200, {"eco": world.key, "lista": [world.key]}))

    output = world.call("get_form", {"slug": "demo"})

    assert world.key not in output.text and world.key not in json.dumps(output.data)
    assert output.data == {"eco": MASK, "lista": [MASK]}


def test_a_call_waiting_for_the_person_is_marked_and_nothing_was_sent(world: World) -> None:
    first = world.call("activate_form", {"slug": "demo"})

    assert first.awaiting and not first.is_error and first.status is None
    assert world.api.calls() == []
    second = world.call("activate_form", {"slug": "demo", "confirm": True})
    assert not second.awaiting and second.status == 404  # a API de mentira não tem a rota


def test_a_companion_confirmation_that_waits_is_marked_too(world: World) -> None:
    _contact_gate(world)
    world.call("publish_form_draft", {**PUBLISH, "confirm": True})
    second = world.call("publish_form_draft", {**PUBLISH, "confirm": True})

    assert second.awaiting and "confirm_contact" in second.text


def test_when_a_composite_calls_the_next_step_is_the_composite_and_nothing_else_changes(
    world: World,
) -> None:
    spec = world.tools["activate_form"]

    shown = asyncio.run(
        world.runner.call(spec, {"slug": "demo"}, NeverAsks(), via="minha_composta")
    )
    direct = world.call("activate_form", {"slug": "demo"})

    assert (
        "chame `minha_composta` de novo, com os mesmos argumentos e `confirm: true`" in shown.text
    )
    assert "chame `activate_form` de novo" in direct.text
    assert shown.text.replace("minha_composta", "activate_form") == direct.text
    # A confirmação é a mesma chamada: repetir pela ferramenta gerada, com `confirm`, vale.
    sent = world.call("activate_form", {"slug": "demo", "confirm": True})
    assert sent.status == 404 and len(world.api.calls()) == 1


def test_what_a_composite_bound_to_the_call_is_part_of_what_the_confirmation_covers(
    world: World,
) -> None:
    """`bound` é o que a pessoa viu junto com a chamada e a chamada não leva (o mapeamento que a composta
    guarda logo depois da conexão): a mesma chamada com outro `bound` é outra ação."""
    spec = world.tools["activate_form"]
    arguments = {"slug": "demo"}

    def call(bound: Any, **more: Any) -> ToolOutput:
        return asyncio.run(world.runner.call(spec, {**arguments, **more}, NeverAsks(), bound=bound))

    shown = call({"sources": {"101": "lead.name"}})
    other = call({"sources": {"101": "lead.email"}}, confirm=True)
    unbound = call(None, confirm=True)
    same = call({"sources": {"101": "lead.name"}}, confirm=True)

    assert shown.awaiting and other.awaiting and unbound.awaiting  # nenhum dos três vale
    assert (
        not same.awaiting and same.status == 404
    )  # a mostrada vale (a API de mentira não tem a rota)
    assert [r.method for r in world.api.calls()] == ["POST"] and "bound" not in str(
        world.api.calls()[0].content
    )  # o `bound` não viaja à API


def test_a_companion_step_shown_for_a_composite_names_the_composite(world: World) -> None:
    _contact_gate(world)
    spec = world.tools["publish_form_draft"]
    arguments = {**PUBLISH, "confirm": True}
    asyncio.run(world.runner.call(spec, arguments, NeverAsks(), via="minha_composta"))

    second = asyncio.run(world.runner.call(spec, arguments, NeverAsks(), via="minha_composta"))

    assert (
        second.awaiting
        and "chame `minha_composta` de novo, com os mesmos argumentos" in second.text
    )
