"""As operações dos formulários: `list_forms`, `get_form`, `activate_form`, `deactivate_form`,
`get_form_retention`, `save_form_retention`, `get_form_version` e `compare_form_versions`.

Leem pela mesma consulta (`app.admin.queries.forms`) e pela mesma ação (`app.admin.actions.versions`)
que as páginas `/admin/forms/...`, e gravam pelos mesmos serviços (`app.forms.stored.set_active`,
`app.forms.retention.save`): a regra é uma só, e a API a traduz em JSON e status. A auditoria sai dos
serviços, com o autor `chave:<nome>` da chave que chamou.

Nada aqui traz dado de uma lead: as leituras são de `leitura`, e só mudar a situação ou a retenção
pede `total`.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import Depends, Path, Query, Request
from fastapi.responses import JSONResponse

from app.admin.actions import versions as version_actions
from app.admin.api.common import PageParams, now, page_params, paginate
from app.admin.api.errors import ApiError, ErrorItem
from app.admin.api.operations import OperationRouter
from app.admin.api.schemas.drafts import CreateFormRequest, ImportFormRequest
from app.admin.api.schemas.forms import (
    ActivateFormRequest,
    DeactivateFormRequest,
    FormComparison,
    FormDetail,
    FormPage,
    FormRetention,
    FormSummary,
    FormVersion,
    SaveFormRetentionRequest,
)
from app.admin.api.security import CurrentKey
from app.admin.queries import forms as form_queries
from app.deps import Deps, get_deps
from app.forms import UnknownForm, drafts
from app.forms import retention as form_retention
from app.forms import stored as stored_forms
from app.forms.definition import DefinitionError, Problem
from app.forms.definition.document import TOO_DEEP

logger = logging.getLogger(__name__)

TAG = "Formulários"
router = OperationRouter(
    tag=TAG,
    description=(
        "Os formulários de diagnóstico: a situação (ativo ou desativado), a versão publicada e o "
        "rascunho pendente, os prazos de retenção, as versões e a comparação entre elas. Ler é de "
        "`leitura`; criar, importar, ativar, desativar e mudar a retenção são só de `total` (e as "
        'que mudam o que a lead vê ou apagam dado exigem `"confirm": true`). Editar o rascunho de um '
        "formulário é da tag «Rascunho»; testá-lo, da «Teste do rascunho»; revisá-lo, publicá-lo e "
        "voltar a uma versão, da «Publicação»."
    ),
)

FORM = Annotated[
    str,
    Path(description="O endereço do formulário (`slug`), como `list_forms` o mostra."),
]
NOT_FOUND = "Formulário não encontrado."


def form_detail(deps: Deps, slug: str, request: Request) -> form_queries.FormDetail:
    """O formulário como a página o lê, ou `404`."""
    settings = deps.settings
    with deps.sessions() as session:
        found = form_queries.form_detail(
            session,
            slug,
            deps.forms,
            public_base_url=settings.public_base_url,
            retention_pdf_days=settings.retention_pdf_days,
            retention_days=settings.retention_days,
            now=now(request),
        )
    if found is None:
        raise ApiError("not_found", NOT_FOUND)
    return found


@router.operation(
    "GET",
    "/forms",
    operation_id="list_forms",
    role="leitura",
    summary="Listar os formulários",
    description=(
        "Lista todos os formulários, por endereço (`slug`), `limit` por página (de 1 a 200; padrão "
        "50), com o cursor da seguinte em `next_cursor`: o formulário que chega durante a leitura não "
        "pula nem repete nenhum. Cada item traz a situação (`active`), a versão publicada atual, se "
        "ela ainda compila neste deploy, se há rascunho pendente, o que conferir nos dados "
        "(`data_notices`) e quantos envios o formulário tem. Um formulário que ainda não tem versão "
        "publicada (criado do zero ou duplicado) aparece com `published_version` nulo. Para o "
        "destino, a retenção, a integração e o histórico de versões, use `get_form`. Não grava nada."
    ),
    returns="Uma página de formulários, com `next_cursor` para a seguinte (nulo na última).",
    response_model=FormPage,
)
def list_forms(request: Request, page: Annotated[PageParams, Depends(page_params)]) -> FormPage:
    deps = get_deps()
    settings = deps.settings
    with deps.sessions() as session:
        rows = form_queries.form_summaries(
            session, deps.forms, public_base_url=settings.public_base_url, now=now(request)
        )
    chunk, next_cursor = paginate(rows, page, key=lambda row: row.slug)
    return FormPage(items=[FormSummary.of(row) for row in chunk], next_cursor=next_cursor)


@router.operation(
    "GET",
    "/forms/{slug}",
    operation_id="get_form",
    role="leitura",
    summary="Ler um formulário",
    description=(
        "O formulário de endereço `slug`: o que `list_forms` mostra e mais o rótulo da versão atual, "
        "o histórico de versões (da mais recente para a mais antiga), o estado do rascunho, o "
        "destino do webhook que o serve (só o host: o caminho da URL costuma valer como senha), os "
        "prazos de retenção e a integração com a planilha (nunca o token). Um formulário sem versão "
        "publicada devolve `published_version` nulo e `versions` vazio; um cuja versão atual não "
        "compila mais devolve `version_compiles` falso. A definição de uma versão se lê com "
        "`get_form_version`. Não grava nada."
    ),
    returns="O formulário, com a situação, as versões, o rascunho, o destino, a retenção e a integração.",
    response_model=FormDetail,
)
def get_form(slug: FORM, request: Request) -> FormDetail:
    return FormDetail.of(form_detail(get_deps(), slug, request))


# --- criar e importar ---------------------------------------------------------------------------

CREATE_REFUSED = "O formulário não foi criado; veja o motivo de cada campo em `errors`."
IMPORT_REFUSED = "A definição não vale; veja o motivo de cada campo em `errors`. Nada foi gravado."
IMPORT_TEXT = (
    "Importar publica já a versão 1: o formulário vai ao ar no endereço público dele e passa a "
    "receber os envios das leads, sem rascunho nem teste antes."
)
CONTACT_TEXT = (
    "O prompt deste formulário manda {roles} da lead ao LLM em todo pedido. Para importar mesmo "
    "assim, repita a chamada com `confirm_contact: true`, dizendo que você sabe o que ele envia ao "
    "provedor do modelo. Nada foi gravado."
)


def _field_errors(problems: tuple[Problem, ...]) -> list[ErrorItem]:
    """Um erro por campo recusado, na ordem em que a criação os achou, com o motivo que a página
    mostra ao lado dele (`drafts.problems_by_field`: o primeiro problema de cada campo vale)."""
    reasons = drafts.problems_by_field(problems)
    return [
        ErrorItem(field=path, message=reasons[path])
        for path in dict.fromkeys(p.path for p in problems)
    ]


@router.operation(
    "POST",
    "/forms",
    operation_id="create_form",
    role="total",
    status_code=201,
    errors={
        422: "O corpo não vale (`validation_failed`): o título em branco ou longo demais, o endereço "
        "em uso, reservado ou fora do formato, ou o formulário de origem que não existe ou não "
        "compila mais neste deploy. O campo vem em `errors[].field` (`title`, `slug` ou `source`) "
        "e nada é criado."
    },
    summary="Criar um formulário",
    description=(
        "Cria um formulário novo **sem versão publicada**: só o rascunho dele, que parte do modelo "
        "inicial do sistema ou, com `source`, da cópia de outro formulário (a versão publicada "
        "dele, ou o rascunho, se ele ainda não foi publicado). A cópia preserva a política de liberação, mas remove o vínculo de encaminhamento: configure um receptor próprio no novo formulário. O formulário só vai ao ar na "
        "primeira publicação (`publish_form_draft`), que cria a versão 1: até lá a página pública "
        "dele não existe e nenhuma lead o vê. `title` é o nome do formulário; `slug`, o endereço "
        "(sai do título se faltar; antes da primeira publicação ele ainda muda, com "
        "`save_form_draft`). Se algo não vale (título em branco, endereço já usado ou reservado, "
        "formulário de origem que não existe), a resposta é `422` com o campo em "
        "`errors[].field` e nada é criado; repetir a chamada com o mesmo endereço também dá `422`, "
        "porque o endereço já existe. A criação fica na auditoria (`form.create`, ou "
        "`form.duplicate` com `source`) em nome da chave. Para editar o rascunho, use "
        "`get_form_draft`, `save_form_draft` e `patch_form_draft`."
    ),
    returns="O formulário criado, sem versão publicada, com o estado do rascunho dele em `draft`.",
    response_model=FormDetail,
)
def create_form(body: CreateFormRequest, key: CurrentKey, request: Request) -> FormDetail:
    deps = get_deps()
    try:
        created = drafts.create(
            deps,
            title=body.title,
            slug=body.slug or "",
            actor=key.actor,
            source=body.source or "",
            generation=body.generation,
        )
    except DefinitionError as refused:
        raise ApiError(
            "validation_failed", CREATE_REFUSED, errors=_field_errors(refused.problems)
        ) from None
    logger.info("api: formulário criado slug=%s por=%s", created, key.actor)
    return FormDetail.of(form_detail(deps, created, request))


@router.operation(
    "POST",
    "/forms/import",
    operation_id="import_form",
    role="total",
    external_effect=True,
    heavy=True,
    status_code=201,
    confirmation_text=IMPORT_TEXT,
    errors={
        422: "O corpo não vale (`validation_failed`: a definição não compila, o endereço está em "
        "uso ou reservado, uma imagem não existe ou o PDF de exemplo não sai; cada problema vem "
        "em `errors` com o caminho e a linha), falta `confirm: true` ou, quando o prompt cita "
        "dados de contato da lead, falta `confirm_contact: true` (`confirmation_required`). Em "
        "qualquer caso, nada é gravado."
    },
    summary="Importar um formulário",
    description=(
        "Cria um formulário a partir de uma definição pronta (o JSON que `get_form_version` "
        "devolve e que a exportação do admin escreve) e **publica já a versão 1**: o formulário "
        "passa a receber envios no endereço público dele, sem rascunho nem teste antes. Por isso a "
        'operação é marcada e exige `"confirm": true` (sem ele, `422 confirmation_required` e '
        "nada acontece). Confere a definição inteira como o banco a guarda: o compilador (com "
        "todos os problemas de uma vez), o endereço (livre e não reservado), as imagens que o PDF "
        "e a prévia pedem (precisam existir em Imagens, com os mesmos nomes) e o PDF de exemplo. "
        "Se o prompt cita o nome, o e-mail ou o telefone da lead, ele os manda ao LLM em todo "
        "pedido, e a importação exige também `confirm_contact: true` (sem ele, `422 "
        "confirmation_required` com os dados citados em `detail`, e nada é gravado); a mesma "
        "confirmação vale em `publish_form_draft`. A importação fica na auditoria (`form.import`, "
        "com os dados de contato confirmados) em nome da chave. Para criar um formulário e editar "
        "antes de publicar, use `create_form`."
    ),
    returns="O formulário importado, com a versão 1 publicada.",
    response_model=FormDetail,
)
def import_form(body: ImportFormRequest, key: CurrentKey, request: Request) -> FormDetail:
    deps = get_deps()
    try:
        text = json.dumps(body.sent, ensure_ascii=False)
    except (
        RecursionError
    ):  # aninhado a centenas de níveis: cabe no corpo, e o `json` não o percorre
        raise ApiError.invalid(IMPORT_REFUSED, [Problem("", TOO_DEEP)]) from None
    try:
        imported = stored_forms.import_definition(
            deps,
            text,
            actor=key.actor,
            note=body.note or "",
            contact_confirmed=body.confirm_contact,
        )
    except DefinitionError as refused:
        raise ApiError.invalid(IMPORT_REFUSED, refused.problems) from None
    except stored_forms.ContactInPrompt as cited:
        raise ApiError(
            "confirmation_required",
            CONTACT_TEXT.format(roles=stored_forms.contact_in_words(cited.roles)),
        ) from None
    logger.info(
        "api: formulário importado slug=%s versão=%s por=%s",
        imported.slug,
        imported.version_number,
        key.actor,
    )
    return FormDetail.of(form_detail(deps, imported.slug, request))


# --- a situação ---------------------------------------------------------------------------------

ACTIVATE_TEXT = (
    "Ativar o formulário volta a receber envios: a página pública e a API pública passam a aceitar "
    "as respostas das leads."
)
DEACTIVATE_TEXT = (
    "Desativar tira o formulário do ar para as leads: a página pública passa a dizer que ele está "
    "indisponível e a API pública recusa envios novos. Os pedidos que já existem seguem até o PDF e "
    "a entrega."
)
ALREADY = {
    True: "Nada mudou: o formulário já estava ativo (outra chamada, ou um pedido repetido).",
    False: "Nada mudou: o formulário já estava inativo (outra chamada, ou um pedido repetido).",
}


def _set_active(slug: str, active: bool, key: CurrentKey, request: Request) -> FormDetail:
    """Muda a situação pelo serviço da página (`stored_forms.set_active`, que audita em nome de
    quem chama) e devolve o formulário como ficou."""
    deps = get_deps()
    try:
        changed = stored_forms.set_active(deps.sessions, slug, active, actor=key.actor)
    except UnknownForm:
        raise ApiError("not_found", NOT_FOUND) from None
    if not changed:
        raise ApiError("state_conflict", ALREADY[active])
    logger.info(
        "api: formulário %s slug=%s por=%s", "ativado" if active else "desativado", slug, key.actor
    )
    return FormDetail.of(form_detail(deps, slug, request))


@router.operation(
    "POST",
    "/forms/{slug}/activate",
    operation_id="activate_form",
    role="total",
    external_effect=True,
    confirmation_text=ACTIVATE_TEXT,
    errors={
        409: "O formulário já estava ativo (`state_conflict`): nada mudou e nada foi auditado. "
        "Esta operação não pede versão."
    },
    summary="Ativar um formulário",
    description=(
        "Volta a receber envios: a página pública `/p/<slug>` e a API pública passam a aceitar as "
        "respostas das leads de novo. Muda o que a lead vê, por isso a operação é marcada e exige "
        '`"confirm": true` (sem ele, `422 confirmation_required` e nada acontece). A ativação fica '
        "na auditoria em nome da chave. Se o formulário já estava ativo, a resposta é `409 "
        "state_conflict` e nada é auditado: repetir a chamada é seguro."
    ),
    returns="O formulário como ficou, com `active` verdadeiro.",
    response_model=FormDetail,
)
def activate_form(
    slug: FORM, body: ActivateFormRequest, key: CurrentKey, request: Request
) -> FormDetail:
    return _set_active(slug, True, key, request)


@router.operation(
    "POST",
    "/forms/{slug}/deactivate",
    operation_id="deactivate_form",
    role="total",
    destructive=True,
    external_effect=True,
    confirmation_text=DEACTIVATE_TEXT,
    errors={
        409: "O formulário já estava inativo (`state_conflict`): nada mudou e nada foi auditado. "
        "Esta operação não pede versão."
    },
    summary="Desativar um formulário",
    description=(
        "Tira o formulário do ar para as leads: a página pública `/p/<slug>` passa a dizer que ele "
        "está indisponível e a API pública recusa envios novos (`410 form_unavailable`). Os pedidos "
        "que já existem seguem a versão deles até o PDF e a entrega; o destino do webhook e os "
        "prazos de retenção continuam editáveis. Para voltar, use `activate_form`. Como tira o "
        'formulário do ar, a operação é marcada e exige `"confirm": true` (sem ele, `422 '
        "confirmation_required` e nada acontece). A desativação fica na auditoria em nome da chave. "
        "Se o formulário já estava inativo, a resposta é `409 state_conflict` e nada é auditado."
    ),
    returns="O formulário como ficou, com `active` falso.",
    response_model=FormDetail,
)
def deactivate_form(
    slug: FORM, body: DeactivateFormRequest, key: CurrentKey, request: Request
) -> FormDetail:
    return _set_active(slug, False, key, request)


# --- a retenção ---------------------------------------------------------------------------------

RETENTION_TEXT = (
    "Mudar os prazos de retenção vale também para os pedidos que já existem, e reduzir um prazo "
    "apaga dado de lead na próxima retenção, sem volta."
)
RETENTION_CHANGED = (
    "Os prazos mudaram desde que você os leu (outra pessoa ou outra chamada). Nada foi gravado: "
    "leia de novo com `get_form_retention` e refaça a mudança."
)


def _retention(deps: Deps, slug: str) -> FormRetention:
    """Os prazos do formulário como a página os lê, ou `404`."""
    with deps.sessions() as session:
        found = form_queries.form_retention_of(session, slug, deps.settings)
    if found is None:
        raise ApiError("not_found", NOT_FOUND)
    return FormRetention.of(found)


def _confirmation_asked(needed: form_retention.ConfirmationNeeded, *, again: bool) -> ApiError:
    """A recusa de uma redução sem a contagem certa: diz o que a próxima retenção apagaria, e o
    `seen` que a confirma. `again`: a pessoa mandou uma contagem, e ela já não é a de agora."""
    reduction = form_queries.reduction_of(needed.due, needed.today)
    return ApiError(
        "confirmation_required",
        f"{'A contagem mudou desde a que você confirmou. ' if again else ''}"
        f"{form_retention.deletion_sentence(reduction.due)} Para gravar mesmo assim, repita a "
        f'chamada com `"seen": "{reduction.seen}"` (a contagem que você viu). Nada foi gravado.',
    )


@router.operation(
    "GET",
    "/forms/{slug}/retention",
    operation_id="get_form_retention",
    role="leitura",
    summary="Ler os prazos de retenção de um formulário",
    description=(
        "Os dois prazos de retenção do formulário `slug`: o do arquivo do PDF e o do pedido inteiro "
        "(lead, respostas, plano), em dias. `own` é o que o formulário define (nulo: o padrão do "
        "ambiente), `default` os padrões do ambiente e `in_force` o que vale de fato. `version` é o "
        "texto opaco que `save_form_retention` devolve em `expected_version`. `link_ttl_days` e "
        "`max_days` são os limites que a mudança confere. Não grava nada."
    ),
    returns="Os prazos do formulário, os padrões do ambiente, o que vale e a versão.",
    response_model=FormRetention,
)
def get_form_retention(slug: FORM) -> FormRetention:
    return _retention(get_deps(), slug)


@router.operation(
    "PUT",
    "/forms/{slug}/retention",
    operation_id="save_form_retention",
    role="total",
    destructive=True,
    confirmation_text=RETENTION_TEXT,
    errors={
        409: "Os prazos mudaram desde que você os leu (`version_conflict`, com a versão de "
        "agora): nada é gravado. Leia de novo e refaça a mudança.",
        422: "O corpo não vale (`validation_failed`: um prazo fora de 1 a 3650 dias, ou menor do "
        "que a regra aceita, vem com o campo em `errors`), falta `confirm: true` (`confirmation_"
        "required`) ou falta a contagem de uma redução (`confirmation_required`, com a contagem "
        "e o `seen` que a confirma). Em qualquer caso, nada é gravado.",
    },
    summary="Mudar os prazos de retenção de um formulário",
    description=(
        "Grava os dois prazos do formulário, sem publicação: valem a partir da próxima retenção, "
        "também para os pedidos que já existem. Troca os dois de uma vez (`null` volta um ao padrão "
        "do ambiente); `expected_version` é a `version` que `get_form_retention` devolveu, e se "
        "mudou a resposta é `409 version_conflict` e nada é gravado. As regras são as da página: de "
        "1 a 3650 dias, o PDF não dura menos que a validade do link da lead mais uma folga, e o "
        "pedido não dura menos que o PDF (um prazo fora disso é `422 validation_failed`, com o "
        "campo `pdf_days` ou `request_days`). **Reduzir um prazo apaga dado de lead na próxima "
        'retenção**, sem volta: por isso a operação é marcada e exige `"confirm": true`, e uma '
        "redução (o prazo que vale diminui, inclusive voltar a um padrão menor) exige ainda a "
        "contagem do que ela apagaria. Sem `seen`, ou com uma contagem que já não é a de agora, a "
        "resposta é `422 confirmation_required` com a contagem e o `seen` a repetir; só então "
        "grava. Gravar prazos iguais aos de agora não muda nada. A mudança fica na auditoria em "
        "nome da chave, com os prazos de antes e de depois e a contagem confirmada, nunca dado de "
        "lead."
    ),
    returns="Os prazos como ficaram, com a versão nova.",
    response_model=FormRetention,
)
def save_form_retention(
    slug: FORM, body: SaveFormRetentionRequest, key: CurrentKey, request: Request
) -> FormRetention:
    deps = get_deps()
    confirmed = form_retention.parse_seen(body.seen) if body.seen is not None else None
    try:
        saved = form_retention.save(
            deps.sessions,
            deps.settings,
            slug,
            new=form_retention.Periods(body.pdf_days, body.request_days),
            expected=body.expected_version,
            actor=key.actor,
            now=now(request),
            confirmed=confirmed,
        )
    except UnknownForm:
        raise ApiError("not_found", NOT_FOUND) from None
    except form_retention.Changed:
        with deps.sessions() as session:
            current = form_retention.periods_of(session, slug)
        raise ApiError(
            "version_conflict",
            RETENTION_CHANGED,
            current_version=current.token if current is not None else None,
        ) from None
    except form_retention.Refused as refused:
        raise ApiError(
            "validation_failed",
            "Os prazos não valem; veja o motivo de cada campo em `errors`.",
            errors=[ErrorItem(field=name, message=why) for name, why in refused.problems.items()],
        ) from None
    except form_retention.ConfirmationNeeded as needed:
        raise _confirmation_asked(needed, again=confirmed is not None) from None
    if saved.changed:
        logger.info(
            "api: prazos de retenção alterados formulario=%s pdf=%s pedido=%s por=%s",
            slug,
            saved.periods.pdf_days,
            saved.periods.request_days,
            key.actor,
        )
    return _retention(deps, slug)


# --- as versões ---------------------------------------------------------------------------------

MAX_VERSION = 10**version_actions.MAX_NUMBER_DIGITS - 1  # o banco guarda o número num integer
NO_DEFINITION = (
    "Esta versão não existe: só as versões publicadas dos formulários do banco têm uma definição."
)
NOTHING_TO_COMPARE = (
    "Este formulário ainda não tem versão publicada para comparar: a primeira publicação cria a "
    "versão 1."
)
NO_SUCH_VERSION = "Um dos lados da comparação não existe neste formulário."
SIDE_PATTERN = rf"^(draft|[0-9]{{1,{version_actions.MAX_NUMBER_DIGITS}}})$"
SIDE_TEXT = (
    "o número de uma versão publicada (`get_form` lista as que existem) ou `draft`, o rascunho do "
    "formulário como está salvo"
)


@router.operation(
    "GET",
    "/forms/{slug}/versions/{number}",
    operation_id="get_form_version",
    role="leitura",
    errors={
        404: "O formulário não existe, ou não tem essa versão publicada (`not_found`): só as "
        "versões publicadas têm uma definição."
    },
    summary="Ler a definição de uma versão publicada",
    description=(
        "A definição da versão `number` do formulário `slug`, **exatamente como o banco a guarda**: "
        "o mesmo JSON que a exportação da página escreve, e que importado em outro ambiente dá o "
        "mesmo formulário. Ela não passa pelo compilador na resposta, então uma versão que o código "
        "de hoje já não compila também se lê (para guardar, comparar ou consertar). O schema "
        "`FormDocument` do contrato descreve o formato que o compilador aceita, e **não garante o "
        "texto guardado**: a resposta não é validada contra ele, e uma versão antiga pode trazer "
        "valores que ele recusa. Os padrões do "
        "formato ficam como foram escritos (uma pergunta com `required: true` por extenso a "
        "mostra assim) e uma condição (`show_when`) vem em regras, não em frase. É por isso que "
        "`compare_form_versions`, que compara as definições na forma normal e escreve as condições "
        "em frase, não é a diferença entre duas respostas desta operação. Junto vêm o número, quem "
        "publicou, quando, a nota e a impressão digital. Só as versões publicadas têm definição; o "
        "rascunho se compara com uma versão por `compare_form_versions`. Não grava nada."
    ),
    returns="A versão publicada, com a definição como o banco a guarda.",
    response_model=FormVersion,
)
def get_form_version(
    slug: FORM,
    number: Annotated[
        int,
        Path(
            ge=1,
            le=MAX_VERSION,
            description="O número da versão publicada (`versions` de `get_form` lista as que "
            "existem).",
        ),
    ],
) -> JSONResponse:
    with get_deps().sessions() as session:
        found = stored_forms.stored_version(session, slug, number)
    if found is None:
        raise ApiError("not_found", NO_DEFINITION)
    # A definição sai como está no banco: o modelo `FormVersion` a descreve no contrato, mas não a
    # relê (a leitura pelo modelo acrescentaria os padrões e recusaria a versão que não compila).
    return JSONResponse(FormVersion.body_of(found))


def _side(side: str | None) -> str:
    """O lado da comparação no vocabulário da ação: `draft` é o rascunho; vazio, a escolha padrão."""
    if side is None:
        return ""
    return version_actions.DRAFT_REF if side == "draft" else side


@router.operation(
    "GET",
    "/forms/{slug}/comparison",
    operation_id="compare_form_versions",
    role="leitura",
    errors={
        404: "O formulário não existe, ainda não tem versão publicada, ou um dos lados não existe "
        "(`not_found`)."
    },
    summary="Comparar duas versões de um formulário",
    description=(
        "Compara dois lados do formulário `slug`, seção por seção (página pública, avisos, etapas "
        "e perguntas, identidade, consentimento, pontuação, instruções, prompt, modelo do plano, "
        "fases, dados, PDF, prévia, exemplos e `outras`): `from` e `to` são, cada um, "
        f"{SIDE_TEXT}. Sem `from` ou sem `to`, vale a escolha padrão de cada lado: se há um "
        "rascunho pendente (`draft_pending`), a versão atual contra o rascunho; senão, a versão "
        "anterior à atual contra a atual. Cada seção diz se mudou (`changed`) e traz as linhas da "
        "diferença com duas de contexto. **A comparação lê a forma normal das definições**, e por "
        "isso difere da diferença entre duas respostas de `get_form_version`: o padrão do formato "
        "escrito por extenso (`required: true`) não conta como mudança, e uma condição "
        "(`show_when`) aparece na frase em português (`Aparece quando ... é ...`), não nas regras "
        "do JSON. Um formulário sem versão publicada não tem o que comparar (`404`). Não grava "
        "nada."
    ),
    returns="As seções comparadas, com as linhas que mudaram, e quais versões podem ser escolhidas.",
    response_model=FormComparison,
)
def compare_form_versions(
    slug: FORM,
    old: Annotated[
        str | None,
        Query(
            alias="from",
            pattern=SIDE_PATTERN,
            description=f"O lado de onde a mudança parte: {SIDE_TEXT}. Ausente: a escolha padrão.",
            examples=["1"],
        ),
    ] = None,
    new: Annotated[
        str | None,
        Query(
            alias="to",
            pattern=SIDE_PATTERN,
            description=f"O lado a que a mudança chega: {SIDE_TEXT}. Ausente: a escolha padrão.",
            examples=["draft"],
        ),
    ] = None,
) -> FormComparison:
    try:
        with get_deps().sessions() as session:
            result = version_actions.compare(session, slug, old=_side(old), new=_side(new))
    except UnknownForm:
        raise ApiError("not_found", NOT_FOUND) from None
    except version_actions.NothingToCompare:
        raise ApiError("not_found", NOTHING_TO_COMPARE) from None
    except version_actions.UnknownVersion:
        raise ApiError("not_found", NO_SUCH_VERSION) from None
    return FormComparison.of(result)
