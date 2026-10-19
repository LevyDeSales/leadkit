"""A tela de configurar o MCP no admin (`/admin/mcp`): do zero ao servidor funcionando, sem o README.

Quatro passos: a chave (uma que já existe, só nome e papel, ou uma nova, com o segredo mostrado uma
vez); instalar o programa no computador; o comando do Claude Code ou o bloco do Claude Desktop, já
com o endereço deste admin; e conferir a chave. Tudo o que a tela mostra sai daqui, em português; o
template (`admin/mcp.html`) só dispõe.

O que o MCP espera vem de `app.admin_mcp.config` (os nomes das variáveis e a validação do endereço),
para a tela e o servidor não discordarem. A chave **nunca** vem daqui para um comando, a não ser a
que acabou de ser criada (a resposta que a mostra uma vez); a de uma chave que já existe é o
marcador `cole-a-chave-aqui`, que o `mcp.js` troca, no navegador, pela chave que a pessoa colar. O
segredo não volta ao servidor em nenhum pedido.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.admin import api_key_pages as key_pages
from app.admin.api import keys
from app.admin.api.errors import PREFIX as API_PREFIX
from app.admin.api.reference import REFERENCE_PATH
from app.admin_mcp import config as mcp_config

logger = logging.getLogger(__name__)

MCP_URL = "/admin/mcp"
CREATE_KEY_URL = "/admin/mcp/keys"
ME_URL = f"{API_PREFIX}/me"  # o que o botão «Conferir» chama, do próprio navegador
SERVER_NAME = "leadkit"
PLACEHOLDER = "cole-a-chave-aqui"
REPO_URL = "https://github.com/LevyDeSales/leadkit.git"
VENV_DIR = "~/.venvs/gerador-mcp"
DEFAULT_PYTHON = "/Users/voce/.venvs/gerador-mcp/bin/python"
PYTHON_PLACEHOLDER_USER = "voce"
DESKTOP_LOG = "~/Library/Logs/Claude/mcp-server-leadkit.log"
MAX_PATH_LENGTH = 300
# A seção «MCP» da referência (Scalar): o fragmento é o do título `## MCP: usar a API pelo Claude` da
# descrição do OpenAPI (`app.admin.api.application.DESCRIPTION`); um teste confere que os dois combinam.
REFERENCE_MCP_URL = f"{REFERENCE_PATH}#description/mcp-usar-a-api-pelo-claude"


@dataclass(frozen=True)
class Client:
    value: str
    label: str
    help: str


CLIENTS = (
    Client("desktop", "Claude Desktop", "o aplicativo de conversa"),
    Client("code", "Claude Code", "o assistente do Terminal"),
)
DEFAULT_CLIENT = CLIENTS[0].value

LEAD_DATA_HELP = (
    "Liga o nome, o e-mail e as respostas das leads, e o PDF de cada pedido, para o Claude. "
    "O que ele ler passa pela Anthropic, que processa a conversa. Deixe desligado, a menos que "
    "você precise disso."
)
LEAD_DATA_NEEDS_TOTAL = "Só existe com uma chave Total: escolha uma chave Total no passo 1."
UPLOAD_HELP = (
    "A pasta de onde o Claude pode enviar imagens para os formulários. Sem ela, ele não envia "
    "nenhuma. A imagem enviada fica pública, sem login, em qualquer página de formulário que a "
    "use: crie uma pasta só para isso, com o que pode ir ao ar, e não use Imagens, Documentos "
    "nem a sua pasta pessoal."
)
DOWNLOAD_HELP = (
    "Onde ficam os PDFs e as páginas que o Claude baixar. Se deixar em branco, vai para uma "
    "pasta só sua, dentro da pasta temporária do computador."
)

# Os dois papéis, com o texto desta tela (a página das chaves fala para quem administra; esta, para
# quem só quer o Claude funcionando).
ROLE_CARDS = (
    key_pages.RoleOption(
        "leitura",
        key_pages.ROLE_LABELS["leitura"],
        "Só consulta: o Claude vê pedidos, formulários e configurações, nunca dados de leads. "
        "A melhor para começar.",
    ),
    key_pages.RoleOption(
        "total",
        key_pages.ROLE_LABELS["total"],
        "Faz tudo o que você faz no admin, inclusive publicar formulários e criar chaves. "
        "O MCP pede a sua confirmação antes das ações mais sérias.",
    ),
)

PROBLEM_LABELS = {
    "python": "Caminho do Python",
    "upload_dir": "Pasta para enviar imagens",
    "download_dir": "Pasta para os arquivos baixados",
}

_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f\"'`]")
_ABSOLUTE = re.compile(r"(/|~/|[A-Za-z]:[\\/])")
_ROOT_OR_HOME = re.compile(
    r"""(?ix)
    (?: ~/?
      | /
      | [a-z]:[\\/]?
      | /(?:users|home)/[^/]+/?
      | [a-z]:[\\/]users[\\/][^\\/]+[\\/]?
    )
    """
)


def folder_problem(text: str, *, personal_folder_ok: bool = False) -> str | None:
    """Por que o caminho não serve ao comando, em português; nada (`None`) se serve. Confere a
    forma: quem roda o servidor é o computador da operadora, que este admin não enxerga, então não
    olha o disco. A pasta de envio recusa a raiz do disco e a pasta pessoal, como o servidor do MCP
    (`GERADOR_MCP_UPLOAD_DIR`)."""
    if not text:
        return None
    if len(text) > MAX_PATH_LENGTH or _BAD_CHARS.search(text):
        return "Tire as aspas e os caracteres de controle do caminho."
    if _ABSOLUTE.match(text) is None and text != "~":
        return "Escreva o caminho completo, começando por / ou ~/ (ou pela letra do disco)."
    if not personal_folder_ok and _ROOT_OR_HOME.fullmatch(text):
        return "Use uma pasta só para isso: nem a raiz do disco, nem a sua pasta pessoal."
    return None


def python_problem(text: str, client: str) -> str | None:
    """O caminho do Python do MCP: o formato vale, e o Claude Desktop não expande o `~`."""
    problem = folder_problem(text, personal_folder_ok=True)
    if problem is not None:
        return problem
    if client == "desktop" and text.startswith("~"):
        return "O Claude Desktop não entende o «~»: escreva o caminho completo, como /Users/seu-usuario/..."
    return None


def python_is_placeholder(text: str) -> bool:
    """Ainda é o exemplo (`/Users/voce/...`): a pessoa não trocou pelo usuário dela."""
    return f"/{PYTHON_PLACEHOLDER_USER}/" in text.replace("\\", "/")


@dataclass(frozen=True)
class Setup:
    """O que a operadora escolheu na tela, já conferido: a chave (`key_id`), o cliente, o dado da lead
    (só com chave `total`), as pastas e o caminho do Python. Vem da query string (`GET`) ou do
    formulário (`POST`); nunca traz o segredo."""

    key_id: str = ""
    client: str = DEFAULT_CLIENT
    lead_data: bool = False
    upload_dir: str = ""
    download_dir: str = ""
    python: str = DEFAULT_PYTHON


@dataclass(frozen=True)
class KeyOption:
    """Uma chave que dá para escolher: só o nome e o papel. O segredo nem existe aqui."""

    key_id: str
    name: str
    role: str
    role_label: str
    selected: bool = False

    @property
    def label(self) -> str:
        return f"{self.name} · {self.role_label}"


def key_options(infos: Sequence[keys.KeyInfo], now: datetime, selected: str) -> list[KeyOption]:
    """As chaves que valem hoje (as vencidas e as revogadas não servem a ninguém), da mais nova
    para a mais antiga."""
    return [
        KeyOption(
            key_id=info.key_id,
            name=info.name,
            role=info.role,
            role_label=key_pages.ROLE_LABELS[info.role],
            selected=info.key_id == selected,
        )
        for info in infos
        if info.state(now) == "active"
    ]


def setup_of(params: Mapping[str, str], infos: Sequence[keys.KeyInfo], now: datetime) -> Setup:
    """A escolha a partir dos campos da tela. Tolerante: o que não vale volta ao padrão (uma chave que
    não existe mais, um cliente desconhecido), e o dado da lead só fica ligado com uma chave `total`."""
    active = {info.key_id: info for info in infos if info.state(now) == "active"}
    key_id = params.get("key", "").strip()
    if key_id not in active:
        key_id = ""
    client = params.get("client", "")
    if client not in {option.value for option in CLIENTS}:
        client = DEFAULT_CLIENT
    lead = (
        params.get("lead_data", "") in ("1", "on")
        and key_id != ""
        and active[key_id].role == "total"
    )
    return Setup(
        key_id=key_id,
        client=client,
        lead_data=lead,
        upload_dir=params.get("upload_dir", "").strip(),
        download_dir=params.get("download_dir", "").strip(),
        python=params.get("python", "").strip() or DEFAULT_PYTHON,
    )


# --- os blocos para copiar --------------------------------------------------------------------------

Part = tuple[str, bool]  # (texto, é o lugar da chave?)


@dataclass(frozen=True)
class Block:
    """Um texto para copiar, em partes: o lugar da chave é uma parte à parte, para o `mcp.js` trocar o
    marcador pela chave sem mexer no resto. `text` é o que o botão «Copiar» leva."""

    id: str
    title: str
    parts: tuple[Part, ...]

    @property
    def text(self) -> str:
        return "".join(text for text, _ in self.parts)


class _Writer:
    def __init__(self, key: str) -> None:
        self._key = key
        self.parts: list[Part] = []

    def text(self, value: str) -> None:
        self.parts.append((value, False))

    def key(self) -> None:
        self.parts.append((self._key, True))


def environment(
    api_url: str, setup: Setup, *, problems: Mapping[str, str]
) -> list[tuple[str, str | None]]:
    """As variáveis do bloco `env`, na ordem em que a tela as mostra. A chave leva `None`: o lugar
    dela é preenchido à parte. Uma pasta com problema fica de fora (o comando não carrega um valor que
    o servidor recusaria)."""
    pairs: list[tuple[str, str | None]] = [
        (mcp_config.ENV_URL, api_url),
        (mcp_config.ENV_KEY, None),
    ]
    if setup.lead_data:
        pairs.append((mcp_config.ENV_LEAD_DATA, "1"))
    if setup.upload_dir and "upload_dir" not in problems:
        pairs.append((mcp_config.ENV_UPLOAD_DIR, setup.upload_dir))
    if setup.download_dir and "download_dir" not in problems:
        pairs.append((mcp_config.ENV_DOWNLOAD_DIR, setup.download_dir))
    return pairs


def _shell_path(path: str) -> str:
    """O caminho para o shell: o `~/` do começo fica fora das aspas, para o shell expandi-lo."""
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


def claude_code_command(
    pairs: Sequence[tuple[str, str | None]], python: str, key: str, *, ask_for_key: bool
) -> list[Part]:
    """O `claude mcp add` numa linha só: cola igual em qualquer terminal (uma linha com `\\` no fim
    quebraria no PowerShell). Todas as opções (`--env`, `--scope`, `--transport`) vêm antes do nome do
    servidor, e uma opção entre o último `--env` e o nome impede a CLI de ler o nome como mais uma
    variável. Com `ask_for_key`, a chave vem de uma pergunta do terminal (`read -s`), e não do
    comando: não vai para o histórico do shell."""
    out = _Writer(key)
    if ask_for_key:
        out.text(
            "printf 'Cole a chave e tecle Enter (ela não aparece): '; read -rs GERADOR_API_KEY; echo; "
        )
    out.text("claude mcp add")
    for name, value in pairs:
        if name == mcp_config.ENV_KEY:
            if ask_for_key:
                out.text(' --env "GERADOR_API_KEY=$GERADOR_API_KEY"')
            else:
                out.text(f" --env {name}=")
                out.key()
        else:
            out.text(" --env " + shlex.quote(f"{name}={value}"))
    out.text(
        f" --scope user --transport stdio {SERVER_NAME} -- {_shell_path(python)} -m app.admin_mcp"
    )
    return out.parts


def desktop_entry(
    pairs: Sequence[tuple[str, str | None]], python: str, key: str, *, depth: int
) -> list[Part]:
    """O servidor no formato do `claude_desktop_config.json`, com o recuo de `depth` níveis."""
    pad = "  " * depth
    out = _Writer(key)

    def quoted(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    out.text(f"{pad}{quoted(SERVER_NAME)}: {{\n")
    out.text(f'{pad}  "command": {quoted(python)},\n')
    out.text(f'{pad}  "args": ["-m", "app.admin_mcp"],\n')
    out.text(f'{pad}  "env": {{\n')
    for index, (name, value) in enumerate(pairs):
        comma = "," if index < len(pairs) - 1 else ""
        if name == mcp_config.ENV_KEY:
            out.text(f'{pad}    {quoted(name)}: "')
            out.key()
            out.text(f'"{comma}\n')
        else:
            out.text(f"{pad}    {quoted(name)}: {quoted(value or '')}{comma}\n")
    out.text(f"{pad}  }}\n")
    out.text(f"{pad}}}")
    return out.parts


def desktop_file(pairs: Sequence[tuple[str, str | None]], python: str, key: str) -> list[Part]:
    """O arquivo inteiro: serve a quem ainda não tem nenhum servidor configurado."""
    return [
        ('{\n  "mcpServers": {\n', False),
        *desktop_entry(pairs, python, key, depth=2),
        ("\n  }\n}", False),
    ]


# --- a ajuda que não depende da escolha ------------------------------------------------------------


def install_commands() -> list[Block]:
    """Instalar uma vez e achar o caminho do Python. O venv fica fora do clone, como o README manda."""
    install = "\n".join(
        [
            f"git clone {REPO_URL}",
            "cd leadkit",
            f"UV_PROJECT_ENVIRONMENT={VENV_DIR} uv sync --locked --extra mcp --python 3.11",
        ]
    )
    return [
        Block("bloco-instalar", "Instalar o programa (uma vez)", ((install, False),)),
        Block(
            "bloco-caminho", "Ver o caminho do Python", ((f"echo {VENV_DIR}/bin/python", False),)
        ),
    ]


def terminal_test(api_url: str, python: str) -> Block:
    """Rodar o servidor à mão, sem o Claude. A chave é pedida pelo terminal (não vai ao histórico),
    e o servidor diz em stderr quantas ferramentas a chave dá. Ctrl-D encerra."""
    quoted_url = shlex.quote(api_url)
    command = (
        "printf 'Cole a chave e tecle Enter (ela não aparece): '; read -rs GERADOR_API_KEY; echo; "
        f"export GERADOR_API_KEY; GERADOR_API_URL={quoted_url} {_shell_path(python)} -m app.admin_mcp; "
        "unset GERADOR_API_KEY"
    )
    return Block("bloco-teste", "Rodar o programa à mão (opcional)", ((command, False),))


# --- as ferramentas por papel -----------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCounts:
    """Quantas ferramentas o MCP oferece a cada tipo de chave, as compostas incluídas."""

    leitura: int
    total: int
    total_with_lead_data: int


def tool_counts(document: Mapping[str, Any]) -> ToolCounts:
    """Conta as ferramentas pelo mesmo caminho do servidor do MCP (o catálogo gerado do `openapi.json`,
    o filtro por papel e as compostas), e não por um número escrito aqui, que envelheceria a cada
    operação nova. Importa o MCP na hora de usar: só o catálogo e as compostas, que não precisam do SDK
    do MCP (um teste cobra isso)."""
    from app.admin_mcp import catalog, composites

    generated = catalog.load(document).tools

    def count(role: str, lead_data: bool) -> int:
        chosen = {
            tool.name: tool
            for tool in catalog.select(generated.values(), role=role, lead_data=lead_data)
        }
        return len(chosen) + len(composites.for_tools(chosen))

    return ToolCounts(
        leitura=count("leitura", False),
        total=count("total", False),
        total_with_lead_data=count("total", True),
    )


# --- a página -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class McpPage:
    """Tudo o que a tela mostra. `blocks` são os do cliente escolhido; `problems`, o problema de cada
    campo (`upload_dir`, `download_dir`, `python`); `config_problem`, o que há de errado com o endereço
    deste admin para o MCP (um `PUBLIC_BASE_URL` em http, por exemplo)."""

    setup: Setup
    clients: tuple[Client, ...]
    options: list[KeyOption]
    selected: KeyOption | None
    api_url: str
    config_problem: str | None
    problems: Mapping[str, str]
    lead_data_available: bool
    python_is_placeholder: bool
    blocks: list[Block]
    safe_block: Block | None  # o comando do Claude Code que pede a chave sem deixá-la no histórico
    entry_block: Block | None  # o trecho do servidor, para quem já tem outros no arquivo do Desktop
    install: list[Block]
    test_block: Block
    key_slot_filled: bool  # a chave nova já está nos blocos
    counts: ToolCounts | None
    new_key: key_pages.NewKey | None = None
    # criar uma chave ali mesmo
    name: str = ""
    role: str = "leitura"
    validity_days: str = str(key_pages.DEFAULT_VALIDITY_DAYS)
    errors: Mapping[str, str] = field(default_factory=dict)
    roles: tuple[key_pages.RoleOption, ...] = ROLE_CARDS
    name_hint: str = keys.NAME_HINT
    name_max_length: int = 60
    default_validity_days: int = key_pages.DEFAULT_VALIDITY_DAYS
    max_validity_days: int = key_pages.MAX_VALIDITY_DAYS
    problem_labels: Mapping[str, str] = field(default_factory=lambda: dict(PROBLEM_LABELS))
    lead_data_help: str = LEAD_DATA_HELP
    lead_data_needs_total: str = LEAD_DATA_NEEDS_TOTAL
    upload_help: str = UPLOAD_HELP
    download_help: str = DOWNLOAD_HELP
    action: str = MCP_URL
    create_action: str = CREATE_KEY_URL
    me_url: str = ME_URL
    reference_url: str = REFERENCE_MCP_URL
    keys_url: str = key_pages.KEYS_URL
    placeholder: str = PLACEHOLDER
    desktop_log: str = DESKTOP_LOG


def _config_problem(api_url: str) -> str | None:
    """Se o endereço deste admin serve ao MCP: o servidor dele recusa um `http` que não é local."""
    try:
        mcp_config.Config.from_env({mcp_config.ENV_URL: api_url, mcp_config.ENV_KEY: PLACEHOLDER})
    except mcp_config.ConfigError as error:
        return str(error)
    return None


def page(
    infos: Sequence[keys.KeyInfo],
    now: datetime,
    *,
    public_base_url: str,
    params: Mapping[str, str],
    counts: ToolCounts | None,
    new_key: key_pages.NewKey | None = None,
    name: str = "",
    role: str = "leitura",
    validity_days: str = str(key_pages.DEFAULT_VALIDITY_DAYS),
    errors: Mapping[str, str] | None = None,
) -> McpPage:
    """A tela para esta escolha. `new_key` é a chave recém-criada (só a resposta que a cria a tem):
    com ela, o lugar da chave nos blocos já vem com o segredo."""
    setup = setup_of(params, infos, now)
    options = key_options(infos, now, setup.key_id)
    selected = next((option for option in options if option.selected), None)
    api_url = public_base_url.rstrip("/")

    problems: dict[str, str] = {}
    if (problem := folder_problem(setup.upload_dir)) is not None:
        problems["upload_dir"] = problem
    if (problem := folder_problem(setup.download_dir, personal_folder_ok=True)) is not None:
        problems["download_dir"] = problem
    if (problem := python_problem(setup.python, setup.client)) is not None:
        problems["python"] = problem

    # A chave nova só entra nos blocos se é a escolhida: trocar para outra chave volta ao marcador.
    filled = new_key is not None and new_key.key_id == setup.key_id
    key_text = new_key.key if new_key is not None and filled else PLACEHOLDER
    pairs = environment(api_url, setup, problems=problems)
    python = setup.python if "python" not in problems else DEFAULT_PYTHON

    blocks: list[Block]
    safe_block: Block | None = None
    entry_block: Block | None = None
    if setup.client == "code":
        blocks = [
            Block(
                "bloco-comando",
                "Comando para colar no Terminal",
                tuple(claude_code_command(pairs, python, key_text, ask_for_key=False)),
            )
        ]
        safe_block = Block(
            "bloco-comando-seguro",
            "O mesmo comando, pedindo a chave no Terminal",
            tuple(claude_code_command(pairs, python, key_text, ask_for_key=True)),
        )
    else:
        blocks = [
            Block(
                "bloco-arquivo",
                "O arquivo claude_desktop_config.json inteiro",
                tuple(desktop_file(pairs, python, key_text)),
            )
        ]
        entry_block = Block(
            "bloco-trecho",
            "Só o trecho do servidor",
            tuple(desktop_entry(pairs, python, key_text, depth=0)),
        )

    return McpPage(
        setup=setup,
        clients=CLIENTS,
        options=options,
        selected=selected,
        api_url=api_url,
        config_problem=_config_problem(api_url),
        problems=problems,
        lead_data_available=selected is not None and selected.role == "total",
        python_is_placeholder=python_is_placeholder(setup.python),
        blocks=blocks,
        safe_block=safe_block,
        entry_block=entry_block,
        install=install_commands(),
        test_block=terminal_test(
            api_url, setup.python if "python" not in problems else DEFAULT_PYTHON
        ),
        key_slot_filled=filled,
        counts=counts,
        new_key=new_key,
        name=name,
        role=role,
        validity_days=validity_days,
        errors=errors or {},
    )
