"""As operações do rascunho de um formulário: `get_form_draft`, `save_form_draft` e
`patch_form_draft`.

O rascunho é um documento (ADR 0015, "O rascunho é um documento"): `GET` o lê inteiro, `PUT` o troca
por inteiro e `PATCH` o edita por JSON Patch (RFC 6902). Os 48 comandos do editor estruturado da
página não viram 48 operações: são edições do estado da tela, e a tabela de equivalências da matriz
(`docs/api/cobertura.md`) diz como cada uma se escreve no documento.

Salvam pelo mesmo serviço das duas páginas de salvar (`app.forms.drafts.save_document`): a regra é
uma só, e a API a traduz em JSON e status. O `PUT` e o `PATCH` fazem as conferências das duas páginas
(a compilação e a dos recursos do PDF, porque o documento é um só), gravam numa transação e
respondem com o documento novo. A auditoria sai do serviço (`form.draft.save`, e `form.rename` se o
endereço mudou), com o autor `chave:<nome>` da chave que chamou.
"""

from __future__ import annotations

import logging

from fastapi.responses import JSONResponse

from app.admin.api.errors import ApiError
from app.admin.api.forms import FORM, NOT_FOUND
from app.admin.api.operations import OperationRouter
from app.admin.api.schemas.drafts import FormDraft, PatchFormDraftRequest, SaveFormDraftRequest
from app.admin.api.security import CurrentKey
from app.deps import Deps, get_deps
from app.forms import UnknownForm, drafts
from app.forms.definition import DefinitionError

logger = logging.getLogger(__name__)

TAG = "Rascunho"
router = OperationRouter(
    tag=TAG,
    description=(
        "O rascunho de um formulário: o documento que a operadora edita sem mexer no que está no ar. "
        "Ler é de `leitura`; trocar o documento (`save_form_draft`) e editá-lo por JSON Patch "
        "(`patch_form_draft`) são de `total` e pedem a versão que você leu (`expected_version`), "
        "porque o rascunho muda por outras mãos. Um formulário novo (`create_form`) só tem o "
        "rascunho; o que o rascunho diz só chega às leads na publicação."
    ),
)

DRAFT_CHANGED = (
    "O rascunho mudou desde que você o leu (outra chamada, outra pessoa, uma publicação). Nada foi "
    "gravado: leia de novo com `get_form_draft` e refaça a mudança."
)
SAVE_REFUSED = "O documento não vale; veja o motivo de cada campo em `errors`. Nada foi gravado."
PATCH_REFUSED = (
    "O patch não vale ou o documento que sai dele não vale; veja o motivo em `errors` (`patch.<n>` "
    "é a operação que não se aplicou). Nada foi gravado."
)
CONFLICT = (
    "O rascunho mudou desde que você o leu (`version_conflict`, com a versão de agora): nada é "
    "gravado. Leia de novo com `get_form_draft` e refaça a mudança."
)


def draft_changed(deps: Deps, slug: str) -> ApiError:
    """O `409`: o rascunho mudou desde a versão que a chamada viu. Traz a de agora (a publicação e a
    volta a uma versão, que também pedem `expected_version`, usam o mesmo)."""
    with deps.sessions() as session:
        current = drafts.load(session, slug)
    if current is None:  # o formulário saiu no meio do caminho
        return ApiError("not_found", NOT_FOUND)
    return ApiError("version_conflict", DRAFT_CHANGED, current_version=current.token)


@router.operation(
    "GET",
    "/forms/{slug}/draft",
    operation_id="get_form_draft",
    role="leitura",
    summary="Ler o rascunho de um formulário",
    description=(
        "O rascunho do formulário `slug`: o documento inteiro (`document`, o schema `FormDocument`, "
        "com as etapas e perguntas, a identidade e o consentimento da lead, a pontuação, as "
        "instruções e o prompt do LLM, o modelo do plano, os dados, o PDF (`pdf`), a prévia da "
        "página de resultado (`preview`) e as respostas de exemplo) e o estado dele. Sem rascunho "
        "salvo, é a própria versão publicada atual. `version` é um texto opaco (a impressão digital "
        "do documento): `save_form_draft` e `patch_form_draft` o devolvem em `expected_version`, "
        "e se o rascunho mudou a resposta delas é `409 version_conflict`. `pending` diz se há "
        "mudança que ainda não foi publicada (antes da versão 1, o rascunho inteiro), `saved_by` e "
        "`saved_at` quem a salvou, `base_version` de que versão ela partiu e `stale_assets` as "
        "imagens trocadas desde a versão atual. O documento sai como o banco o guarda, sem os "
        "padrões do formato (um `required` ausente é `true`). Para comparar com a versão publicada, "
        "use `compare_form_versions` com `to=draft`. Não grava nada."
    ),
    returns="O rascunho: o documento inteiro, a versão dele e o estado.",
    response_model=FormDraft,
)
def get_form_draft(slug: FORM) -> JSONResponse:
    with get_deps().sessions() as session:
        draft = drafts.load(session, slug)
    if draft is None:
        raise ApiError("not_found", NOT_FOUND)
    # O documento sai como o banco o guarda: o modelo `FormDraft` o descreve no contrato, mas não o
    # relê (a leitura pelo modelo acrescentaria os padrões e recusaria um rascunho de formato antigo).
    return JSONResponse(FormDraft.body_of(draft))


@router.operation(
    "PUT",
    "/forms/{slug}/draft",
    operation_id="save_form_draft",
    role="total",
    errors={
        409: CONFLICT,
        422: "O corpo não vale (`validation_failed`): o documento não compila, o PDF ou a prévia "
        "pedem o que o app não serve, o endereço não pode mudar ou não serve. Cada problema vem em "
        "`errors` com o caminho do campo no documento e, no de um template, a linha. Nada é "
        "gravado.",
    },
    summary="Trocar o documento do rascunho",
    description=(
        "Troca o documento inteiro do rascunho do formulário `slug` pelo de `document`, em uma "
        "transação: ou grava tudo, ou nada. `expected_version` é a `version` que "
        "`get_form_draft` devolveu; se o rascunho mudou desde então (outra chamada, outra pessoa, "
        "uma publicação), a resposta é `409 version_conflict`, com a versão de agora, e nada é "
        "gravado. Faz as conferências das duas páginas de salvar: o documento inteiro tem de "
        "compilar neste deploy (todos os problemas de uma vez, cada um com o caminho do campo e a "
        "linha do template) e o PDF e a prévia só podem pedir arquivos do app, imagens que existem "
        "e `data:`. O que não vale não é gravado, e a resposta nunca repete o valor recebido. O "
        "`slug` do documento é o do formulário; **antes da primeira publicação**, um `slug` novo "
        "muda o endereço do formulário e leva o destino do webhook dele junto (depois da "
        "publicação, o endereço não muda mais e a mudança é `422`). `default`, `import` e `new` "
        "são reservados. A versão do texto do consentimento (`consent.version`) é do sistema: o "
        "valor que você mandar é ignorado. Mandar o documento que `get_form_draft` devolveu, sem "
        "mudança, grava o mesmo documento. O rascunho que volta a dizer o mesmo que a versão "
        "publicada deixa de ser pendente. Isto não publica nada: as leads só veem o que "
        "`publish_form_draft` publica. Para mudar uma parte, use `patch_form_draft`. A gravação "
        "fica na auditoria (`form.draft.save`, e `form.rename` se o endereço mudou) em nome da "
        "chave."
    ),
    returns="O rascunho como ficou: o documento guardado, a versão nova e o estado.",
    response_model=FormDraft,
)
def save_form_draft(slug: FORM, body: SaveFormDraftRequest, key: CurrentKey) -> JSONResponse:
    deps = get_deps()
    try:
        saved = drafts.replace_document(
            deps, slug, body.sent, actor=key.actor, expected=body.expected_version
        )
    except UnknownForm:
        raise ApiError("not_found", NOT_FOUND) from None
    except DefinitionError as refused:
        raise ApiError.invalid(SAVE_REFUSED, refused.problems) from None
    except drafts.DraftChanged:
        raise draft_changed(deps, slug) from None
    logger.info(
        "api: rascunho salvo slug=%s pendente=%s por=%s", saved.slug, saved.pending, key.actor
    )
    return JSONResponse(FormDraft.body_of(saved))


@router.operation(
    "PATCH",
    "/forms/{slug}/draft",
    operation_id="patch_form_draft",
    role="total",
    errors={
        409: CONFLICT,
        422: "O corpo não vale (`validation_failed`): uma operação do patch não se aplica "
        "(`patch.<n>`, n a partir de 0) ou o documento que sai dele não vale (o caminho do campo "
        "e, no de um template, a linha). Nada é gravado, nem as operações que valiam.",
    },
    summary="Editar o rascunho por JSON Patch",
    description=(
        "Edita o documento do rascunho do formulário `slug` por um JSON Patch (RFC 6902), **tudo ou "
        "nada**: as operações de `patch` são aplicadas em ordem a uma cópia do documento; se uma "
        "não se aplica (um caminho que não existe, um índice fora da lista, um `test` que falha) "
        "ou se o documento que sai delas não passa nas conferências de `save_form_draft`, nada é "
        "gravado, nem as operações que valiam. `expected_version` é a `version` de "
        "`get_form_draft`; se o rascunho mudou, a resposta é `409 version_conflict`. É o que o "
        "editor da página faz, em uma transação: cada comando do editor se escreve como um patch. "
        "Os caminhos são os do documento (`/steps/0/questions/1/label`), com os índices a partir "
        "de 0 e sem zero à esquerda (`/steps/01` é recusado). Receitas: incluir uma etapa é "
        "`add /steps/-` (com a etapa inteira: `title` e `questions`), removê-la é `remove "
        "/steps/{i}` (tire do `/identity` o papel que apontava para uma pergunta dela), subi-la é "
        "`move` de `/steps/{i}` para `/steps/{i-1}` e descê-la, para `/steps/{i+1}`; as perguntas "
        "são `/steps/{i}/questions/{j}` e as opções, `.../options/{k}`, do mesmo jeito (para "
        "passar uma pergunta à etapa de cima, `move` para `/steps/{i-1}/questions/-`); uma "
        'condição é `add .../show_when` com `{"mode": "all", "rules": [...]}`, uma regra a mais é '
        "`add .../show_when/rules/-` e tirá-la, `remove`; o modelo do plano é `/plan/fields/-` e "
        "`/plan/forbidden/-`; os dados são `/data/{nome}` e as janelas, `/data/{nome}/windows/-`; "
        "a pontuação é `/scoring/scores/-`, `/scoring/priority/-` e `/scoring/persona/-`; os "
        "textos são `replace` de `/intro`, `/prompt`, `/instructions`, `/pdf/html`, `/pdf/css` e "
        "`/preview`. **As respostas de exemplo acompanham as perguntas e o plano de exemplo "
        "acompanha o modelo do plano**, e o documento só passa na conferência se estão de acordo: "
        "incluir uma pergunta obrigatória exige `add /examples/answers/<nome>`, remover uma "
        "pergunta (ou a opção que era a resposta de exemplo) exige `remove` ou `replace` em "
        "`/examples/answers/<nome>`, e um campo novo do plano exige `add /examples/plan/<nome>` "
        "(removê-lo, `remove`), tudo no mesmo patch. Um `test` no começo do patch protege contra "
        "uma mudança no meio do caminho; ele compara com `==`, então `1` e `true` valem o mesmo "
        "(`1 == true`), e `1.0` e `1` também. `errors[].field` é `patch.<n>` para a operação que "
        "não se aplicou, ou o caminho do campo no documento que saiu do patch. Valem as regras de "
        "`save_form_draft`: o endereço (`/slug`) só muda antes da primeira publicação, e a versão "
        "do texto do consentimento é do sistema: o `PUT` a ignora, mas uma operação do patch em "
        "`/consent/version` é recusada (`patch.<n>`; o `test` dela vale). Limites: no máximo 1000 "
        "operações, 20 delas `copy`, valores com até 64 níveis de listas e objetos, e o "
        "documento que sai do patch cabe num `PUT` (255 KiB, medido com os acentos escapados, "
        "porque o corpo da API tem 256 KiB). Isto não publica nada. A gravação fica na "
        "auditoria (`form.draft.save`) em nome da chave."
    ),
    returns="O rascunho como ficou: o documento guardado, a versão nova e o estado.",
    response_model=FormDraft,
)
def patch_form_draft(slug: FORM, body: PatchFormDraftRequest, key: CurrentKey) -> JSONResponse:
    deps = get_deps()
    try:
        saved = drafts.patch_document(
            deps, slug, body.operations(), actor=key.actor, expected=body.expected_version
        )
    except UnknownForm:
        raise ApiError("not_found", NOT_FOUND) from None
    except DefinitionError as refused:
        raise ApiError.invalid(PATCH_REFUSED, refused.problems) from None
    except drafts.DraftChanged:
        raise draft_changed(deps, slug) from None
    logger.info(
        "api: rascunho remendado slug=%s operações=%s pendente=%s por=%s",
        saved.slug,
        len(body.patch),
        saved.pending,
        key.actor,
    )
    return JSONResponse(FormDraft.body_of(saved))
