"""Quais falhas o Hatchet deve repetir e quais são definitivas.

Os adaptadores traduzem erros de provedor em `RetryableError`/`TerminalError`. A mensagem e o
traceback de uma falha ficam gravados no Hatchet e aparecem no painel: nunca coloque dados do lead
nelas (um `ValidationError` do pydantic, por exemplo, traz o valor recebido).
"""

from __future__ import annotations

import re
import traceback
from enum import StrEnum
from pathlib import Path

from hatchet_sdk import NonRetryableException

_CODE_RE = re.compile(r"\[(?P<code>[a-z_]+)\]")
_MAX_CHAIN = 5  # exceções da cadeia no resumo da causa: o bastante para achar a origem


class FailureCode(StrEnum):
    LLM_UNAVAILABLE = "llm_unavailable"
    LLM_REJECTED = "llm_rejected"
    PLAN_INVALID = "plan_invalid"
    RENDER_FAILED = "render_failed"
    STORAGE_FAILED = "storage_failed"
    DELIVERY_REJECTED = "delivery_rejected"
    DELIVERY_UNAVAILABLE = "delivery_unavailable"
    # Sem destino do webhook para o formulário, ou com um segredo que não abre.
    DELIVERY_UNCONFIGURED = "delivery_unconfigured"
    # Integração com a planilha do formulário (`app.integrations`): a planilha não respondeu, recusou
    # o envio, está desativada ou sem token que abra, ou o mapeamento não serve para o envio.
    INTEGRATION_UNAVAILABLE = "integration_unavailable"
    INTEGRATION_REJECTED = "integration_rejected"
    INTEGRATION_UNCONFIGURED = "integration_unconfigured"
    INTEGRATION_MAPPING = "integration_mapping"
    STUCK = "stuck"
    INTERNAL = "internal"


class RetryableError(Exception):
    def __init__(self, code: FailureCode, message: str) -> None:
        super().__init__(f"[{code.value}] {message}")
        self.code = code
        self.detail = message  # a mensagem sem o código, para uma tela


class TerminalError(Exception):
    def __init__(self, code: FailureCode, message: str) -> None:
        super().__init__(f"[{code.value}] {message}")
        self.code = code
        self.detail = message  # a mensagem sem o código, para uma tela


def as_hatchet_error(exc: Exception) -> Exception:
    """TerminalError vira NonRetryableException (falha já); erros desconhecidos viram
    RetryableError(INTERNAL) com o nome do tipo, sem a mensagem original."""
    if isinstance(exc, NonRetryableException | RetryableError):
        return exc
    if isinstance(exc, TerminalError):
        return NonRetryableException(str(exc))
    return RetryableError(FailureCode.INTERNAL, type(exc).__name__)


def cause_summary(exc: BaseException) -> str:
    """O diagnóstico local de uma falha: o tipo e os quadros (arquivo, linha e função) de cada
    exceção da cadeia, da mais recente à causa original, nunca a mensagem. Serve para achar o ponto
    da falha sem levar para o log o dado da lead que a mensagem pode trazer."""
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(parts) < _MAX_CHAIN:
        seen.add(id(current))
        frames = " > ".join(
            f"{Path(frame.filename).name}:{frame.lineno} {frame.name}"
            for frame in traceback.extract_tb(current.__traceback__)
        )
        parts.append(f"{type(current).__name__} [{frames}]")
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


def failure_code_from_errors(task_run_errors: dict[str, str]) -> tuple[FailureCode, str | None]:
    for task_name, serialized in task_run_errors.items():
        match = _CODE_RE.search(serialized)
        if match is None:
            continue
        try:
            return FailureCode(match.group("code")), task_name
        except ValueError:
            continue
    return FailureCode.INTERNAL, next(iter(task_run_errors), None)
