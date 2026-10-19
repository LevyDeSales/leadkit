"""A configuração do MCP: tudo vem do ambiente do cliente MCP (o bloco `env` da configuração dele).

A chave nunca vem pela linha de comando (ela apareceria na lista de processos) nem de um arquivo do
repositório. O `Config` não a mostra: `repr` e `str` a trocam por `[chave]`, para uma exceção, um log
ou um `print` esquecido não a vazarem. Nada aqui importa o app; o prefixo da API é o do contrato
(`servers` do `openapi.json`), e um teste confere que é o mesmo.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from app.admin_mcp.redaction import MASK

# O prefixo da API de admin. É o `servers[0].url` do `openapi.json`; o servidor precisa dele antes de
# ler o documento, e o teste do catálogo confere que os dois dizem o mesmo.
API_PREFIX = "/admin/api/v1"

ENV_URL = "GERADOR_API_URL"
ENV_KEY = "GERADOR_API_KEY"
ENV_LEAD_DATA = "GERADOR_MCP_LEAD_DATA"
ENV_DOWNLOAD_DIR = "GERADOR_MCP_DOWNLOAD_DIR"
ENV_UPLOAD_DIR = "GERADOR_MCP_UPLOAD_DIR"


class ConfigError(Exception):
    """A configuração não vale. A mensagem diz qual variável e nunca repete o valor dela."""


@dataclass(frozen=True)
class Config:
    """O que o servidor precisa saber para falar com a API.

    `base_url` já leva o prefixo (`https://host/admin/api/v1`). `lead_data` é a opção
    `GERADOR_MCP_LEAD_DATA=1`: sem ela, nome, e-mail e respostas das leads não chegam ao modelo.
    `download_dir` é onde o PDF e o HTML vão (sem ele, uma pasta só do usuário no diretório
    temporário). `upload_dir` é a única pasta de onde o servidor lê uma imagem para enviar à API
    (`GERADOR_MCP_UPLOAD_DIR`): sem ela, as ferramentas de arquivo recusam. A imagem enviada é servida
    sem login, e o caminho vem do modelo, então não há um padrão.
    """

    base_url: str
    api_key: str = field(repr=False)
    lead_data: bool = False
    download_dir: Path | None = None
    upload_dir: Path | None = None

    def __repr__(self) -> str:
        return (
            f"Config(base_url={self.base_url!r}, api_key={MASK!r}, lead_data={self.lead_data!r}, "
            f"download_dir={self.download_dir!r}, upload_dir={self.upload_dir!r})"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        """Lê e confere as variáveis. Qualquer problema é um `ConfigError` com a variável, sem o valor."""
        raw_url = env.get(ENV_URL, "").strip()
        if not raw_url:
            raise ConfigError(
                f"Falta {ENV_URL}: o endereço da API, como https://planos.example.test "
                "(no bloco `env` da configuração do MCP)."
            )
        key = env.get(ENV_KEY, "").strip()
        if not key:
            raise ConfigError(
                f"Falta {ENV_KEY}: a chave de API do admin (no bloco `env` da configuração do MCP, "
                "nunca na linha de comando)."
            )
        if not (key.isascii() and key.isprintable()) or any(ch.isspace() for ch in key):
            raise ConfigError(
                f"{ENV_KEY} tem caracteres que uma chave não tem; confira como foi colada."
            )
        download_dir = env.get(ENV_DOWNLOAD_DIR, "").strip()
        upload_dir = env.get(ENV_UPLOAD_DIR, "").strip()
        return cls(
            base_url=_base_url(raw_url),
            api_key=key,
            lead_data=_flag(env.get(ENV_LEAD_DATA, "")),
            download_dir=Path(download_dir).expanduser() if download_dir else None,
            upload_dir=_upload_dir(upload_dir),
        )


def _upload_dir(raw: str) -> Path | None:
    """A pasta de envio de imagens, ou `None` se a variável não foi definida.

    A imagem enviada é servida sem login, e o caminho vem do modelo: a pasta tem de ser uma pasta feita
    para isso. A raiz do disco e a pasta pessoal inteira não são (todo arquivo de imagem do usuário
    estaria ao alcance de um modelo levado por injeção de prompt); outra pasta qualquer, como
    `~/Imagens`, é decisão de quem configura, e o `README` avisa."""
    if not raw:
        return None
    folder = Path(raw).expanduser()
    try:
        resolved = folder.resolve()
        home = Path.home().resolve()
    except (OSError, RuntimeError, ValueError):
        raise ConfigError(f"{ENV_UPLOAD_DIR} não é um caminho válido.") from None
    if resolved == Path(resolved.anchor) or resolved == home:
        raise ConfigError(
            f"{ENV_UPLOAD_DIR} não pode ser a raiz do disco nem a sua pasta pessoal: use uma pasta só "
            "para as imagens que podem ir ao ar (o que estiver nela pode ser publicado sem login)."
        )
    return folder


def _flag(raw: str) -> bool:
    """`1` liga; vazio ou `0` desliga. Outro valor é erro, para `true` ou `sim` não ligarem (nem
    deixarem de ligar) o dado da lead por engano."""
    value = raw.strip()
    if value in ("", "0"):
        return False
    if value == "1":
        return True
    raise ConfigError(f"{ENV_LEAD_DATA} é 1 (liga) ou 0 (desliga); outro valor não vale.")


def _base_url(raw: str) -> str:
    bad = ConfigError(
        f"{ENV_URL} não é um endereço válido (confira o host e a porta), como "
        "https://planos.example.test."
    )
    try:
        parts = urlsplit(raw)
        # `port` valida o número (só dígitos, até 65535): sem isso, `https://host:abc` passaria e
        # estouraria mais adiante, com a pilha na tela.
        port = parts.port
    except ValueError:
        raise bad from None
    if port == 0:
        raise bad
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(
            f"{ENV_URL} precisa ser uma URL http(s) com o host, como https://planos.example.test."
        )
    if parts.username is not None or parts.password is not None:
        raise ConfigError(f"{ENV_URL} não leva usuário nem senha: a chave vai em {ENV_KEY}.")
    if parts.query or parts.fragment:
        raise ConfigError(f"{ENV_URL} é só o endereço, sem parâmetros nem `#`.")
    if parts.scheme == "http" and not _is_loopback(parts.hostname):
        raise ConfigError(
            f"{ENV_URL} precisa ser https: a chave viajaria sem criptografia. "
            "(http só vale para localhost, em desenvolvimento.)"
        )
    path = parts.path.rstrip("/")
    if not path.endswith(API_PREFIX):
        path += API_PREFIX
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
