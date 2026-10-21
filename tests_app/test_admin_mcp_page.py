"""A tela de configurar o MCP no admin (`/admin/mcp`): abre só com login, monta o JSON do Claude Desktop e
o comando do Claude Code com o endereço deste admin, cria uma chave ali mesmo (o segredo aparece uma vez,
com a regra e a auditoria da página das chaves) e a referência da API aponta para ela.

Só o comportamento principal. As chaves destes testes são sorteadas na execução, nunca literais: o
GitGuardian do PR acusa valor com cara de segredo."""

from __future__ import annotations

import html
import json
import re
import shlex
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from test_admin import ORIGIN, FakeClock, csrf_token, login
from test_admin_api_keys import KEY_SHAPE, audited, check, make
from test_admin_destinations import ADMIN_ENV

from app.admin import mcp_pages
from app.admin.api import keys
from app.admin.api.openapi import build
from app.api.main import create_app
from app.deps import Deps
from app.models import ApiKey
from app.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
MCP = "/admin/mcp"
# http em localhost: um endereço https faria o cookie da sessão ser `Secure`, e o TestClient (http) não o
# mandaria; o MCP aceita http só em localhost, como aqui.
ADDRESS = "http://localhost:8123"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def admin(deps: Deps, clock: FakeClock, address: str) -> TestClient:
    deps.settings = Settings(_env_file=None, PUBLIC_BASE_URL=address, **ADMIN_ENV)  # type: ignore[call-arg]
    current = TestClient(create_app(trigger=None, clock=clock), follow_redirects=False)
    assert login(current).status_code == 303
    return current


@pytest.fixture
def client(deps: Deps, clock: FakeClock) -> TestClient:
    return admin(deps, clock, ADDRESS)


def moment_of(clock: FakeClock) -> datetime:
    return datetime.fromtimestamp(clock(), UTC)


def block(page: str, block_id: str) -> str:
    """O texto de um bloco para copiar, como o botão «Copiar» o levaria (sem as marcas do HTML)."""
    found = re.search(rf'<pre[^>]*id="{block_id}"[^>]*>(.*?)</pre>', page, re.DOTALL)
    assert found is not None, f"{block_id} não está na página"
    return html.unescape(re.sub(r"<[^>]+>", "", found.group(1)))


def desktop_env(page: str) -> dict[str, str]:
    """O bloco `env` do JSON do Claude Desktop que a página mostra."""
    document = json.loads(block(page, "bloco-arquivo"))
    server = document["mcpServers"]["leadkit"]
    assert server["args"] == ["-m", "app.admin_mcp"]
    env: dict[str, str] = server["env"]
    return env


def post_key(
    browser: TestClient,
    name: str = "claude-desktop",
    role: str = "leitura",
    validity: str = "90",
    **choice: str,
) -> Any:
    data = {
        "name": name,
        "role": role,
        "validity_days": validity,
        "csrf_token": csrf_token(browser),
        **choice,
    }
    return browser.post(f"{MCP}/keys", data=data, headers=ORIGIN)


def test_the_screen_needs_a_session(client: TestClient, deps: Deps) -> None:
    anonymous = TestClient(client.app, follow_redirects=False)

    for response in (
        anonymous.get(MCP),
        anonymous.post(f"{MCP}/keys", data={"name": "abc", "role": "total"}, headers=ORIGIN),
    ):
        assert response.status_code == 303
        assert response.headers["location"].startswith("/admin/login")
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(ApiKey)) == 0


def test_the_menu_links_to_the_screen_and_marks_it_when_it_is_open(client: TestClient) -> None:
    assert '<a href="/admin/mcp">' in client.get("/admin/api-keys").text
    assert '<a href="/admin/advanced" aria-current="page">' in client.get(MCP).text
    assert '<a href="/admin/mcp">' in client.get("/admin/advanced").text


def test_the_desktop_json_carries_the_address_of_this_admin_and_a_marker_for_the_key(
    client: TestClient,
) -> None:
    page = client.get(MCP).text

    env = desktop_env(page)
    assert env == {"GERADOR_API_URL": ADDRESS, "GERADOR_API_KEY": "cole-a-chave-aqui"}
    assert "data-key-slot>cole-a-chave-aqui</span>" in page
    assert "GERADOR_MCP_LEAD_DATA" not in block(page, "bloco-arquivo")  # desligado por padrão


def test_the_claude_code_command_is_one_line_with_the_options_before_the_server_name(
    client: TestClient,
) -> None:
    page = client.get(MCP, params={"client": "code"}).text

    words = shlex.split(block(page, "bloco-comando"))
    assert words[:3] == ["claude", "mcp", "add"]
    assert "GERADOR_API_URL=" + ADDRESS in words
    assert "GERADOR_API_KEY=cole-a-chave-aqui" in words
    name = words.index("leadkit")
    # a CLI lê o nome como mais uma variável se ele vier logo depois do último --env
    assert words[name - 1] != "--env" and not words[name - 1].startswith("GERADOR_")
    assert words[name - 2 : name] == ["--transport", "stdio"]
    assert words[name + 1 :][0] == "--"
    assert words[-2:] == ["-m", "app.admin_mcp"]
    assert "\n" not in block(page, "bloco-comando").strip()
    # a versão que pede a chave no Terminal não leva o valor da chave no comando
    safe = block(page, "bloco-comando-seguro")
    assert "read -rs GERADOR_API_KEY" in safe and "cole-a-chave-aqui" not in safe


def test_the_options_enter_the_blocks_and_the_lead_data_only_with_a_total_key(
    client: TestClient, deps: Deps
) -> None:
    reading, total = make(deps, "so-leitura"), make(deps, "chave-total", "total")
    options = {
        "lead_data": "1",
        "upload_dir": "/Users/ana/imagens do gerador",
        "download_dir": "~/Downloads/gerador",
    }

    with_total = client.get(MCP, params={"key": total.info.key_id, **options}).text
    env = desktop_env(with_total)
    assert env["GERADOR_MCP_LEAD_DATA"] == "1"
    assert env["GERADOR_MCP_UPLOAD_DIR"] == options["upload_dir"]
    assert env["GERADOR_MCP_DOWNLOAD_DIR"] == options["download_dir"]
    code = client.get(MCP, params={"client": "code", "key": total.info.key_id, **options}).text
    command = shlex.split(block(code, "bloco-comando"))
    assert "GERADOR_MCP_UPLOAD_DIR=/Users/ana/imagens do gerador" in command

    with_reading = client.get(MCP, params={"key": reading.info.key_id, **options}).text
    assert "GERADOR_MCP_LEAD_DATA" not in desktop_env(with_reading)
    lead_data = re.search(r'<input[^>]*name="lead_data"[^>]*>', with_reading)
    assert lead_data is not None and "disabled" in lead_data.group(0)
    assert "checked" not in lead_data.group(0)


def test_a_folder_that_the_server_would_refuse_stays_out_of_the_blocks(
    client: TestClient, deps: Deps
) -> None:
    for folder in ("/", "~", "/Users/ana", "relativa/imagens"):
        page = client.get(MCP, params={"upload_dir": folder}).text

        assert "GERADOR_MCP_UPLOAD_DIR" not in desktop_env(page)
        assert 'id="problema-upload_dir"' in page, folder


def test_only_the_keys_that_work_today_are_offered_with_name_and_role_and_never_a_secret(
    client: TestClient, deps: Deps, clock: FakeClock
) -> None:
    now = moment_of(clock)
    active = make(deps, "chave-ativa", "total", now=now)
    revoked = make(deps, "chave-revogada", now=now)
    make(deps, "chave-vencida", expires_at=now + timedelta(hours=1), now=now)
    with deps.sessions.begin() as session:
        keys.revoke(session, revoked.info.key_id, actor="operadora", now=now)
    clock.advance(2 * 3600)  # a validade passou, e a sessão (8 h) ainda vale

    page = client.get(MCP, params={"key": active.info.key_id}).text

    options = re.findall(r"<option[^>]*>([^<]*)</option>", page)
    assert [o for o in options if "·" in o] == ["chave-ativa · Total"]
    assert f'value="{active.info.key_id}" data-name="chave-ativa" selected' in page
    for created in (active, revoked):
        assert created.key not in page and created.key.split("_", 2)[2] not in page


def test_creating_a_key_there_shows_the_secret_once_and_already_puts_it_in_the_blocks(
    client: TestClient, deps: Deps, clock: FakeClock
) -> None:
    created = post_key(client, role="total")

    assert created.status_code == 200
    assert created.headers["Cache-Control"] == "no-store"
    shown = re.search(r'id="chave-nova"[^>]*>([^<]+)<', created.text)
    assert shown is not None
    secret = shown.group(1)
    assert KEY_SHAPE.fullmatch(secret)
    assert check(deps, secret, now=moment_of(clock)).role == "total"
    assert desktop_env(created.text)["GERADOR_API_KEY"] == secret  # o segredo já está no JSON
    assert re.search(
        r'<option value="[0-9a-f]{8}" data-name="claude-desktop" selected>', created.text
    )
    # a regra e a auditoria são as da página das chaves
    (audit,) = audited(deps, "api_key.create")
    assert audit.actor == "operadora"
    # recarregar a tela, ou abrir qualquer outra, não mostra o segredo de novo
    for url in (MCP, "/admin/api-keys", "/admin"):
        later = client.get(url)
        assert secret not in later.text and secret.split("_", 2)[2] not in later.text, url
    assert "claude-desktop · Total" in client.get(MCP).text


def test_what_was_chosen_in_the_screen_survives_the_creation(client: TestClient) -> None:
    created = post_key(
        client, role="total", client="code", lead_data="1", upload_dir="/Users/ana/imagens"
    )

    page = created.text
    command = shlex.split(block(page, "bloco-comando"))
    assert "GERADOR_MCP_LEAD_DATA=1" in command
    assert "GERADOR_MCP_UPLOAD_DIR=/Users/ana/imagens" in command


def test_a_refused_creation_returns_the_screen_with_what_was_typed_and_the_problem(
    client: TestClient, deps: Deps
) -> None:
    refused = post_key(client, name="no")

    assert refused.status_code == 422
    assert 'id="k-name-erro"' in refused.text and 'value="no"' in refused.text
    assert 'id="chave-nova"' not in refused.text
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(ApiKey)) == 0
    # sob o HTMX a recusa volta com 200, para ele trocar o trecho e a pessoa ver o motivo
    again = client.post(
        f"{MCP}/keys",
        data={"name": "no", "role": "leitura", "validity_days": "90"},
        headers={**ORIGIN, "HX-Request": "true", "X-CSRF-Token": csrf_token(client)},
    )
    assert again.status_code == 200 and 'id="k-name-erro"' in again.text


def test_the_tool_counts_come_from_the_catalog_and_a_total_key_has_more_than_a_reading_one(
    client: TestClient,
) -> None:
    page = client.get(MCP).text

    counts = {
        name: int(value)
        for name, value in re.findall(r'data-tools-(leitura|total|total-lead)="(\d+)"', page)
    }
    assert set(counts) == {"leitura", "total", "total-lead"}
    assert 0 < counts["leitura"] < counts["total"] < counts["total-lead"]


def test_the_reference_has_the_mcp_section_and_the_screen_links_to_it(client: TestClient) -> None:
    description = build()["info"]["description"]

    heading = re.search(r"^## (MCP.*)$", description, re.MULTILINE)
    assert heading is not None and f"({MCP})" in description  # a seção aponta para a tela
    # e a tela aponta para a seção: o fragmento é o título como o Scalar o transforma em âncora
    anchor = re.sub(r"[^a-z0-9]+", "-", heading.group(1).lower()).strip("-")
    assert mcp_pages.REFERENCE_MCP_URL.endswith(f"#description/{anchor}")
    assert f'href="{mcp_pages.REFERENCE_MCP_URL}"' in client.get(MCP).text


def test_the_counts_do_not_need_the_mcp_sdk_in_the_server() -> None:
    """A imagem do servidor não instala o extra `mcp`: contar as ferramentas só pode usar o catálogo e as
    compostas, que não importam o SDK. Um processo novo confere."""
    program = (
        "import sys\n"
        "from app.admin import mcp_pages\n"
        "from app.admin.api.openapi import build\n"
        "counts = mcp_pages.tool_counts(build())\n"
        "assert counts.leitura > 0 and counts.total > counts.leitura\n"
        "sys.exit(1 if 'mcp' in sys.modules else 0)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr


def test_the_python_path_of_the_example_is_flagged_until_it_is_replaced(client: TestClient) -> None:
    example = client.get(MCP).text
    own = client.get(MCP, params={"python": "/Users/ana/.venvs/gerador-mcp/bin/python"}).text

    assert "Falta trocar «voce»" in example and "Falta trocar «voce»" not in own
    assert mcp_pages.DEFAULT_PYTHON in block(example, "bloco-arquivo")
    # o Claude Desktop não expande o «~»
    assert 'id="problema-python"' in client.get(MCP, params={"python": "~/py"}).text


def test_the_install_step_says_to_keep_the_clone_and_any_python_path_is_fine(
    client: TestClient,
) -> None:
    page = client.get(MCP).text

    assert "fora do repositório" not in page
    assert "deixe essa pasta onde está e no branch" in page
    assert "qualquer caminho serve" in page


def test_an_address_the_mcp_would_refuse_is_said_on_the_screen(
    deps: Deps, clock: FakeClock
) -> None:
    insecure = admin(deps, clock, "http://planos.exemplo.test")  # http fora do localhost

    page = insecure.get(MCP).text

    assert "O endereço deste admin não serve ao MCP" in page and "precisa ser https" in page
