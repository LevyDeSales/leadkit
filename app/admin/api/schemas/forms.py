"""Os modelos dos formulários: o que `list_forms`, `get_form`, `activate_form`, `deactivate_form`,
`get_form_retention`, `save_form_retention`, `get_form_version` e `compare_form_versions` recebem e
devolvem.

Dados, não telas: os códigos do banco, as datas em ISO 8601 UTC e nenhum rótulo. Nenhum modelo traz a
URL de um destino (o caminho dela costuma valer como senha: só o host), o token da integração nem o
dado de uma lead (as contagens de envios, sim).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.admin.actions import versions as version_actions
from app.admin.api.common import ConfirmedInput, Page, VersionedInput, utc
from app.admin.queries import forms as queries
from app.forms import drafts
from app.forms import retention as form_retention
from app.forms.definition.changes import SectionChange
from app.forms.definition.document import DefinitionDocument as FormDocument
from app.forms.stored import StoredVersion, VersionEntry

# --- a lista e o detalhe ------------------------------------------------------------------------


class FormSummary(BaseModel):
    """Um formulário na lista."""

    slug: str = Field(
        description="O endereço do formulário: o que identifica o formulário em toda operação e vai "
        "na URL pública (`/p/<slug>`).",
        examples=["diagnostico-rapido"],
    )
    title: str = Field(description="O nome do formulário no admin.")
    active: bool = Field(
        description="`true`: recebe envios no endereço público e na API pública. `false`: a página "
        "diz à lead que ele está indisponível e a API pública recusa envios novos."
    )
    public_url: str = Field(
        description="O endereço público do formulário, o que vai nos anúncios.",
        examples=["https://planos.example.test/p/diagnostico-rapido"],
    )
    published_version: int | None = Field(
        description="O número da versão publicada atual (a de maior número, a que recebe os envios "
        "novos). Nulo: nenhuma versão foi publicada (formulário criado do zero ou duplicado).",
        examples=[2],
    )
    published_at: AwareDatetime | None = Field(
        description="Quando a versão atual foi publicada (ISO 8601, UTC). Nulo, sem versão."
    )
    version_compiles: bool | None = Field(
        description="A versão atual ainda compila com o código deste deploy? `false`: um deploy com "
        "o compilador mais rígido a recusou; ela fica fora do ar até alguém publicar uma nova "
        "(o log diz qual). Nulo: não há versão publicada."
    )
    draft_pending: bool = Field(
        description="Há mudança no rascunho que ainda não foi publicada (antes da versão 1, o "
        "rascunho inteiro; depois, um texto que difere da versão atual ou uma imagem trocada)."
    )
    data_notices: list[str] = Field(
        description="O que conferir nos dados do formulário (por exemplo, uma janela de datas que já "
        "terminou), em português. Vazio: tudo em dia."
    )
    submissions: int = Field(
        description="Quantos envios o formulário tem no banco. A retenção apaga os antigos.",
        examples=[12],
    )

    @classmethod
    def of(cls, row: queries.FormSummary) -> FormSummary:
        return cls(
            slug=row.slug,
            title=row.title,
            active=row.active,
            public_url=row.public_url,
            published_version=row.version_number,
            published_at=utc(row.published_at) if row.published_at else None,
            version_compiles=row.compiles,
            draft_pending=row.draft_pending,
            data_notices=list(row.notices),
            submissions=row.submissions,
        )


class FormPage(Page[FormSummary]):
    """Uma página de formulários, por endereço."""


class FormVersionEntry(BaseModel):
    """Uma versão publicada no histórico do formulário. A definição se lê com `get_form_version`."""

    number: int = Field(description="O número da versão.", examples=[2])
    current: bool = Field(description="É a versão atual (a de maior número).")
    published_by: str = Field(
        description="Quem publicou: o usuário do admin, ou `chave:<nome>` se foi uma chave.",
        examples=["operadora"],
    )
    published_at: AwareDatetime = Field(description="Quando foi publicada (ISO 8601, UTC).")
    note: str = Field(description="A nota da publicação.")
    fingerprint: str = Field(
        description="A impressão digital (SHA-256) da definição da versão: duas versões com a mesma "
        "impressão dizem exatamente o mesmo."
    )

    @classmethod
    def of(cls, entry: VersionEntry, *, current: int | None) -> FormVersionEntry:
        return cls(
            number=entry.number,
            current=entry.number == current,
            published_by=entry.published_by,
            published_at=utc(entry.published_at),
            note=entry.note,
            fingerprint=entry.fingerprint,
        )


class DraftState(BaseModel):
    """O estado do rascunho do formulário: se há mudança pendente, quem salvou e a versão dele."""

    pending: bool = Field(description="Há mudança que ainda não foi publicada.")
    saved_by: str | None = Field(
        description="Quem salvou o rascunho pendente por último. Nulo: não há rascunho salvo."
    )
    saved_at: AwareDatetime | None = Field(
        description="Quando foi salvo (ISO 8601, UTC). Nulo: não há rascunho salvo."
    )
    base_version: int | None = Field(
        description="De que versão o rascunho foi copiado; sem rascunho pendente, a versão atual. "
        "Nulo antes da versão 1."
    )
    current_version: int | None = Field(description="A versão publicada atual. Nulo, sem versão.")
    version: str = Field(
        description="A versão do rascunho, um texto opaco (a impressão digital dele): quem o salva "
        "devolve o que viu em `expected_version`."
    )
    stale_assets: list[str] = Field(
        description="Os nomes das imagens que a versão atual fixou e que foram trocadas desde então, "
        "entre as que o rascunho usa: há o que publicar mesmo sem o texto ter mudado."
    )

    @classmethod
    def of(cls, draft: drafts.Draft) -> DraftState:
        """O estado de um rascunho: o que `get_form` e `get_form_draft` dizem dele."""
        pending = draft.pending
        current_version = draft.current.number if draft.current else None
        return cls(
            pending=pending,
            saved_by=draft.updated_by if pending else None,
            saved_at=utc(draft.updated_at) if pending and draft.updated_at else None,
            base_version=draft.base_version if pending else current_version,
            current_version=current_version,
            version=draft.token,
            stale_assets=list(draft.stale_assets),
        )


class FormDestination(BaseModel):
    """Qual destino do webhook serve o formulário. Nunca a URL: o caminho costuma valer como senha."""

    scope: Literal["form", "default"] = Field(
        description="`form`: o destino é do próprio formulário. `default`: o formulário usa o "
        "destino padrão."
    )
    host: str = Field(description="O host do destino.", examples=["receptor.example.test"])


class FormIntegration(BaseModel):
    """A integração do formulário com a planilha (Baserow), resumida. Nunca traz o token."""

    configured: bool = Field(description="O formulário tem uma integração.")
    active: bool | None = Field(
        description="A integração está ativa (cada pedido concluído vira uma linha na planilha). "
        "Nulo: não há integração."
    )
    host: str | None = Field(
        description="O host da planilha. Nulo: não há integração.",
        examples=["baserow.example.test"],
    )
    table_id: int | None = Field(description="O número da tabela. Nulo: não há integração.")
    pending_syncs: int = Field(description="Quantas sincronizações estão à espera.")
    failed_syncs: int = Field(description="Quantas sincronizações esgotaram as tentativas.")


class RetentionDays(BaseModel):
    """Os dois prazos de retenção, em dias, sempre com valor."""

    pdf_days: int = Field(
        description="Por quantos dias o arquivo do PDF fica guardado depois de gerado.",
        examples=[30],
    )
    request_days: int = Field(
        description="Por quantos dias o pedido inteiro (lead, respostas, plano) fica guardado depois "
        "da última mudança.",
        examples=[60],
    )


class OwnRetention(BaseModel):
    """Os prazos que o próprio formulário define; nulo é o padrão do ambiente."""

    pdf_days: int | None = Field(
        description="O prazo do PDF do formulário. Nulo: vale o padrão do ambiente."
    )
    request_days: int | None = Field(
        description="O prazo do pedido inteiro do formulário. Nulo: vale o padrão do ambiente."
    )


class RetentionState(BaseModel):
    """Os prazos de retenção de um formulário."""

    own: OwnRetention = Field(description="O que o formulário define por conta própria.")
    default: RetentionDays = Field(
        description="Os padrões do ambiente, onde o formulário não define."
    )
    in_force: RetentionDays = Field(
        description="Os prazos que valem de fato: os do formulário, com o padrão do ambiente onde "
        "falta um."
    )
    version: str = Field(
        description="A versão dos prazos, um texto opaco: `save_form_retention` a devolve em "
        "`expected_version`."
    )

    @classmethod
    def of(cls, retention: queries.Retention) -> RetentionState:
        in_force = retention.in_force
        return cls(
            own=OwnRetention(
                pdf_days=retention.own.pdf_days, request_days=retention.own.request_days
            ),
            default=RetentionDays(
                pdf_days=retention.default_pdf_days, request_days=retention.default_request_days
            ),
            in_force=RetentionDays(pdf_days=in_force.pdf_days, request_days=in_force.request_days),
            version=retention.version,
        )


class FormDetail(FormSummary):
    """Um formulário: tudo o que a página dele junta, sem a leitura da versão (a definição se lê com
    `get_form_version`)."""

    version_label: str | None = Field(
        description="O rótulo da versão atual que vai no envio e no webhook (por exemplo, "
        "`diagnostico-rapido-v2`). Nulo: sem versão, ou ela não compila.",
        examples=["diagnostico-rapido-v2"],
    )
    versions: list[FormVersionEntry] = Field(
        description="As versões publicadas, da mais recente para a mais antiga. Vazio: nenhuma."
    )
    draft: DraftState = Field(description="O estado do rascunho.")
    destination: FormDestination | None = Field(
        description="O destino que serve o formulário. Nulo: os planos dele não são entregues em "
        "lugar nenhum."
    )
    retention: RetentionState = Field(description="Os prazos de retenção.")
    integration: FormIntegration = Field(description="A integração com a planilha.")

    @classmethod
    def of(cls, found: queries.FormDetail) -> FormDetail:
        published = found.published
        integration = found.integration
        return cls(
            slug=found.slug,
            title=found.title,
            active=found.active,
            public_url=found.public_url,
            published_version=published.number if published else None,
            published_at=utc(published.published_at) if published else None,
            version_compiles=published.compiles if published else None,
            draft_pending=found.draft.pending,
            data_notices=list(found.notices),
            submissions=found.submissions,
            version_label=published.text if published else None,
            versions=[
                FormVersionEntry.of(entry, current=published.number if published else None)
                for entry in found.history
            ],
            draft=DraftState.of(found.draft),
            destination=FormDestination(
                scope=found.destination.scope,  # type: ignore[arg-type]
                host=found.destination.host,
            )
            if found.destination
            else None,
            retention=RetentionState.of(found.retention),
            integration=FormIntegration(
                configured=integration.configured,
                active=integration.active,
                host=integration.host,
                table_id=integration.table_id,
                pending_syncs=integration.pending,
                failed_syncs=integration.failed,
            ),
        )


# --- a situação ---------------------------------------------------------------------------------


class ActivateFormRequest(ConfirmedInput):
    """Ativar um formulário. Como a ativação muda o que a lead vê, exige `confirm: true`."""


class DeactivateFormRequest(ConfirmedInput):
    """Desativar um formulário. Como a desativação o tira do ar para as leads, exige
    `confirm: true`."""


# --- a retenção ---------------------------------------------------------------------------------

SEEN_FORMAT = (
    "o formato «pedidos:PDFs:capturas», como 3:5:2 (legado «pedidos:PDFs» equivale a zero capturas)"
)


class FormRetention(RetentionState):
    """Os prazos de retenção de um formulário e o que a mudança confere."""

    slug: str = Field(description="O endereço do formulário.")
    title: str = Field(description="O nome do formulário no admin.")
    link_ttl_days: int = Field(
        description="A validade do link de resultado da lead, em dias: o prazo do PDF não pode ser "
        "menor que ela mais uma folga.",
        examples=[30],
    )
    max_days: int = Field(description="O maior prazo aceito, em dias.", examples=[3650])

    @classmethod
    def of(cls, found: queries.FormRetention) -> FormRetention:
        return cls(
            **RetentionState.of(found.retention).model_dump(),
            slug=found.slug,
            title=found.title,
            link_ttl_days=found.link_ttl_days,
            max_days=found.max_days,
        )


# Os dias de um prazo: um inteiro de 1 ao teto do serviço (`form_retention.MAX_DAYS`), sem coerção
# (`"30"` e `1.5` não valem). `null` é o padrão do ambiente.
Days = Annotated[int | None, Field(strict=True, ge=1, le=form_retention.MAX_DAYS)]


class SaveFormRetentionRequest(ConfirmedInput, VersionedInput):
    """Os prazos novos. Como mudar um prazo vale também para os pedidos que já existem, e reduzi-lo
    apaga dado de lead na próxima retenção, a operação exige `confirm: true`; uma redução exige
    ainda a contagem que ela apagaria, em `seen`."""

    pdf_days: Days = Field(
        description="Por quantos dias o arquivo do PDF fica guardado depois de gerado, de 1 a "
        f"{form_retention.MAX_DAYS}. `null` volta ao padrão do ambiente. Não pode ser menor que a "
        "validade do link da lead mais uma folga (`link_ttl_days` de `get_form_retention`).",
        examples=[45],
    )
    request_days: Days = Field(
        description="Por quantos dias o pedido inteiro (lead, respostas, plano) fica guardado "
        f"depois da última mudança, de 1 a {form_retention.MAX_DAYS}. `null` volta ao padrão do "
        "ambiente. Não pode ser menor que o prazo do PDF que vale.",
        examples=[90],
    )
    seen: str | None = Field(
        default=None,
        max_length=32,
        description="A contagem do que a próxima retenção apagaria com os prazos novos, "
        f"{SEEN_FORMAT} (pedidos inteiros, arquivos de PDF e capturas), do jeito que a recusa "
        "`confirmation_required` a devolveu. Só uma **redução** de prazo (o que vale diminui, "
        "inclusive voltar a um padrão menor) precisa dela: sem ela, ou se a contagem cresceu desde "
        "que foi lida, a resposta é `422 confirmation_required` com a contagem de agora e nada é "
        "gravado. Ausente numa mudança que não reduz.",
        examples=["3:5"],
    )

    @field_validator("seen")
    @classmethod
    def _a_count(cls, value: str | None) -> str | None:
        if value is not None and form_retention.parse_seen(value) is None:
            raise ValueError(f"Use {SEEN_FORMAT}, a contagem que a recusa devolveu.")
        return value


# --- uma versão e a comparação entre versões ----------------------------------------------------


class FormVersionInfo(BaseModel):
    """O que se sabe de uma versão publicada, sem a definição."""

    slug: str = Field(description="O endereço do formulário.")
    number: int = Field(description="O número da versão.", examples=[2])
    published_by: str = Field(
        description="Quem publicou: o usuário do admin, ou `chave:<nome>` se foi uma chave.",
        examples=["operadora"],
    )
    published_at: AwareDatetime = Field(description="Quando foi publicada (ISO 8601, UTC).")
    note: str = Field(description="A nota da publicação.")
    fingerprint: str = Field(
        description="A impressão digital (SHA-256) da definição, como o banco a guarda."
    )

    @classmethod
    def of(cls, found: StoredVersion) -> FormVersionInfo:
        return cls(
            slug=found.slug,
            number=found.number,
            published_by=found.published_by,
            published_at=utc(found.published_at),
            note=found.note,
            fingerprint=found.fingerprint,
        )


class FormVersion(FormVersionInfo):
    """Uma versão publicada com a definição. A definição é o JSON **como o banco a guarda** (o que a
    exportação da página escreve): importada em outro ambiente, dá o mesmo formulário."""

    definition: FormDocument = Field(
        description="A definição da versão, exatamente como está guardada. O schema `FormDocument` "
        "descreve o formato que o compilador de hoje aceita, e **não garante o texto guardado**: ele "
        "não é validado na resposta, e uma versão antiga pode trazer valores que o schema de hoje "
        "recusa (coerções do formato antigo) ou já não compilar. Os padrões do formato ficam como "
        "foram escritos (um `required: true` por extenso aparece), e uma condição (`show_when`) "
        "vem em regras, não em frase."
    )

    @classmethod
    def body_of(cls, found: StoredVersion) -> dict[str, Any]:
        """O corpo da resposta, com a definição sem passar pelo modelo (o modelo só a descreve no
        contrato): o JSON que o banco guarda."""
        return {**FormVersionInfo.of(found).model_dump(mode="json"), "definition": found.definition}


class ComparisonSide(BaseModel):
    """Um dos dois lados de uma comparação."""

    kind: Literal["version", "draft"] = Field(
        description="`version`: uma versão publicada (o `number` diz qual). `draft`: o rascunho do "
        "formulário, como está salvo."
    )
    number: int | None = Field(description="O número da versão. Nulo no rascunho.", examples=[2])

    @classmethod
    def of(cls, ref: str) -> ComparisonSide:
        if ref == version_actions.DRAFT_REF:
            return cls(kind="draft", number=None)
        return cls(kind="version", number=int(ref))


class DiffLine(BaseModel):
    """Uma linha da diferença de uma seção, no formato de uma diferença unificada."""

    kind: Literal["add", "del", "ctx", "hunk"] = Field(
        description="`add`: linha que só existe no lado `to`. `del`: linha que só existe no lado "
        "`from`. `ctx`: linha igual, para situar a mudança. `hunk`: o cabeçalho de um trecho "
        "(`@@ -79,5 +79,6 @@`)."
    )
    text: str = Field(
        description="A linha, com o prefixo da diferença (`+`, `-`, um espaço ou `@@`) e o recuo do "
        "texto comparado."
    )


class ComparisonSection(BaseModel):
    """Uma seção da definição numa comparação."""

    key: str = Field(
        description="O id da seção (`pagina`, `etapas`, `prompt`...).", examples=["etapas"]
    )
    title: str = Field(description="O nome da seção, em português.")
    changed: bool = Field(description="A seção difere entre os dois lados.")
    lines: list[DiffLine] = Field(
        description="As linhas que mudaram, com duas de contexto. Vazio se a seção não mudou."
    )

    @classmethod
    def of(cls, change: SectionChange) -> ComparisonSection:
        return cls(
            key=change.key,
            title=change.title,
            changed=change.changed,
            lines=[DiffLine(kind=line.kind, text=line.text) for line in change.lines],  # type: ignore[arg-type]
        )


class FormComparison(BaseModel):
    """A comparação de dois lados de um formulário, seção por seção."""

    model_config = ConfigDict(populate_by_name=True)

    slug: str = Field(description="O endereço do formulário.")
    from_: ComparisonSide = Field(alias="from", description="O lado de onde a mudança parte.")
    to: ComparisonSide = Field(description="O lado a que a mudança chega.")
    versions: list[int] = Field(
        description="As versões publicadas, da mais recente para a mais antiga: as que `from` e `to` "
        "podem escolher (além de `draft`)."
    )
    draft_pending: bool = Field(
        description="Há mudança pendente no rascunho (é o que faz a escolha padrão comparar a versão "
        "atual com ele)."
    )
    changed_sections: int = Field(description="Quantas seções mudaram.", examples=[1])
    sections: list[ComparisonSection] = Field(
        description="Todas as seções, na ordem da leitura do formulário, a última sendo `outras` "
        "(o que a definição tem além das seções conhecidas)."
    )

    @classmethod
    def of(cls, result: version_actions.Comparison) -> FormComparison:
        return cls(
            slug=result.draft.slug,
            **{"from": ComparisonSide.of(result.old)},
            to=ComparisonSide.of(result.new),
            versions=list(result.numbers),
            draft_pending=result.draft.pending,
            changed_sections=sum(1 for change in result.sections if change.changed),
            sections=[ComparisonSection.of(change) for change in result.sections],
        )
