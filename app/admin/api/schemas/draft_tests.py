"""Os modelos do teste do rascunho: o que `assemble_form_draft_prompt`, `score_form_draft`,
`list_form_draft_tests`, `start_form_draft_test`, `get_form_draft_test` recebem e devolvem.

Dados, não telas: os códigos do banco (`queued`, `succeeded`), as datas em ISO 8601 UTC e nenhum
rótulo. **As respostas de um teste são da operadora, fictícias, e não de uma lead**: por isso o prompt, o
plano e as respostas de um teste não são dado de lead e saem para o papel `leitura`. O que o teste
guarda (e esta API devolve) é só o que a operadora digitou para testar; nenhum modelo aqui lê pedido,
envio nem lead.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import date
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, RootModel

from app.admin.actions import draft_tests as actions
from app.admin.api.common import ApiInput, ConfirmedInput, Page, utc
from app.admin.api.errors import ErrorItem
from app.forms import draft_test_commands as commands
from app.forms import draft_tests, drafts
from app.forms.definition.phases import PhaseStatus

# --- o que a chamada manda ----------------------------------------------------------------------

ANSWERS_TEXT = (
    "As respostas que o teste usa: um objeto com uma chave por pergunta do formulário (o `name` "
    "dela, em `get_form_draft.document.steps`), do jeito que a lead as enviaria. Ausente ou nulo: "
    "as respostas de exemplo do rascunho (`examples.answers`). Um objeto vazio **não** é «sem "
    "respostas»: é um objeto sem nenhuma, e as perguntas obrigatórias o recusam. Uma chave que "
    "não é de uma pergunta do formulário é `422` no campo `answers`, sem citar a chave. **Use "
    "dados fictícios:** as respostas de um teste ficam guardadas com o resultado dele, e qualquer chave "
    "o lê."
)
SIMULATED_DATE_TEXT = (
    "A data simulada, no formato `AAAA-MM-DD` (entre 2000 e 2100). O rascunho é compilado com o "
    "relógio parado nesse dia (ao meio-dia, em São Paulo): o prompt recebe a data em `today` e o "
    "calendário das fases, em `phases`, e o teste com o LLM usa a mesma data. Ausente ou vazia: "
    "hoje, em São Paulo."
)
SIMULATED_DATE_FIELD = "simulated_date"  # o campo `date` da página, na API


class DraftTestInput(ApiInput):
    """As respostas e a data simulada de um prompt, de uma pontuação ou de um teste."""

    expected_version: str | None = Field(
        default=None,
        description="Versão de get_form_draft.version (Draft.token). Se divergir do rascunho usado, 409 state_conflict antes de qualquer efeito. Ausente mantém compatibilidade.",
    )
    answers: dict[str, Any] | None = Field(
        default=None,
        description=ANSWERS_TEXT,
        examples=[{"nome": "Contadora Exemplo", "tamanho_equipe": "2_a_5"}],
    )
    simulated_date: str | None = Field(
        default=None,
        max_length=32,
        description=SIMULATED_DATE_TEXT,
        examples=["2027-03-01"],
    )

    @property
    def day(self) -> str:
        """A data como o serviço a quer: vazia, se não veio."""
        return self.simulated_date or ""


class AssembleFormDraftPromptRequest(DraftTestInput):
    """As respostas e a data com que montar o prompt do rascunho."""


class ScoreFormDraftRequest(ApiInput):
    """As respostas com que pontuar o rascunho. A pontuação só lê as respostas: não tem data."""

    expected_version: str | None = Field(
        default=None,
        description="Versão de get_form_draft.version (Draft.token). Se divergir do rascunho usado, 409 state_conflict antes de qualquer efeito. Ausente mantém compatibilidade.",
    )
    answers: dict[str, Any] | None = Field(
        default=None,
        description=ANSWERS_TEXT,
        examples=[{"nome": "Contadora Exemplo", "tamanho_equipe": "2_a_5"}],
    )


class StartFormDraftTestRequest(ConfirmedInput, DraftTestInput):
    """As respostas e a data do teste, e a confirmação da chamada paga ao LLM."""

    command_id: str | None = Field(
        default=None,
        max_length=128,
        description="Identidade dt1 de get_form_draft_test_command_context, disponível antes do envio. Exige expected_version; não é segredo nem autorização.",
    )
    replay_only: bool = Field(
        default=False,
        description="Somente consultar e comparar um comando já persistido, sem confirmação, criação ou despacho, inclusive quando a identidade não existe.",
    )


# --- o prompt montado ---------------------------------------------------------------------------

WINDOW_STATES = Literal["upcoming", "opens", "open", "closes", "opens_and_closes", "closed"]


class PromptWindow(BaseModel):
    """Uma janela de datas do formulário, vista de uma fase."""

    name: str = Field(description="O nome da janela nos dados do formulário.")
    label: str = Field(description="O rótulo dela (o nome, se não tem outro).")
    start: date = Field(description="O primeiro dia da janela.")
    end: date = Field(description="O último dia da janela.")
    state: WINDOW_STATES = Field(
        description="Onde a fase cai na janela: `upcoming` (ainda não abriu), `opens` (abre nesta "
        "fase), `open` (aberta durante toda a fase), `closes` (aberta e fecha nesta fase), "
        "`opens_and_closes` (abre e fecha nesta fase) ou `closed` (já fechou)."
    )


class PromptAllowedValues(BaseModel):
    """Os valores que um campo do plano pode ter numa fase."""

    field: str = Field(description="O nome do campo do plano.")
    values: list[str] = Field(description="Os valores permitidos nesta fase, na ordem das opções.")


class PromptPhase(BaseModel):
    """Uma fase do calendário que o prompt recebe (`phases`) na data do prompt."""

    number: int = Field(description="O número da fase, a partir de 1.")
    first: date = Field(description="O primeiro dia da fase.")
    last: date = Field(description="O último dia da fase.")
    period: str = Field(description="O período da fase, como o prompt o cita.")
    windows: list[PromptWindow] = Field(description="As janelas de datas, vistas desta fase.")
    allowed: list[PromptAllowedValues] = Field(
        description="Os valores permitidos de cada campo ligado a janelas, nesta fase."
    )

    @classmethod
    def of(cls, phase: PhaseStatus) -> PromptPhase:
        return cls(
            number=phase.number,
            first=phase.first,
            last=phase.last,
            period=phase.period,
            windows=[
                PromptWindow(
                    name=window.name,
                    label=window.label,
                    start=window.start,
                    end=window.end,
                    state=window.state,  # type: ignore[arg-type]
                )
                for window in phase.windows.values()
            ],
            allowed=[
                PromptAllowedValues(field=name, values=list(values))
                for name, values in phase.allowed.items()
            ],
        )


class AssembledPrompt(BaseModel):
    """O que o LLM receberia do rascunho com estas respostas, nesta data."""

    instructions: str = Field(
        description="As instruções do sistema (`instructions` do documento), como vão ao LLM."
    )
    prompt: str = Field(
        description="O prompt montado com as respostas, as notas, os dados e a data: o texto que o "
        "LLM lê, no teste e depois da publicação."
    )
    prompt_version: str = Field(
        description="A versão do prompt que a publicação gravaria nos planos.",
        examples=["demo-definicao-v2"],
    )
    today: date = Field(
        description="A data de «hoje» que o prompt recebeu, em São Paulo (a simulada, se veio)."
    )
    simulated_date: date | None = Field(
        description="A data simulada que a chamada pediu. Nulo: o prompt é o de hoje."
    )
    uses_example_answers: bool = Field(
        description="As respostas são as de exemplo do rascunho (a chamada não mandou `answers`)."
    )
    answers: dict[str, Any] = Field(
        description="As respostas que o prompt usou, já conferidas pelas perguntas do rascunho (as "
        "que a chamada mandou ou as de exemplo). Fictícias."
    )
    phases: list[PromptPhase] = Field(
        description="O calendário das fases que o prompt recebe na data dele: vazio num "
        "formulário sem fases com datas."
    )

    @classmethod
    def of(cls, assembled: drafts.Assembled) -> AssembledPrompt:
        templates = assembled.spec.templates
        phases = templates.phases() if templates is not None else []
        return cls(
            instructions=assembled.instructions,
            prompt=assembled.prompt,
            prompt_version=assembled.spec.prompt_version,
            today=assembled.today,
            simulated_date=assembled.simulated,
            uses_example_answers=assembled.examples,
            answers=assembled.answers.model_dump(mode="json"),
            phases=[PromptPhase.of(phase) for phase in phases],
        )


# --- a pontuação --------------------------------------------------------------------------------


class FormDraftScore(BaseModel):
    """A pontuação do rascunho com estas respostas: cada nota, a prioridade, a persona e a regra
    que decidiu as duas."""

    uses_example_answers: bool = Field(
        description="As respostas são as de exemplo do rascunho (a chamada não mandou `answers`)."
    )
    scored: bool = Field(
        description="O rascunho tem pontuação. `false`: não há nota, prioridade nem persona, e os "
        "campos abaixo ficam vazios."
    )
    scores: dict[str, Any] = Field(
        description="Cada nota pelo nome, na ordem em que a pontuação as declara, e, nas que têm "
        "faixa, a chave e o rótulo do nível. Os valores são números ou textos."
    )
    priority: str | None = Field(
        description="A prioridade que as regras de `scoring.priority` deram. Nulo: nenhuma regra "
        "deu valor (o rascunho pode não ter regras de prioridade)."
    )
    priority_rule: int | None = Field(
        description="Qual regra decidiu a prioridade, a partir de 1, na ordem de "
        "`scoring.priority`. Nulo: nenhuma."
    )
    persona: str | None = Field(
        description="A persona que as regras de `scoring.persona` deram. Nulo: nenhuma regra deu "
        "valor."
    )
    persona_rule: int | None = Field(
        description="Qual regra decidiu a persona, a partir de 1, na ordem de `scoring.persona`. "
        "Nulo: nenhuma."
    )
    has_priority_rules: bool = Field(description="O rascunho declara regras de prioridade.")
    has_persona_rules: bool = Field(description="O rascunho declara regras de persona.")

    @classmethod
    def of(cls, scored: drafts.Scored) -> FormDraftScore:
        run = scored.run
        scoring = scored.spec.scoring
        if run is None or scoring is None:
            return cls(
                uses_example_answers=scored.examples,
                scored=False,
                scores={},
                priority=None,
                priority_rule=None,
                persona=None,
                persona_rule=None,
                has_priority_rules=False,
                has_persona_rules=False,
            )
        return cls(
            uses_example_answers=scored.examples,
            scored=True,
            scores=dict(run.result.scores),
            priority=run.result.priority,
            priority_rule=None if run.priority_rule is None else run.priority_rule + 1,
            persona=run.result.persona,
            persona_rule=None if run.persona_rule is None else run.persona_rule + 1,
            has_priority_rules=scoring.has_priority,
            has_persona_rules=scoring.has_persona,
        )


# --- os testes ----------------------------------------------------------------------------------

TEST_STATUSES = Literal["queued", "running", "succeeded", "failed", "interrupted"]
PUBLICATION = Literal["current", "unlocks", "stale"]


class DraftTestUsage(BaseModel):
    """O que o LLM gastou no teste, como o adaptador o grava. Os campos que o adaptador não deu
    ficam nulos (o LLM falso do modo de teste não gasta token)."""

    input_tokens: int | None = Field(description="Tokens de entrada, somados nas chamadas.")
    output_tokens: int | None = Field(description="Tokens de saída, somados nas chamadas.")
    total_tokens: int | None = Field(description="Tokens no total, somados nas chamadas.")
    repairs: int | None = Field(
        description="Quantas rodadas de reparo o adaptador fez (o plano não passou na "
        "validação e o LLM foi chamado de novo). Nulo: o adaptador não disse."
    )
    repair_reasons: list[str] = Field(
        description="Por que o plano precisou de reparo: `caminho:tipo` de cada erro, nunca o valor "
        "nem a mensagem."
    )
    fallback: bool = Field(description="O modelo de reserva respondeu, no lugar do principal.")
    fake: bool = Field(description="Quem respondeu foi o LLM falso do modo de teste.")

    @classmethod
    def of(cls, usage: Mapping[str, Any]) -> DraftTestUsage:
        def count(name: str) -> int | None:
            value = usage.get(name)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        reasons = usage.get("repair_reasons")
        return cls(
            input_tokens=count("input_tokens"),
            output_tokens=count("output_tokens"),
            total_tokens=count("total_tokens"),
            repairs=count("repairs"),
            repair_reasons=[str(item) for item in reasons] if isinstance(reasons, list) else [],
            fallback=bool(usage.get("fallback")),
            fake=bool(usage.get("fake")),
        )


def _brief_of(run: draft_tests.DraftTestResult) -> dict[str, Any]:
    return {
        "id": run.id,
        "status": run.status,
        "finished": run.finished,
        "started_by": run.started_by,
        "started_at": utc(run.started_at),
        "finished_at": utc(run.finished_at),
        "simulated_date": run.simulated_date,
        "failure_code": run.failure_code,
    }


def _detail_of(found: actions.Found) -> dict[str, Any]:
    run = found.result
    return {
        **_brief_of(run),
        "draft_version": run.definition_fingerprint,
        "publication": draft_tests.gate_of(found.draft, run),
        "answers": run.answers,
        "instructions": run.instructions,
        "prompt": run.prompt,
        "plan": run.plan,
        "model": run.model,
        "usage": DraftTestUsage.of(run.usage or {}),
        "llm_ms": run.llm_ms,
        "pdf_ms": run.pdf_ms,
        "pdf_pages": run.pdf_pages,
        "has_pdf": run.has_pdf,
        "failure_detail": run.failure_detail,
    }


class FormDraftTestBrief(BaseModel):
    """Um teste na lista dos recentes. O resultado inteiro se lê com `get_form_draft_test`."""

    id: uuid.UUID = Field(description="O id do teste.")
    status: TEST_STATUSES = Field(
        description="`queued` (esperando o worker), `running` (o LLM escreve o plano e o PDF "
        "sai), `succeeded`, `failed` (veja `failure_code`) ou `interrupted` (passou do prazo sem "
        "terminar: o worker caiu ou reiniciou)."
    )
    finished: bool = Field(description="O teste terminou (`succeeded`, `failed` ou `interrupted`).")
    started_by: str = Field(
        description="Quem começou: o usuário do admin, ou `chave:<nome>` se foi uma chave.",
        examples=["operadora"],
    )
    started_at: AwareDatetime = Field(description="Quando entrou na fila (ISO 8601, UTC).")
    finished_at: AwareDatetime | None = Field(
        description="Quando terminou (ISO 8601, UTC). Nulo: não terminou, ou foi interrompido."
    )
    simulated_date: date | None = Field(
        description="A data simulada do teste. Nulo: o teste foi na data do clique."
    )
    failure_code: str | None = Field(
        description="Por que falhou: um código do pipeline (`llm_unavailable`, `plan_invalid`, "
        "`render_failed`, `internal`...), `not_started` (o engine do Hatchet não recebeu o teste) "
        "ou `interrupted`. Nulo se não falhou."
    )

    @classmethod
    def of(cls, run: draft_tests.DraftTestResult) -> FormDraftTestBrief:
        return cls(**_brief_of(run))


class FormDraftTest(FormDraftTestBrief):
    """O resultado de um teste do rascunho: o que foi ao LLM, o plano que voltou, o PDF, os tempos,
    os tokens e as rodadas de reparo. Um teste que ainda roda traz só o estado e o que foi ao LLM:
    o plano, os tempos e os tokens chegam quando ele termina."""

    draft_version: str = Field(
        description="A versão do rascunho que foi testada (a `version` de `get_form_draft` naquele "
        "momento)."
    )
    publication: PUBLICATION | None = Field(
        description="O que o teste diz à publicação, se teve sucesso: `current` (testou o rascunho "
        "como está agora), `unlocks` (depois dele, o rascunho só mudou em partes que não vão ao "
        "LLM: o teste ainda libera a publicação) ou `stale` (o que vai ao LLM mudou depois dele: "
        "não vale). Nulo: o teste não teve sucesso."
    )
    answers: dict[str, Any] = Field(
        description="As respostas que o teste usou, já conferidas pelas perguntas. Fictícias."
    )
    instructions: str = Field(description="As instruções do sistema, como foram ao LLM.")
    prompt: str = Field(
        description="O prompt, como foi ao LLM (montado no momento do clique ou na data simulada)."
    )
    plan: dict[str, Any] | None = Field(
        description="O plano que o LLM escreveu e a validação aceitou, no modelo do formulário. "
        "Nulo até o teste terminar com sucesso."
    )
    model: str | None = Field(description="O modelo do LLM que respondeu.", examples=["gpt-5"])
    usage: DraftTestUsage = Field(description="Os tokens e as rodadas de reparo do LLM.")
    llm_ms: int | None = Field(description="Quanto o LLM levou, em milissegundos.")
    pdf_ms: int | None = Field(description="Quanto o PDF levou, em milissegundos.")
    pdf_pages: int | None = Field(description="Quantas páginas tem o PDF.")
    has_pdf: bool = Field(
        description="O teste chegou ao PDF: `download_form_draft_test_pdf` o entrega."
    )
    failure_detail: str | None = Field(
        description="A mensagem da falha, só com códigos e contagens (nunca o conteúdo do plano "
        "nem das respostas). Nulo se não falhou."
    )

    @classmethod
    def read(cls, found: actions.Found) -> FormDraftTest:
        """O resultado do teste achado, com o que ele diz à publicação do rascunho de agora."""
        return cls(**_detail_of(found))


class StartedFormDraftTest(FormDraftTest):
    """O teste que entrou na fila, e se o engine do Hatchet recebeu o disparo."""

    launched: bool = Field(
        description="`true`: o engine do Hatchet recebeu o disparo da task. `false`: não recebeu a "
        "tempo, e o teste **já terminou** como `failed` com `failure_code` `not_started` (o LLM não "
        "foi chamado, nada foi gasto e não há pausa): tente de novo. Não é um erro da chamada."
    )

    @classmethod
    def started(cls, found: actions.Found, *, launched: bool) -> StartedFormDraftTest:
        return cls(**_detail_of(found), launched=launched)


class StartFormDraftTestResponse(RootModel[StartedFormDraftTest | commands.ReceiptResponse]):
    """União nomeada: mantém o JSON legado ou o envelope de recibo, sem camada adicional."""


class FormDraftTests(Page[FormDraftTestBrief]):
    """A página do teste, em dados: as respostas de exemplo, o prompt montado com elas e os testes
    recentes, estes no envelope das listas (`items` e `next_cursor`)."""

    items: list[FormDraftTestBrief] = Field(
        description="Os testes guardados do formulário, o mais recente primeiro. São poucos (os "
        "`keep_latest` mais recentes), todos nesta página."
    )
    next_cursor: str | None = Field(
        default=None,
        description="Sempre nulo: os testes guardados cabem todos numa resposta, e a lista não "
        "tem `limit` nem `cursor`. O campo existe para o envelope ser o de toda lista.",
    )
    example_answers: dict[str, Any] = Field(
        description="As respostas de exemplo do rascunho (`examples.answers` do documento): o que "
        "o teste usa quando a chamada não manda `answers`."
    )
    prompt: AssembledPrompt | None = Field(
        description="O prompt montado com as respostas de exemplo, na data de hoje. Nulo se não "
        "deu para montar: o motivo vem em `problems`."
    )
    problems: list[ErrorItem] = Field(
        description="Por que o prompt não montou (o caminho de cada campo, no documento do "
        "rascunho). Vazio quando `prompt` veio. Não é um erro da chamada: os testes recentes saem "
        "do mesmo jeito."
    )
    keep_latest: int = Field(description="Quantos testes ficam guardados em cada formulário.")
    keep_days: int = Field(description="Por quantos dias um teste fica guardado.")

    @classmethod
    def of(cls, state: actions.Testing) -> FormDraftTests:
        examples = state.draft.document.get("examples")
        answers = examples.get("answers") if isinstance(examples, Mapping) else None
        return cls(
            example_answers=dict(answers) if isinstance(answers, Mapping) else {},
            prompt=AssembledPrompt.of(state.assembled) if state.assembled is not None else None,
            problems=[
                ErrorItem(field=problem.path, message=problem.message, line=problem.line)
                for problem in state.problems
            ],
            items=[FormDraftTestBrief.of(run) for run in state.recent],
            next_cursor=None,
            keep_latest=draft_tests.KEEP_TESTS,
            keep_days=draft_tests.TEST_DAYS,
        )
