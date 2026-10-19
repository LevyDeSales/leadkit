"""Os modelos da publicação do rascunho: o que `review_form_draft_publication`,
`publish_form_draft` e `revert_form_draft_to_version` recebem e devolvem.

A revisão (`PublicationReview`) é a mesma nas duas operações que a mostram: a leitura a monta do que
a publicação faria (sem o PDF de exemplo, que só a publicação gera), e a simulação e a publicação a
levam dentro de `FormDraftPublication`, com o que o PDF e a nota acrescentam. Nada aqui traz dado de
lead: a revisão fala da configuração do formulário.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import AwareDatetime, BaseModel, Field

from app.admin.api.common import ConfirmedInput, VersionedInput, utc
from app.admin.api.errors import ErrorItem
from app.admin.api.schemas.forms import ComparisonSection, DraftState, FormVersionInfo
from app.forms import drafts
from app.forms import stored as stored_forms
from app.forms.definition import Problem
from app.forms.definition.changes import DroppedValue

CONFIRM_PUBLISH_TEXT = (
    "Precisa ser `true` para publicar de verdade: publicar muda o que a lead vê e vale já para os "
    "envios novos. Sem ele, a resposta é `422 confirmation_required` e nada acontece. **Com "
    "`dry_run: true` não é exigido**, porque uma simulação não tem efeito."
)


# --- a revisão ----------------------------------------------------------------------------------


class PublicationTest(BaseModel):
    """O que a publicação pede do teste do que vai ao LLM."""

    status: drafts.LlmTestStatus = Field(
        description="`not_needed`: nada que vai ao LLM mudou (ou não há o que publicar), a publicação "
        "não pede teste. `covered`: mudou, e um teste com sucesso depois da mudança o cobre (é o "
        "`current` ou o `unlocks` de `get_form_draft_test`). `missing`: mudou, e falta um teste com "
        "sucesso; a publicação é recusada até ele (`start_form_draft_test`).",
        examples=["covered"],
    )
    llm_changes: list[str] = Field(
        description="O que mudou e vai ao LLM ou o valida, por extenso, em português (`o prompt`, "
        "`as instruções`, `as perguntas que o prompt cita`). Vazio quando nada que vai ao LLM mudou.",
        examples=[["o prompt"]],
    )
    tested_at: AwareDatetime | None = Field(
        description="Quando terminou o teste com sucesso que cobre o que vai ao LLM agora (ISO 8601, "
        "UTC). Nulo sem teste que cubra, ou quando nada pede teste."
    )


class DroppedOptionValue(BaseModel):
    """Um valor de opção da versão publicada que a versão nova deixa de gravar na mesma pergunta."""

    question: str = Field(
        description="O nome da pergunta na versão publicada.", examples=["tamanho_equipe"]
    )
    label: str = Field(description="O rótulo da pergunta na versão publicada.")
    value: str = Field(description="O valor que os envios novos não trazem mais.")
    option: str = Field(description="O rótulo da opção na versão publicada.")

    @classmethod
    def of(cls, found: DroppedValue) -> DroppedOptionValue:
        return cls(
            question=found.question, label=found.label, value=found.value, option=found.option
        )


class PublicationReview(BaseModel):
    """O que a publicação do rascunho faria e o que falta para ela."""

    slug: str = Field(description="O endereço do formulário.", examples=["diagnostico-rapido"])
    version: str = Field(
        description="A versão do rascunho que esta revisão leu, um texto opaco (a impressão digital "
        "dele): `publish_form_draft` a recebe em `expected_version`, e se o rascunho mudou desde "
        "então a resposta é `409 version_conflict`."
    )
    current_version: int | None = Field(
        description="A versão publicada atual. Nulo na primeira publicação, que cria a versão 1 e "
        "põe o formulário no ar."
    )
    number: int = Field(description="O número da versão que a publicação cria.", examples=[2])
    first_publication: bool = Field(
        description="É a primeira publicação do formulário: a versão 1, que o põe no ar. Nela, "
        "`changes` vem vazia (tudo é novo)."
    )
    nothing_to_publish: bool = Field(
        description="O rascunho diz o mesmo que a versão publicada (e nenhuma imagem dela foi "
        "trocada): não há o que publicar, e publicar é recusado."
    )
    changes: list[ComparisonSection] = Field(
        description="As seções da definição que mudam, com as linhas da diferença contra a versão "
        "publicada atual (a mesma de `compare_form_versions`). Vazia na primeira publicação e quando "
        "não há o que publicar."
    )
    test: PublicationTest = Field(description="O que a publicação pede do teste do que vai ao LLM.")
    contact: list[stored_forms.ContactRole] = Field(
        description="Os dados de contato da lead que o prompt manda ao LLM em todo pedido: `name`, "
        "`email`, `phone`. Quando há algum, publicar pede `confirm_contact: true`. Vazia quando o "
        "prompt não cita nenhum."
    )
    consent_version: str | None = Field(
        description="A versão nova do texto do consentimento, que a publicação sobe sozinha quando o "
        "texto muda (quem estiver com a página aberta concorda de novo antes de enviar). Nulo se o "
        "texto não muda ou na primeira publicação."
    )
    dropped_values: list[DroppedOptionValue] = Field(
        description="Os valores de opção que a versão publicada grava e a nova não grava mais na "
        "mesma pergunta: os envios novos não os trazem, e o receptor do webhook pode depender deles."
    )
    stale_assets: list[str] = Field(
        description="Os nomes das imagens que a versão atual fixou e que foram trocadas desde então, "
        "entre as que o rascunho usa: há o que publicar mesmo sem o texto ter mudado, e a publicação "
        "fixa o conteúdo de agora."
    )
    missing: list[str] = Field(
        description="O que falta para publicar, em português e de uma vez (vazia: nada falta). Na "
        "leitura (`review_form_draft_publication`) são só as razões que não dependem de quem publica: "
        "nada para publicar, a definição que não compila e o teste do que vai ao LLM. Na simulação "
        "(`dry_run`) são também a nota, a confirmação dos dados de contato (`confirm_contact`) e o PDF "
        "de exemplo, com os valores que você mandou. Na publicação de verdade, vazia."
    )
    problems: list[ErrorItem] = Field(
        description="Os problemas da definição que não compila ou do PDF de exemplo que o adaptador "
        "recusou, cada um com o caminho do campo (`field`) e a linha do template (`line`). A leitura "
        "não gera o PDF, então só traz os da definição."
    )

    @classmethod
    def of(
        cls, found: drafts.Review, *, missing: Sequence[str], problems: Sequence[Problem]
    ) -> PublicationReview:
        draft = found.draft
        return cls(
            slug=draft.slug,
            version=draft.token,
            current_version=draft.current.number if draft.current is not None else None,
            number=found.number,
            first_publication=found.first,
            nothing_to_publish=found.nothing,
            changes=[]
            if found.first
            else [ComparisonSection.of(c) for c in found.changes if c.changed],
            test=PublicationTest(
                status=found.test_status,
                llm_changes=list(found.llm_changes),
                tested_at=utc(found.tested_at) if found.tested_at is not None else None,
            ),
            contact=list(found.contact),
            consent_version=found.consent_version,
            dropped_values=[DroppedOptionValue.of(item) for item in found.dropped_values()],
            stale_assets=list(draft.stale_assets),
            missing=list(missing),
            problems=[ErrorItem(field=p.path, message=p.message, line=p.line) for p in problems],
        )


# --- publicar -----------------------------------------------------------------------------------


class PublishFormDraftRequest(ConfirmedInput, VersionedInput):
    """Publicar o rascunho como a versão seguinte, ou só simular a publicação."""

    confirm: bool = Field(default=False, description=CONFIRM_PUBLISH_TEXT, examples=[True])
    note: str = Field(
        default="",
        max_length=stored_forms.NOTE_MAX_LENGTH,
        description="A nota do que mudou, que fica no histórico com a versão. **Obrigatória para "
        "publicar**: em branco, a publicação é recusada (`422`, com a razão em `reasons`). Na "
        f"simulação pode faltar, e a falta aparece em `missing`. No máximo "
        f"{stored_forms.NOTE_MAX_LENGTH} caracteres.",
        examples=["Prompt pede o momento do mercado."],
    )
    confirm_contact: bool = Field(
        default=False,
        description="Confirma que o prompt do formulário manda dados de contato da lead (o nome, o "
        "e-mail ou o telefone) ao LLM em todo pedido. Só é exigido quando o prompt os cita (a revisão "
        "os lista em `contact`): então, sem `true` aqui, e se nada mais falta, a resposta é `422 "
        "confirmation_required` com os dados citados em `detail` e nada é gravado. É outra "
        "confirmação além de `confirm`: `confirm` diz que você sabe que publicar muda o que a lead "
        "vê; este diz que você sabe o que o prompt envia ao provedor do modelo. A simulação não o "
        "exige: ela só diz, em `missing`, que a publicação vai pedi-lo.",
        examples=[True],
    )
    dry_run: bool = Field(
        default=False,
        description="`true`: só simula. Roda as mesmas conferências da publicação, **inclusive o PDF "
        "de exemplo no adaptador** (custa CPU), e devolve o que faria e o que falta, **sem gravar, "
        "sem auditar e sem exigir o `confirm`**. É a mesma operação e pede papel `total`; quem só tem "
        "`leitura` usa `review_form_draft_publication`, que não gera o PDF.",
        examples=[True],
    )


class FormDraftPublication(BaseModel):
    """O resultado de `publish_form_draft`: a versão que foi publicada ou, na simulação, o que seria."""

    dry_run: bool = Field(description="A chamada foi uma simulação: nada foi gravado nem auditado.")
    published: bool = Field(
        description="Uma versão foi criada. Sempre falso na simulação; verdadeiro quando a "
        "publicação aconteceu (se algo faltasse, a resposta seria `422`)."
    )
    ready: bool = Field(
        description="Nada falta: publicar agora, com a mesma nota e as mesmas confirmações, "
        "funcionaria. Na simulação diz se vale chamar a publicação de verdade; depois de publicar, "
        "é verdadeiro."
    )
    review: PublicationReview = Field(
        description="O que a publicação faz (ou fez): o número da versão, as mudanças por seção, o "
        "que vai ao LLM e o teste que o cobre, os dados de contato no prompt e o que falta. Depois de "
        "publicar, `review.version` é o rascunho que foi publicado, que não existe mais: use "
        "`draft.version`."
    )
    published_version: FormVersionInfo | None = Field(
        description="A versão que a publicação criou, como o histórico a mostra. Nulo na simulação."
    )
    draft: DraftState = Field(
        description="O rascunho depois da chamada. Na simulação não mudou (`pending` segue "
        "verdadeiro); depois de publicar, `pending` é falso e `current_version` é a versão nova."
    )


# --- voltar a uma versão ------------------------------------------------------------------------


class RevertFormDraftRequest(ConfirmedInput, VersionedInput):
    """Copiar uma versão publicada para o rascunho."""
