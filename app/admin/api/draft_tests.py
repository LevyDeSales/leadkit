"""O teste do rascunho de um formulário: montar o prompt (`assemble_form_draft_prompt`), pontuar
(`score_form_draft`), listar os testes recentes (`list_form_draft_tests`), iniciar um teste com o LLM
(`start_form_draft_test`), ler o resultado dele (`get_form_draft_test`) e baixar o PDF que ele gerou
(`download_form_draft_test_pdf`).

Agem pelas mesmas ações das páginas do teste (`app.admin.actions.draft_tests`): o que a página do
teste junta, o disparo da task `draft_test` no Hatchet e a busca de um teste são uma regra só, e a API
a traduz em JSON e status. Montar o prompt, pontuar e listar **só leem**: não gravam, não auditam e
**não chamam o LLM**. Só `start_form_draft_test` o faz, e no worker, não na chamada.

**Cada teste é uma chamada paga ao LLM.** `start_form_draft_test` é marcada `x-external-effect`, exige
`"confirm": true`, e a regra de vez continua nos serviços: um teste por vez em cada formulário e 30 s
de pausa depois de cada um, que viram `409 state_conflict` e `429 test_paused` (com `Retry-After`).
Repetir a chamada depois de a resposta se perder cai numa dessas duas, sem segundo teste (ADR 0015,
"Concorrência e repetição"). **Um disparo que não chega ao engine não é erro:** o teste termina como
`not_started`, a resposta diz `launched: false` e nada foi gasto.

**Os dados de um teste são da operadora, não de uma lead**: as respostas são fictícias e ficam
guardadas com o resultado, por isso o papel `leitura` lê o prompt, o plano e o PDF de um teste. Mesmo
assim, nenhuma mensagem de erro nem log cita as respostas, o prompt ou o plano, e a task do Hatchet
recebe só o id do teste (`CONTRIBUTING.md`, "Hatchet e dados da lead").
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Annotated, Any

from fastapi import Path, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.admin.actions import draft_tests as draft_test_actions
from app.admin.api.common import PdfResponse, now
from app.admin.api.errors import ApiError, ErrorItem
from app.admin.api.forms import FORM, NOT_FOUND
from app.admin.api.operations import OperationRouter
from app.admin.api.schemas.draft_tests import (
    SIMULATED_DATE_FIELD,
    AssembledPrompt,
    AssembleFormDraftPromptRequest,
    DraftTestInput,
    FormDraftScore,
    FormDraftTest,
    FormDraftTests,
    ScoreFormDraftRequest,
    StartedFormDraftTest,
    StartFormDraftTestRequest,
    StartFormDraftTestResponse,
)
from app.admin.api.security import CurrentKey
from app.deps import get_deps
from app.forms import UnknownForm, draft_tests, drafts
from app.forms import draft_test_commands as commands
from app.forms.definition import DefinitionError, Problem
from app.forms.definition.document import CompletionDoc

TAG = "Teste do rascunho"
router = OperationRouter(
    tag=TAG,
    description=(
        "Testar o rascunho de um formulário antes de publicar: montar o prompt que o LLM receberia, "
        "pontuar com outras respostas, iniciar um teste de verdade com o LLM (uma chamada paga) e ler "
        "o resultado dele, o plano e o PDF. Montar o prompt, pontuar, listar e ler o resultado são de "
        "`leitura` e não gravam nada nem chamam o LLM; iniciar um teste (`start_form_draft_test`) é de "
        "`total`, exige `confirm: true` e respeita um teste por vez e a pausa entre eles. Os dados de "
        "um teste são fictícios, da operadora: nenhuma lead entra aqui. Um teste com sucesso é a "
        "prova que a publicação (`publish_form_draft`) pede para o que vai ao LLM."
    ),
)

TEST_ID = Annotated[
    str,
    Path(
        description="O id do teste (`id`), um UUID, como `list_form_draft_tests` ou "
        "`start_form_draft_test` o devolvem. Um valor que não é um UUID também é `404 not_found` "
        "(não `422`), como na página. Só os testes mais recentes de cada formulário ficam "
        "guardados, por poucos dias."
    ),
]
NOT_ASSEMBLED = (
    "O prompt não foi montado; veja o motivo de cada campo em `errors` (`answers.<pergunta>` é uma "
    "resposta; o resto é do documento do rascunho). Nada foi gravado."
)
NOT_SCORED = (
    "A pontuação não foi calculada; veja o motivo de cada campo em `errors` (`answers.<pergunta>` é "
    "uma resposta; um caminho de `scoring` é uma fórmula do rascunho). Nada foi gravado."
)
TEST_NOT_FOUND = (
    "Teste não encontrado: o id não é um UUID ou não é de um teste deste formulário, ou o teste já "
    "saiu (só os mais recentes ficam guardados, por poucos dias)."
)
NO_PDF = "Este teste não tem PDF: ele não chegou até lá (está na fila ou rodando, ou falhou antes)."
LOOKUP_404 = (
    "O formulário não existe, o `test_id` não é um UUID ou o teste não é dele (`not_found`)."
)
PDF_404 = (
    "O formulário não existe, o `test_id` não é um UUID, o teste não é dele ou ele não chegou ao PDF "
    "(`not_found`)."
)
NOT_STARTED = (
    "O teste não começou; veja o motivo de cada campo em `errors` (`answers.<pergunta>` é uma "
    "resposta; `simulated_date`, a data; o resto é do documento do rascunho). Nada foi gravado, "
    "disparado nem gasto."
)
START_TEXT = (
    "Iniciar o teste do rascunho é uma chamada paga ao LLM, com o prompt montado do rascunho e as "
    "respostas do teste (o provedor do modelo as recebe). Fica na auditoria em nome da chave, e só "
    "um teste roda por vez em cada formulário."
)


def _invalid(detail: str, problems: Iterable[Problem]) -> ApiError:
    """O `422` dos problemas: o campo da data se chama `simulated_date` na API (`date` na página)."""
    items = [
        ErrorItem(
            field=SIMULATED_DATE_FIELD if problem.path == "date" else problem.path,
            message=problem.message,
            line=problem.line,
        )
        for problem in problems
    ]
    return ApiError("validation_failed", detail, errors=items)


def _testing(
    request: Request,
    slug: str,
    *,
    answers: dict[str, Any] | None,
    day: str = "",
    scoring: bool = False,
    prompt_only: bool = False,
    refused: str,
    expected_version: str | None = None,
) -> draft_test_actions.Testing:
    """O estado do teste, ou o `404` do formulário e o `422` das respostas, da data e do rascunho."""
    try:
        state = draft_test_actions.testing(
            get_deps(),
            slug,
            answers=answers,
            day=day,
            scoring=scoring,
            prompt_only=prompt_only,
            now=now(request),
            expected_version=expected_version,
        )
    except draft_tests.DraftVersionConflict:
        raise ApiError("state_conflict", draft_tests.VERSION_CONFLICT) from None
    except UnknownForm:
        raise ApiError("not_found", NOT_FOUND) from None
    if state.problems:
        raise _invalid(refused, state.problems)
    return state


def _found(request: Request, slug: str, test_id: str) -> draft_test_actions.Found:
    """O teste do formulário, ou `404`."""
    try:
        return draft_test_actions.find(get_deps(), slug, test_id, now=now(request))
    except UnknownForm:
        raise ApiError("not_found", NOT_FOUND) from None
    except draft_test_actions.UnknownTest:
        raise ApiError("not_found", TEST_NOT_FOUND) from None


@router.operation(
    "POST",
    "/forms/{slug}/draft/prompt",
    operation_id="assemble_form_draft_prompt",
    role="leitura",
    errors={
        409: "A versão esperada diverge do rascunho usado ou o formulário é captura (`state_conflict`); para captura, use test_capture_form_draft.",
        404: "O formulário não existe (`not_found`).",
        422: "As respostas, a data ou o rascunho não valem (`validation_failed`): o motivo vem em "
        "`errors`, com `answers.<pergunta>` para uma resposta, `answers` para uma resposta a uma "
        "pergunta que o formulário não tem (sem citar a chave), `simulated_date` para a data e o "
        "caminho no documento para o rascunho, nunca com o valor recebido. Nada é gravado.",
    },
    summary="Montar o prompt do rascunho",
    description=(
        "Monta o prompt que o LLM receberia do rascunho do formulário `slug` (o rascunho salvo ou, "
        "sem rascunho salvo, a versão publicada atual) com `answers` e `simulated_date`: as "
        "instruções, o prompt montado com as respostas, as notas, os dados e a data, e o calendário "
        "das fases (`phases`) nessa data. É o mesmo prompt que `start_form_draft_test` manda ao LLM. "
        "**Não grava nada, não é auditada e não chama o LLM**: serve para conferir o que o prompt "
        "diz antes de gastar um teste. Sem `answers`, usa as respostas de exemplo do rascunho; as "
        "que você manda são conferidas pelas perguntas dele, todos os problemas de uma vez (`422` "
        "com `answers.<pergunta>` e o motivo, nunca o valor). A data simulada compila o rascunho "
        "com o relógio parado nesse dia: a pontuação, as janelas de datas e o `today` do prompt a "
        "seguem. Use respostas fictícias. Para ver só a pontuação, `score_form_draft`; para os "
        "testes que já rodaram, `list_form_draft_tests`. A guarda da finalidade e a montagem usam "
        "o mesmo rascunho lido: uma edição concorrente vale para a próxima operação; se o rascunho "
        "lido é captura, esta operação responde `409 state_conflict`."
    ),
    returns="O prompt montado: as instruções, o prompt, a data, as respostas usadas e as fases.",
    response_model=AssembledPrompt,
)
def assemble_form_draft_prompt(
    slug: FORM, body: AssembleFormDraftPromptRequest, request: Request
) -> AssembledPrompt:
    state = _testing(
        request,
        slug,
        answers=body.answers,
        day=body.day,
        refused=NOT_ASSEMBLED,
        prompt_only=True,
        expected_version=body.expected_version,
    )
    assert state.assembled is not None  # sem problemas, o prompt montou
    return AssembledPrompt.of(state.assembled)


@router.operation(
    "POST",
    "/forms/{slug}/draft/score",
    operation_id="score_form_draft",
    role="leitura",
    errors={
        409: "A versão esperada diverge do rascunho usado (`state_conflict`).",
        404: "O formulário não existe (`not_found`).",
        422: "As respostas ou o rascunho não valem (`validation_failed`): o motivo vem em "
        "`errors`, com `answers.<pergunta>` para uma resposta, `answers` para uma resposta a uma "
        "pergunta que o formulário não tem (sem citar a chave) e, se uma fórmula não calcula com "
        "elas (uma divisão por zero), o caminho da fórmula em `scoring`; nunca o valor "
        "recebido. Nada é gravado.",
    },
    summary="Pontuar o rascunho com outras respostas",
    description=(
        "Calcula a pontuação do rascunho do formulário `slug` (o salvo ou, sem rascunho salvo, a "
        "versão publicada atual) com `answers`: cada nota, as faixas, a prioridade e a persona, e a "
        "regra (a partir de 1) que decidiu as duas. **Não grava nada, não é auditada e não chama o "
        "LLM.** Sem `answers`, usa as respostas de exemplo do rascunho. Um rascunho sem pontuação "
        "responde `scored: false`, sem notas. As respostas são conferidas pelas perguntas, e uma "
        "fórmula que não calcula com elas (uma divisão por zero) é `422` com o caminho da fórmula. "
        "A pontuação só lê as respostas, por isso a operação não tem data. Use respostas "
        "fictícias."
    ),
    returns="A pontuação: as notas, a prioridade, a persona e a regra que decidiu cada uma.",
    response_model=FormDraftScore,
)
def score_form_draft(slug: FORM, body: ScoreFormDraftRequest, request: Request) -> FormDraftScore:
    state = _testing(
        request,
        slug,
        answers=body.answers,
        scoring=True,
        refused=NOT_SCORED,
        expected_version=body.expected_version,
    )
    assert state.scored is not None  # sem problemas, a pontuação calculou
    return FormDraftScore.of(state.scored)


@router.operation(
    "GET",
    "/forms/{slug}/draft/tests",
    operation_id="list_form_draft_tests",
    role="leitura",
    errors={404: "O formulário não existe (`not_found`)."},
    summary="Listar os testes do rascunho",
    description=(
        "A página do teste do rascunho do formulário `slug`, em dados: as respostas de exemplo do "
        "rascunho (`example_answers`), o prompt montado com elas na data de hoje (`prompt`) e os "
        "testes guardados, em `items` (o envelope das listas), o mais recente primeiro. Só os "
        "`keep_latest` mais recentes de cada formulário ficam guardados, por `keep_days` dias, "
        "então todos cabem na resposta: `next_cursor` é sempre nulo e a lista não tem `limit` nem "
        "`cursor`. Cada teste vem em resumo (estado, quem começou, quando, a data simulada e o "
        "código da falha); "
        "o resultado inteiro, com o plano, os tempos e os tokens, é `get_form_draft_test`. Se o "
        "prompt não monta (um rascunho que já não compila neste deploy), `prompt` é nulo e o "
        "motivo vem em `problems`, sem que a chamada falhe: os testes saem do mesmo jeito. Para "
        "achar o teste que está rodando, procure `finished: false`. Não grava nada nem chama o "
        "LLM."
    ),
    returns="Os testes recentes do rascunho (`items`), as respostas de exemplo e o prompt montado.",
    response_model=FormDraftTests,
)
def list_form_draft_tests(slug: FORM, request: Request) -> FormDraftTests:
    try:
        state = draft_test_actions.testing(get_deps(), slug, now=now(request))
    except UnknownForm:
        raise ApiError("not_found", NOT_FOUND) from None
    return FormDraftTests.of(state)


@router.operation(
    "POST",
    "/forms/{slug}/draft/tests",
    operation_id="start_form_draft_test",
    role="total",
    external_effect=True,
    confirmation_text=START_TEXT,
    confirm_unless="replay_only",
    errors={
        403: "Autoridade incompatível com o comando (`command_forbidden`), sem revelar recibo.",
        404: "O formulário não existe (`not_found`) ou não há comando (`command_not_found`).",
        409: "Já há um teste deste formulário em andamento (`state_conflict`): espere ele terminar "
        "(`get_form_draft_test`, ou `list_form_draft_tests` para achá-lo). Nada é gravado nem "
        "disparado. É isto o que uma chamada repetida depois de uma resposta perdida recebe, sem "
        "segundo teste no caminho legado. Com identidade, namespace, formulário, revisão ou "
        "argumentos incompatíveis com o vínculo retornam `command_conflict`, sem novo efeito.",
        410: "Identidade dt1 expirada (`command_expired`); não será recriada.",
        422: "O corpo não vale (`validation_failed`: as respostas, a data ou o rascunho; o motivo "
        "vem em `errors`, com `answers.<pergunta>`, `answers` para uma pergunta que o formulário "
        "não tem e `simulated_date`, nunca o valor) ou falta "
        "`confirm: true` (`confirmation_required`). Em qualquer caso, nada é gravado, disparado "
        "nem gasto.",
        429: "O limite de uso da chave (`rate_limited`) ou a pausa de 30 s depois do último teste "
        "deste formulário (`test_paused`: cada teste é uma chamada paga ao LLM). Leva "
        "`Retry-After`, em segundos. Nada é gravado nem disparado.",
    },
    summary="Testar o rascunho com o LLM",
    description=(
        "Com `command_id` emitido por `get_form_draft_test_command_context`, exige revisão "
        "explícita e retorna `receipt`, sem conteúdo do teste. `replay_only:true` apenas compara "
        "os argumentos históricos e recupera o recibo, sem confirmação ou efeito, inclusive em "
        "miss. O MCP faz essa consulta antes de perguntar; somente command_not_found elegível "
        "permite seguir para a confirmação atual. Outra autoridade recebe command_forbidden; "
        "mudança de revisão/argumentos/escopo conflita. A janela dt1 fixa é de três dias para "
        "criar desde emissão e três dias para o recibo desde commit, sem renovação. Resultado "
        "removido aparece como expired; despacho uncertain não prova custo zero nem permite "
        "retry por replay. Identidade não autentica pessoa. O caminho sem identidade preserva "
        "o contrato legado descrito a seguir, inclusive queued/launched e revisão opcional. "
        "Põe na fila um teste do rascunho do formulário `slug`, **uma chamada de verdade e paga ao "
        "LLM**, com `answers` (ou as respostas de exemplo do rascunho) e `simulated_date`, e "
        "dispara a task `draft_test` do worker, que recebe só o id do teste. O worker monta o "
        "prompt (o mesmo de `assemble_form_draft_prompt`), chama o LLM uma vez, com a rodada de "
        "reparo do adaptador, e gera o PDF de exemplo, **sem criar envio, pedido, plano, arquivo "
        "no armazenamento nem entrega**. A resposta é o teste na fila (`queued`) e `launched`; "
        "acompanhe com `get_form_draft_test` até `finished: true`. Um teste com sucesso do "
        "rascunho (ou de um rascunho que só mudou em partes que não vão ao LLM depois dele) é a "
        "prova que `publish_form_draft` pede. Tem efeito fora do sistema e custo, por isso exige "
        '`"confirm": true`; sem ele a resposta é `422 confirmation_required` e nada acontece. Só um '
        "teste roda por vez em cada formulário (`409 state_conflict`) e há uma pausa de 30 s depois "
        "de cada um (`429 test_paused`, com `Retry-After`): se a resposta se perder e você repetir "
        "a chamada, ela cai numa das duas, sem segundo teste. `launched: false` **não é erro**: o "
        "engine do Hatchet não recebeu o teste, que já terminou como `failed` com `failure_code` "
        "`not_started` (o LLM não foi chamado, nada foi gasto e não há pausa): tente de novo. Um "
        "teste que passa de 6 minutos na fila ou de 10 rodando conta como `interrupted` e libera a "
        "vez. A partida fica na auditoria (`form.draft.test`) em nome da chave, com o endereço, o "
        "id do teste, a versão do rascunho e a data simulada, nunca as respostas. Use respostas "
        "fictícias: elas ficam com o resultado, e qualquer chave o lê."
    ),
    returns="Com command_id: recibo seguro do teste original. Sem identidade: teste e launched legados.",
    response_model=StartFormDraftTestResponse,
)
async def start_form_draft_test(
    slug: FORM, body: StartFormDraftTestRequest, key: CurrentKey, request: Request
) -> StartedFormDraftTest | commands.ReceiptResponse:
    try:
        started = await draft_test_actions.start(
            get_deps(),
            request.app.state.test_trigger,
            slug,
            actor=key.actor,
            expected_version=body.expected_version,
            answers=body.answers,
            day=body.day,
            now=now(request),
            clock=lambda: now(request),
            command_id=body.command_id,
            principal=commands.Principal("api_key", key.key_id),
            confirmed=body.confirm,
            replay_only=body.replay_only,
            channel="api",
        )
    except commands.CommandError as refused:
        raise ApiError(refused.code, refused.message) from None
    except UnknownForm:
        raise ApiError("not_found", NOT_FOUND) from None
    except DefinitionError as refused:
        raise _invalid(NOT_STARTED, refused.problems) from None
    except draft_tests.DraftVersionConflict:
        raise ApiError("state_conflict", draft_tests.VERSION_CONFLICT) from None
    except draft_tests.DraftTestRefused as refused:
        if refused.paused:
            raise ApiError(
                "test_paused", refused.message, retry_after=refused.retry_after
            ) from None
        raise ApiError("state_conflict", refused.message) from None
    if isinstance(started, commands.CommandReceipt):
        return commands.ReceiptResponse(receipt=started)
    found = await run_in_threadpool(_found, request, slug, str(started.test_id))
    return StartedFormDraftTest.started(found, launched=started.launched)


@router.operation(
    "GET",
    "/forms/{slug}/draft/tests/{test_id}",
    operation_id="get_form_draft_test",
    role="leitura",
    errors={404: LOOKUP_404},
    summary="Ler o resultado de um teste do rascunho",
    description=(
        "O estado e o resultado do teste `test_id` do rascunho do formulário `slug`. Enquanto ele "
        "roda (`queued`, `running`), traz o estado e o que foi ao LLM (as respostas, as instruções "
        "e o prompt), e o plano, os tempos e os tokens ainda são nulos: consulte de novo até "
        "`finished: true`. Terminado com sucesso (`succeeded`), traz o plano que o LLM escreveu, o "
        "modelo, os tempos do LLM e do PDF, os tokens, as rodadas de reparo (`usage`), o número de "
        "páginas do PDF (`download_form_draft_test_pdf` o entrega) e o que o teste diz à "
        "publicação (`publication`). Falhado (`failed`) ou interrompido (`interrupted`), traz o "
        "código e a mensagem da falha, só com códigos e contagens, nunca o conteúdo. Um teste que "
        "passa do prazo sem terminar sai como `interrupted`. Só os testes mais recentes ficam "
        "guardados, por poucos dias (`404 not_found` depois). Os dados são os de exemplo da "
        "operadora, não de uma lead. Não grava nada."
    ),
    returns="O teste: o estado, o que foi ao LLM e, terminado, o plano, os tempos e os tokens.",
    response_model=FormDraftTest,
)
def get_form_draft_test(slug: FORM, test_id: TEST_ID, request: Request) -> FormDraftTest:
    return FormDraftTest.read(_found(request, slug, test_id))


@router.operation(
    "GET",
    "/forms/{slug}/draft/tests/{test_id}/pdf",
    operation_id="download_form_draft_test_pdf",
    role="leitura",
    heavy=True,
    errors={404: PDF_404},
    summary="Baixar o PDF de um teste do rascunho",
    description=(
        "Baixa o PDF que o teste `test_id` do rascunho do formulário `slug` gerou: os bytes do "
        "arquivo (`application/pdf`), como anexo, com o nome `teste-<slug>-<AAAAMMDD-HHMM>.pdf`. "
        "É o PDF do plano de exemplo do rascunho, com as respostas fictícias do teste, e não o de "
        "uma lead, por isso o papel `leitura` o baixa. `404 not_found` se o teste não existe, já "
        "saiu, ou não chegou ao PDF (está na fila, rodando ou falhou antes dele; veja "
        "`has_pdf` em `get_form_draft_test`). O PDF fica na linha do teste, no banco, e sai com "
        "ela. Conta 5 fichas do limite de uso. Não grava nada."
    ),
    returns="O arquivo PDF do teste, como anexo (`Content-Disposition: attachment`).",
    response_model=None,
    response_class=PdfResponse,
)
def download_form_draft_test_pdf(slug: FORM, test_id: TEST_ID, request: Request) -> PdfResponse:
    try:
        pdf = draft_test_actions.pdf(get_deps(), slug, test_id, now=now(request))
    except UnknownForm:
        raise ApiError("not_found", NOT_FOUND) from None
    except draft_test_actions.UnknownTest:
        raise ApiError("not_found", TEST_NOT_FOUND) from None
    except draft_test_actions.NoPdf:
        raise ApiError("not_found", NO_PDF) from None
    return PdfResponse(
        pdf.content, headers={"Content-Disposition": f'attachment; filename="{pdf.filename}"'}
    )


class CaptureTestResult(BaseModel):
    today: date
    scores: dict[str, Any]
    completion: CompletionDoc


@router.operation(
    "POST",
    "/forms/{slug}/draft/capture-test",
    operation_id="test_capture_form_draft",
    role="leitura",
    summary="Testar captura localmente",
    description="Valida respostas fictícias, data, pontuação e conclusão do rascunho de captura. Não persiste submissão ou teste pago e não chama IA, renderização, storage ou fila.",
    returns="Data, notas e conclusão da versão do rascunho.",
    response_model=CaptureTestResult,
    errors={
        404: "Formulário não encontrado.",
        409: "A versão esperada diverge do rascunho usado ou o rascunho exige geração PDF (`state_conflict`).",
        422: "Respostas, data ou definição inválidas.",
    },
)
def test_capture_form_draft(
    slug: FORM, body: DraftTestInput, request: Request
) -> CaptureTestResult:
    with get_deps().sessions() as session:
        draft = drafts.load(session, slug)
    if draft is None:
        raise ApiError("not_found", NOT_FOUND)
    try:
        draft_tests.check_version(draft, body.expected_version)
        result = drafts.test_capture(draft, answers=body.answers, day=body.day, now=now(request))
    except draft_tests.DraftVersionConflict:
        raise ApiError("state_conflict", draft_tests.VERSION_CONFLICT) from None
    except DefinitionError as exc:
        raise _invalid(NOT_STARTED, exc.problems) from None
    return CaptureTestResult(
        today=result.today,
        scores=result.scores,
        completion=CompletionDoc(**vars(result.completion)),
    )


@router.operation(
    "GET",
    "/forms/{slug}/draft/test-command-context",
    operation_id="get_form_draft_test_command_context",
    role="leitura",
    summary="Obter identidade para um novo teste",
    description="Emite identidade dt1 e revisão do rascunho sem gravar ou confirmar nada. Use antes da partida e conserve o ID para recuperar resposta perdida. Não é aprovação; a janela técnica fixa é de três dias. Não depende do relógio da cliente.",
    returns="Identidade, revisão, relógio do servidor e limite exclusivo da criação.",
    response_model=commands.CommandContext,
    errors={503: "Namespace da instalação ausente (`unavailable`)."},
)
def get_form_draft_test_command_context(slug: FORM, request: Request) -> commands.CommandContext:
    try:
        return commands.issue_context(get_deps(), slug, now=now(request))
    except UnknownForm:
        raise ApiError("not_found", NOT_FOUND) from None
    except commands.CommandError as refused:
        raise ApiError(refused.code, refused.message) from None


@router.operation(
    "GET",
    "/forms/{slug}/draft/test-commands/{command_id}",
    operation_id="get_form_draft_test_command",
    role="leitura",
    summary="Consultar recibo de um teste por comando",
    description="Recupera somente IDs e fatos seguros do comando da mesma autoridade, sem ler respostas, gerar ou reenfileirar. Recibo dt1 vence três dias após commit. Se o resultado sair antes, mantém o test_id e informa result_state=expired. Identidade antiga não volta a criar após descarte.",
    returns="Recibo seguro do teste original, ou expiração explícita.",
    response_model=commands.ReceiptResponse,
    errors={
        403: "Comando indisponível para esta autoridade (`command_forbidden`).",
        404: "Identidade ausente (`command_not_found`).",
        409: "Conflito de estado do vínculo: namespace ou formulário incompatível com a "
        "identidade (`command_conflict`). Não é conflito de versão do rascunho atual; a "
        "consulta conserva a revisão histórica.",
        410: "Identidade expirada (`command_expired`).",
    },
)
def get_form_draft_test_command(
    slug: FORM, command_id: str, key: CurrentKey, request: Request
) -> commands.ReceiptResponse:
    try:
        receipt = commands.resolve(
            get_deps(),
            slug,
            command_id,
            principal=commands.Principal("api_key", key.key_id),
            now=now(request),
        )
        return commands.ReceiptResponse(receipt=receipt)
    except commands.CommandError as refused:
        raise ApiError(refused.code, refused.message) from None
