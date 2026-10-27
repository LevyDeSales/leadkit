"""As ferramentas compostas: poucas, escritas à mão, cada uma só chama ferramentas geradas.

Uma ferramenta gerada é uma por operação; os fluxos da operadora juntam várias (ver o que pede atenção,
diagnosticar um pedido, editar e publicar um formulário, conectar uma planilha). Uma composta é uma
função que faz essas chamadas numa só, e **nada além disso**: ela não fala HTTP, não importa o app e
não conhece a API. As regras que as ferramentas geradas já cumprem valem nela porque cada chamada
passa pelo **mesmo caminho** (`ToolRunner.call`):

- o argumento de caminho só vale em `[A-Za-z0-9._~-]` (um `slug` com `/` não roteia para outra
  operação), o arquivo de uma imagem só vale dentro de `GERADOR_MCP_UPLOAD_DIR`, a chave sai de todo
  texto, e o dado da lead segue a política do catálogo (`include_lead_data` fixo em falso, sem `q`);
- a **confirmação** das operações marcadas é a do servidor: com *elicitation* ele pergunta à pessoa; sem
  ela, a primeira chamada só descreve o efeito e a operação executa na chamada seguinte, e nenhum
  `confirm` que o modelo escreveu vale antes de a pessoa ter visto aquela mesma chamada. Uma composta
  **nunca escreve `confirm`**: ela só repassa o que o modelo mandou no argumento dela, e o `ToolRunner`
  decide o que vale (a impressão digital é a da chamada gerada, com os argumentos que a composta
  montou, então a confirmação cobre **aquela** ação e não outra).

**Só o que declara.** `Composite.calls` é a lista das operações que a composta chama; `Steps.call`
recusa qualquer outra. A composta só é registrada se **toda** operação dela está registrada para o papel
da chave e a política do dado da lead (`available`): uma chave `leitura` não recebe a composta que
escreve, e sem `GERADOR_MCP_LEAD_DATA` nenhuma composta chama `download_job_pdf`, e a que lê um pedido não
pede `include_lead_data`. As ferramentas geradas seguem registradas ao lado.

**Passos que a API não marca.** `Steps.gate` dá a um passo da composta a confirmação própria, com a
regra das marcadas (guardar o mapeamento de uma integração ativa muda o que a planilha do cliente recebe, e
`save_form_integration_mapping` não é marcada na API); e `Steps.call(..., bind=...)` põe na impressão
digital da chamada o que a pessoa viu junto com ela e que ela não leva (o mapeamento que vem depois da
conexão), para a confirmação não valer com outro.

**A descrição diz o efeito.** O texto da ferramenta lista as operações que ela chama e, entre elas, as
marcadas (`x-destructive`, `x-external-effect`) com o que cada uma faz; as dicas do MCP (`readOnlyHint`,
`destructiveHint`, `openWorldHint`) saem das mesmas marcas.

**Falha pela metade.** O texto de uma composta que parou no meio diz o que já foi feito e o que não foi
(`Progress`): ela não repete uma escrita às cegas, e o `409` e o `422` da API chegam com o texto de
sempre (`errors`, `reasons`, a versão de agora).

Não depende do SDK do MCP nem do httpx: roda e se testa sem rede.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from app.admin_mcp.catalog import CONFIRM, ToolSpec
from app.admin_mcp.client import InputError
from app.admin_mcp.confirmation import ConfirmationOutcome, blocked_message
from app.admin_mcp.results import ToolOutput
from app.admin_mcp.runner import Prompter, ToolRunner, clean, fingerprint

logger = logging.getLogger("gerador_mcp")

CONFIRMATIONS = (
    "confirm",
    "confirm_contact",
)  # o que a composta repassa, e só do que o modelo mandou
SHOWN_ERROR = 240  # caracteres da primeira linha de um erro, quando entra numa lista
MAX_LISTED = 20  # itens de uma lista no texto de uma composta, quando o chamador não escolhe

Handler = Callable[["Steps", dict[str, Any]], Awaitable[ToolOutput]]


class CompositeError(Exception):
    """Um erro de programação da composta (chamou o que não declarou). Nunca chega ao modelo com o
    texto da exceção: vira «erro inesperado», e o log tem o tipo."""


@dataclass(frozen=True)
class Composite:
    """Uma ferramenta composta. `calls` são as operações geradas que ela chama (e só elas); `notes` é o
    que o gerador acrescenta à descrição (as ferramentas chamadas e as ações marcadas que ela pode
    disparar)."""

    name: str
    summary: str
    details: str
    calls: tuple[str, ...]
    input_schema: Mapping[str, Any]
    annotations: Mapping[str, bool]
    notes: tuple[str, ...]
    run: Handler

    @property
    def description(self) -> str:
        return "\n\n".join(part for part in (self.summary, self.details, *self.notes) if part)

    async def invoke(
        self,
        runner: ToolRunner,
        tools: Mapping[str, ToolSpec],
        arguments: Mapping[str, Any] | None,
        prompter: Prompter,
    ) -> ToolOutput:
        """Executa a composta. Nunca levanta (como `ToolRunner.call`): a falha vira um `ToolOutput` de
        erro, e o texto sai sem a chave."""
        try:
            given = dict(arguments or {})
            check_arguments(self.input_schema, given)
            output = await self.run(Steps(self, runner, tools, prompter), given)
        except InputError as problem:
            output = ToolOutput(str(problem), is_error=True)
        except Exception as problem:  # o SDK devolveria str(problem) ao modelo: aqui só o tipo
            logger.exception("Falha inesperada em %s (%s)", self.name, type(problem).__name__)
            output = ToolOutput(
                f"Erro inesperado no servidor MCP ({type(problem).__name__}); veja o log dele.",
                is_error=True,
            )
        logger.info("%s: %s", self.name, "falhou" if output.is_error else "ok")
        return replace(output, text=runner.client.redact(output.text))


class Steps:
    """O que a composta tem na mão: chamar uma ferramenta gerada, e só as que declarou."""

    def __init__(
        self,
        composite: Composite,
        runner: ToolRunner,
        tools: Mapping[str, ToolSpec],
        prompter: Prompter,
    ) -> None:
        self.composite = composite
        self._runner = runner
        self._tools = tools
        self._prompter = prompter

    def has(self, name: str) -> bool:
        """A ferramenta está registrada para esta chave (para dizer ao modelo o que ela pode fazer)."""
        return name in self._tools

    async def call(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        preface: str | None = None,
        bind: Any = None,
    ) -> ToolOutput:
        """Chama a ferramenta gerada `name` pelo `ToolRunner`, como uma chamada direta.

        `preface` entra antes da pergunta à pessoa, se a operação pedir confirmação: é o que a composta
        já descobriu e a pessoa precisa ver para confirmar **aquela** ação (as mudanças de uma
        publicação, o relatório de um mapeamento). Só entra texto que a composta montou de dados da API
        (limpos) e dos argumentos que o modelo mandou (limpos). A composta põe o mesmo texto no retorno
        que espera a chamada seguinte (`waiting`), para o cliente sem *elicitation* também o mostrar.

        `bind` é o que a pessoa vê junto com a chamada e que a chamada não leva (o mapeamento que a
        composta guarda logo depois da conexão): entra na impressão digital, então a confirmação não
        vale para a mesma chamada com outro `bind`."""
        if name not in self.composite.calls:
            raise CompositeError(f"`{self.composite.name}` não declarou a chamada a `{name}`")
        prompter = Prefaced(self._prompter, preface) if preface else self._prompter
        return await self._runner.call(
            self._tools[name], dict(arguments or {}), prompter, via=self.composite.name, bound=bind
        )

    async def gate(
        self,
        step: str,
        bound: Mapping[str, Any],
        *,
        question: str,
        model_says_yes: bool,
    ) -> Gate:
        """A confirmação **própria** de um passo da composta que a API não marca (guardar o mapeamento
        de uma integração ativa), com a mesma regra das ferramentas marcadas: com *elicitation*, o
        servidor pergunta e ignora o `confirm` do modelo; sem ela, a primeira vez só devolve o texto
        (`question`, o que muda) e o passo executa na chamada seguinte, com `confirm: true`, **se for a
        mesma** (a impressão digital leva `bound`: o que a pessoa viu). Não manda nada à API."""
        key = fingerprint(f"{self.composite.name}:{step}", bound)
        pending = self._runner.pending
        answer = await self._prompter.ask(question)
        decision = pending.decide(key, answer, [CONFIRM], explicit=model_says_yes)
        if decision is ConfirmationOutcome.APPROVED:
            pending.forget(key)
            return Gate("yes", "")
        if decision is not ConfirmationOutcome.UNSUPPORTED:
            return Gate("no", blocked_message(decision), decision)
        return Gate(
            "wait",
            "CONFIRMAÇÃO NECESSÁRIA: nada foi executado.\n\n"
            f"{question}\n\n"
            "Pergunte à pessoa se ela confirma, mostrando o texto acima. Só se ela disser que sim, "
            f"chame `{self.composite.name}` de novo, com os mesmos argumentos e `confirm: true`.",
        )


@dataclass(frozen=True)
class Gate:
    """A resposta de `Steps.gate`: `yes`, `no` (confirmação bloqueada) ou `wait` (sem suporte declarado
    a formulário: `text` descreve o efeito a confirmar, e nada foi executado)."""

    answer: str
    text: str
    outcome: ConfirmationOutcome | None = None


class Prefaced:
    """O `Prompter` com um texto antes da pergunta do `ToolRunner`."""

    def __init__(self, inner: Prompter, preface: str) -> None:
        self._inner = inner
        self._preface = preface

    async def ask(self, message: str) -> ConfirmationOutcome:
        return await self._inner.ask(f"{self._preface}\n\n{message}")


# --- os argumentos -------------------------------------------------------------------------------


def check_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> None:
    """Os nomes e os obrigatórios, como o `ToolRunner` confere os de uma ferramenta gerada. O tipo de
    cada um é do SDK (que valida o esquema) e, no fim, da API."""
    unknown = sorted(set(arguments) - set(schema["properties"]))
    if unknown:
        raise InputError(
            "Argumentos que esta ferramenta não tem: " + ", ".join(f"`{n}`" for n in unknown) + "."
        )
    missing = [name for name in schema.get("required", []) if name not in arguments]
    if missing:
        raise InputError(
            "Faltam argumentos obrigatórios: " + ", ".join(f"`{n}`" for n in missing) + "."
        )


def integer(arguments: Mapping[str, Any], name: str, default: int, low: int, high: int) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InputError(f"`{name}` é um número inteiro de {low} a {high}.")
    return value


def flag(arguments: Mapping[str, Any], name: str, default: bool = False) -> bool:
    """Um booleano estrito: só `true` e `false` valem (um texto `"false"` não vira verdadeiro)."""
    value = arguments.get(name, default)
    if not isinstance(value, bool):
        raise InputError(f"`{name}` é `true` ou `false`.")
    return value


def confirmations(arguments: Mapping[str, Any]) -> dict[str, bool]:
    """As confirmações que **o modelo** mandou na chamada da composta (`confirm`, `confirm_contact`),
    para repassar à **única** chamada gerada que as lê. O `ToolRunner` decide se valem: com
    *elicitation* ele as ignora e pergunta; sem ela, só valem se a pessoa já viu aquela chamada."""
    return {name: True for name in CONFIRMATIONS if arguments.get(name) is True}


# --- o registro ----------------------------------------------------------------------------------


def borrowed(tools: Mapping[str, ToolSpec], operation: str, *names: str) -> dict[str, Any]:
    """O esquema de entrada de `names`, copiado da ferramenta gerada `operation` (o texto do contrato é
    um só). Devolve `{"properties": ..., "$defs": ...}`, com o fecho dos modelos que eles citam."""
    schema = tools[operation].input_schema
    properties = {name: copy.deepcopy(schema["properties"][name]) for name in names}
    definitions: dict[str, Any] = {}
    pending = list(properties.values())
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(node)
        elif isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = ref.removeprefix("#/$defs/")
                if target not in definitions:
                    definitions[target] = copy.deepcopy(schema["$defs"][target])
                    pending.append(definitions[target])
            pending.extend(node.values())
    return {"properties": properties, "$defs": definitions}


def build(
    tools: Mapping[str, ToolSpec],
    *,
    name: str,
    summary: str,
    details: str,
    calls: Sequence[str],
    properties: Mapping[str, Any],
    required: Sequence[str] = (),
    definitions: Mapping[str, Any] | None = None,
    run: Handler,
) -> Composite | None:
    """A composta, ou `None` se alguma operação dela não está registrada para esta chave.

    O esquema de entrada fecha (`additionalProperties: false`), e a descrição e as dicas saem das
    operações que ela chama: lista as ferramentas e as ações marcadas, com o efeito de cada uma."""
    if any(call not in tools for call in calls):
        return None
    specs = [tools[call] for call in calls]
    schema: dict[str, Any] = {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }
    if definitions:
        schema["$defs"] = dict(sorted(definitions.items()))
    marked = [spec for spec in specs if spec.marked]
    notes = [
        "Chama só ferramentas geradas, que seguem disponíveis: "
        + ", ".join(f"`{spec.name}`" for spec in specs)
        + "."
    ]
    if marked:
        notes.append(
            "Ações marcadas que ela pode disparar: "
            + "; ".join(_marked_text(spec) for spec in marked)
            + ". Cada uma pede a confirmação da pessoa pelo servidor MCP, como se você a chamasse "
            "sozinho, e a confirmação cobre só aquela chamada: esta ferramenta nunca confirma por você "
            "(`confirm: true` só depois do sim da pessoa a uma chamada que o servidor já mostrou)."
        )
    elif all(spec.read_only for spec in specs):
        notes.append("Só lê: não grava nada nem dispara nenhuma ação marcada.")
    else:
        notes.append(
            "Não dispara nenhuma ação marcada (a API não pede confirmação para o que grava)."
        )
    return Composite(
        name=name,
        summary=summary,
        details=details,
        calls=tuple(calls),
        input_schema=schema,
        annotations={
            "readOnlyHint": all(spec.read_only for spec in specs),
            "destructiveHint": any(spec.destructive for spec in specs),
            "openWorldHint": any(spec.external_effect for spec in specs),
        },
        notes=tuple(notes),
        run=run,
    )


def _marked_text(spec: ToolSpec) -> str:
    """Uma ação marcada na descrição: o nome, o resumo e o efeito (e, se a operação dispensa a
    confirmação na simulação, que ela só simula com esse argumento)."""
    effect = "; ".join(spec.effects)
    if spec.confirm_unless:
        effect += f"; com `{spec.confirm_unless}: true` só simula e não pede confirmação"
    return f"`{spec.name}` ({spec.summary}: {effect})"


Builder = Callable[[Mapping[str, ToolSpec]], Composite | None]


def available(tools: Mapping[str, ToolSpec], builders: Sequence[Builder]) -> list[Composite]:
    """As compostas que esta chave pode ter: as que têm **todas** as operações que chamam entre as
    `tools` registradas (o papel e a política do dado da lead já aplicados), e cujo nome não é o de uma
    ferramenta gerada (`without_clashes`)."""
    found = (builder(tools) for builder in builders)
    return without_clashes(tools, [composite for composite in found if composite is not None])


def without_clashes(
    tools: Mapping[str, ToolSpec], composites: Iterable[Composite]
) -> list[Composite]:
    """As compostas cujo nome não é o de uma ferramenta gerada registrada. A API pode ser mais nova que
    o MCP e ganhar uma operação com o nome de uma composta: **a gerada vale** (é o contrato), a composta
    é pulada e o log avisa, com o nome entre aspas (`%r`: o nome vem de um documento de fora)."""
    kept = []
    for composite in composites:
        if composite.name in tools:
            logger.warning(
                "Composta %r pulada: a API tem uma operação com esse nome, e a gerada vale",
                composite.name,
            )
        else:
            kept.append(composite)
    return kept


# --- o texto -------------------------------------------------------------------------------------


def lines_of(*blocks: Sequence[str]) -> str:
    """Os blocos de linhas separados por uma linha em branco (os vazios somem)."""
    return "\n\n".join("\n".join(block) for block in blocks if block)


def first_line(output: ToolOutput) -> str:
    """A primeira linha do texto de uma falha, limpa e curta, para uma lista de seções."""
    line = output.text.strip().splitlines()[0] if output.text.strip() else "sem texto"
    text = clean(line)
    return text if len(text) <= SHOWN_ERROR else text[: SHOWN_ERROR - 1] + "…"


def listed(items: Sequence[str], limit: int = MAX_LISTED) -> list[str]:
    """Os itens como linhas `- ...`, até `limit`; o resto vira uma linha de contagem."""
    lines = [f"- {item}" for item in items[:limit]]
    if len(items) > limit:
        lines.append(f"- ... e mais {len(items) - limit}.")
    return lines


def plain(value: Any, limit: int = 200) -> str:
    """Um valor que veio da API ou do modelo, em uma linha, sem caractere de controle e curto."""
    text = clean(str(value))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def body(output: ToolOutput) -> dict[str, Any]:
    """O JSON de uma resposta que deu certo, ou `{}`."""
    return output.data if isinstance(output.data, dict) else {}


def succeeded(output: ToolOutput) -> bool:
    """A chamada executou e a API respondeu com sucesso (não falhou, e não parou à espera da pessoa)."""
    return not output.is_error and not output.awaiting


@dataclass
class Progress:
    """O que uma composta já fez, para dizer a verdade se parar no meio: as escritas que valeram e o
    que ficou por fazer."""

    done: list[str] = field(default_factory=list)
    not_done: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = []
        if self.done:
            out += ["", "Já feito:", *(f"- {item}" for item in self.done)]
        if self.not_done:
            out += ["", "Não feito:", *(f"- {item}" for item in self.not_done)]
        return out


def untouched(output: ToolOutput) -> bool:
    """A chamada falhou **sem deixar nada gravado**: a confirmação foi bloqueada, a API recusou (4xx: ela não
    grava ao recusar) ou o Baserow não respondeu (`502 upstream_failed`, que o contrato diz que não
    grava). Uma falha de rede, ou outro 5xx, não prova nada: a escrita pode ter acontecido."""
    if output.confirmation is not None and output.confirmation not in (
        ConfirmationOutcome.APPROVED,
        ConfirmationOutcome.UNSUPPORTED,
    ):
        return True
    if output.status is None:
        return False
    refusal = output.data.get("code") if isinstance(output.data, dict) else None
    return 400 <= output.status < 500 or (output.status == 502 and refusal == "upstream_failed")


def refused(
    step: str, output: ToolOutput, progress: Progress, *, nothing: str | None = None
) -> ToolOutput:
    """O retorno de uma composta que parou em `step`: o texto da falha como a ferramenta gerada o deu
    (com `errors`, `reasons` e a versão de agora), o que já foi feito e o que não foi.

    `nothing` é o que se pode afirmar quando o passo era uma **escrita** que a API recusou (`Nada foi
    publicado.`): só entra se a falha não deixou nada gravado (`untouched`); senão o texto manda
    conferir o estado antes de repetir, porque não se sabe."""
    lines = [f"Parei em «{step}».", "", output.text.rstrip()]
    if nothing is not None:
        lines += [
            "",
            nothing
            if untouched(output)
            else "Não sei se este passo chegou a gravar: confira o estado com a ferramenta de leitura "
            "antes de repetir.",
        ]
    lines += progress.lines()
    return ToolOutput(
        "\n".join(lines), is_error=True, status=output.status, confirmation=output.confirmation
    )


def waiting(report: Sequence[str], output: ToolOutput) -> ToolOutput:
    """O retorno de uma composta que parou à espera da pessoa: o que ela já levantou e, depois, a
    descrição do efeito da **única** ação que espera o sim (o texto do `ToolRunner`)."""
    return ToolOutput("\n".join([*report, "", "---", output.text]), awaiting=True)
