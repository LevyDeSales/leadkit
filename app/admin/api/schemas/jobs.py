"""Os modelos dos pedidos: o que `list_jobs`, `get_job` e `download_job_pdf` devolvem, e o que
`reprocess_job`, `resend_job_delivery` e `resend_job_sync` recebem e devolvem.

**O estado do pedido fica separado do dado da lead.** Todo modelo de leitura tem as duas partes: os
campos do estado (código, situação, etapa, tentativa, formulário, datas, código da falha, entregas,
sincronizações, linha do tempo) e o bloco `lead_data` (nome, e-mail, telefone, prioridade, pontuação,
respostas, consentimento, plano e os textos livres dos motivos), que só vem a quem pediu
`include_lead_data=true` e tem o papel `total`. Um campo pessoal fora do bloco furaria a barreira
entre `leitura` e a lead (ADR 0015, "Segredos e dados sensíveis"), e o teste de contrato reprova.

Os códigos são os da API pública, em inglês (`queued`, `completed`, `failed`); as datas, ISO 8601 UTC.
Nenhum rótulo: a página é que traduz.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints

from app.admin.actions.jobs import REASON_MAX_LENGTH, REASON_MIN_LENGTH
from app.admin.api.common import ConfirmedInput, Page, utc
from app.admin.queries import jobs as queries

JobStatus = Literal["queued", "running", "completed", "failed"]
JobStage = Literal["queued", "generating", "rendering", "completed", "failed"]
DeliveryStatus = Literal["pending", "succeeded", "failed"]
DeliveryFilter = Literal[
    "failed", "pending"
]  # o `delivery` de `list_jobs`; `queries.DELIVERY_FILTERS`
SyncFilter = Literal["failed", "pending"]  # o `sync` de `list_jobs`; `queries.SYNC_FILTERS`

STATUS_TEXT = (
    "`queued` (na fila), `running` (gerando), `completed` (concluído) ou `failed` (falhou). O estado "
    "da tentativa atual."
)
STAGE_TEXT = (
    "Em que passo o pedido está: `queued`, `generating` (a IA escreve o plano), `rendering` (monta o "
    "PDF), `completed` ou `failed`."
)


class DeliveryBrief(BaseModel):
    """A entrega do PDF da tentativa atual, resumida."""

    destination: str = Field(
        description="Para onde vai a entrega. Hoje, só `webhook`.", examples=["webhook"]
    )
    status: DeliveryStatus = Field(
        description="`pending` (ainda não saiu), `succeeded` (o destino recebeu) ou `failed` (esgotou "
        "as tentativas; pode ser reenviada)."
    )


class SyncBrief(BaseModel):
    """A sincronização do PDF da tentativa atual com a planilha, resumida. Sem o texto do erro nem
    nada da linha da planilha: o detalhe está em `get_job`."""

    integration: str = Field(
        description="O tipo da integração. Hoje, só `baserow`.", examples=["baserow"]
    )
    status: DeliveryStatus = Field(
        description="`pending` (ainda não sincronizou), `succeeded` (a linha está na planilha) ou "
        "`failed` (esgotou as tentativas; pode ser reenviada com `resend_job_sync`)."
    )


class SummaryLeadData(BaseModel):
    """O dado da lead de um pedido na lista. Só vem com `include_lead_data=true` e papel `total`."""

    name: str | None = Field(
        description="O nome do envio, se informado e a identidade histórica estiver disponível.",
        examples=["Ana Souza"],
    )
    email: str | None = Field(
        description="O e-mail do envio. Nulo quando a identidade histórica não puder ser recuperada.",
        examples=["ana@example.test"],
    )
    priority: str | None = Field(
        description="A prioridade que a pontuação deu à lead (`P1`, `P2` ou `P3`). Nula se o "
        "formulário não pontua.",
        examples=["P2"],
    )


class JobSummary(BaseModel):
    """Um pedido na lista: o estado, sem nada que diga quem é a lead."""

    public_id: str = Field(
        description="O código do pedido, o mesmo que a lead e o webhook usam.",
        examples=["k3JdQ8s_Lw0vX2aB"],
    )
    status: JobStatus = Field(description=STATUS_TEXT)
    stage: JobStage = Field(description=STAGE_TEXT)
    attempt: int = Field(
        description="A tentativa atual (1 no início; cada reprocessamento soma 1).", examples=[1]
    )
    form_slug: str = Field(description="O endereço do formulário que a lead respondeu.")
    form_version: str = Field(
        description="A versão do formulário com que ela respondeu, no texto que o webhook leva.",
        examples=["2027-01-v3"],
    )
    failure_code: str | None = Field(
        description="Se o pedido falhou, o código da falha (`llm_unavailable`, `llm_rejected`, "
        "`plan_invalid`, `render_failed`, `storage_failed`, `stuck`, `internal`...). Nulo se não "
        "falhou. O texto do motivo não sai aqui: só em `lead_data.notes` de `get_job`.",
        examples=["llm_unavailable"],
    )
    created_at: AwareDatetime = Field(description="Quando o pedido chegou (ISO 8601, UTC).")
    deliveries: list[DeliveryBrief] = Field(
        description="A situação de cada entrega do PDF da tentativa atual."
    )
    syncs: list[SyncBrief] = Field(
        description="A situação de cada sincronização com a planilha do PDF da tentativa atual. Vazia "
        "se o formulário não tem integração (ou o pedido ainda não concluiu)."
    )
    lead_data: SummaryLeadData | None = Field(
        description="O dado da lead. Nulo, a menos que a chamada peça `include_lead_data=true` e a "
        "chave tenha o papel `total`."
    )

    @classmethod
    def of(cls, row: queries.JobRow) -> JobSummary:
        state = row.state
        return cls(
            public_id=state.public_id,
            status=state.status,  # type: ignore[arg-type]
            stage=state.stage,  # type: ignore[arg-type]
            attempt=state.attempt,
            form_slug=state.form_slug,
            form_version=state.form_version,
            failure_code=state.failure_code,
            created_at=utc(state.created_at),
            deliveries=[
                DeliveryBrief(destination=d.destination, status=d.status)  # type: ignore[arg-type]
                for d in state.deliveries
            ],
            syncs=[
                SyncBrief(integration=sync.integration, status=sync.status)  # type: ignore[arg-type]
                for sync in state.syncs
            ],
            lead_data=SummaryLeadData(
                name=row.lead.name, email=row.lead.email, priority=row.lead.priority
            )
            if row.lead is not None
            else None,
        )


class JobPage(Page[JobSummary]):
    """Uma página de pedidos, dos mais novos para os mais antigos."""

    total: int | None = Field(
        default=None,
        description="Quantos pedidos há com os filtros (`q` e `status`), em todas as páginas. Só vem "
        "com `include_total=true`, porque contar custa. Nulo, sem ele.",
    )


# --- o detalhe ----------------------------------------------------------------------------------

EventType = Literal[
    "received",
    "reprocess_requested",
    "plan_generated",
    "pdf_generated",
    "completed",
    "failed",
    "processing",
    "queued",
    "delivery_resend_requested",
    "delivery_succeeded",
    "delivery_failed",
    "delivery_pending",
]
NoteKind = Literal["failure_detail", "reprocess_reason", "delivery_error", "sync_error"]


class JobPdf(BaseModel):
    """O PDF de uma tentativa. O arquivo se baixa com `download_job_pdf` (chave `total`)."""

    version: int = Field(
        description="A tentativa a que o PDF pertence; é o `version` de `download_job_pdf`.",
        examples=[1],
    )
    byte_size: int = Field(description="O tamanho do arquivo, em bytes.", examples=[184320])
    created_at: AwareDatetime = Field(description="Quando o PDF foi gerado (ISO 8601, UTC).")
    purged_at: AwareDatetime | None = Field(
        description="Quando o prazo de retenção apagou o arquivo (o registro fica). Nulo: o arquivo "
        "ainda existe."
    )


class JobDelivery(BaseModel):
    """A entrega do PDF de uma tentativa ao destino externo."""

    id: uuid.UUID = Field(description="O id da entrega.")
    destination: str = Field(description="Para onde vai. Hoje, só `webhook`.", examples=["webhook"])
    status: DeliveryStatus = Field(
        description="`pending` (ainda não saiu), `succeeded` (o destino recebeu) ou `failed` (esgotou "
        "as tentativas)."
    )
    attempts: int = Field(description="Quantas vezes o envio foi tentado.", examples=[1])
    attempt: int = Field(description="A tentativa do pedido a que o PDF desta entrega pertence.")
    delivered_at: AwareDatetime | None = Field(
        description="Quando o destino recebeu (ISO 8601, UTC). Nulo se ainda não recebeu."
    )
    scope: Literal["form", "default"] | None = Field(
        description="Qual destino do webhook o último envio usou: `form` (o do formulário) ou "
        "`default` (o padrão). Nulo antes do primeiro envio."
    )
    host: str | None = Field(
        description="O host do destino do último envio (nunca o caminho da URL, que vale como senha).",
        examples=["receptor.example.test"],
    )
    run_url: str | None = Field(
        description="O link da execução desta entrega no painel do Hatchet, se o ambiente tem o "
        "modelo do link e a entrega foi disparada."
    )


class JobSync(BaseModel):
    """A sincronização do PDF de uma tentativa com a tabela de uma integração."""

    id: uuid.UUID = Field(description="O id da sincronização.")
    integration: str = Field(description="O tipo da integração. Hoje, só `baserow`.")
    host: str = Field(description="O host da planilha.", examples=["baserow.example.test"])
    table_id: int = Field(description="O número da tabela na planilha.", examples=[7])
    status: DeliveryStatus = Field(
        description="`pending` (ainda não sincronizou), `succeeded` ou `failed` (esgotou as "
        "tentativas)."
    )
    attempts: int = Field(description="Quantas vezes o envio foi tentado.")
    attempt: int = Field(
        description="A tentativa do pedido a que o PDF desta sincronização pertence."
    )
    outcome: Literal["created", "updated"] | None = Field(
        description="O que a sincronização fez com a linha: `created` ou `updated`. Nulo se ainda "
        "não sincronizou."
    )
    row_id: int | None = Field(description="O número da linha na tabela. Nulo se ainda não há.")
    synced_at: AwareDatetime | None = Field(description="Quando sincronizou (ISO 8601, UTC).")
    created_at: AwareDatetime = Field(description="Quando a sincronização foi criada.")


class JobEvent(BaseModel):
    """Um acontecimento de uma tentativa. Não traz texto livre: o motivo e o erro, quando existem, vão
    em `lead_data.notes`. Os campos além de `type` e `at` só valem para alguns tipos."""

    type: EventType = Field(
        description="O que aconteceu: `received` (o pedido chegou), `reprocess_requested` (alguém "
        "pediu uma nova tentativa), `plan_generated`, `pdf_generated`, `completed`, `failed`, "
        "`processing` (em andamento), `queued`, `delivery_resend_requested` (alguém pediu o reenvio) "
        "e `delivery_succeeded`, `delivery_failed` ou `delivery_pending`."
    )
    at: AwareDatetime | None = Field(
        description="Quando aconteceu (ISO 8601, UTC). Nulo quando não ficou registrado."
    )
    actor: str | None = Field(
        description="Quem pediu, em `reprocess_requested` e `delivery_resend_requested`: o usuário "
        "do admin ou `chave:<nome>`.",
        examples=["operadora"],
    )
    failure_code: str | None = Field(
        description="O código da falha, em `failed`.", examples=["llm_unavailable"]
    )
    stage: JobStage | None = Field(description="Em que passo está, em `processing`.")
    delivery_id: uuid.UUID | None = Field(
        description="A entrega de que o evento fala (o `id` em `deliveries`), nos `delivery_*`."
    )
    model: str | None = Field(
        description="O modelo de IA que escreveu o plano, em `plan_generated`."
    )
    prompt_version: str | None = Field(description="A versão do prompt, em `plan_generated`.")


class JobAttempt(BaseModel):
    """Uma tentativa do pedido, com o que aconteceu nela."""

    attempt: int = Field(description="O número da tentativa (1 é a primeira).", examples=[1])
    outcome: JobStatus | None = Field(
        description="Como a tentativa terminou (ou em que pé está, se é a atual). Nulo se uma "
        "tentativa antiga não deixou registro do fim."
    )
    events: list[JobEvent] = Field(description="Os acontecimentos, em ordem.")


class JobConsent(BaseModel):
    """Os consentimentos que a lead deu."""

    privacy_accepted_at: AwareDatetime | None = Field(
        description="Quando aceitou o tratamento das respostas para gerar o plano."
    )
    text_version: str | None = Field(
        description="A versão do texto de consentimento que ela viu.", examples=["2027-01-v3"]
    )
    marketing_email: bool = Field(description="Autorizou novidades por e-mail.")
    marketing_whatsapp: bool = Field(description="Autorizou novidades por WhatsApp.")


class JobPlan(BaseModel):
    """O plano da tentativa mais recente."""

    version: int = Field(description="A tentativa que gerou este plano.")
    model: str = Field(description="O modelo de IA que o escreveu.")
    prompt_version: str = Field(description="A versão do prompt.")
    created_at: AwareDatetime = Field(description="Quando foi gerado (ISO 8601, UTC).")
    document: dict[str, Any] = Field(
        description="O plano validado, como o formulário o define (os campos variam por formulário)."
    )


class JobNote(BaseModel):
    """O texto livre de um motivo ou de um erro. Os adaptadores só escrevem códigos e status nos erros,
    mas o motivo de um reprocessamento é o que alguém digitou e pode citar a lead: por isso o texto
    vem aqui, no `lead_data`, e a linha do tempo só diz o que aconteceu."""

    kind: NoteKind = Field(
        description="De onde vem: `failure_detail` (o detalhe da falha do pedido), "
        "`reprocess_reason` (o motivo de um reprocessamento), `delivery_error` ou `sync_error` (o "
        "último erro de uma entrega ou de uma sincronização)."
    )
    attempt: int = Field(description="A tentativa do pedido a que o texto pertence.")
    reference: uuid.UUID | None = Field(
        description="O `id` da entrega ou da sincronização de que o texto é. Nulo nos outros."
    )
    text: str = Field(description="O texto, como foi gravado.")


class JobLeadData(BaseModel):
    """O dado da lead de um pedido. É dado pessoal: só vem com `include_lead_data=true` e chave
    `total`, e o papel `leitura` nunca o recebe."""

    name: str | None = Field(
        description="O nome do envio, se informado e a identidade histórica estiver disponível.",
        examples=["Ana Souza"],
    )
    email: str | None = Field(
        description="O e-mail do envio. Nulo quando a identidade histórica não puder ser recuperada.",
        examples=["ana@example.test"],
    )
    phone: str | None = Field(
        description="O telefone do envio, se coletado e a identidade histórica estiver disponível."
    )
    persona: str | None = Field(description="A persona que a pontuação deu. Nula se não pontua.")
    priority: str | None = Field(
        description="A prioridade (`P1`, `P2` ou `P3`). Nula se o formulário não pontua.",
        examples=["P2"],
    )
    scores: dict[str, Any] = Field(
        description="A pontuação, campo a campo (uso interno do comercial; os campos variam por "
        "formulário)."
    )
    answers: dict[str, Any] = Field(
        description="As respostas, por nome de pergunta, como a lead as enviou. Os rótulos e a "
        "ordem de cada pergunta estão na versão do formulário (`form_slug` e `form_version`)."
    )
    consent: JobConsent
    plan: JobPlan | None = Field(
        description="O plano da tentativa mais recente. Nulo se ainda não foi gerado."
    )
    notes: list[JobNote] = Field(
        description="Os textos livres dos motivos e dos erros (podem citar a lead)."
    )

    @classmethod
    def of(cls, lead: queries.LeadData) -> JobLeadData:
        return cls(
            name=lead.name,
            email=lead.email,
            phone=lead.phone,
            persona=lead.persona,
            priority=lead.priority,
            scores=lead.scores,
            answers=lead.answers,
            consent=JobConsent(
                privacy_accepted_at=utc(lead.consent.privacy_at),
                text_version=lead.consent.text_version,
                marketing_email=lead.consent.marketing_email,
                marketing_whatsapp=lead.consent.marketing_whatsapp,
            ),
            plan=JobPlan(
                version=lead.plan.version,
                model=lead.plan.model,
                prompt_version=lead.plan.prompt_version,
                created_at=utc(lead.plan.created_at),
                document=lead.plan.document,
            )
            if lead.plan is not None
            else None,
            notes=[
                JobNote(
                    kind=note.kind,  # type: ignore[arg-type]
                    attempt=note.attempt,
                    reference=note.ref,
                    text=note.text,
                )
                for note in lead.notes
            ],
        )


def _delivery_of(delivery: queries.DeliveryState, run_url_template: str | None) -> dict[str, Any]:
    """Os campos de `JobDelivery` de uma entrega lida: um só lugar para o pedido e para o reenvio."""
    return {
        "id": delivery.id,
        "destination": delivery.destination,
        "status": delivery.status,
        "attempts": delivery.attempts,
        "attempt": delivery.job_attempt,
        "delivered_at": utc(delivery.delivered_at),
        "scope": delivery.scope,
        "host": delivery.host,
        "run_url": queries.run_url(run_url_template, delivery.hatchet_run_id),
    }


def _sync_of(sync: queries.SyncState) -> dict[str, Any]:
    """Os campos de `JobSync` de uma sincronização lida."""
    return {
        "id": sync.id,
        "integration": sync.integration,
        "host": sync.host,
        "table_id": sync.table_id,
        "status": sync.status,
        "attempts": sync.attempts,
        "attempt": sync.job_attempt,
        "outcome": sync.outcome,
        "row_id": sync.row_id,
        "synced_at": utc(sync.synced_at),
        "created_at": utc(sync.created_at),
    }


def _state_fields(state: queries.JobState, run_url_template: str | None) -> dict[str, Any]:
    """Os campos de `JobState`, o estado do pedido, sem nada da lead."""
    return {
        "public_id": state.public_id,
        "status": state.status,
        "stage": state.stage,
        "attempt": state.attempt,
        "form_slug": state.form_slug,
        "form_version": state.form_version,
        "failure_code": state.failure_code,
        "created_at": utc(state.created_at),
        "completed_at": utc(state.completed_at),
        "link_expires_at": utc(state.link_expires_at),
        "link_expired": state.link_expired,
        "run_url": queries.run_url(run_url_template, state.hatchet_run_id),
        "pdfs": [
            JobPdf(
                version=pdf.version,
                byte_size=pdf.byte_size,
                created_at=utc(pdf.created_at),
                purged_at=utc(pdf.purged_at),
            )
            for pdf in state.pdfs
        ],
        "deliveries": [JobDelivery(**_delivery_of(d, run_url_template)) for d in state.deliveries],
        "syncs": [JobSync(**_sync_of(s)) for s in state.syncs],
        "timeline": [
            JobAttempt(
                attempt=attempt.number,
                outcome=attempt.outcome,  # type: ignore[arg-type]
                events=[
                    JobEvent(
                        type=event.type,  # type: ignore[arg-type]
                        at=utc(event.at),
                        actor=event.actor,
                        failure_code=event.failure_code,
                        stage=event.stage,  # type: ignore[arg-type]
                        delivery_id=event.delivery_id,
                        model=event.model,
                        prompt_version=event.prompt_version,
                    )
                    for event in attempt.events
                ],
            )
            for attempt in state.timeline
        ],
    }


class JobState(BaseModel):
    """O estado de um pedido, sem nada que diga quem é a lead."""

    public_id: str = Field(
        description="O código do pedido, o mesmo que a lead e o webhook usam.",
        examples=["k3JdQ8s_Lw0vX2aB"],
    )
    status: JobStatus = Field(description=STATUS_TEXT)
    stage: JobStage = Field(description=STAGE_TEXT)
    attempt: int = Field(
        description="A tentativa atual (1 no início; cada reprocessamento soma 1).", examples=[1]
    )
    form_slug: str = Field(description="O endereço do formulário que a lead respondeu.")
    form_version: str = Field(
        description="A versão do formulário com que ela respondeu, no texto que o webhook leva.",
        examples=["2027-01-v3"],
    )
    failure_code: str | None = Field(
        description="Se o pedido falhou, o código da falha. Nulo se não falhou. O texto do motivo "
        "só sai em `lead_data.notes`.",
        examples=["llm_unavailable"],
    )
    created_at: AwareDatetime = Field(description="Quando o pedido chegou (ISO 8601, UTC).")
    completed_at: AwareDatetime | None = Field(
        description="Quando a tentativa atual terminou com sucesso. Nulo se não terminou."
    )
    link_expires_at: AwareDatetime = Field(
        description="Até quando o link de resultado da lead vale (ISO 8601, UTC)."
    )
    link_expired: bool = Field(description="O link da lead já passou do prazo, agora.")
    run_url: str | None = Field(
        description="O link da execução no painel do Hatchet, se o ambiente tem o modelo do link."
    )
    pdfs: list[JobPdf] = Field(description="Os PDFs, da tentativa mais nova para a mais antiga.")
    deliveries: list[JobDelivery] = Field(
        description="As entregas ao webhook, da mais antiga para a mais nova."
    )
    syncs: list[JobSync] = Field(
        description="As sincronizações com a planilha, da mais nova para a mais antiga."
    )
    timeline: list[JobAttempt] = Field(
        description="A linha do tempo, tentativa por tentativa, sem texto livre."
    )

    @classmethod
    def from_state(cls, state: queries.JobState, run_url_template: str | None) -> JobState:
        return cls(**_state_fields(state, run_url_template))


class JobDetail(JobState):
    """Um pedido: o estado e, só se pedido, o dado da lead."""

    lead_data: JobLeadData | None = Field(
        description="O dado da lead. Nulo, a menos que a chamada peça `include_lead_data=true` e a "
        "chave tenha o papel `total`."
    )

    @classmethod
    def of(cls, detail: queries.JobDetail, run_url_template: str | None) -> JobDetail:
        return cls(
            **_state_fields(detail.state, run_url_template),
            lead_data=JobLeadData.of(detail.lead) if detail.lead is not None else None,
        )


# --- as ações: reprocessar e reenviar -----------------------------------------------------------

LAUNCHED_TEXT = (
    "`true`: o motor de execução (Hatchet) recebeu o disparo. `false`: ele não respondeu a tempo, o "
    "que **não é erro**: a ação vale, o item ficou pendente sem execução e o reconciliador o dispara "
    "em até poucos minutos. Não repita a chamada (seria `409 state_conflict`)."
)


class ReprocessJobRequest(ConfirmedInput):
    """O pedido de uma nova tentativa. Como a ação dispara trabalho e chama o webhook de novo, exige
    `confirm: true`."""

    reason: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=REASON_MIN_LENGTH, max_length=REASON_MAX_LENGTH
        ),
    ] = Field(
        description=f"Por que reprocessar, de {REASON_MIN_LENGTH} a {REASON_MAX_LENGTH} caracteres "
        "(depois de aparado). Texto livre, que fica na auditoria em nome da chave: não cite o nome "
        "nem o contato da lead.",
        examples=["Prompt do formulário corrigido pela equipe"],
    )
    expected_attempt: int = Field(
        ge=1,
        description="A tentativa atual que você leu (o `attempt` de `get_job`). Se o pedido já está "
        "noutra, a resposta é `409 state_conflict` e nada acontece: é o que impede uma segunda "
        "tentativa quando a chamada é repetida depois de a resposta se perder.",
        examples=[1],
    )


class ReprocessedJob(JobState):
    """O pedido depois do reprocessamento: o estado novo (`attempt` é a tentativa criada, e `status`
    é `queued`, ou já adiante, se o motor foi rápido) e se o disparo chegou ao motor."""

    launched: bool = Field(description=LAUNCHED_TEXT)

    @classmethod
    def with_launch(
        cls, state: queries.JobState, run_url_template: str | None, *, launched: bool
    ) -> ReprocessedJob:
        return cls(**_state_fields(state, run_url_template), launched=launched)


class ResendRequest(ConfirmedInput):
    """O pedido de reenvio de uma entrega ou de uma sincronização. Chama o destino de novo, por isso
    exige `confirm: true`."""


class ResentDelivery(JobDelivery):
    """A entrega depois do reenvio: de volta para `pending` (ou já adiante, se o motor foi rápido) e
    se o disparo chegou ao motor."""

    launched: bool = Field(description=LAUNCHED_TEXT)

    @classmethod
    def with_launch(
        cls, delivery: queries.DeliveryState, run_url_template: str | None, *, launched: bool
    ) -> ResentDelivery:
        return cls(**_delivery_of(delivery, run_url_template), launched=launched)


class ResentSync(JobSync):
    """A sincronização depois do reenvio: de volta para `pending` (ou já adiante, se o motor foi
    rápido) e se o disparo chegou ao motor."""

    launched: bool = Field(description=LAUNCHED_TEXT)

    @classmethod
    def with_launch(cls, sync: queries.SyncState, *, launched: bool) -> ResentSync:
        return cls(**_sync_of(sync), launched=launched)
