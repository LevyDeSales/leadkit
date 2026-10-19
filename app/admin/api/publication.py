"""A publicação do rascunho: `review_form_draft_publication`, `publish_form_draft` (com `dry_run`) e
`revert_form_draft_to_version`.

Agem pelos mesmos serviços das páginas da publicação (`app.forms.drafts`: `review`, `publish` e
`revert`): o que a publicação faria, o que falta e a gravação são uma regra só, e a API a traduz em
JSON e status. **Nenhuma conferência é afrouxada aqui**: publicar pede a definição que compila, a nota,
o teste do que vai ao LLM, a confirmação dos dados de contato no prompt e o PDF de exemplo no adaptador.

- `review_form_draft_publication` só lê (`leitura`) e **não gera o PDF**.
- `publish_form_draft` é `x-external-effect` (muda o que a lead vê e vale já para os envios novos), exige
  `"confirm": true` e, com `dry_run: true`, só simula: roda tudo, o PDF de exemplo incluído, e não grava,
  não audita e não pede o `confirm` (`Confirmation.unless`, em `app.admin.api.operations`). Conta como
  operação pesada (5 fichas), porque a simulação e a publicação renderizam o PDF.
- `revert_form_draft_to_version` é `x-destructive` (troca o que o rascunho tinha): copia a versão para o
  rascunho, e a publicação segue o caminho normal.

A recusa da publicação volta como `422 validation_failed` com as duas listas de `PublishRefused`:
`reasons` (o que falta, em português) e `errors` (os problemas, com o caminho do campo). Só falta a
confirmação dos dados de contato: `422 confirmation_required`, como em `import_form` (ADR 0015,
"Decisões da fase 3.1"). A auditoria sai do serviço (`form.publish`, `form.draft.revert`), com o autor
`chave:<nome>`.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Path
from fastapi.responses import JSONResponse

from app.admin.api.drafts import draft_changed
from app.admin.api.errors import ApiError, ErrorItem
from app.admin.api.forms import FORM, MAX_VERSION, NOT_FOUND
from app.admin.api.keys import KeyInfo
from app.admin.api.operations import OperationRouter
from app.admin.api.schemas.drafts import FormDraft
from app.admin.api.schemas.forms import DraftState, FormVersionInfo
from app.admin.api.schemas.publication import (
    FormDraftPublication,
    PublicationReview,
    PublishFormDraftRequest,
    RevertFormDraftRequest,
)
from app.admin.api.security import CurrentKey
from app.deps import Deps, get_deps
from app.forms import UnknownForm, drafts
from app.forms import stored as stored_forms
from app.forms.definition import DefinitionError, Problem

logger = logging.getLogger(__name__)

TAG = "Publicação"
router = OperationRouter(
    tag=TAG,
    description=(
        "Publicar o rascunho de um formulário e voltar a uma versão publicada. Publicar cria a versão "
        "seguinte e **muda o que a lead vê, já para os envios novos**: por isso a revisão "
        "(`review_form_draft_publication`, de `leitura`, sem gerar o PDF) mostra antes as mudanças por "
        "seção, o que vai ao LLM, os dados de contato no prompt e o que falta, e a simulação "
        "(`publish_form_draft` com `dry_run: true`, de `total`) roda também o PDF de exemplo sem "
        "gravar. Publicar de verdade (`publish_form_draft`) e voltar a uma versão "
        "(`revert_form_draft_to_version`) são de `total`, exigem `confirm: true` (a simulação não) e "
        "pedem a versão do rascunho que você viu (`expected_version`). Editar o rascunho é da tag "
        "«Rascunho»; testá-lo com o LLM, da «Teste do rascunho»."
    ),
)

PUBLISH_TEXT = (
    "Publicar muda o que a lead vê: a versão nova vale já para os envios novos, sem reiniciar, e não "
    "se desfaz (só se publica outra por cima)."
)
PUBLISH_REFUSED = (
    "A publicação não aconteceu; veja o que falta em `reasons` e, se houver, o caminho de cada "
    "problema em `errors`. Nada foi gravado."
)
CONTACT_REASON = (
    "O prompt manda {roles} da lead ao LLM em todo pedido. Para publicar mesmo assim, mande "
    "`confirm_contact: true`, dizendo que você sabe o que ele envia ao provedor do modelo."
)
CONTACT_TEXT = CONTACT_REASON + " Nada foi gravado."
REVERT_TEXT = (
    "Voltar a uma versão troca o rascunho inteiro pela cópia dela: o que estava no rascunho se "
    "perde. O que as leads veem só muda na publicação seguinte."
)
REVERT_BROKEN = (
    "A versão não compila mais com o código deste deploy; veja o motivo de cada campo em `errors`. "
    "Nada foi gravado."
)
VERSION_NOT_FOUND = "O formulário não existe, ou não tem essa versão publicada (`not_found`)."
CHANGED = (
    "O rascunho mudou desde que você o leu (`version_conflict`, com a versão de agora): nada é "
    "gravado. Leia de novo com `review_form_draft_publication` e refaça a chamada."
)
PUBLISH_422 = (
    "O corpo não vale (`validation_failed`: `errors` traz o campo, e `reasons`, o que falta para "
    "publicar: nada para publicar, a definição que não compila, o teste do que vai ao LLM, a nota, o "
    "PDF de exemplo que o adaptador recusou) ou falta confirmar (`confirmation_required`: o `confirm: "
    "true`, que a simulação com `dry_run: true` dispensa, ou o `confirm_contact: true` quando o "
    "prompt cita dados de contato e nada mais falta). Em qualquer caso, nada é gravado."
)


def _roles(roles: tuple[str, ...]) -> str:
    return stored_forms.contact_in_words(roles)


def _reasons(reasons: tuple[str, ...], contact: tuple[str, ...]) -> list[str]:
    """As razões do serviço para a API: a confirmação dos dados de contato diz o campo a mandar
    (`confirm_contact`), que a página não tem como dizer (lá é uma caixa). Nenhuma outra muda."""
    if drafts.CONTACT_UNCONFIRMED not in reasons:
        return list(reasons)
    said = CONTACT_REASON.format(roles=_roles(contact))
    return [said if reason == drafts.CONTACT_UNCONFIRMED else reason for reason in reasons]


def _items(problems: tuple[Problem, ...]) -> list[ErrorItem]:
    return [ErrorItem(field=p.path, message=p.message, line=p.line) for p in problems]


def _refusal_items(refused: drafts.PublishRefused) -> list[ErrorItem]:
    """Os `errors` da recusa: os problemas da definição ou do PDF e, se faltou a nota, o campo `note`
    (a falta dela é uma razão do serviço, mas é também o campo do corpo que o cliente corrige)."""
    items = _items(refused.problems)
    if drafts.NOTE_REQUIRED in refused.reasons:
        items.append(ErrorItem(field="note", message=drafts.NOTE_REQUIRED))
    return items


@router.operation(
    "GET",
    "/forms/{slug}/draft/review",
    operation_id="review_form_draft_publication",
    role="leitura",
    summary="Revisar o que a publicação do rascunho faria",
    description=(
        "O que publicar o rascunho do formulário `slug` faria e o que falta para isso, **sem gerar o "
        "PDF de exemplo** e sem gravar nada: `number` é a versão que seria criada, `changes` as "
        "seções que mudam contra a versão publicada atual (com as linhas da diferença, como em "
        "`compare_form_versions`), `test` o que vai ao LLM e se um teste com sucesso o cobre "
        "(`not_needed`, `covered` ou `missing`: a publicação é recusada enquanto falta), `contact` os "
        "dados de contato da lead que o prompt manda ao LLM (publicar pede `confirm_contact: true`), "
        "`consent_version` a versão nova do texto do consentimento, `dropped_values` os valores de "
        "opção que os envios novos deixam de trazer, `stale_assets` as imagens trocadas desde a "
        "versão atual e `missing` e `problems` o que falta, tudo de uma vez: nada para publicar, a "
        "definição que não compila e o teste. A nota e a confirmação dos dados de contato são de quem "
        "publica e não entram aqui. `version` é a do rascunho que esta revisão leu: "
        "`publish_form_draft` a recebe em `expected_version`. O PDF de exemplo só a simulação "
        "(`publish_form_draft` com `dry_run: true`, de `total`) e a publicação geram. Um formulário "
        "criado do zero tem a primeira publicação, que cria a versão 1 e o põe no ar."
    ),
    returns="A revisão: o que a publicação faria e o que falta.",
    response_model=PublicationReview,
)
def review_form_draft_publication(slug: FORM) -> PublicationReview:
    with get_deps().sessions() as session:
        draft = drafts.load(session, slug)
        if draft is None:
            raise ApiError("not_found", NOT_FOUND)
        found = drafts.review(session, draft)
    return PublicationReview.of(found, missing=found.blockers(), problems=found.problems)


def _simulation(deps: Deps, slug: str, body: PublishFormDraftRequest, key: KeyInfo) -> JSONResponse:
    """A simulação: o que a publicação faria, sem gravar nem auditar."""
    simulation = drafts.publish(
        deps,
        slug,
        actor=key.actor,
        note=body.note,
        expected=body.expected_version,
        contact_confirmed=body.confirm_contact,
        dry_run=True,
    )
    review = PublicationReview.of(
        simulation.review,
        missing=_reasons(simulation.reasons, simulation.review.contact),
        problems=simulation.problems,
    )
    result = FormDraftPublication(
        dry_run=True,
        published=False,
        ready=simulation.ready,
        review=review,
        published_version=None,
        draft=DraftState.of(simulation.review.draft),
    )
    logger.info(
        "api: simulação de publicação slug=%s pronta=%s por=%s", slug, simulation.ready, key.actor
    )
    return JSONResponse(result.model_dump(mode="json"))


@router.operation(
    "POST",
    "/forms/{slug}/draft/publish",
    operation_id="publish_form_draft",
    role="total",
    external_effect=True,
    heavy=True,
    confirm_unless="dry_run",
    confirmation_text=PUBLISH_TEXT,
    errors={409: CHANGED, 422: PUBLISH_422},
    summary="Publicar o rascunho (ou simular a publicação)",
    description=(
        "Publica o rascunho do formulário `slug` como a versão seguinte, com a `note`. **Muda o que "
        "a lead vê**: a versão nova vale já para os envios novos, sem reiniciar (os pedidos que "
        "já existem seguem a versão deles), a situação do formulário não muda e o rascunho sai. Por "
        'isso a operação é marcada e exige `"confirm": true` (sem ele, `422 confirmation_required` e '
        "nada acontece), e `expected_version` é a `version` que `review_form_draft_publication` ou "
        "`get_form_draft` devolveu: se o rascunho mudou desde então, `409 version_conflict`. "
        "**Nada é afrouxado em relação à página**: a definição tem de compilar neste deploy, a nota é "
        "obrigatória, se mudou algo que vai ao LLM é preciso um teste com sucesso depois da mudança "
        "(`start_form_draft_test`), se o prompt cita o nome, o e-mail ou o telefone da lead é preciso "
        "`confirm_contact: true` (a mesma confirmação de `import_form`; sem ela, e se nada mais falta, "
        "`422 confirmation_required` com os dados citados em `detail`) e o PDF de exemplo tem de sair "
        "no adaptador, com as imagens que a versão fixa. Recusada, a resposta é `422 "
        "validation_failed` com as duas listas: `reasons`, o que falta, e `errors`, os problemas da "
        "definição ou do PDF com o caminho do campo e a linha (e a nota que falta, no campo `note`). Repetir a chamada depois de uma "
        "resposta perdida é seguro e não publica de novo: dá `422` (nada a publicar) ou, se o "
        "rascunho mudou na publicação (a versão do consentimento sobe), `409`. Com "
        "**`dry_run: true`** roda as mesmas conferências, **inclusive o PDF de exemplo no "
        "adaptador**, e responde `200` com o que faria e o que falta (`review`, `ready`), **sem "
        "gravar, sem auditar e sem exigir o `confirm`**, porque não tem efeito; a falta de nota, de "
        "teste ou de `confirm_contact` não recusa a simulação, aparece em `review.missing`. É a mesma "
        "operação e pede papel `total`; quem só tem `leitura` usa `review_form_draft_publication`. A "
        "publicação fica na auditoria (`form.publish`, com a versão, a impressão digital, as imagens "
        "fixadas e os dados de contato confirmados) em nome da chave."
    ),
    returns=(
        "A versão publicada (`published_version`), a revisão que a publicação seguiu e o rascunho "
        "depois dela; na simulação, o que ela faria, sem versão criada."
    ),
    response_model=FormDraftPublication,
)
def publish_form_draft(slug: FORM, body: PublishFormDraftRequest, key: CurrentKey) -> JSONResponse:
    deps = get_deps()
    try:
        if body.dry_run:
            return _simulation(deps, slug, body, key)
        published = drafts.publish(
            deps,
            slug,
            actor=key.actor,
            note=body.note,
            expected=body.expected_version,
            contact_confirmed=body.confirm_contact,
        )
    except UnknownForm:
        raise ApiError("not_found", NOT_FOUND) from None
    except drafts.DraftChanged:
        raise draft_changed(deps, slug) from None
    except drafts.PublishRefused as refused:
        if refused.only_contact_missing:
            raise ApiError(
                "confirmation_required", CONTACT_TEXT.format(roles=_roles(refused.review.contact))
            ) from None
        raise ApiError(
            "validation_failed",
            PUBLISH_REFUSED,
            errors=_refusal_items(refused),
            reasons=_reasons(refused.reasons, refused.review.contact),
        ) from None
    with deps.sessions() as session:
        draft = drafts.load(session, slug)
        version = stored_forms.stored_version(session, slug, published.number)
    assert draft is not None and version is not None  # a publicação acabou de criar os dois
    result = FormDraftPublication(
        dry_run=False,
        published=True,
        ready=True,
        review=PublicationReview.of(published.review, missing=[], problems=[]),
        published_version=FormVersionInfo.of(version),
        draft=DraftState.of(draft),
    )
    logger.info(
        "api: formulário publicado slug=%s versão=%s por=%s", slug, published.number, key.actor
    )
    return JSONResponse(result.model_dump(mode="json"))


@router.operation(
    "POST",
    "/forms/{slug}/versions/{number}/revert",
    operation_id="revert_form_draft_to_version",
    role="total",
    destructive=True,
    confirmation_text=REVERT_TEXT,
    errors={
        404: VERSION_NOT_FOUND,
        409: CHANGED,
        422: "O corpo não vale (`validation_failed`: o `number` não é um número de versão, falta o "
        "`expected_version`, ou a versão não compila mais com o código deste deploy, e então `errors` "
        "traz o caminho de cada problema dela) ou falta `confirm: true` (`confirmation_required`). "
        "Em qualquer caso, nada é gravado.",
    },
    summary="Voltar o rascunho a uma versão publicada",
    description=(
        "Copia a versão `number` do formulário `slug` para o rascunho, **no lugar do que ele tinha** "
        '(o que o rascunho tinha se perde, por isso a operação é `x-destructive` e exige `"confirm": '
        "true`: sem ele, `422 confirmation_required` e nada acontece). `expected_version` é a "
        "`version` do rascunho que você viu (`get_form_draft` ou `review_form_draft_publication`); se "
        "ele mudou, `409 version_conflict`. **Isto não publica nada**: as leads só veem o que "
        "`publish_form_draft` publica, e a cópia segue o caminho normal (o teste, se o que vai ao LLM "
        "difere da versão atual, e a publicação, que cria uma versão nova igual à antiga). Voltar à "
        "versão atual descarta o rascunho. Uma versão antiga que o código deste deploy não compila mais "
        "não volta: `422` com os problemas dela em `errors`. A operação fica na auditoria "
        "(`form.draft.revert`, com o número da versão) em nome da chave. As versões que existem estão "
        "em `get_form`; a definição de uma, em `get_form_version`."
    ),
    returns="O rascunho como ficou: o documento copiado, a versão nova dele e o estado.",
    response_model=FormDraft,
)
def revert_form_draft_to_version(
    slug: FORM,
    number: Annotated[
        int,
        Path(
            ge=1,
            le=MAX_VERSION,
            description="O número da versão publicada a copiar (`versions` de `get_form` lista as "
            "que existem).",
        ),
    ],
    body: RevertFormDraftRequest,
    key: CurrentKey,
) -> JSONResponse:
    deps = get_deps()
    try:
        reverted = drafts.revert(
            deps, slug, number, actor=key.actor, expected=body.expected_version
        )
    except UnknownForm:
        raise ApiError("not_found", VERSION_NOT_FOUND) from None
    except drafts.DraftChanged:
        raise draft_changed(deps, slug) from None
    except DefinitionError as broken:
        raise ApiError.invalid(REVERT_BROKEN, broken.problems) from None
    logger.info(
        "api: versão copiada para o rascunho slug=%s versão=%s por=%s", slug, number, key.actor
    )
    return JSONResponse(FormDraft.body_of(reverted))
