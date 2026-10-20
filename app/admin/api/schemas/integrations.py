"""Os modelos da integração com o Baserow: o que `get_form_integration`, `connect_form_integration`,
`save_form_integration_mapping`, `activate_form_integration`, `deactivate_form_integration`,
`remove_form_integration`, `send_form_integration_test` e
`resend_failed_form_integration_syncs` recebem e devolvem.

Dados, não telas: os códigos do banco e do Baserow (`single_select`, `read_only`, `lead.name`,
`answer:tamanho_equipe`), as datas em ISO 8601 UTC e nenhum rótulo.

**O token do Baserow só entra** (na conexão): nenhum modelo de resposta o traz, e a leitura diz só
`token_set` e `token_unreadable`. Nenhum exemplo parece um segredo.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, SecretStr, field_validator

from app.admin import launching
from app.admin.actions import integrations as actions
from app.admin.api.common import ConfirmedInput, VersionedInput, utc
from app.admin.api.schemas.capture_deliveries import CaptureDeliveryState
from app.admin.queries import integrations as queries
from app.integrations import mapping, push, syncs
from app.integrations.config import StoredIntegration

# --- a conexão ----------------------------------------------------------------------------------


class BaserowConnection(BaseModel):
    """A conexão do formulário com uma tabela do Baserow, como está guardada. **Nunca traz o token**:
    `token_set` diz que há um e `token_unreadable`, se ele ainda abre."""

    base_url: str = Field(
        description="O endereço do Baserow, sem barra no fim.",
        examples=["https://baserow.example.test"],
    )
    host: str = Field(description="O host do endereço.", examples=["baserow.example.test"])
    table_id: int = Field(
        description="O número da tabela (o que vem depois de `/table/` no endereço dela).",
        examples=[7],
    )
    version: str = Field(
        description="A versão da integração: um texto opaco que muda a cada mudança (conexão, "
        "mapeamento, ativação). Mande-o em `expected_version` ao escrever. Antes de a integração "
        "existir, a versão é `0`.",
        examples=["3"],
    )
    active: bool = Field(
        description="`true`: cada pedido concluído do formulário vira uma linha na tabela. Uma "
        "integração nasce desativada."
    )
    token_set: bool = Field(
        description="Há um token guardado. Sempre `true` numa integração que existe: o token é "
        "obrigatório ao conectar. O token em si nunca volta."
    )
    token_unreadable: bool = Field(
        description="O token guardado **não abre** com a `WEBHOOK_ENCRYPTION_KEY` atual (a chave foi "
        "trocada sem manter a antiga). Ninguém lê a tabela nem sincroniza até alguém mandar o token "
        "de novo em `connect_form_integration`."
    )
    capture_mapped_fields: int = Field(
        default=0, description="Campos do perfil de captura, separado do perfil PDF."
    )
    capture_key_field_id: int | None = Field(
        default=None, description="Campo que identifica a submissão no perfil captura."
    )
    mapped_fields: int = Field(
        description="Quantos campos da tabela o mapeamento guardado preenche."
    )
    key_field_id: int | None = Field(
        description="O campo da tabela que guarda o código do pedido, por onde a sincronização "
        "acha a linha de volta. Nulo enquanto nada foi mapeado."
    )
    created_by: str = Field(description="Quem conectou: o usuário do admin ou `chave:<nome>`.")
    created_at: AwareDatetime = Field(description="Quando a integração foi criada (ISO 8601, UTC).")
    updated_by: str = Field(description="Quem fez a última mudança.")
    updated_at: AwareDatetime = Field(description="Quando foi a última mudança.")
    token_changed_at: AwareDatetime = Field(
        description="Quando o token foi colado pela última vez (a rotação)."
    )

    @classmethod
    def of(cls, stored: StoredIntegration, *, token_unreadable: bool) -> BaserowConnection:
        return cls(
            base_url=stored.base_url,
            host=stored.host,
            table_id=stored.table_id,
            version=str(stored.version),
            active=stored.active,
            token_set=True,
            token_unreadable=token_unreadable,
            mapped_fields=len(stored.entries),
            capture_mapped_fields=len(stored.capture_entries),
            capture_key_field_id=stored.capture_key_field_id,
            key_field_id=stored.key_field_id,
            created_by=stored.created_by,
            created_at=utc(stored.created_at),
            updated_by=stored.updated_by,
            updated_at=utc(stored.updated_at),
            token_changed_at=utc(stored.token_changed_at),
        )


class ConnectFormIntegrationRequest(ConfirmedInput, VersionedInput):
    """A conexão a gravar. O token só entra: nenhuma resposta o devolve. Como reconectar a outro
    endereço ou a outra tabela apaga o mapeamento e desativa a integração, a operação exige
    `confirm: true` em toda chamada."""

    base_url: str = Field(
        description="O endereço do Baserow: `https` (em produção só `https`), sem usuário e senha, "
        "sem `?` nem `#` e sem IP link-local. Barras no fim são ignoradas.",
        examples=["https://baserow.example.test"],
    )
    table_id: int = Field(
        ge=1,
        le=actions.MAX_TABLE_ID,
        description="O número da tabela: o que vem depois de `/table/` no endereço dela no Baserow.",
        examples=[7],
    )
    token: SecretStr | None = Field(
        default=None,
        description="O **token de banco de dados** do Baserow, com permissão de leitura, criação e "
        "atualização na tabela. **Obrigatório ao criar** (`expected_version` `0`); numa integração "
        "que existe, em branco (ou ausente, ou nulo) **mantém o token guardado**. O token só entra: "
        "nunca volta em resposta, log, auditoria nem mensagem de erro.",
    )


class BaserowMappedField(BaseModel):
    """Um campo da tabela e a fonte que o preenche, como o mapeamento foi guardado."""

    field_id: int = Field(description="O id do campo no Baserow.", examples=[101])
    name: str = Field(description="O nome do campo quando o mapeamento foi guardado.")
    type: str = Field(
        description="O tipo do campo quando o mapeamento foi guardado (`text`, `long_text`, `email`, "
        "`phone_number`, `url`, `boolean`, `single_select` ou `multiple_select`).",
        examples=["single_select"],
    )
    source: str = Field(
        description="A chave da fonte que o preenche: `lead.name`, `lead.email`, `lead.phone`, "
        "`job.public_id` (o código do pedido, a chave da sincronização), `consent.privacy`, "
        "`answer:<pergunta>` (a resposta de uma pergunta do formulário) e as outras que "
        "`get_form_integration` lista em `sources` de cada campo.",
        examples=["lead.name", "answer:tamanho_equipe"],
    )

    @classmethod
    def of(cls, entry: mapping.Entry) -> BaserowMappedField:
        return cls(field_id=entry.field_id, name=entry.name, type=entry.type, source=entry.source)


# --- a tabela, lida agora -----------------------------------------------------------------------


class BaserowTableField(BaseModel):
    """Um campo da tabela como o Baserow o descreve **agora**, com o que o preenche e as fontes que
    servem a ele."""

    id: int = Field(description="O id do campo no Baserow.", examples=[101])
    name: str = Field(description="O nome do campo.")
    type: str = Field(description="O tipo do campo no Baserow.", examples=["single_select"])
    primary: bool = Field(description="É o campo primário da tabela.")
    unsupported: str | None = Field(
        description="Por que a integração não preenche este campo: `read_only` (o Baserow o calcula, "
        "como uma data de criação) ou `unsupported_type` (o tipo não é um dos que a integração "
        "preenche). Nulo: preenche. Um campo assim não oferece fontes e não entra no mapeamento.",
        examples=["read_only"],
    )
    sources: list[str] = Field(
        description="As chaves das fontes que servem ao tipo do campo, na ordem do catálogo do "
        "formulário: um campo de texto aceita qualquer uma, um sim ou não vem de um consentimento e "
        "uma seleção vem de uma pergunta de escolha. Vazio num campo que não se preenche."
    )
    source: str | None = Field(
        description="A fonte guardada no mapeamento para este campo. Nulo: não está mapeado."
    )
    suggested_source: str | None = Field(
        description="A fonte que o **nome do campo** sugere, enquanto nada foi mapeado (a página a "
        "mostra como sugestão): ainda não foi salva. Nulo quando há mapeamento guardado ou o nome "
        "não sugere nada."
    )

    @classmethod
    def of(cls, choice: queries.FieldChoice) -> BaserowTableField:
        return cls(
            id=choice.id,
            name=choice.name,
            type=choice.type,
            primary=choice.primary,
            unsupported=choice.unsupported,
            sources=list(choice.sources),
            source=choice.selected if choice.selected and not choice.suggested else None,
            suggested_source=choice.selected if choice.suggested else None,
        )


class BaserowOptionPair(BaseModel):
    """Uma opção de pergunta do formulário e a opção do campo de seleção do Baserow que ela alcança."""

    value: str = Field(description="O valor da opção no formulário.", examples=["so_eu"])
    label: str = Field(
        description="O rótulo que a lead viu. O casamento com o Baserow é pelo rótulo, sem "
        "diferença de maiúsculas, acentos, espaços e travessões.",
        examples=["Só eu"],
    )
    baserow_option: str | None = Field(
        description="A opção do Baserow que ela alcança. Nulo: **sem par** — a lead que escolher esta "
        "opção não gera linha (a sincronização falha) até a opção existir no Baserow."
    )


class BaserowReportField(BaseModel):
    """Um campo mapeado no relatório. Nos campos de seleção, `options` mostra o casamento de cada
    opção da pergunta."""

    field_id: int = Field(description="O id do campo no Baserow.")
    name: str = Field(description="O nome do campo.")
    type: str = Field(description="O tipo do campo.")
    source: str = Field(description="A chave da fonte que o preenche.")
    options: list[BaserowOptionPair] = Field(
        description="Só nos campos de seleção preenchidos por uma pergunta; vazio nos outros."
    )


class BaserowReport(BaseModel):
    """O relatório do mapeamento guardado contra a tabela de agora: o que se confere antes de ativar.
    Uma integração só ativa com `ok` verdadeiro."""

    ok: bool = Field(
        description="`true`: há campos mapeados, nenhum problema e toda opção tem par."
    )
    problems: list[str] = Field(
        description="O que impede a sincronização: um campo que sumiu ou mudou de tipo, uma fonte que "
        "a versão atual do formulário não tem, o código do pedido sem campo. Frases em português."
    )
    unmatched_options: int = Field(
        description="Quantas opções do formulário não têm par no Baserow (somando os campos)."
    )
    fields: list[BaserowReportField] = Field(description="Os campos mapeados que valem, em ordem.")

    @classmethod
    def of(cls, report: mapping.Report) -> BaserowReport:
        return cls(
            ok=report.ok,
            problems=list(report.problems),
            unmatched_options=report.unmatched,
            fields=[
                BaserowReportField(
                    field_id=item.entry.field_id,
                    name=item.entry.name,
                    type=item.entry.type,
                    source=item.entry.source,
                    options=[
                        BaserowOptionPair(
                            value=pair.value,
                            label=pair.label,
                            baserow_option=pair.baserow.value if pair.baserow else None,
                        )
                        for pair in item.pairs
                    ],
                )
                for item in report.fields
            ],
        )


# --- as sincronizações --------------------------------------------------------------------------


class BaserowSyncCounts(BaseModel):
    """Quantas sincronizações da integração estão em cada estado."""

    pending: int = Field(description="À espera do envio (ou de nova tentativa).")
    succeeded: int = Field(description="Já viraram uma linha na tabela.")
    failed: int = Field(
        description="Esgotaram as tentativas. Reenvie-as depois de corrigir o token, a tabela ou o "
        "mapeamento."
    )


class BaserowFailedSync(BaseModel):
    """Uma sincronização que falhou. O texto livre do erro não vem aqui: está em `get_job`
    (`lead_data`, papel `total`)."""

    id: uuid.UUID = Field(description="O id da sincronização.")
    job_public_id: str = Field(description="O código do pedido a que ela pertence.")
    attempt: int = Field(
        description="A tentativa do pedido a que o PDF desta sincronização pertence."
    )
    attempts: int = Field(description="Quantas vezes o envio foi tentado.")
    failure_code: str | None = Field(
        description="O código da falha (`integration_unavailable`, `integration_rejected`, "
        "`integration_unconfigured` ou `integration_mapping`). Nulo se não ficou registrado.",
        examples=["integration_rejected"],
    )
    created_at: AwareDatetime = Field(description="Quando a sincronização foi criada.")

    @classmethod
    def of(cls, row: syncs.SyncRow) -> BaserowFailedSync:
        return cls(
            id=row.id,
            job_public_id=row.job_public_id,
            attempt=row.job_attempt,
            attempts=row.attempts,
            failure_code=queries.failure_code_of(row.last_error),
            created_at=utc(row.created_at),
        )


# --- a leitura ----------------------------------------------------------------------------------


class BaserowIntegration(BaseModel):
    """A integração de um formulário com uma tabela do Baserow, com a tabela **lida agora**."""

    capture_deliveries: list[CaptureDeliveryState] = Field(
        default_factory=list,
        description="Entregas de captura por destino; sem dados pessoais ou erro bruto. Retry individual usa retry_capture_delivery.",
    )
    profile: str = Field(
        default="pdf", description="Finalidade do perfil consultado: pdf ou capture."
    )
    capture_mapping: list[BaserowMappedField] = Field(
        default_factory=list, description="Perfil de captura preservado independentemente do PDF."
    )
    form_slug: str = Field(description="O endereço do formulário.")
    published: bool = Field(
        description="O formulário tem versão publicada. Sem ela não há perguntas para mapear, e "
        "`connect_form_integration` e `save_form_integration_mapping` recusam."
    )
    connected: bool = Field(
        description="O formulário tem uma integração (`connection` não é nulo)."
    )
    connection: BaserowConnection | None = Field(
        description="A conexão guardada, sem o token. Nulo: o formulário não tem integração."
    )
    mapping: list[BaserowMappedField] = Field(
        description="O mapeamento guardado, um item por campo preenchido. Vem do banco: está aqui "
        "mesmo quando o Baserow não respondeu."
    )
    fields: list[BaserowTableField] | None = Field(
        description="Os campos da tabela **como o Baserow os descreve agora**, na ordem dele. Nulo "
        "quando não há conexão ou versão publicada, ou quando a tabela não foi lida "
        "(`fields_error` diz por quê)."
    )
    fields_error: str | None = Field(
        description="Por que `fields` e `report` não vieram: o Baserow não respondeu ou recusou o "
        "token (com o status), ou o token guardado não abre. Nulo quando a tabela foi lida."
    )
    report: BaserowReport | None = Field(
        description="O relatório do mapeamento guardado contra a tabela de agora. Nulo quando nada "
        "está mapeado ou a tabela não foi lida."
    )
    sync_counts: BaserowSyncCounts = Field(description="As sincronizações por estado.")
    failed_syncs: list[BaserowFailedSync] = Field(
        description="As sincronizações que falharam mais recentes (até 10)."
    )

    @classmethod
    def of(cls, detail: queries.IntegrationDetail) -> BaserowIntegration:
        stored = detail.stored
        return cls(
            profile=detail.profile,
            capture_deliveries=list(detail.capture_deliveries),
            capture_mapping=[BaserowMappedField.of(entry) for entry in stored.capture_entries]
            if stored
            else [],
            form_slug=detail.slug,
            published=detail.published,
            connected=stored is not None,
            connection=BaserowConnection.of(stored, token_unreadable=detail.token_unreadable)
            if stored is not None
            else None,
            mapping=[BaserowMappedField.of(entry) for entry in stored.entries] if stored else [],
            fields=[BaserowTableField.of(choice) for choice in detail.fields]
            if detail.fields is not None
            else None,
            fields_error=detail.read_error,
            report=BaserowReport.of(detail.report) if detail.report is not None else None,
            sync_counts=BaserowSyncCounts(
                pending=detail.counts.pending,
                succeeded=detail.counts.succeeded,
                failed=detail.counts.failed,
            ),
            failed_syncs=[BaserowFailedSync.of(row) for row in detail.failures],
        )


# --- o mapeamento -------------------------------------------------------------------------------


class SaveFormIntegrationMappingRequest(VersionedInput):
    """O mapeamento a gravar: a fonte de cada campo da tabela."""

    profile: Literal["pdf", "capture"] | None = Field(
        default=None,
        description="Omitido usa a finalidade publicada. Pode preparar o outro perfil explicitamente.",
    )
    sources: dict[str, str] = Field(
        description="A fonte que preenche cada campo da tabela, pelo **id do campo no Baserow** "
        '(o `id` de cada item de `fields` em `get_form_integration`): `{"101": "lead.name"}`. '
        "Só entram os campos que a integração vai preencher; um campo ausente (ou com a fonte em "
        "branco) fica sem mapeamento, e **um id que a tabela não tem é ignorado**. As chaves são "
        "números inteiros positivos na forma canônica (`101`, e não `0101`), com no máximo 10 "
        "algarismos. As fontes que servem a cada campo estão em `sources` de `fields`. **Precisa de "
        "um campo, e só um, com `job.public_id`** (o código do pedido, de texto ou texto longo): é "
        "por ele que a sincronização acha a linha de volta. O mapeamento é conferido contra a "
        "tabela de agora e contra as perguntas da versão publicada.",
        examples=[{"101": "lead.name", "104": "job.public_id", "108": "answer:tamanho_equipe"}],
        json_schema_extra={"propertyNames": {"pattern": "^[1-9][0-9]*$"}},
    )

    @field_validator("sources", mode="before")
    @classmethod
    def _check_sources(cls, value: Any) -> Any:
        """As chaves são ids de campo (números inteiros positivos, na forma canônica: `101`, e não
        `0101`) e cada fonte é um texto. A mensagem não repete o que veio."""
        if not isinstance(value, dict):
            return value  # o Pydantic recusa: "Informe um objeto."
        for key, source in value.items():
            # `field_number` confere o tamanho antes do `int()`: uma chave com milhares de algarismos
            # é um `422` em português, e não um `500`. Na forma canônica, `101` e `0101` não colidem.
            number = actions.field_number(key) if isinstance(key, str) else None
            if number is None or str(number) != key:
                raise ValueError(
                    "As chaves de `sources` são os ids dos campos da tabela: números inteiros "
                    "positivos, como `101`."
                )
            if not isinstance(source, str):
                # ValueError, e não TypeError: o Pydantic só leva o ValueError ao cliente como 422.
                raise ValueError(  # noqa: TRY004
                    "Cada fonte é um texto: a chave da fonte, como `lead.name`."
                )
        return value

    def chosen(self) -> dict[int, str]:
        """As fontes pelo id do campo, como a ação as recebe."""
        return {int(key): source for key, source in self.sources.items()}


class BaserowMapping(BaseModel):
    """O mapeamento como ficou guardado, com o relatório contra a tabela de agora."""

    profile: Literal["pdf", "capture"] = Field(
        default="pdf",
        description="Perfil que foi salvo; a chave e as entradas abaixo pertencem a ele.",
    )
    version: str = Field(
        description="A versão nova da integração: mande-a em `expected_version` na próxima escrita.",
        examples=["3"],
    )
    active: bool = Field(
        description="A integração está ativa depois desta gravação. Um mapeamento com pendências "
        "desativa uma integração ativa."
    )
    deactivated: bool = Field(
        description="`true`: a integração estava ativa e **esta gravação a desativou**, porque o "
        "relatório do mapeamento novo tem pendências (`report.ok` falso). Corrija-as e ative de novo."
    )
    key_field_id: int | None = Field(
        description="O campo da tabela que guarda o código do pedido (a chave da sincronização)."
    )
    entries: list[BaserowMappedField] = Field(
        description="O mapeamento guardado, um item por campo."
    )
    report: BaserowReport = Field(
        description="O relatório do mapeamento novo contra a tabela de agora: o que se confere antes "
        "de ativar."
    )

    @classmethod
    def of(cls, saved: actions.MappingSaved) -> BaserowMapping:
        integration = saved.integration
        return cls(
            profile=saved.profile,
            version=str(integration.version),
            active=integration.active,
            deactivated=saved.deactivated,
            key_field_id=integration.capture_key_field_id
            if saved.profile == "capture"
            else integration.key_field_id,
            entries=[
                BaserowMappedField.of(entry)
                for entry in (
                    integration.capture_entries
                    if saved.profile == "capture"
                    else integration.entries
                )
            ],
            report=BaserowReport.of(saved.report),
        )


# --- ativar e desativar -------------------------------------------------------------------------


class ActivateFormIntegrationRequest(ConfirmedInput, VersionedInput):
    """O pedido de ativação. Ativar faz cada pedido que ficar pronto daqui em diante virar uma linha na
    planilha do cliente, por isso a operação é marcada e exige `confirm: true` em toda chamada."""


class DeactivateFormIntegrationRequest(VersionedInput):
    """O pedido de desativação: só a versão que você viu. Desligar não tem marca: não escreve fora do
    sistema, e as sincronizações que repetiam esperam a reativação."""


# --- remover ------------------------------------------------------------------------------------


class RemovedFormIntegration(BaseModel):
    """A integração que saiu, como estava, e o estado das sincronizações que saíram com ela. O token
    nunca vem. As linhas que já estavam na tabela do Baserow ficam lá."""

    form_slug: str = Field(description="O endereço do formulário.", examples=["demo"])
    host: str = Field(
        description="O host do Baserow a que a integração estava ligada.",
        examples=["baserow.example.test"],
    )
    table_id: int = Field(description="O número da tabela a que ela estava ligada.", examples=[7])
    was_active: bool = Field(
        description="A integração estava ativa quando foi removida: a partir de agora, os pedidos "
        "novos do formulário deixam de virar linha."
    )
    mapped_fields: int = Field(description="Quantos campos o mapeamento apagado preenchia.")
    sync_counts: BaserowSyncCounts = Field(
        description="As sincronizações que existiam, por estado, e que o estado apagado levou "
        "consigo. Quem remove vê aqui o que ficou sem registro (`failed` não será reenviado)."
    )

    @classmethod
    def of(cls, removed: actions.Removed) -> RemovedFormIntegration:
        integration, counts = removed.integration, removed.sync_counts
        return cls(
            form_slug=integration.form_slug,
            host=integration.host,
            table_id=integration.table_id,
            was_active=integration.active,
            mapped_fields=len(integration.entries),
            sync_counts=BaserowSyncCounts(
                pending=counts.pending, succeeded=counts.succeeded, failed=counts.failed
            ),
        )


# --- o envio de teste ---------------------------------------------------------------------------


class SendFormIntegrationTestRequest(ConfirmedInput):
    """O pedido do envio de teste. Escreve (ou atualiza) uma linha na planilha do cliente, por isso a
    operação é marcada e exige `confirm: true` em toda chamada."""

    profile: Literal["pdf", "capture"] | None = Field(
        default=None,
        description="Perfil do teste sintético; omitido usa a finalidade publicada. Não cria submissão real.",
    )


class SentFormIntegrationTest(BaseModel):
    """O que o envio de teste fez. **Um teste que o Baserow recusou não é um erro da chamada**: a resposta
    é `200` com `ok` falso e o motivo em `error`."""

    ok: bool = Field(
        description="`true`: a linha de teste está na tabela. `false`: o teste não passou, e `error` "
        "diz por quê (o token foi recusado, a tabela mudou, o mapeamento não serve)."
    )
    public_id: str = Field(
        description="O código do pedido da linha de teste, fixo por formulário: testar de novo "
        "atualiza esta mesma linha, e o app não a apaga.",
        examples=["teste-demo"],
    )
    outcome: str | None = Field(
        description="`created`: a linha de teste não existia e foi criada. `updated`: já existia (de "
        "um teste anterior) e foi atualizada. Nulo quando o teste não passou.",
        examples=["created"],
    )
    row_id: int | None = Field(
        description="O id da linha na tabela do Baserow. Nulo quando o teste não passou.",
        examples=[12],
    )
    error: str | None = Field(
        description="Por que o teste não passou, em frases: a etapa, o status e o código de erro do "
        "Baserow, e os nomes de campos e perguntas, que são configuração. Nunca o token nem dado de "
        "lead. Nulo quando passou."
    )

    @classmethod
    def of(cls, result: push.SampleResult) -> SentFormIntegrationTest:
        return cls(
            ok=result.ok,
            public_id=result.public_id,
            outcome=result.outcome,
            row_id=result.row_id,
            error=result.error,
        )


# --- o reenvio em lote --------------------------------------------------------------------------


class ResendFailedFormIntegrationSyncsRequest(ConfirmedInput):
    """O pedido do reenvio em lote. Cada sincronização reenviada escreve uma linha na planilha do
    cliente, por isso a operação é marcada e exige `confirm: true` em toda chamada."""


class ResentFailedFormIntegrationSyncs(BaseModel):
    """Quantas sincronizações voltaram para a fila e se o disparo chegou ao engine."""

    resent: int = Field(
        description="Quantas sincronizações que falharam voltaram para a fila (`pending`): até `limit`, "
        "as mais antigas primeiro. Zero: nenhuma tinha falhado, e nada foi gravado nem disparado."
    )
    limit: int = Field(
        description="O máximo que uma chamada reenvia. Se `still_failed` não é zero, repita a "
        "chamada: ela leva as que sobraram.",
        examples=[200],
    )
    still_failed: int = Field(
        description="Quantas sincronizações da integração continuam falhadas depois desta chamada: as "
        "que passaram do limite."
    )
    launched: bool = Field(
        description="`true`: o engine de execução recebeu o disparo da primeira sincronização, e as "
        "outras seguem em segundo plano, uma a uma. `false`: o engine não respondeu a tempo (ou "
        "`resent` é zero). **`false` não é erro**: as sincronizações voltaram para a fila de "
        "qualquer modo, e o reconciliador dispara o que ficar sem execução em até poucos minutos; "
        "não reenvie de novo."
    )

    @classmethod
    def of(cls, done: actions.FailedResent, *, launched: bool) -> ResentFailedFormIntegrationSyncs:
        return cls(
            resent=len(done.sync_ids),
            limit=launching.RESEND_FAILED_LIMIT,
            still_failed=done.still_failed,
            launched=launched,
        )
