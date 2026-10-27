"""O que volta de uma ferramenta: o JSON da API, o erro dela, ou o arquivo salvo.

- **JSON:** o texto da resposta, como a API o escreveu (já é o estado novo do recurso).
- **Erro da API** (`{"code", "detail", "errors", "reasons", "current_version"}`): um texto em português
  com o status, o código, a explicação, o problema de cada campo (`errors`), os motivos da recusa de
  uma publicação (`reasons`) e a versão de agora num conflito. A API nunca devolve o valor recebido,
  e este módulo não devolve o corpo de uma resposta que não é o erro dela.
- **Resposta que não é JSON** (o PDF e as páginas HTML das prévias): o conteúdo vai para um arquivo,
  e a ferramenta devolve o caminho, o tipo, o tamanho e o sha256, sem os bytes. O arquivo é só do
  usuário (pasta `0700`, arquivo `0600`) e leva no nome o `operation_id` e um pedaço do hash, nunca o
  código do pedido nem o endereço do formulário.

Tudo passa pelo `Redactor`, que tira a chave de qualquer texto.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.admin_mcp.catalog import ToolSpec
from app.admin_mcp.client import ApiResponse
from app.admin_mcp.confirmation import ConfirmationOutcome
from app.admin_mcp.redaction import Redactor

# O teto do texto que volta ao modelo. Uma resposta maior é cortada, e o corte é dito.
MAX_TEXT = 400_000
MAX_LISTED = 50  # itens de `errors` e de `reasons` que o texto lista
EXTENSIONS = {"application/pdf": ".pdf", "text/html": ".html"}


@dataclass(frozen=True)
class ToolOutput:
    """O resultado de uma chamada: o texto e se é uma falha.

    Os outros campos são para as ferramentas compostas (`compose.py`), que juntam várias
    chamadas e precisam saber o que cada uma deu sem ler o texto: `status` é o status HTTP da
    resposta da API (`None` quando nada foi enviado: argumento que não vale, pergunta recusada, ou a
    confirmação ainda por vir); `data` é o JSON da resposta (o corpo do erro, numa recusa), já sem a
    chave; e `awaiting` diz que a chamada **parou à espera da confirmação da pessoa**: nada foi
    executado. `confirmation` preserva o motivo de um bloqueio antes do efeito, sem analisar texto.
    Ficam fora da igualdade: o que se compara é o texto e se falhou.
    """

    text: str
    is_error: bool = False
    status: int | None = field(default=None, compare=False)
    data: Any = field(default=None, compare=False, repr=False)
    awaiting: bool = field(default=False, compare=False)
    confirmation: ConfirmationOutcome | None = field(default=None, compare=False)


def render(
    spec: ToolSpec,
    response: ApiResponse,
    *,
    redact: Redactor,
    download_dir: Path | None,
    simulated: bool = False,
) -> ToolOutput:
    """O retorno de `spec` para a resposta da API."""
    status = response.status
    if not response.ok:
        failure = redact.tree(_payload(response))
        uncertain = not spec.read_only and not simulated and not known_refusal(status, failure)
        message = render_error(response, uncertain=uncertain)
        if uncertain:
            message += (
                "\nNão sei se a operação chegou a gravar: confira o estado com a ferramenta de leitura "
                "antes de repetir."
            )
        return ToolOutput(redact(message), is_error=True, status=status, data=failure)
    if _is_json(response.media_type):
        return ToolOutput(
            redact(_json_text(response)), status=status, data=redact.tree(_payload(response))
        )
    if not response.content:
        return ToolOutput("Pronto: a API respondeu sem corpo.", status=status)
    return ToolOutput(redact(save_download(spec, response, download_dir)), status=status)


def known_refusal(status: int, data: Any) -> bool:
    """A API recusou sem gravar, conforme o contrato: 4xx ou `502 upstream_failed`.

    Outros erros HTTP não comprovam ausência de efeito, mesmo quando trazem corpo de erro.
    """
    code = data.get("code") if isinstance(data, dict) else None
    return 400 <= status < 500 or (status == 502 and code == "upstream_failed")


def render_error(response: ApiResponse, *, uncertain: bool = False) -> str:
    """O erro da API em português. Uma resposta que não é o erro dela (um proxy, uma página) vira só o
    status: o corpo dela não é repetido."""
    payload = _payload(response)
    if not isinstance(payload, dict) or "code" not in payload:
        return f"A API respondeu {response.status}, sem o corpo de erro esperado."
    action = "respondeu" if uncertain else "recusou"
    lines = [
        f"A API {action} ({response.status} {payload['code']}): {payload.get('detail', '')}".rstrip()
    ]
    errors = payload.get("errors") or []
    for item in errors[:MAX_LISTED]:
        where = item.get("field") or "corpo"
        line = f" (linha {item['line']})" if item.get("line") else ""
        lines.append(f"- {where}{line}: {item.get('message', '')}")
    if len(errors) > MAX_LISTED:
        lines.append(f"- ... e mais {len(errors) - MAX_LISTED} problemas.")
    reasons = payload.get("reasons") or []
    if reasons:
        lines.append("O que falta para publicar:")
        lines.extend(f"- {reason}" for reason in reasons[:MAX_LISTED])
    if payload.get("current_version") is not None:
        lines.append(
            f"A versão de agora é {payload['current_version']}: leia o recurso de novo, confira o "
            "que mudou e repita com essa versão em `expected_version`."
        )
    if response.retry_after:
        lines.append(
            f"Após conferir o estado, aguarde {response.retry_after} s antes de uma nova tentativa."
            if uncertain
            else f"Tente de novo depois de {response.retry_after} s."
        )
    return "\n".join(lines)


def confirmation_detail(response: ApiResponse) -> str | None:
    """O `detail` de um `422 confirmation_required`, ou `None` se a resposta é outra."""
    payload = _payload(response)
    if (
        response.status == 422
        and isinstance(payload, dict)
        and payload.get("code") == "confirmation_required"
    ):
        return str(payload.get("detail") or "A API pede uma confirmação.")
    return None


def _is_json(media_type: str) -> bool:
    return media_type == "application/json" or media_type.endswith("+json")


def _payload(response: ApiResponse) -> Any:
    if not _is_json(response.media_type):
        return None
    try:
        return json.loads(response.content)
    except ValueError:
        return None


def _json_text(response: ApiResponse) -> str:
    text = response.content.decode("utf-8", errors="replace")
    if len(text) <= MAX_TEXT:
        return text
    return (
        text[:MAX_TEXT]
        + f"\n[... resposta cortada: mais {len(text) - MAX_TEXT} caracteres. Peça menos itens "
        "(`limit`) ou use o cursor.]"
    )


# --- o arquivo -----------------------------------------------------------------------------------


def save_download(spec: ToolSpec, response: ApiResponse, download_dir: Path | None) -> str:
    digest = hashlib.sha256(response.content).hexdigest()
    extension = EXTENSIONS.get(response.media_type, ".bin")
    folder = _private_folder(download_dir)
    target = folder / f"{spec.name}-{digest[:12]}{extension}"
    # O nome vem do `operation_id` de um documento de fora (já conferido no catálogo); aqui ainda se
    # confere que o arquivo cai na pasta, e em nenhum outro lugar.
    if target.resolve().parent != folder.resolve():
        raise OSError("O nome do arquivo sairia da pasta dos arquivos; não o salvei.")
    descriptor, temporary = tempfile.mkstemp(dir=folder, prefix=".parte-")  # 0600
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(response.content)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return (
        "A resposta não é JSON: salvei num arquivo (só seu).\n"
        f"caminho: {target}\n"
        f"tipo: {response.media_type or 'desconhecido'}\n"
        f"tamanho: {len(response.content)} bytes\n"
        f"sha256: {digest}"
    )


def _private_folder(chosen: Path | None) -> Path:
    """A pasta dos arquivos: a escolhida em `GERADOR_MCP_DOWNLOAD_DIR`, ou uma só do usuário no
    diretório temporário. A pasta é `0700`; no padrão, também se confere que é do usuário e que não é
    um atalho (symlink) posto por outro, para ninguém ler nem trocar um PDF."""
    if chosen is not None:
        chosen.mkdir(mode=0o700, parents=True, exist_ok=True)
        return chosen
    owner = os.getuid() if hasattr(os, "getuid") else None
    folder = (
        Path(tempfile.gettempdir()) / f"gerador-mcp-{owner if owner is not None else 'usuario'}"
    )
    folder.mkdir(mode=0o700, exist_ok=True)
    status = folder.lstat()
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise OSError("A pasta temporária dos arquivos não é uma pasta comum; não a usei.")
    if owner is not None and status.st_uid != owner:
        raise OSError("A pasta temporária dos arquivos é de outro usuário; não a usei.")
    folder.chmod(0o700)
    return folder
