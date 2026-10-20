"""Os modelos do rascunho e da criação de formulários: o que `create_form`, `import_form`,
`get_form_draft`, `save_form_draft` e `patch_form_draft` recebem e devolvem.

O documento do rascunho é um só, o `FormDocument` do compilador (ADR 0015, "O rascunho é um
documento"). **O OpenAPI o descreve com o esquema de `FormDocument`, mas quem o valida é o compilador**
(`compile_definition`), para os erros saírem com o caminho do campo e a linha do template
(`Problem.path` e `Problem.line`), e não com a mensagem do Pydantic: o corpo recebe o documento como
chegou (`AsSent`) e o serviço o confere inteiro. Na resposta, o documento sai como o banco o guarda, sem
passar pelo modelo (que acrescentaria os padrões e recusaria um rascunho de um formato antigo).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, WrapValidator, field_validator, model_validator
from pydantic_core.core_schema import ValidatorFunctionWrapHandler

from app.admin.api.common import ApiInput, ConfirmedInput, VersionedInput
from app.admin.api.schemas.forms import DraftState
from app.forms import drafts
from app.forms import stored as stored_forms
from app.forms.definition.document import DefinitionDocument as FormDocument
from app.forms.draft_patch import MAX_OPERATIONS, MAX_VALUE_DEPTH, depth_of


def _as_sent(value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
    """Entrega o documento como chegou, sem passar pelo Pydantic: o compilador o confere depois."""
    return value


# O esquema de `FormDocument` no contrato, e o documento como chegou na validação. Tipo: o do esquema;
# em tempo de execução, o que veio no JSON (um objeto, ou o que a pessoa mandou de errado).
AsSent = Annotated[FormDocument, WrapValidator(_as_sent)]

DOCUMENT_FIELDS = (
    "`errors[].field` é o caminho no documento, sem prefixo (`steps.1.questions.0.label`), com os "
    "índices das listas a partir de 0; `line` é a linha do template, quando o problema é de um."
)


# --- criar --------------------------------------------------------------------------------------


class CreateFormRequest(ApiInput):
    """Um formulário novo: o título, o endereço e, se quiser, o formulário de onde copiar."""

    generation: Literal["pdf", "none"] | None = Field(
        default=None,
        description="Finalidade: pdf gera plano; none apenas salva respostas. Ausente preserva a "
        "origem ou usa PDF. Copiar uma captura com pdf prepara os blocos de geração do modelo "
        "inicial no novo rascunho, mantendo perguntas e respostas de exemplo da origem.",
    )
    title: str = Field(
        max_length=drafts.TITLE_MAX_LENGTH,
        description="O nome do formulário no admin e o título da página pública dele. Vira o título "
        f"do rascunho. No máximo {drafts.TITLE_MAX_LENGTH} caracteres.",
        examples=["Diagnóstico de gestão do escritório"],
    )
    slug: str | None = Field(
        default=None,
        max_length=stored_forms.SLUG_MAX_LENGTH,
        description="O endereço do formulário: letras minúsculas sem acento, números e hífens "
        f"(no máximo {stored_forms.SLUG_MAX_LENGTH} caracteres). Vai na URL pública (`/p/<slug>`) e "
        "identifica o formulário em toda operação. Ausente ou vazio: sai do título. Não pode ser o de "
        "outro formulário nem uma palavra reservada (`import`, `new` e `default`). Antes da primeira "
        "publicação ele ainda muda (`save_form_draft`); depois, não.",
        examples=["gestao-do-escritorio"],
    )
    source: str | None = Field(
        default=None,
        description="O endereço de um formulário para copiar (`list_forms` lista os que existem): o "
        "rascunho parte da versão publicada dele (ou do rascunho, se ele ainda não foi publicado), "
        "com o título, o endereço e a versão do texto do consentimento novos. Ausente ou vazio: o "
        "rascunho parte do modelo inicial.",
        examples=["diagnostico-rapido"],
    )


class ImportFormRequest(ConfirmedInput):
    """A definição de um formulário para importar, pronta, e publicar como a versão 1."""

    definition: AsSent = Field(
        description="A definição do formulário, o JSON que `get_form_version` devolve e a exportação "
        "da página escreve (o schema `FormDocument`). Quem confere é o compilador, com todos os "
        "problemas de uma vez. " + DOCUMENT_FIELDS
    )
    note: str | None = Field(
        default=None,
        max_length=stored_forms.NOTE_MAX_LENGTH,
        description="A nota da versão 1, que fica no histórico. Ausente ou vazia: "
        f"«{stored_forms.DEFAULT_NOTE}». No máximo {stored_forms.NOTE_MAX_LENGTH} caracteres.",
    )
    confirm_contact: bool = Field(
        default=False,
        description="Confirma que o prompt do formulário manda dados de contato da lead (o nome, o "
        "e-mail ou o telefone) ao LLM em todo pedido. Só é exigido quando o prompt os cita: então, "
        "sem `true` aqui a resposta é `422 confirmation_required`, `detail` diz quais dados o prompt "
        "cita e nada é gravado. É outra confirmação além de `confirm`: `confirm` diz que você sabe que "
        "importar publica o formulário; este diz que você sabe o que o prompt envia ao provedor do "
        "modelo. A decisão vale igual em `publish_form_draft`.",
        examples=[True],
    )

    @property
    def sent(self) -> Any:
        """A definição como chegou."""
        return self.definition


# --- o rascunho ---------------------------------------------------------------------------------


class FormDraft(DraftState):
    """O rascunho de um formulário: o documento inteiro e o estado dele. `version` é o que
    `save_form_draft` e `patch_form_draft` devolvem em `expected_version`."""

    slug: str = Field(description="O endereço do formulário.", examples=["diagnostico-rapido"])
    title: str = Field(description="O nome do formulário no admin.")
    document: FormDocument = Field(
        description="O documento do rascunho, **exatamente como está guardado**: o schema "
        "`FormDocument` descreve o formato que o compilador de hoje aceita, e **não garante o texto "
        "guardado** (a resposta não é validada contra ele). Sem rascunho salvo, é a própria versão "
        "publicada atual, que um deploy mais rígido pode já não compilar. Tem o PDF (`pdf`), a prévia "
        "da página de resultado (`preview`) e as respostas de exemplo (`examples`). A versão do texto "
        "do consentimento (`consent.version`) é do sistema: a publicação a sobe sozinha quando o texto "
        "muda, e o valor que você mandar ao salvar é ignorado."
    )

    @classmethod
    def body_of(cls, draft: drafts.Draft) -> dict[str, Any]:
        """O corpo da resposta, com o documento sem passar pelo modelo (ele só o descreve no
        contrato): o JSON que o banco guarda, que um `PUT` devolve igual."""
        state = DraftState.of(draft).model_dump(mode="json")
        return {**state, "slug": draft.slug, "title": draft.title, "document": draft.document}


class SaveFormDraftRequest(VersionedInput):
    """O documento novo do rascunho, inteiro."""

    document: AsSent = Field(
        description="O documento inteiro do rascunho (o schema `FormDocument`): troca o que havia, "
        "inclusive o PDF e a prévia. Quem o confere é o compilador, com todos os problemas de uma "
        "vez, e mais o que o PDF e a prévia pedem (só arquivos do app, imagens que existem e "
        "`data:`). O `slug` do documento é o do formulário; antes da primeira publicação, outro "
        "`slug` muda o endereço do formulário junto (e o destino do webhook dele). "
        + DOCUMENT_FIELDS
    )

    @property
    def sent(self) -> Any:
        """O documento como chegou."""
        return self.document


# --- o JSON Patch -------------------------------------------------------------------------------

VALUE_OPERATIONS = ("add", "replace", "test")
FROM_OPERATIONS = ("move", "copy")
MAX_POINTER_LENGTH = 500


class PatchOperation(ApiInput):
    """Uma operação do JSON Patch (RFC 6902) sobre o documento do rascunho."""

    op: Literal["add", "remove", "replace", "move", "copy", "test"] = Field(
        description="`add`: inclui `value` em `path` (numa lista, entra no índice de `path` ou, com "
        "`-`, no fim). `remove`: tira o que está em `path`. `replace`: troca o que está em `path` por "
        "`value`. `move`: tira de `from` e põe em `path` (para subir uma etapa, mova `/steps/1` para "
        "`/steps/0`). `copy`: copia de `from` para `path`. `test`: confere que o valor em `path` é "
        "`value`; se não for, o patch todo é recusado.",
        examples=["replace"],
    )
    path: str = Field(
        max_length=MAX_POINTER_LENGTH,
        description="O caminho no documento, um JSON Pointer (RFC 6901): `/title`, "
        "`/steps/0/questions/1/label`. Os índices das listas começam em 0, e `-` é o fim da lista "
        "(em `add`, `move` e `copy`). Uma chave com `/` ou `~` escreve `~1` e `~0`. Vazio é o "
        "documento inteiro. Um índice não leva zero à esquerda (`/steps/01` é recusado), e a versão "
        "do texto do consentimento (`/consent/version`) é do sistema: só o `test` a lê.",
        examples=["/steps/0/title"],
    )
    value: Any = Field(
        default=None,
        description="O valor, de qualquer tipo JSON (`null` também), com até 64 níveis de listas e "
        "objetos. Obrigatório em `add`, `replace` e `test`; as outras operações não o levam. O `test` "
        "compara com `==`: `1` e `true` valem o mesmo.",
    )
    from_: str | None = Field(
        default=None,
        alias="from",
        max_length=MAX_POINTER_LENGTH,
        description="O caminho de origem, também um JSON Pointer. Obrigatório em `move` e `copy`; as "
        "outras operações não o levam.",
        examples=["/steps/1"],
    )

    @field_validator("value")
    @classmethod
    def _not_too_deep(cls, value: Any) -> Any:
        """Um valor aninhado a milhares de níveis derrubaria o que o percorre (a cópia, a conta do
        tamanho): o teto é o do documento, e a conta não recorre."""
        if depth_of(value) > MAX_VALUE_DEPTH:
            raise ValueError(f"O valor passa de {MAX_VALUE_DEPTH} níveis de listas e objetos.")
        return value

    @model_validator(mode="after")
    def _members(self) -> PatchOperation:
        """Os membros que cada operação leva, do RFC 6902: `value` em `add`, `replace` e `test`;
        `from` em `move` e `copy`. Um membro que a operação não usa é recusado, em vez de ignorado: é
        quase sempre a operação errada."""
        sent = self.model_fields_set
        if self.op in VALUE_OPERATIONS and "value" not in sent:
            raise ValueError(f"Em `{self.op}`, informe `value`.")
        if self.op not in VALUE_OPERATIONS and "value" in sent:
            raise ValueError(f"`{self.op}` não leva `value`.")
        if self.op in FROM_OPERATIONS and "from_" not in sent:
            raise ValueError(f"Em `{self.op}`, informe `from`.")
        if self.op not in FROM_OPERATIONS and "from_" in sent:
            raise ValueError(f"`{self.op}` não leva `from`.")
        return self

    def as_json(self) -> dict[str, Any]:
        """A operação como o RFC a escreve: só os membros que ela levou."""
        found: dict[str, Any] = {"op": self.op, "path": self.path}
        if "value" in self.model_fields_set:
            found["value"] = self.value
        if "from_" in self.model_fields_set:
            found["from"] = self.from_
        return found


class PatchFormDraftRequest(VersionedInput):
    """Um JSON Patch para o documento do rascunho."""

    patch: list[PatchOperation] = Field(
        min_length=1,
        max_length=MAX_OPERATIONS,
        description="As operações, aplicadas em ordem a uma cópia do documento (RFC 6902). **Tudo ou "
        "nada**: se uma não se aplica, ou se o documento que sai delas não é válido, nada é "
        "gravado. Uma operação que não se aplica é o erro `patch.<n>` (n a partir de 0). No máximo "
        "1000 operações, 20 delas `copy`.",
        examples=[[{"op": "replace", "path": "/intro", "value": "Uma introdução nova."}]],
    )

    def operations(self) -> list[dict[str, Any]]:
        return [operation.as_json() for operation in self.patch]
