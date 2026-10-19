"""As prévias do rascunho de um formulário: `render_form_draft_pdf_preview` (o PDF),
`render_form_draft_result_preview` (a página de resultado) e `render_form_draft_public_page` (a página
pública).

Agem pelas mesmas ações das páginas do editor do PDF e da prévia (`app.admin.actions.previews`): o
contrato do rascunho com o PDF e a prévia que a chamada manda, a escolha do plano, o PDF do adaptador do
pedido e o HTML de cada página são uma regra só, e a API os devolve como a página os mostra para os
mesmos dados (os testes de paridade comparam). **As três só leem**: não gravam, não auditam, não chamam o
LLM e não tocam dado de lead, porque usam o plano de exemplo do rascunho ou o do último teste, e as
respostas de exemplo. Por isso são de `leitura`; custam 5 fichas do limite de uso (renderizar gasta CPU
do processo que também serve as leads).

**A resposta não é JSON.** O PDF sai como `application/pdf`, e as duas páginas como `text/html` com
`Content-Disposition: inline`. O erro (o template com a linha, o plano do último teste que falta, o PDF
que o adaptador não gerou) segue no formato de erro da API.

**Mandar um template é do papel `total`.** O PDF e a página de resultado aceitam no corpo o PDF, a
prévia e as notas que a chamada quer testar (`pdf`, `preview`); compilá-los e renderizá-los custa CPU do
processo que também serve as leads, então esses campos pedem `total` (`ensure_role`, o `403
forbidden_role` vem antes de qualquer conta). A chave `leitura` pré-visualiza o rascunho salvo (sem corpo,
com `{}` ou só com `source`), que já compilou quando foi salvo.

**O HTML da API não roda no contexto do admin, servido por ela.** As páginas pedem o CSS, o JavaScript e
as imagens do app (`/static` e as imagens do formulário), então levam um `<base>` para a origem do app
(`PUBLIC_BASE_URL`), e a resposta traz a política de conteúdo no cabeçalho (`PUBLIC_PAGE_POLICY` e
`RESULT_PAGE_POLICY`): `sandbox` sem `allow-same-origin`, ou seja, uma origem nula, sem acesso à sessão
do admin, aos cookies nem à API; a página pública roda só os scripts do app (`script-src 'self'`) e a de
resultado nenhum, nem o que o operador escreveu na prévia. **Essa proteção é do cabeçalho:** salva em
arquivo ou aberta fora da resposta da API, a página perde o cabeçalho, e só resta a `<meta http-equiv>` com
a mesma política (menos o `sandbox` e o `frame-ancestors`, que a meta não aceita, e com a origem do app no
lugar do `'self'`); por isso o HTML de um template escrito por quem chama não deve ser aberto fora da
API num contexto que importe. O `<base>` só vale quando `PUBLIC_BASE_URL` tem a mesma origem da API
(`base-uri 'self'`): senão o navegador o ignora, e a página pede os arquivos à origem de onde veio. Quem
só quer ler a estrutura do formulário usa `get_form_draft`, que é JSON.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Query, Request
from fastapi.responses import Response

from app.admin.actions import previews
from app.admin.api.common import HtmlResponse, PdfResponse, now
from app.admin.api.errors import ApiError, ErrorItem
from app.admin.api.forms import FORM, NOT_FOUND
from app.admin.api.keys import KeyInfo
from app.admin.api.operations import OperationRouter
from app.admin.api.schemas.previews import PreviewDraftRequest
from app.admin.api.security import CurrentKey, ensure_role
from app.deps import Deps, get_deps
from app.forms import UnknownForm, draft_previews
from app.forms.definition import DefinitionError
from app.web.origin import origin_of

logger = logging.getLogger(__name__)

TAG = "Prévias do rascunho"
router = OperationRouter(
    tag=TAG,
    description=(
        "Pré-visualizar o rascunho de um formulário sem gravar nada: o PDF, a página de resultado e a "
        "página pública, como a lead os veria, com o plano de exemplo do rascunho (ou o do último "
        "teste) e as respostas de exemplo. São de `leitura`, não chamam o LLM e custam 5 fichas do "
        "limite de uso. O PDF e a página de resultado aceitam o PDF e a prévia que você quer testar no "
        "corpo (sem gravá-los); o erro de um template volta com o campo e a linha. As respostas não são "
        "JSON: o PDF é `application/pdf` e as páginas são `text/html`. Para ler a estrutura do "
        "rascunho, `get_form_draft`; para gravá-la, `save_form_draft`."
    ),
)

# Sem corpo, vale o rascunho salvo: o corpo é opcional, e o contrato o declara como o modelo (sem um
# `anyOf` com nulo), com o valor que ele tem quando nada é mandado.
NO_CHANGES = PreviewDraftRequest()
MAX_STEP = 9999
STEP = Annotated[
    int,
    Query(
        ge=1,
        le=MAX_STEP,
        description="A etapa em que a página abre, a partir de 1 (a primeira, se ausente). Passando "
        "do número de etapas do formulário, abre na última. A página traz todas as etapas, porque "
        "as condições atravessam etapas, e o JavaScript dela abre a pedida.",
    ),
]

SELF = "'self'"
NONE = "'none'"


def content_policy(*, scripts: bool, origin: str | None = None, header: bool = True) -> str:
    """A política de conteúdo do HTML que a API entrega. No **cabeçalho** (`header=True`), `sandbox` sem
    `allow-same-origin` dá ao documento uma origem nula: ele não lê o cookie nem o armazenamento do admin,
    nem fala com a API como o admin, ainda que alguém o abra no navegador. O resto fecha tudo o que a
    página não usa: só o app serve CSS, imagem e fonte (`'self'`, a origem da resposta), nada se
    conecta (`connect-src 'none'`) nem se envia (`form-action 'none'`), e nada emoldura a página.
    `scripts` deixa rodar os scripts do app (a página pública); sem ele, nenhum script roda (a de
    resultado, que leva o HTML que o operador escreveu).

    Na **`<meta http-equiv>`** (`header=False`), a mesma política, menos o `sandbox` e o
    `frame-ancestors`, que a meta não aceita. Ela vale onde o cabeçalho se perdeu (a página salva em
    arquivo, mostrada fora da API): lá `'self'` seria o arquivo, e os arquivos vêm da origem do app
    (`origin`, a do `<base>`), então ela entra ao lado do `'self'` (que, servida pela API, é a mesma
    origem) e é a única que o `base-uri` deixa; sem origem, a meta não deixa nenhum `<base>`."""
    sources = SELF if header or origin is None else f"{SELF} {origin}"
    parts = []
    if header:
        parts.append("sandbox allow-scripts" if scripts else "sandbox")
    parts.append("default-src 'none'")
    if scripts:
        parts.append(f"script-src {sources}")
    parts += [f"style-src {sources}", f"img-src {sources} data:", f"font-src {sources}"]
    if scripts:
        parts.append("connect-src 'none'")
    parts.append("form-action 'none'")
    parts.append(f"base-uri {SELF if header else (origin or NONE)}")
    if header:
        parts.append("frame-ancestors 'none'")
    parts.append("object-src 'none'")
    return "; ".join(parts)


PUBLIC_PAGE_POLICY = content_policy(scripts=True)
# A página de resultado leva o HTML que o operador escreveu: nenhum script roda nela.
RESULT_PAGE_POLICY = content_policy(scripts=False)
PAGE_HEADERS = {"X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}
# O PDF não leva `sandbox` (o visualizador de PDF do Chrome não abre num documento assim), mas ninguém o
# emoldura.
PDF_POLICY = "frame-ancestors 'none'"

TEMPLATES_ASKED = "Mandar o PDF, a prévia ou as notas no corpo (`pdf`, `preview`)"
ROLE_403 = (
    "A chave `leitura` mandou o PDF, a prévia ou as notas no corpo (`pdf`, `preview`) "
    "(`forbidden_role`): compilar e renderizar um texto que quem chama escreveu é só do papel `total`. "
    "Nada é compilado. Sem esses campos do corpo (sem corpo, `{}` ou só `source`), a `leitura` "
    "pré-visualiza o rascunho salvo."
)
PLAN_409 = (
    "A prévia pediu o plano do último teste (`source: test`), mas não há um teste com sucesso guardado "
    "ou o modelo do plano do rascunho já não o aceita (`state_conflict`): rode o teste de novo "
    "(`start_form_draft_test`) ou use o plano de exemplo."
)
TEMPLATE_422 = (
    "O corpo não vale ou um template não compila (`validation_failed`): o PDF ou a prévia pedem o que "
    "o app não serve, ou têm um erro. Cada problema vem em `errors` com o caminho no documento "
    "(`pdf.html`, `pdf.css`, `preview` ou `pdf.scores.<n>`) e a linha do template, nunca com o valor "
    "recebido; `pdf` sem linha é um PDF que o adaptador não conseguiu gerar. Nada é gravado."
)
NOT_RENDERED = "A prévia não saiu; veja o motivo de cada campo em `errors`. Nada foi gravado."
PAGE_NOT_BUILT = "A página não saiu; veja o motivo de cada campo em `errors`. Nada foi gravado."
PDF_NOT_MADE = "O PDF não foi gerado: {detail}."


def _preview(
    request: Request, key: KeyInfo, slug: str, body: PreviewDraftRequest
) -> draft_previews.TemplatePreview:
    """A prévia dos templates com o que o corpo manda, ou o `403`, o `404`, o `409` e o `422` dela. O
    `403` vem antes de qualquer conta: quem manda um template ou as notas pede ao processo que os
    compile e renderize, e isso é do papel `total`."""
    if body.sends_templates:
        ensure_role(key, "total", what=TEMPLATES_ASKED)
    try:
        return previews.template_preview(
            get_deps(), slug, body.changes(), source=body.source, now=now(request)
        )
    except UnknownForm:
        raise ApiError("not_found", NOT_FOUND) from None
    except DefinitionError as refused:
        raise ApiError.invalid(NOT_RENDERED, refused.problems) from None
    except draft_previews.PreviewUnavailable as unavailable:
        raise ApiError("state_conflict", str(unavailable)) from None


def _frame(deps: Deps, *, scripts: bool) -> dict[str, str | None]:
    """O que a página leva no `<head>` além do HTML da página (os argumentos de `render_result` e
    `render_public_page`): o `<base>` para a origem do app (`PUBLIC_BASE_URL`), onde ficam os arquivos e as
    imagens que a página pede pelo caminho, e a `<meta>` com a política de conteúdo, que vale onde o
    cabeçalho se perdeu. Sem uma origem que sirva, a página sai sem `<base>`, e a meta, sem origem."""
    origin = origin_of(deps.settings.public_base_url) or None
    return {
        "base_href": f"{origin}/" if origin else None,
        "content_policy": content_policy(scripts=scripts, origin=origin, header=False),
    }


def _page(html: str, *, policy: str, filename: str) -> HtmlResponse:
    return HtmlResponse(
        html,
        headers={
            **PAGE_HEADERS,
            "Content-Security-Policy": policy,
            "Content-Disposition": f'inline; filename="{filename}"',
        },
    )


@router.operation(
    "POST",
    "/forms/{slug}/draft/pdf-preview",
    operation_id="render_form_draft_pdf_preview",
    role="leitura",
    heavy=True,
    errors={
        403: ROLE_403,
        404: "O formulário não existe (`not_found`).",
        409: PLAN_409,
        422: TEMPLATE_422,
    },
    summary="Pré-visualizar o PDF do rascunho",
    description=(
        "Gera o PDF do rascunho do formulário `slug` como a lead o receberia, com o plano de exemplo "
        "do rascunho (ou o do último teste, com `source: test`) e as respostas de exemplo, e o devolve "
        "como `application/pdf` (`Content-Disposition: inline`). **Não grava nada, não é auditada e "
        "não chama o LLM**: serve para ver o PDF antes de salvar e de publicar. O corpo é opcional: "
        "sem ele, vale o rascunho salvo; com `pdf` (`html`, `css`, `scores`) e `preview`, a prévia usa "
        "esses textos no lugar dos salvos, campo a campo (o que você deixa de fora é o do rascunho), "
        "como o editor do PDF faz com o que ainda não foi salvo (**mandar `pdf` ou `preview` é só do papel "
        "`total`**: a `leitura` pré-visualiza o rascunho salvo, sem corpo, com `{}` ou só com "
        "`source`). É o mesmo PDF do adaptador do pedido "
        "(o WeasyPrint): só abre arquivos do app, as imagens do formulário (`asset:<nome>`) e `data:`, "
        "no ambiente isolado dos templates e com os mesmos limites de tamanho. Um template com erro é "
        "`422` com o campo (`pdf.html`, `pdf.css` ou `preview`) e a linha, nunca com o valor recebido; "
        "o plano do último teste que falta, ou que o modelo do rascunho já não aceita, é `409`. Conta "
        "5 fichas do limite de uso. Os dados são os de exemplo do rascunho, nunca os de uma lead. "
        "Para ver a página de resultado, `render_form_draft_result_preview`."
    ),
    returns="O PDF do rascunho, inline (`application/pdf`).",
    response_model=None,
    response_class=PdfResponse,
)
def render_form_draft_pdf_preview(
    slug: FORM, request: Request, key: CurrentKey, body: PreviewDraftRequest = NO_CHANGES
) -> Response:
    found = _preview(request, key, slug, body)
    try:
        rendered = previews.render_pdf(get_deps(), found)
    except previews.PdfFailed as failed:
        raise ApiError(
            "validation_failed",
            NOT_RENDERED,
            errors=[ErrorItem(field="pdf", message=PDF_NOT_MADE.format(detail=failed.detail))],
        ) from None
    return PdfResponse(
        rendered.content,
        headers={
            "Content-Disposition": 'inline; filename="previa.pdf"',
            "Content-Security-Policy": PDF_POLICY,
        },
    )


@router.operation(
    "POST",
    "/forms/{slug}/draft/result-preview",
    operation_id="render_form_draft_result_preview",
    role="leitura",
    heavy=True,
    errors={
        403: ROLE_403,
        404: "O formulário não existe (`not_found`).",
        409: PLAN_409,
        422: TEMPLATE_422,
    },
    summary="Pré-visualizar a página de resultado do rascunho",
    description=(
        "Monta a página de resultado do rascunho do formulário `slug` como a lead a vê quando o plano "
        "fica pronto (a moldura de verdade, com o título, a prévia do plano no meio e um arquivo para "
        "baixar de mentira), com o plano de exemplo do rascunho (ou o do último teste, com `source: "
        "test`) e as respostas de exemplo, e a devolve como `text/html` (`Content-Disposition: "
        "inline`). **Não grava nada, não é auditada e não chama o LLM.** O corpo é opcional: sem ele, "
        "vale o rascunho salvo; com `pdf` e `preview`, a prévia usa esses textos no lugar dos salvos, "
        "campo a campo, como o editor faz com o que ainda não foi salvo (**mandar `pdf` ou `preview` é só "
        "do papel `total`**: a `leitura` pré-visualiza o rascunho salvo, sem corpo, com `{}` ou só com "
        "`source`). O HTML leva um `<base>` para a origem do app (o CSS e as imagens dele são pedidos "
        "pelo caminho; só vale se `PUBLIC_BASE_URL` é a origem da API) e, **servido por esta API**, uma "
        "política de conteúdo no cabeçalho que não deixa rodar script nenhum, nem o que você escreveu "
        "na prévia, e dá ao documento uma origem nula. Essa proteção é do cabeçalho: salvo em arquivo "
        "ou aberto fora da resposta da API, o HTML só tem a `<meta http-equiv>` com a mesma política "
        "(sem o `sandbox`), então não o abra num contexto que importe. Um template com erro é `422` "
        "com o campo e a linha; o plano do último teste que falta é `409`. Conta 5 fichas do limite de uso. Os dados são os "
        "de exemplo do rascunho, nunca os de uma lead. Para ver o PDF, "
        "`render_form_draft_pdf_preview`."
    ),
    returns="A página de resultado do rascunho em HTML, inline (`text/html`).",
    response_model=None,
    response_class=HtmlResponse,
)
def render_form_draft_result_preview(
    slug: FORM, request: Request, key: CurrentKey, body: PreviewDraftRequest = NO_CHANGES
) -> Response:
    deps = get_deps()
    found = _preview(request, key, slug, body)
    try:
        html = previews.render_result(found, **_frame(deps, scripts=False))
    except DefinitionError as refused:
        raise ApiError.invalid(NOT_RENDERED, refused.problems) from None
    return _page(html, policy=RESULT_PAGE_POLICY, filename="previa-resultado.html")


@router.operation(
    "GET",
    "/forms/{slug}/draft/public-page",
    operation_id="render_form_draft_public_page",
    role="leitura",
    heavy=True,
    errors={
        404: "O formulário não existe (`not_found`).",
        422: "`step` não é um número de 1 a 9999 (`validation_failed`, campo `step`), ou o rascunho "
        "guardado não compila neste deploy (um deploy com o compilador mais rígido): os problemas vêm "
        "em `errors`, com o caminho no documento e a linha do template.",
    },
    summary="Pré-visualizar a página pública do rascunho",
    description=(
        "Devolve a página pública do rascunho guardado do formulário `slug` como a lead a veria (o "
        "mesmo HTML, o mesmo CSS e o mesmo JavaScript da página de verdade), em `text/html` "
        "(`Content-Disposition: inline`), aberta na etapa `step`. A página traz todas as etapas e as "
        "condições (as perguntas e as opções que aparecem e somem) e anda pelos botões, mas **não "
        "tem formulário para enviar e não guarda nada no aparelho**. Lê o rascunho salvo (a versão "
        "publicada, se não há rascunho); não grava nada, não é auditada e não usa dado de lead. O HTML "
        "leva um `<base>` para a origem do app, de onde vêm o CSS e o JavaScript (`/static`), e uma "
        "política de conteúdo que só deixa rodar os scripts do app, num documento de origem nula, sem "
        "acesso à sessão do admin; essa proteção é do cabeçalho da resposta (salva em arquivo, a página "
        "só leva a `<meta http-equiv>` com a mesma política, sem o `sandbox`), e o `<base>` só vale se "
        "`PUBLIC_BASE_URL` é a origem da API. Conta 5 fichas do limite de uso. Quem só quer ler a estrutura do "
        "formulário usa `get_form_draft`, que é JSON."
    ),
    returns="A página pública do rascunho em HTML, inline (`text/html`).",
    response_model=None,
    response_class=HtmlResponse,
)
def render_form_draft_public_page(slug: FORM, step: STEP = 1) -> Response:
    deps = get_deps()
    try:
        page = previews.public_page(deps, slug, step)
    except UnknownForm:
        raise ApiError("not_found", NOT_FOUND) from None
    except DefinitionError as refused:
        raise ApiError.invalid(PAGE_NOT_BUILT, refused.problems) from None
    html = previews.render_public_page(page, **_frame(deps, scripts=True))
    return _page(html, policy=PUBLIC_PAGE_POLICY, filename="previa-pagina-publica.html")
