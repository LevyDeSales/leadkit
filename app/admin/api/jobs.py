"""Os pedidos: as leituras `list_jobs`, `get_job` e `download_job_pdf`, e as ações `reprocess_job`,
`resend_job_delivery` e `resend_job_sync`.

Leem pela mesma consulta (`app.admin.queries.jobs`) e agem pelas mesmas ações (`app.admin.actions.jobs`)
que as páginas `/admin` e `/admin/jobs/{public_id}`: a regra é uma só, e a API a traduz em JSON e
status. Nenhuma leitura é auditada, como no admin (decisão 2 da revisão da ADR 0015): o cuidado com o
dado da lead está em quem pode lê-lo. As ações são auditadas pelo serviço, em nome de
`chave:<nome>`.

**As ações disparam trabalho no Hatchet, e um disparo que não chega ao engine não é erro:** a ação
vale (a tentativa nova existe; a entrega ou a sincronização voltou para pendente), a resposta diz
`launched: false` e o reconciliador dispara. Por isso nenhuma das três tem `502`. Repetir a chamada
depois de a resposta se perder é `409 state_conflict`, sem efeito duplicado (ADR 0015, "Concorrência
e repetição").

**O papel `leitura` não vê a lead.** `list_jobs` e `get_job` seguem em `leitura` e devolvem o pedido
sem nome, e-mail, telefone, respostas, pontuação, consentimento nem plano. O bloco `lead_data` só vem
com `include_lead_data=true` e papel `total` (`ensure_role` dá o `403 forbidden_role` de quem pede
sem ter o papel, antes de qualquer consulta), e a busca por parte do nome ou do e-mail, que serviria
para sondar quem é lead, também é só do `total`: a chave `leitura` busca só pelo código do pedido.
`download_job_pdf` entrega o plano da lead, e é `total`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Literal

from fastapi import Depends, Path, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import AwareDatetime

from app.admin.actions import jobs as job_actions
from app.admin.api import keys
from app.admin.api.common import (
    PageParams,
    PdfResponse,
    decode_keyset,
    encode_keyset,
    now,
    page_params,
)
from app.admin.api.errors import MEANING, ApiError, ErrorItem
from app.admin.api.operations import OperationRouter
from app.admin.api.schemas.jobs import (
    DeliveryFilter,
    JobDetail,
    JobPage,
    JobPlanProjection,
    JobStatus,
    JobSummary,
    ReprocessedJob,
    ReprocessJobRequest,
    ResendDeliveryRequest,
    ResendRequest,
    ResentDelivery,
    ResentSync,
    SyncFilter,
)
from app.admin.api.security import CurrentKey, ensure_role
from app.admin.queries import jobs as job_queries
from app.deps import get_deps
from app.errors import RetryableError, TerminalError

logger = logging.getLogger(__name__)

TAG = "Pedidos"
router = OperationRouter(
    tag=TAG,
    description=(
        "Os pedidos de plano: cada formulário que uma lead enviou, com a situação, as tentativas, as "
        "entregas ao webhook, as sincronizações com a planilha e a linha do tempo. O estado do "
        "pedido é de `leitura`; o dado da lead (nome, e-mail, respostas, pontuação, consentimento "
        "e plano) e o PDF do plano são só de `total`, e só vêm quando pedidos. Reprocessar um pedido "
        "e reenviar uma entrega ou uma sincronização com a planilha são ações de `total`."
    ),
)

LEAD_DATA_ASKED = "Pedir o dado da lead (`include_lead_data=true`)"
LEAD_DATA_403 = (
    "A chave tem papel `leitura` e a chamada pediu `include_lead_data=true`, que é só do papel "
    "`total` (`forbidden_role`). Sem o parâmetro, a chave `leitura` lê o estado do pedido."
)
# O `422` de `form_version` sem `form`: a versão é de um formulário, e a mensagem não repete o valor.
FORM_VERSION_NEEDS_FORM = "Informe também o `form`: a versão é de um formulário."
INCLUDE_LEAD_DATA = Annotated[
    bool,
    Query(
        description="`true` traz o bloco `lead_data` (nome, e-mail e o resto do dado da lead). Só "
        "com chave `total`: com `leitura` é `403 forbidden_role`. Sem ele, o bloco vem nulo, "
        "mesmo para `total`."
    ),
]


def _cursor_of(row: job_queries.JobRow) -> str:
    """O cursor da página seguinte: a chave de ordenação (`created_at`, `id`) do último pedido."""
    return encode_keyset(row.state.created_at, row.state.id)


def _after(cursor: str | None) -> job_queries.JobCursor | None:
    if cursor is None:
        return None
    return job_queries.JobCursor(*decode_keyset(cursor))


def job_filters(
    q: Annotated[
        str,
        Query(
            max_length=job_queries.Q_MAX_LENGTH,
            description="O que buscar: o código do pedido (exato) e, com chave `total`, parte do "
            "nome ou do e-mail da lead (sem diferenciar maiúsculas). Vazio: todos.",
        ),
    ] = "",
    status: Annotated[
        JobStatus | None,
        Query(description="Só os pedidos neste estado. Ausente: todos."),
    ] = None,
    form: Annotated[
        str | None,
        Query(
            max_length=job_queries.FORM_MAX_LENGTH,
            pattern=job_queries.FORM_PATTERN,
            description="Só os pedidos deste formulário, pelo endereço (`slug`, como `list_forms` o "
            "mostra): letras minúsculas sem acento, números e hífens entre as palavras. Um endereço "
            "que nenhum formulário tem dá a lista vazia. Ausente: todos os formulários.",
            examples=["diagnostico-rapido"],
        ),
    ] = None,
    form_version: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=job_queries.FORM_VERSION_MAX_LENGTH,
            description="Só os pedidos feitos nesta versão do formulário, pelo texto que `form_version` "
            "mostra em cada pedido e o webhook leva: `<endereço>-v<número>` nos pedidos novos, e o que o "
            "código gravava nos feitos antes da migration `0008`, como `mcc-2026.10` (são duas versões "
            "do mesmo formulário, não o número). **Só vale junto com `form`**: sozinho é `422`. Uma "
            "versão que o formulário não tem, ou que é de outro formulário, dá a lista vazia. Ausente: "
            "todas as versões.",
            examples=["diagnostico-rapido-v2"],
        ),
    ] = None,
    delivery: Annotated[
        DeliveryFilter | None,
        Query(
            description="Só os pedidos com uma entrega ao webhook neste estado **na tentativa "
            "atual**, a que a lista mostra em cada pedido: `failed` (esgotou as tentativas; pode "
            "ser reenviada) ou `pending` (ainda não saiu). Uma entrega de uma tentativa antiga, já "
            "reprocessada, não conta, e o pedido sem entrega (na fila, ou de um formulário sem "
            "destino) não aparece em nenhum dos dois. Ausente: todos."
        ),
    ] = None,
    sync: Annotated[
        SyncFilter | None,
        Query(
            description="Só os pedidos com uma sincronização com a planilha neste estado **na "
            "tentativa atual**, a que a lista mostra em cada pedido: `failed` (esgotou as "
            "tentativas; pode ser reenviada com `resend_job_sync`) ou `pending` (ainda não "
            "sincronizou). Uma falha de uma tentativa antiga, já refeita, não conta, e o pedido sem "
            "integração (ou que ainda não concluiu) não aparece em nenhum dos dois. Ausente: todos."
        ),
    ] = None,
    created_from: Annotated[
        AwareDatetime | None,
        Query(
            description="Só os pedidos que chegaram a partir deste instante, **inclusive**. ISO 8601 "
            "com fuso, `Z` ou `-03:00` (Brasília): sem fuso é `422`. O `+` de um fuso como `+02:00` "
            "vai como `%2B` na URL (`2027-01-15T00:00:00%2B02:00`); um `+` solto vira espaço e dá "
            "`422`. Ausente: sem limite.",
            examples=["2027-01-15T00:00:00-03:00"],
        ),
    ] = None,
    created_to: Annotated[
        AwareDatetime | None,
        Query(
            description="Só os pedidos que chegaram antes deste instante, **exclusive**. O mesmo "
            "formato de `created_from`. Um `created_to` antes de `created_from` dá a lista vazia. "
            "Ausente: sem limite.",
            examples=["2027-01-16T00:00:00-03:00"],
        ),
    ] = None,
) -> job_queries.JobFilters:
    """Os filtros da lista de pedidos, como a API os lê: o que não tem a forma do parâmetro é `422`
    (a página os ignora). A chegada são os limites `created_from` e `created_to`; o `period` é só da
    página, que o converte nos mesmos limites. Um filtro novo entra aqui e em `JobFilters`.

    A versão sem o formulário é recusada aqui: a página a ignora, e a API, que não repete o valor,
    diz que ela não vale sozinha."""
    if form_version is not None and form is None:
        raise ApiError(
            "validation_failed",
            "A requisição não vale; veja o motivo de cada campo em `errors`.",
            errors=[ErrorItem(field="form_version", message=FORM_VERSION_NEEDS_FORM)],
        )
    return job_queries.JobFilters(
        q=q,
        status=status or "",
        form=form or "",
        form_version=form_version or "",
        delivery=delivery or "",
        sync=sync or "",
        created_from=created_from,
        created_to=created_to,
    )


@router.operation(
    "GET",
    "/jobs",
    operation_id="list_jobs",
    role="leitura",
    lead_data=True,
    errors={403: LEAD_DATA_403},
    summary="Listar os pedidos",
    description=(
        "Lista os pedidos de plano, do mais novo para o mais antigo, `limit` por página (de 1 a 200; "
        "padrão 50), com o cursor da seguinte em `next_cursor`: o pedido que chega durante a leitura "
        "não pula nem repete nenhuma linha. Cada item traz o estado do pedido (código, situação, "
        "etapa, tentativa, formulário e versão, quando chegou, o código da falha e a situação de "
        "cada entrega ao webhook e de cada sincronização com a planilha da tentativa atual) e "
        "**nada que diga quem é a lead**. "
        "Filtros, todos juntos (E): `status`; `form`, o endereço do formulário (`list_forms` lista "
        "os que existem), que traz só os pedidos dele, e um endereço que nenhum formulário tem "
        "dá a lista vazia, não erro; `form_version`, a versão do formulário em que o pedido foi "
        "feito, no texto que cada item mostra em `form_version` (só junto com `form`; uma versão "
        "que o formulário não tem dá a lista vazia: serve a ver se os pedidos da versão que "
        "acabou de ser publicada estão falhando); `delivery`, `failed` ou `pending`, que traz os pedidos com "
        "uma entrega ao webhook nesse estado na tentativa atual (acha o que não chegou ao destino "
        "sem abrir pedido por pedido; uma falha de uma tentativa já reprocessada não conta); "
        "`sync`, `failed` ou `pending`, que traz os pedidos com uma sincronização com a planilha "
        "nesse estado na tentativa atual (acha as linhas que faltam na planilha; uma falha já "
        "refeita não conta); `created_from` (inclusive) e `created_to` (exclusive), "
        "instantes ISO 8601 com fuso que cortam pela data em que o pedido chegou (para o movimento "
        "de hoje, `created_from` é a meia-noite de Brasília, como `2027-01-15T00:00:00-03:00`: é "
        "o que o filtro Recebido da página calcula, em dias de calendário de Brasília); e `q`, que "
        "acha o pedido pelo código exato e, só com chave "
        "`total`, também por parte do nome ou do e-mail da lead; com chave `leitura`, `q` busca só "
        "pelo código, e um nome ou e-mail simplesmente não acha ninguém (buscar pessoas serviria "
        "para sondar quem é lead). Os filtros valem em todas as páginas: o `next_cursor` continua "
        "filtrado. `include_total=true` acrescenta quantos pedidos há com os filtros (contar "
        "custa). `include_lead_data=true`, só com chave `total`, põe em cada item o bloco "
        "`lead_data` (nome, e-mail e prioridade); com chave `leitura` é `403 forbidden_role`. Não "
        "grava nada."
    ),
    returns="Uma página de pedidos, com `next_cursor` para a seguinte (nulo na última).",
    response_model=JobPage,
)
def list_jobs(
    key: CurrentKey,
    filters: Annotated[job_queries.JobFilters, Depends(job_filters)],
    page: Annotated[PageParams, Depends(page_params)],
    include_total: Annotated[
        bool,
        Query(description="`true` traz o total de pedidos com os filtros. Contar custa."),
    ] = False,
    include_lead_data: INCLUDE_LEAD_DATA = False,
) -> JobPage:
    if include_lead_data:
        ensure_role(key, "total", what=LEAD_DATA_ASKED)
    conditions = filters.conditions(search_lead=keys.allows(key.role, "total"))
    after = _after(page.cursor)
    with get_deps().sessions() as session:
        rows, more = job_queries.jobs_after(
            session,
            conditions,
            forms=get_deps().forms,
            limit=page.limit,
            after=after,
            with_lead=include_lead_data,
        )
        total = job_queries.count_jobs(session, conditions) if include_total else None
    return JobPage(
        items=[JobSummary.of(row) for row in rows],
        next_cursor=_cursor_of(rows[-1]) if more else None,
        total=total,
    )


@router.operation(
    "GET",
    "/jobs/{public_id}",
    operation_id="get_job",
    role="leitura",
    lead_data=True,
    errors={403: LEAD_DATA_403},
    summary="Ler um pedido",
    description=(
        "O pedido de código `public_id`: situação, etapa, tentativa atual, formulário e versão, "
        "datas, até quando o link da lead vale, o código da falha, os PDFs de cada tentativa, as "
        "evidências técnicas em `current_pdf` (tentativa atual, existência, validade estrutural, "
        "acesso ao storage e verificação, sem conceder autorização), as "
        "entregas ao webhook, as sincronizações com a planilha e a linha do tempo, tentativa por "
        "tentativa (o que aconteceu e quando, sem texto livre). É o que se lê para saber o que "
        "falhou e o que fazer. **Nada disso diz quem é a lead.** `include_lead_data=true`, só com "
        "chave `total`, acrescenta o bloco `lead_data`: nome, e-mail, telefone, prioridade e "
        "persona, pontuação, respostas, consentimento, o plano da última tentativa e os textos "
        "livres dos motivos (o motivo de um reprocessamento, o detalhe da falha e o último erro de "
        "cada entrega e sincronização, que podem citar a lead). Com chave `leitura`, pedir o bloco "
        "é `403 forbidden_role`, antes mesmo de procurar o pedido; sem o parâmetro, nem a chave "
        "`total` o recebe. Não grava nada."
    ),
    returns="O pedido: o estado e, só se pedido por uma chave `total`, o bloco `lead_data`.",
    response_model=JobDetail,
)
def get_job(
    request: Request,
    key: CurrentKey,
    public_id: Annotated[
        str, Path(description="O código do pedido (`public_id`), como `list_jobs` o mostra.")
    ],
    include_lead_data: INCLUDE_LEAD_DATA = False,
) -> JobDetail:
    if include_lead_data:
        ensure_role(key, "total", what=LEAD_DATA_ASKED)
    deps = get_deps()
    with deps.sessions() as session:
        found = job_queries.read_job(
            session,
            public_id,
            forms=deps.forms,
            now=now(request),
            with_lead=include_lead_data,
            storage=deps.storage,
        )
    if found is None:
        raise ApiError("not_found", "Pedido não encontrado.")
    return JobDetail.of(found, deps.settings.hatchet_run_url_template)


@router.operation(
    "GET",
    "/jobs/{public_id}/plan-projection",
    operation_id="get_job_plan_projection",
    role="total",
    lead_data=True,
    errors={
        409: "A tentativa atual não tem plano ou a versão histórica não permite projetá-lo (`state_conflict`)."
    },
    summary="Consultar o plano por audiência",
    description="Devolve somente o plano da tentativa atual e referências da versão histórica. Participante remove campos internos antes da serialização; interna conserva o documento validado. Exige chave total. O documento inteiro é dado da lead; o MCP só oferece esta operação com GERADOR_MCP_LEAD_DATA=1. Não concede acesso público, não gera outro plano nem envia eventos.",
    returns="Documento projetado para a audiência explícita, sem respostas ou contato.",
    response_model=JobPlanProjection,
)
def get_job_plan_projection(
    public_id: Annotated[str, Path(description="Código do pedido.")],
    audience: Annotated[
        Literal["participant", "internal"],
        Query(description="Audiência do documento; participante por omissão."),
    ] = "participant",
) -> JobPlanProjection:
    deps = get_deps()
    try:
        with deps.sessions() as session:
            found = job_queries.get_job_plan_projection(
                session, public_id, forms=deps.forms, audience=audience
            )
    except job_queries.ProjectionUnavailable as exc:
        raise ApiError("state_conflict", str(exc)) from None
    if found is None:
        raise ApiError("not_found", "Pedido não encontrado.")
    from dataclasses import asdict

    values = asdict(found)
    document = values.pop("document")
    return JobPlanProjection(**values, lead_data={"document": document})


@router.operation(
    "GET",
    "/jobs/{public_id}/pdf",
    operation_id="download_job_pdf",
    role="total",
    lead_data=True,
    heavy=True,
    errors={
        404: "O pedido não existe, ou ainda não tem o PDF da tentativa pedida (`not_found`).",
        410: MEANING[410],
        502: MEANING[502],
    },
    summary="Baixar o PDF do plano",
    description=(
        "Baixa o PDF do plano da lead: os bytes do arquivo (`application/pdf`), da tentativa "
        "atual ou da que `version` pedir, e **nunca uma URL assinada** (a assinatura seria um segundo "
        "segredo, fora da chave e da auditoria). O plano é dado da lead, por isso só a chave `total` "
        "baixa. `404 not_found` se o pedido não existe ou ainda não tem o PDF dessa tentativa; "
        "`410 gone` se o prazo de retenção já apagou o arquivo; `502 upstream_failed` se o "
        "PDF não pôde ser verificado ou acessado. Conta 5 fichas do limite de uso. "
        "Um histórico sem evidência é validado neste download autorizado: se os bytes, hash, "
        "tamanho, páginas e vínculo forem corretos, persiste somente a prova técnica, sem alterar "
        "o processamento nem chamar IA. Sem `version`, nunca entrega uma tentativa anterior."
    ),
    returns="O arquivo PDF do plano, como anexo (`Content-Disposition: attachment`).",
    response_model=None,
    response_class=PdfResponse,
)
def download_job_pdf(
    public_id: Annotated[
        str, Path(description="O código do pedido (`public_id`), como `list_jobs` o mostra.")
    ],
    version: Annotated[
        int | None,
        Query(
            ge=1,
            description="A tentativa do PDF (`version` de `get_job.pdfs`). Ausente: a tentativa atual, sem fallback para uma anterior.",
        ),
    ] = None,
) -> PdfResponse:
    try:
        pdf = job_actions.pdf_file(get_deps(), public_id, version, signed_url=False)
    except job_actions.NoPdf:
        raise ApiError(
            "not_found", "Este pedido não existe ou ainda não tem o PDF dessa tentativa."
        ) from None
    except job_actions.PdfRemoved:
        raise ApiError(
            "gone", "Este PDF já foi apagado do armazenamento pelo prazo de retenção (LGPD)."
        ) from None
    except (RetryableError, TerminalError, OSError) as failure:
        # Nem a mensagem do provedor sai na resposta, nem a causa no log: só o tipo.
        logger.warning(
            "api: o armazenamento não entregou o PDF pedido=%s erro=%s",
            public_id,
            type(failure).__name__,
        )
        raise ApiError(
            "upstream_failed",
            "O PDF não pôde ser verificado ou acessado. Consulte a disponibilidade e tente novamente.",
        ) from None
    assert pdf.content is not None  # sem `signed_url`, a ação sempre devolve os bytes
    return PdfResponse(
        pdf.content,
        headers={"Content-Disposition": f'attachment; filename="{pdf.filename}"'},
    )


# --- as ações -----------------------------------------------------------------------------------

JOB_NOT_FOUND = "Pedido não encontrado."
CONFLICT_REPEATED = (
    "Se a chamada anterior se perdeu e você a repete, a resposta é esta, sem efeito duplicado: leia o "
    "pedido de novo (`get_job`) para ver o que aconteceu."
)


def _state_after(request: Request, public_id: str) -> job_queries.JobState:
    """O estado do pedido depois da ação, lido de novo (sem o dado da lead)."""
    with get_deps().sessions() as session:
        found = job_queries.read_job(
            session,
            public_id,
            forms=get_deps().forms,
            now=now(request),
            with_lead=False,
            storage=get_deps().storage,
        )
    if found is None:
        raise ApiError("not_found", JOB_NOT_FOUND)
    return found.state


@router.operation(
    "POST",
    "/jobs/{public_id}/reprocess",
    operation_id="reprocess_job",
    role="total",
    external_effect=True,
    errors={
        404: "O pedido não existe (`not_found`).",
        409: (
            "O pedido está na fila ou sendo gerado, ou já não está na tentativa `expected_attempt` "
            "(outra pessoa ou uma chamada repetida o reprocessou): `state_conflict`. Nada acontece e "
            "nenhuma segunda tentativa é criada. " + CONFLICT_REPEATED
        ),
    },
    summary="Reprocessar um pedido",
    description=(
        "Cria uma **nova tentativa** do pedido e a dispara: o pedido volta para a fila (`queued`, "
        "ou já adiante, se o motor foi rápido), a IA escreve o plano de novo, o PDF é refeito e a "
        "entrega ao webhook corre outra vez. Só "
        "vale para um pedido concluído (`completed`) ou que falhou (`failed`); os planos e PDFs das "
        "tentativas anteriores ficam no histórico. Tem efeito para a lead e para fora do sistema "
        '(uma chamada paga à IA e o webhook de novo), por isso exige `"confirm": true`; sem ele a '
        "resposta é `422 confirmation_required` e nada acontece. A ação fica na auditoria em nome da "
        "chave que chamou, com o `reason`. `expected_attempt` é a tentativa que você leu em "
        "`get_job` (`attempt`): se o pedido já está noutra, ou está na fila ou gerando, a resposta é "
        "`409 state_conflict` e nada acontece. É isso que torna seguro repetir a chamada depois de "
        "uma resposta perdida: não cria uma segunda tentativa. A resposta é o pedido no estado novo "
        "(`attempt` é a tentativa criada) e `launched`: `true` se o motor de execução recebeu o "
        "disparo, `false` se não respondeu a tempo. `false` **não é erro**: a tentativa existe e o "
        "reconciliador a dispara em até poucos minutos; não reprocesse de novo."
    ),
    returns="O pedido no estado novo e `launched`, que diz se o disparo chegou ao motor.",
    response_model=ReprocessedJob,
)
async def reprocess_job(
    request: Request,
    public_id: Annotated[
        str, Path(description="O código do pedido (`public_id`), como `list_jobs` o mostra.")
    ],
    body: ReprocessJobRequest,
    key: CurrentKey,
) -> ReprocessedJob:
    deps = get_deps()
    try:
        done = await job_actions.reprocess(
            deps,
            request.app.state.trigger,
            public_id,
            actor=key.actor,
            reason=body.reason,
            confirm=body.confirm,
            expected_attempt=body.expected_attempt,
        )
    except job_actions.InvalidReprocess as invalid:
        raise ApiError(
            "validation_failed",
            "O reprocessamento não vale; veja o motivo de cada campo em `errors`.",
            errors=[ErrorItem(field=name, message=text) for name, text in invalid.errors.items()],
        ) from None
    except job_actions.JobNotFound:
        raise ApiError("not_found", JOB_NOT_FOUND) from None
    except job_actions.ReprocessRefused:
        raise ApiError(
            "state_conflict",
            "Este pedido não pode ser reprocessado agora: ele está na fila ou sendo gerado, ou já "
            "não está na tentativa que você viu (`expected_attempt`). Nada foi feito. "
            + CONFLICT_REPEATED,
        ) from None
    state = await run_in_threadpool(_state_after, request, public_id)
    return ReprocessedJob.with_launch(
        state, deps.settings.hatchet_run_url_template, launched=done.launched
    )


@router.operation(
    "POST",
    "/jobs/{public_id}/deliveries/{delivery_id}/resend",
    operation_id="resend_job_delivery",
    role="total",
    external_effect=True,
    errors={
        404: "O pedido não existe, ou a entrega não é dele (`not_found`).",
        409: (
            "A entrega não está em `failed`, ou receptor, revisão, audiência, tentativa ou "
            "retenção não permitem repetir (`state_conflict`). Nada acontece. " + CONFLICT_REPEATED
        ),
    },
    summary="Reenviar uma entrega ao webhook",
    description=(
        "Novas entregas fixam receptor, revisão, audiência e tentativa. Informe "
        "`expected_config_version` e `expected_attempt`, lidos agora, para reautorizar a revisão "
        "do mesmo receptor; a audiência do fato não muda. Tentativa anterior, receptor removido "
        "ou retenção encerrada são recusados. Históricos sem correlação continuam internos legados. "
        "Reenvia ao webhook o PDF de uma entrega que **falhou** (`failed`, em `get_job.deliveries`): "
        "ela volta para pendente (`pending`) e o envio é disparado com a autorização conferida. "
        'Chama um serviço de fora, por isso exige `"confirm": '
        "true`; sem ele a resposta é `422 confirmation_required` e nada acontece. A ação fica na "
        "auditoria em nome da chave que chamou. Uma entrega que não falhou (já pendente ou "
        "entregue) é `409 state_conflict`: é isso que torna seguro repetir a chamada depois de uma "
        "resposta perdida, sem enviar duas vezes. A resposta é a entrega no estado novo e "
        "`launched`: `true` se o motor de execução recebeu o disparo, `false` se não respondeu a "
        "tempo. `false` **não é erro**: a entrega está pendente e o reconciliador a dispara em "
        "até poucos minutos; não reenvie de novo."
    ),
    returns="A entrega no estado novo e `launched`, que diz se o disparo chegou ao motor.",
    response_model=ResentDelivery,
)
async def resend_job_delivery(
    request: Request,
    public_id: Annotated[
        str, Path(description="O código do pedido (`public_id`), como `list_jobs` o mostra.")
    ],
    delivery_id: Annotated[
        uuid.UUID,
        Path(description="O `id` da entrega, como `get_job` a mostra em `deliveries`."),
    ],
    body: ResendDeliveryRequest,
    key: CurrentKey,
) -> ResentDelivery:
    deps = get_deps()
    try:
        done = await job_actions.resend_delivery(
            deps,
            public_id,
            delivery_id,
            actor=key.actor,
            expected_config_version=body.expected_config_version,
            expected_attempt=body.expected_attempt,
        )
    except job_actions.JobNotFound:
        raise ApiError("not_found", JOB_NOT_FOUND) from None
    except job_actions.ResendRefused as refused:
        raise _resend_refused("entrega", refused) from None
    state = await run_in_threadpool(_state_after, request, public_id)
    delivery = next((item for item in state.deliveries if item.id == delivery_id), None)
    if delivery is None:
        raise ApiError("not_found", "Entrega não encontrada.")
    return ResentDelivery.with_launch(
        delivery, deps.settings.hatchet_run_url_template, launched=done.launched
    )


@router.operation(
    "POST",
    "/jobs/{public_id}/syncs/{sync_id}/resend",
    operation_id="resend_job_sync",
    role="total",
    external_effect=True,
    errors={
        404: "O pedido não existe, ou a sincronização não é dele (`not_found`).",
        409: (
            "A sincronização não está em `failed`: já voltou para a fila, ou já foi feita "
            "(`state_conflict`). Nada acontece. " + CONFLICT_REPEATED
        ),
    },
    summary="Reenviar uma sincronização com a planilha",
    description=(
        "Reenvia à planilha (Baserow) a linha de uma sincronização que **falhou** (`failed`, em "
        "`get_job.syncs`): ela volta para pendente (`pending`) e é disparada de novo, com a "
        "conexão e o mapeamento que valem agora para o formulário (o que se faz depois de "
        "corrigir o token, a tabela ou o mapeamento). Escreve numa planilha de fora, por isso exige "
        '`"confirm": true`; sem ele a resposta é `422 confirmation_required` e nada acontece. A '
        "ação fica na auditoria em nome da chave que chamou. Uma sincronização que não falhou é "
        "`409 state_conflict`: é isso que torna seguro repetir a chamada depois de uma resposta "
        "perdida. A resposta é a sincronização no estado novo e `launched`: `true` se o motor de "
        "execução recebeu o disparo, `false` se não respondeu a tempo. `false` **não é erro**: a "
        "sincronização está pendente e o reconciliador a dispara em até poucos minutos."
    ),
    returns="A sincronização no estado novo e `launched`, que diz se o disparo chegou ao motor.",
    response_model=ResentSync,
)
async def resend_job_sync(
    request: Request,
    public_id: Annotated[
        str, Path(description="O código do pedido (`public_id`), como `list_jobs` o mostra.")
    ],
    sync_id: Annotated[
        uuid.UUID,
        Path(description="O `id` da sincronização, como `get_job` a mostra em `syncs`."),
    ],
    body: ResendRequest,
    key: CurrentKey,
) -> ResentSync:
    try:
        done = await job_actions.resend_sync(get_deps(), public_id, sync_id, actor=key.actor)
    except job_actions.JobNotFound:
        raise ApiError("not_found", JOB_NOT_FOUND) from None
    except job_actions.ResendRefused as refused:
        raise _resend_refused("sincronização", refused) from None
    state = await run_in_threadpool(_state_after, request, public_id)
    sync = next((item for item in state.syncs if item.id == sync_id), None)
    if sync is None:
        raise ApiError("not_found", "Sincronização não encontrada.")
    return ResentSync.with_launch(sync, launched=done.launched)


def _resend_refused(what: str, refused: job_actions.ResendRefused) -> ApiError:
    """A recusa de um reenvio: `404` se a linha não é deste pedido, `409` se não está em `failed`."""
    if refused.missing:
        return ApiError("not_found", f"Esta {what} não existe neste pedido.")
    if what == "entrega":
        return ApiError(
            "state_conflict",
            "A entrega precisa ter falhado e manter a mesma audiência e tentativa. Leia a revisão atual do mesmo destino e envie expected_config_version e expected_attempt; confira também a retenção. Nada foi feito.",
        )
    return ApiError(
        "state_conflict",
        f"Só dá para reenviar uma {what} que falhou (`failed`), e esta já não está nesse estado. "
        "Nada foi feito. " + CONFLICT_REPEATED,
    )
