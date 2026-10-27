"""A chamada de uma ferramenta, com a confirmação antes de qualquer efeito.

`ToolRunner.call` é o miolo do servidor: confere os argumentos, pede a confirmação da pessoa onde a
operação é marcada, monta e envia a requisição (`client`), e devolve o texto (`results`). Não conhece
o SDK do MCP: quem pergunta à pessoa é um `Prompter`, que o `server` monta com a *elicitation* do
cliente e os testes trocam por um de mentira.

**Quem confirma.** A API exige `confirm: true` nas operações marcadas (`x-destructive`,
`x-external-effect`), e o MCP é quem pergunta à pessoa, nesta ordem:

1. O cliente sabe perguntar (*elicitation*): o servidor pergunta, com o efeito da operação, e só com o
   sim manda `confirm: true`. O `confirm` que o modelo mandou por conta própria é **ignorado**: nada
   que o modelo escreva pula a pergunta.
2. O cliente não sabe: a primeira chamada **só devolve a descrição do efeito** (nada foi executado) e
   guarda a chamada; a operação executa na chamada seguinte, com os mesmos argumentos e `confirm:
   true`. Um `confirm: true` numa chamada que o servidor ainda não mostrou à pessoa não vale: ele
   mostra primeiro. (É uma convenção entre o modelo e a pessoa, e não uma barreira: o modelo pode
   fazer as duas chamadas em seguida. Quem barra é a aprovação por chamada do cliente e a chave
   `leitura`; `SECURITY.md`, "O MCP de admin".)

A operação com `x-confirm-unless: dry_run` não pergunta quando o `dry_run` é `true` (só simula).

**As compostas** (`compose.py`) chamam as ferramentas geradas por aqui, `ToolRunner.call`, e por isso valem
as mesmas regras: a pergunta, a impressão digital e o que viaja à API são os de uma chamada direta. O
`ToolOutput` diz o status da API, o JSON e se a chamada parou à espera da pessoa (`awaiting`), e `via`
faz o texto da chamada seguinte mandar repetir a composta, e não a ferramenta gerada.

**Antes de qualquer pergunta**, o argumento que não vale é erro da ferramenta: um argumento de caminho
fora de `[A-Za-z0-9._~-]` (que o servidor da API decodificaria para outra operação) e um arquivo fora da
pasta `GERADOR_MCP_UPLOAD_DIR`. Uma ferramenta com **arquivo** (`upload_asset`, `replace_asset`) pede a
confirmação da pessoa sempre, tenha ou não marca da API, mostrando o caminho resolvido e o tamanho: o
caminho vem do modelo, e a imagem enviada é servida sem login. A confirmação vale para aquele arquivo
(a impressão digital leva o sha256), e os bytes que vão à API são os que a pessoa viu.

**Confirmações além do `confirm`.** Alguns `422 confirmation_required` pedem um segundo campo
booleano do corpo, que a API não nomeia no erro (`confirm_contact` na publicação e na importação,
quando o prompt cita contato da lead; `confirm_http` num destino em `http`). O catálogo os conhece
pelo esquema (`ToolSpec.companions`); o servidor mostra o `detail` da API à pessoa pelo mesmo caminho
(pergunta, ou devolve o texto e espera a chamada seguinte com o campo `true`) e **nunca manda um
desses campos que o modelo escreveu antes de a pessoa ter visto o `detail`**.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any, Protocol

from app.admin_mcp.catalog import CONFIRM, ToolSpec
from app.admin_mcp.client import (
    ApiClient,
    ApiUnavailable,
    InputError,
    Upload,
    check_path_arguments,
    prepare,
    read_uploads,
)
from app.admin_mcp.config import Config
from app.admin_mcp.confirmation import ConfirmationOutcome, blocked_message
from app.admin_mcp.results import ToolOutput, confirmation_detail, render

logger = logging.getLogger("gerador_mcp")

PENDING_TTL = 900.0  # segundos que uma chamada mostrada à pessoa espera a seguinte
PENDING_LIMIT = 256
SHOWN_VALUE = 60  # caracteres de um valor de caminho ou de query na pergunta
SHOWN_DETAILS = 600  # caracteres da descrição da operação no texto da primeira chamada


class Prompter(Protocol):
    """Quem pergunta pelo cliente; só `APPROVED` autoriza e só `UNSUPPORTED` permite duas etapas."""

    async def ask(self, message: str) -> ConfirmationOutcome: ...


class NeverAsks:
    """O `Prompter` de quem não pode perguntar: sempre o caminho da chamada seguinte."""

    async def ask(self, message: str) -> ConfirmationOutcome:
        return ConfirmationOutcome.UNSUPPORTED


class PendingConfirmations:
    """As chamadas que o servidor já mostrou à pessoa, esperando a seguinte com a confirmação.

    A chave é a impressão digital da chamada (a ferramenta e os argumentos, sem as confirmações), e o
    valor, quais confirmações (`confirm`, `confirm_contact`) a pessoa já viu. Expira (a pessoa não
    voltou) e tem teto (um modelo em laço não enche a memória).
    """

    def __init__(
        self,
        *,
        ttl: float = PENDING_TTL,
        limit: int = PENDING_LIMIT,
        clock: Any = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._limit = limit
        self._clock = clock
        self._items: OrderedDict[str, tuple[float, set[str]]] = OrderedDict()

    def remember(self, fingerprint: str, flags: Iterable[str]) -> None:
        self._purge()
        _, known = self._items.pop(fingerprint, (0.0, set()))
        self._items[fingerprint] = (self._clock(), known | set(flags))
        while len(self._items) > self._limit:
            self._items.popitem(last=False)

    def allows(self, fingerprint: str, flag: str) -> bool:
        self._purge()
        item = self._items.get(fingerprint)
        return item is not None and flag in item[1]

    def forget(self, fingerprint: str) -> None:
        self._items.pop(fingerprint, None)

    def decide(
        self,
        fingerprint: str,
        outcome: ConfirmationOutcome,
        flags: Iterable[str],
        *,
        explicit: bool,
    ) -> ConfirmationOutcome:
        """Decide uma tentativa; falhas apagam todas as flags, não só a pergunta atual."""
        flags = tuple(flags)
        if outcome is not ConfirmationOutcome.UNSUPPORTED:
            self.forget(fingerprint)
            return outcome
        if explicit and all(self.allows(fingerprint, flag) for flag in flags):
            return ConfirmationOutcome.APPROVED
        self.remember(fingerprint, flags)
        return ConfirmationOutcome.UNSUPPORTED

    def _purge(self) -> None:
        now = self._clock()
        for key in [k for k, (at, _) in self._items.items() if now - at > self._ttl]:
            del self._items[key]


def fingerprint(
    name: str,
    arguments: Mapping[str, Any],
    uploads: Mapping[str, Upload] | None = None,
    bound: Any = None,
) -> str:
    """A impressão digital da chamada: a ferramenta, os argumentos e, para um arquivo, o caminho já
    resolvido e o sha256 do conteúdo (a confirmação vale para **aquele** arquivo, e não para o nome).

    `bound` é o que uma ferramenta composta mostrou à pessoa junto com a chamada e que a chamada não
    leva (o mapeamento que ela guarda logo depois da conexão): entra na impressão digital, então a
    confirmação não vale para a mesma chamada com outro `bound`."""
    files = {n: [str(u.path), u.size, u.sha256] for n, u in (uploads or {}).items()}
    parts = [name, arguments, files] if bound is None else [name, arguments, files, bound]
    text = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=True)
    return hashlib.sha256(text.encode()).hexdigest()


class ToolRunner:
    def __init__(
        self,
        client: ApiClient,
        config: Config,
        *,
        pending: PendingConfirmations | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.pending = pending or PendingConfirmations()

    async def call(
        self,
        spec: ToolSpec,
        arguments: Mapping[str, Any] | None,
        prompter: Prompter,
        *,
        via: str | None = None,
        bound: Any = None,
    ) -> ToolOutput:
        """Executa `spec` com os `arguments`. Nunca levanta: a falha vira um `ToolOutput` de erro, sem a
        chave (nem o texto de exceções de biblioteca, que o SDK devolveria cru).

        `via` é a ferramenta composta que fez a chamada (`compose.py`): quando a confirmação fica para a
        chamada seguinte, o texto manda repetir **ela**, e não a ferramenta gerada. Nada mais muda: a
        pergunta, a impressão digital e o que viaja à API são os de uma chamada direta, menos `bound`:
        o que a composta mostrou à pessoa junto com a chamada entra na impressão digital."""
        try:
            output = await self._call(spec, dict(arguments or {}), prompter, via, bound)
        except (InputError, ApiUnavailable) as problem:
            output = ToolOutput(str(problem), is_error=True)
        except OSError:
            output = ToolOutput(
                "Não consegui ler ou gravar o arquivo (permissão, espaço ou caminho).",
                is_error=True,
            )
        except Exception as problem:  # o SDK devolveria str(problem) ao modelo: aqui só o tipo
            logger.exception("Falha inesperada em %s (%s)", spec.name, type(problem).__name__)
            output = ToolOutput(
                f"Erro inesperado no servidor MCP ({type(problem).__name__}); veja o log dele.",
                is_error=True,
            )
        redact = self.client.redact
        logger.info("%s: %s", spec.name, "falhou" if output.is_error else "ok")
        return replace(output, text=redact(output.text))

    async def _call(
        self,
        spec: ToolSpec,
        arguments: dict[str, Any],
        prompter: Prompter,
        via: str | None,
        bound: Any,
    ) -> ToolOutput:
        _check_names(spec, arguments)
        # Antes de perguntar a quem quer que seja: um argumento que não vale (caminho com `/`, arquivo
        # fora da pasta de envio) é erro da ferramenta, e a pessoa não é incomodada por ele.
        check_path_arguments(spec, arguments)
        uploads = read_uploads(spec, arguments, self.config.upload_dir)
        gates = {CONFIRM, *spec.companions} & set(spec.locations)
        given = {name: arguments.pop(name) for name in list(arguments) if name in gates}
        key = fingerprint(spec.name, arguments, uploads, bound)
        send: dict[str, bool] = {}

        if spec.needs_confirmation(arguments):
            answer = await prompter.ask(question(spec, arguments, uploads))
            decision = self.pending.decide(
                key, answer, [CONFIRM], explicit=given.get(CONFIRM) is True
            )
            if decision is ConfirmationOutcome.UNSUPPORTED:
                return ToolOutput(first_step(spec, arguments, uploads, via), awaiting=True)
            if decision is not ConfirmationOutcome.APPROVED:
                return blocked(decision)
            send[CONFIRM] = True
            if answer is ConfirmationOutcome.UNSUPPORTED:
                for name in spec.companions:
                    if given.get(name) is True and self.pending.allows(key, name):
                        send[name] = True
        try:
            return await self._execute(spec, arguments, send, key, prompter, uploads, via, given)
        except ApiUnavailable:
            # Não se sabe se a escrita aconteceu: a confirmação desta chamada não vale mais, e uma
            # nova tentativa pergunta à pessoa de novo (depois de ela conferir o estado).
            self.pending.forget(key)
            raise

    async def _execute(
        self,
        spec: ToolSpec,
        arguments: dict[str, Any],
        send: dict[str, bool],
        key: str,
        prompter: Prompter,
        uploads: Mapping[str, Upload],
        via: str | None,
        given: Mapping[str, Any],
    ) -> ToolOutput:
        response = await self.client.send(prepare(spec, arguments, send, uploads=uploads))
        detail = confirmation_detail(response)
        missing = [name for name in spec.companions if name not in send]
        if detail is not None and missing:
            answer = await prompter.ask(companion_question(spec, detail))
            decision = self.pending.decide(
                key, answer, missing, explicit=all(given.get(name) is True for name in missing)
            )
            if decision is ConfirmationOutcome.APPROVED:
                send.update({name: True for name in missing})
                response = await self.client.send(prepare(spec, arguments, send, uploads=uploads))
            elif decision is ConfirmationOutcome.UNSUPPORTED:
                known = missing + ([CONFIRM] if spec.needs_confirmation(arguments) else [])
                self.pending.remember(key, known)
                step = companion_step(spec, detail, missing, arguments, via)
                return ToolOutput(step, awaiting=True)
            else:
                return blocked(decision)
        if response.ok:
            self.pending.forget(key)
        return render(
            spec, response, redact=self.client.redact, download_dir=self.config.download_dir
        )


def blocked(outcome: ConfirmationOutcome) -> ToolOutput:
    return ToolOutput(blocked_message(outcome), is_error=True, confirmation=outcome)


def _check_names(spec: ToolSpec, arguments: Mapping[str, Any]) -> None:
    unknown = sorted(set(arguments) - set(spec.locations))
    if unknown:
        raise InputError(
            "Argumentos que esta ferramenta não tem: " + ", ".join(f"`{n}`" for n in unknown) + "."
        )
    missing = [name for name in spec.input_schema.get("required", []) if name not in arguments]
    if missing:
        raise InputError(
            "Faltam argumentos obrigatórios: " + ", ".join(f"`{n}`" for n in missing) + "."
        )


# --- os textos da confirmação --------------------------------------------------------------------


def target(
    spec: ToolSpec, arguments: Mapping[str, Any], uploads: Mapping[str, Upload] | None = None
) -> list[str]:
    """Sobre o que a chamada age, em linhas: os valores curtos do caminho e da query (o endereço do
    formulário, a versão), o arquivo (o caminho **já resolvido** e o tamanho: é o que a pessoa
    confirma) e só os **nomes** dos campos do corpo, que podem levar segredo (o token de uma
    integração) ou texto longo. Todo valor que veio do modelo passa por `clean`."""
    lines = []
    scalars = [
        f"{name}={_short(value)}"
        for name, value in arguments.items()
        if spec.locations.get(name) in ("path", "query")
        and isinstance(value, str | int | float | bool)
    ]
    if scalars:
        lines.append("Alvo: " + ", ".join(scalars))
    for name, found in (uploads or {}).items():
        lines.append(f"Arquivo ({name}): {clean(str(found.path))} ({found.size} bytes)")
    fields = [name for name in arguments if spec.locations.get(name) in ("body", "file")]
    if fields:
        lines.append("Campos enviados: " + ", ".join(fields))
    return lines


def question(
    spec: ToolSpec, arguments: Mapping[str, Any], uploads: Mapping[str, Upload] | None = None
) -> str:
    """A pergunta à pessoa (pela *elicitation*), antes de uma operação marcada ou de um arquivo."""
    lines = [f"Confirma «{spec.summary}» (`{spec.name}`)?", ""]
    lines.append("Efeito: " + "; ".join(spec.effects) + ".")
    lines.extend(target(spec, arguments, uploads))
    return "\n".join(lines)


def first_step(
    spec: ToolSpec,
    arguments: Mapping[str, Any],
    uploads: Mapping[str, Upload] | None = None,
    via: str | None = None,
) -> str:
    """O retorno da primeira chamada, para o cliente que não sabe perguntar. Com `via`, a chamada
    seguinte é a da ferramenta composta."""
    lines = [
        "CONFIRMAÇÃO NECESSÁRIA: nada foi executado.",
        "",
        f"{spec.summary} (`{spec.name}`). Efeito: {'; '.join(spec.effects)}.",
        *target(spec, arguments, uploads),
        "",
        _excerpt(spec.details, SHOWN_DETAILS),
        "",
        (
            "Pergunte à pessoa se ela confirma, mostrando o efeito acima. Só se ela disser que sim, "
            f"chame `{via or spec.name}` de novo, com os mesmos argumentos e `confirm: true`."
        ),
    ]
    return "\n".join(lines)


def companion_question(spec: ToolSpec, detail: str) -> str:
    return f"A API pede mais uma confirmação para «{spec.summary}» (`{spec.name}`):\n\n{detail}\n\nConfirma?"


def companion_step(
    spec: ToolSpec,
    detail: str,
    missing: list[str],
    arguments: Mapping[str, Any],
    via: str | None = None,
) -> str:
    flags = ", ".join(f"`{name}: true`" for name in missing)
    again = "`confirm: true` e " if spec.needs_confirmation(arguments) else ""
    return (
        "CONFIRMAÇÃO NECESSÁRIA: nada foi gravado.\n\n"
        f"{detail}\n\n"
        "Pergunte à pessoa se ela confirma. Só se ela disser que sim, chame "
        f"`{via or spec.name}` de novo, com os mesmos argumentos, {again}{flags}."
    )


def clean(text: str) -> str:
    """O texto sem quebra de linha nem caractere de controle ou de formatação (`\\n`, `\\r`, o sinal
    de direção `U+202E`...): um valor que veio do modelo não desenha uma linha a mais na pergunta nem
    se passa por uma frase do servidor. Cada um vira um espaço, e os espaços em sequência, um."""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())


def _short(value: Any) -> str:
    text = clean(str(value))
    return text if len(text) <= SHOWN_VALUE else text[: SHOWN_VALUE - 1] + "…"


def _excerpt(text: str, limit: int) -> str:
    """O começo de `text` até `limit` caracteres, cortado no fim de uma frase se houver uma."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind(".\n"))
    return (cut[: end + 1] if end > limit // 2 else cut.rstrip() + "…").strip()
