"""Rotas do painel admin (`/admin`).

Toda rota fora de `/admin/login` passa pelo guarda de sessão do roteador protegido: sem sessão
válida, redireciona para o login. A sessão é renovada em toda resposta (expira por inatividade).
"""

from __future__ import annotations

import hmac
import json
import logging
import math
import uuid
from collections.abc import Callable, Coroutine, Sequence
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.routing import APIRoute
from starlette.datastructures import FormData

from app import destinations
from app.admin import draft_pages, integration_views, launching, views
from app.admin.actions import destinations as destination_actions
from app.admin.actions import draft_tests as draft_test_actions
from app.admin.actions import integrations as integration_actions
from app.admin.actions import jobs as job_actions
from app.admin.actions import previews as preview_actions
from app.admin.actions import versions as version_actions
from app.admin.actions.integrations import INTEGRATION_MISSING, NOT_PUBLISHED
from app.admin.auth import COOKIE_NAME, COOKIE_PATH, AdminAuth, AdminSession, instant
from app.admin.form_editor import FormEdit
from app.admin.queries import forms as form_queries
from app.admin.queries import jobs as job_queries
from app.deps import get_deps
from app.errors import RetryableError, TerminalError
from app.forms import GenerationUnavailable, UnknownForm, draft_previews, draft_tests, drafts
from app.forms import assets as form_assets
from app.forms import retention as form_retention
from app.forms import stored as stored_forms
from app.forms.definition import DefinitionError, Problem
from app.integrations import config as integration_config
from app.integrations import mapping as integration_mapping
from app.internal_links import AdminPdfAccess
from app.models import utcnow
from app.version import describe
from app.web.origin import origin_of
from app.web.templating import templates

logger = logging.getLogger(__name__)

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; "
    "base-uri 'none'; object-src 'none'"
)
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    # same-origin, e não no-referrer: com no-referrer o navegador manda `Origin: null` nos
    # formulários e a conferência de origem recusaria o próprio admin.
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": CSP,
}
# A prévia da página pública do rascunho roda num iframe da página da prévia, para ter a largura de
# um celular: só ela pode ser emoldurada, e só pelo próprio admin.
FRAMEABLE_HEADERS = {
    "X-Frame-Options": "SAMEORIGIN",
    "Content-Security-Policy": CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self'"),
}
# O PDF da prévia do editor do PDF também roda num iframe da página do editor. O visualizador de PDF
# de alguns navegadores é um plugin embutido, e `object-src 'none'` pode impedi-lo; um PDF não traz
# script da página, então só a moldura fica na política.
FRAMEABLE_PDF_CSP = "frame-ancestors 'self'"
LOGIN_FAILED = "Usuário ou senha incorretos."
JOB_NOT_FOUND = "Pedido não encontrado. Confira o código e tente de novo."
REPROCESS_QUEUED = "Tentativa {attempt} enfileirada em seu nome."
REPROCESS_DEFERRED = (
    "Tentativa {attempt} criada em seu nome. O disparo será refeito automaticamente em instantes."
)
REPROCESS_REFUSED = (
    "Este pedido não pode ser reprocessado agora: ele está na fila ou gerando, ou outra pessoa "
    "já o reprocessou. Confira a linha do tempo."
)
DELIVERY_NOT_FOUND = "Entrega não encontrada."
RESEND_LAUNCHED = "Reenvio disparado. Acompanhe o estado da entrega abaixo."
RESEND_DEFERRED = (
    "A entrega voltou para a fila; o envio será disparado automaticamente em instantes."
)
RESEND_REFUSED = "A entrega precisa ter falhado e manter a mesma audiência e tentativa. Confira o destino, a revisão atual e o prazo de retenção antes de confirmar o reenvio."
PDF_REMOVED = "Este PDF já foi apagado do armazenamento pelo prazo de retenção (LGPD)."
ENV_LABELS = {"development": "Desenvolvimento", "test": "Teste"}
STOP_POLLING = 286  # código do HTMX para parar o `hx-trigger="every ..."`


class LoginRequired(Exception):
    """Sem sessão válida: a resposta vira redirecionamento para o login."""


class ForgedRequest(Exception):
    """POST sem o token CSRF da sessão ou vindo de outra origem."""


class NotFound(Exception):
    """O que o caminho pede não existe: a resposta vira a página de erro com esta mensagem."""


class AdminRoute(APIRoute):
    """Toda resposta do admin sai com os cabeçalhos de privacidade e a sessão renovada."""

    async def refuse_before_body(self, request: Request) -> None:
        """Gancho das rotas que recebem um arquivo: o FastAPI lê o corpo `multipart` antes de rodar
        as dependências, então a recusa que não precisa do corpo (a sessão, a origem) sai daqui,
        levantando `LoginRequired` ou `ForgedRequest`. Por padrão, não recusa nada."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handle = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                await self.refuse_before_body(request)
                response = await handle(request)
            except LoginRequired:
                response = _to_login(request)
                if COOKIE_NAME in request.cookies:
                    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
            except ForgedRequest:
                logger.warning("admin: POST recusado (CSRF/origem) em %s", request.url.path)
                response = _error(
                    request,
                    403,
                    "Não deu para confirmar que o pedido saiu deste painel. "
                    "Recarregue a página e tente de novo.",
                )
            except GenerationUnavailable:
                response = _error(
                    request,
                    409,
                    "Esta operação não está disponível para a finalidade atual do formulário. "
                    "Recarregue o rascunho e escolha o teste correspondente.",
                )
            except NotFound as exc:
                response = _error(request, 404, str(exc))
            session = getattr(request.state, "admin_session", None)
            if session is not None:
                _auth(request).set_cookie(response, session)
            response.headers.update(SECURITY_HEADERS)
            frameable = getattr(request.state, "frameable", False)
            if frameable:
                response.headers.update(FRAMEABLE_HEADERS)
            if frameable == "pdf":
                response.headers["Content-Security-Policy"] = FRAMEABLE_PDF_CSP
            return response

        return handler


def _auth(request: Request) -> AdminAuth:
    return request.app.state.admin_auth


def _to_login(request: Request) -> Response:
    path = request.url.path
    target = "/admin/login" + (f"?{urlencode({'next': path})}" if path != "/admin" else "")
    if request.headers.get("hx-request") == "true":
        # Num pedido do HTMX, um 303 traria a página de login para dentro do trecho da lista.
        return Response(status_code=401, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


def _safe_next(value: str) -> str:
    """Só caminhos do próprio admin, sem query string (evita redirecionamento aberto)."""
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or value.startswith("//") or "\\" in value:
        return "/admin"
    return parts.path if parts.path.startswith("/admin") else "/admin"


def require_same_origin(request: Request) -> None:
    """O POST tem de vir do próprio app: `Origin` (ou, na falta dele, `Referer`) igual à origem
    de PUBLIC_BASE_URL ou à do próprio pedido."""
    sent = request.headers.get("origin") or request.headers.get("referer") or ""
    allowed = {
        origin_of(get_deps().settings.public_base_url),
        origin_of(f"{request.url.scheme}://{request.headers.get('host', '')}"),
    } - {""}
    if origin_of(sent) not in allowed:
        raise ForgedRequest


def require_session(request: Request) -> AdminSession:
    session = _auth(request).read(request.cookies.get(COOKIE_NAME))
    if session is None:
        raise LoginRequired
    request.state.admin_session = session
    return session


async def require_csrf(
    request: Request, session: Annotated[AdminSession, Depends(require_session)]
) -> None:
    """Todo método que altera estado precisa do token da sessão (campo oculto ou cabeçalho do
    HTMX) e de origem igual à do app."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    require_same_origin(request)
    sent = request.headers.get("x-csrf-token")
    if sent is None:
        sent = (await posted_form(request)).get("csrf_token")
    if not isinstance(sent, str) or not hmac.compare_digest(
        sent.encode(), session.csrf_token.encode()
    ):
        raise ForgedRequest


# O editor estruturado manda um campo por texto de cada pergunta e de cada opção: um formulário
# grande, como o MCC, passa dos mil campos do Starlette. O corpo continua limitado a
# `MAX_BODY_BYTES` (`app.api.main`), e só uma sessão do admin chega a mandar um formulário aqui.
MAX_FORM_FIELDS = 5000


async def posted_form(request: Request) -> FormData:
    """O formulário do pedido. O primeiro a ler o corpo define o teto de campos (o Starlette guarda
    o formulário lido): por isso a conferência do CSRF lê por aqui também."""
    return await request.form(max_fields=MAX_FORM_FIELDS)


PostedForm = Annotated[FormData, Depends(posted_form)]


def _error(request: Request, status_code: int, message: str) -> Response:
    return templates.TemplateResponse(
        request, "admin/error.html", {"message": message}, status_code=status_code
    )


def _page(
    request: Request, name: str, context: dict[str, Any], *, status_code: int = 200
) -> Response:
    """Página autenticada: injeta sessão, aviso pendente (mostrado uma vez) e ambiente."""
    session: AdminSession = request.state.admin_session
    flash, session.flash = session.flash, None
    settings = get_deps().settings
    return templates.TemplateResponse(
        request,
        name,
        {
            **context,
            "session": session,
            "flash": flash,
            "env_label": ENV_LABELS.get(settings.app_env),
            "version_label": describe(settings.app_version, settings.app_build_date),
        },
        status_code=status_code,
    )


router = APIRouter()  # o que o app monta: as rotas abertas e as protegidas
open_routes = APIRouter(prefix="/admin", route_class=AdminRoute)
protected = APIRouter(
    prefix="/admin",
    route_class=AdminRoute,
    dependencies=[Depends(require_csrf)],  # que depende de require_session
)


@open_routes.get("/login")
def login_page(request: Request, next: str = "") -> Response:
    if _auth(request).read(request.cookies.get(COOKIE_NAME)) is not None:
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(
        request, "admin/login.html", {"error": None, "next": _safe_next(next)}
    )


@open_routes.post("/login", dependencies=[Depends(require_same_origin)])
def login(
    request: Request,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next_path: Annotated[str, Form(alias="next")] = "",
) -> Response:
    """Síncrona de propósito: o argon2 roda no threadpool, fora do event loop."""
    auth = _auth(request)
    client_ip = request.client.host if request.client else "desconhecido"
    next_path = _safe_next(next_path)
    wait = auth.throttle.retry_after(client_ip)
    if not wait:
        too_long = len(username) > 200 or len(password) > 1024
        if not too_long and auth.check_credentials(username, password):
            auth.throttle.success(client_ip)
            request.state.admin_session = auth.new_session(username)
            logger.info("admin: login de %s", username)
            return RedirectResponse(next_path, status_code=303)
        wait = auth.throttle.failure(client_ip)
        logger.warning("admin: login recusado ip=%s bloqueio=%ss", client_ip, wait)
    if wait:
        minutes = math.ceil(wait / 60)
        message = (
            "Muitas tentativas seguidas. Tente de novo em "
            f"{minutes} {'minuto' if minutes == 1 else 'minutos'}."
        )
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {"error": message, "next": next_path},
            status_code=429,
            headers={"Retry-After": str(wait)},
        )
    return templates.TemplateResponse(
        request, "admin/login.html", {"error": LOGIN_FAILED, "next": next_path}, status_code=400
    )


@protected.post("/logout")
def logout(request: Request) -> Response:
    request.state.admin_session = None  # o wrapper não regrava o cookie
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
    return response


@protected.get("")
def job_list(
    request: Request,
    q: str = "",
    status: str = "",
    form: str = "",
    form_version: str = "",
    period: str = "",
    delivery: str = "",
    sync: str = "",
    page: str = "1",
) -> Response:
    filters = job_queries.JobFilters(
        q=q,
        status=status,
        form=form,
        form_version=form_version,
        period=period,
        delivery=delivery,
        sync=sync,
        now=_now(request),
    )
    deps = get_deps()
    with deps.sessions() as session:
        found = views.list_jobs(
            session, filters, forms=deps.forms, page=int(page) if page.isdigit() else 1
        )
        overview = views.destinations_overview(session, deps.forms.current_forms(), deps.cipher)
    response = _page(
        request,
        "admin/jobs.html",
        {
            "unserved": overview.unserved,
            "unreadable": overview.unreadable,
            "found": found,
            "filters": filters,
            "status_options": views.STATUS_OPTIONS,
            "period_options": views.PERIOD_OPTIONS,
            "delivery_options": views.DELIVERY_OPTIONS,
            "sync_options": views.SYNC_OPTIONS,
            "nothing_found": views.nothing_found(filters),
            "previous_url": filters.url(found.page - 1) if found.page > 1 else None,
            "next_url": filters.url(found.page + 1) if found.page < found.pages else None,
        },
    )
    if request.headers.get("HX-Request"):
        # O formulário manda os campos vazios (`?q=&form=&form_version=&period=&status=&delivery=&sync=`);
        # a barra de endereço guarda só o que filtra, o mesmo que os links da paginação e "Limpar
        # filtros" escrevem.
        response.headers["HX-Push-Url"] = filters.url(found.page)
    return response


def _detail_page(
    request: Request,
    public_id: str,
    *,
    errors: dict[str, str] | None = None,
    reason: str = "",
    status_code: int = 200,
    release_form: dict | None = None,
) -> Response:
    deps = get_deps()
    with deps.sessions() as session:
        detail = views.job_detail(
            session,
            public_id,
            forms=deps.forms,
            run_url_template=deps.settings.hatchet_run_url_template,
            now=utcnow(),
            storage=deps.storage,
        )
    if detail is None:
        return _error(request, 404, JOB_NOT_FOUND)
    return _page(
        request,
        "admin/job.html",
        {
            "job": detail,
            "errors": errors or {},
            "reason": reason,
            "release_form": release_form
            or {
                "idempotency_key": str(uuid.uuid4()),
                "mode": "standard",
                "reason_code": "operator_review",
            },
        },
        status_code=status_code,
    )


@protected.get("/jobs/{public_id}")
def job_detail(request: Request, public_id: str) -> Response:
    return _detail_page(request, public_id)


@protected.get("/jobs/{public_id}/participant")
def participant_plan(request: Request, public_id: str) -> Response:
    from dataclasses import asdict

    from app.admin.queries import jobs as queries

    deps = get_deps()
    try:
        with deps.sessions() as session:
            found = queries.get_job_plan_projection(
                session, public_id, forms=deps.forms, audience="participant"
            )
    except queries.ProjectionUnavailable as exc:
        return _error(request, 409, str(exc))
    if found is None:
        return _error(request, 404, JOB_NOT_FOUND)
    return _page(request, "admin/participant_plan.html", {"projection": asdict(found)})


@protected.post("/jobs/{public_id}/reprocess")
async def reprocess_job(
    request: Request,
    public_id: str,
    session: Annotated[AdminSession, Depends(require_session)],
    reason: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
    attempt: Annotated[str, Form()] = "",
) -> Response:
    """Nova tentativa auditada em nome de quem está logado, disparada uma única vez. O campo
    `attempt` é a tentativa que a pessoa viu: se mudou (duplo clique, outra pessoa), nada muda.
    A regra é da ação `job_actions.reprocess`, a mesma da API."""
    try:
        done = await job_actions.reprocess(
            get_deps(),
            request.app.state.trigger,
            public_id,
            actor=session.username,
            reason=reason,
            confirm=bool(confirm),
            expected_attempt=int(attempt) if attempt.isdigit() else None,
        )
    except job_actions.InvalidReprocess as invalid:
        return await run_in_threadpool(
            _detail_page,
            request,
            public_id,
            errors=invalid.errors,
            reason=reason.strip(),
            status_code=422,
        )
    except job_actions.JobNotFound:
        return _error(request, 404, JOB_NOT_FOUND)
    except job_actions.ReprocessRefused:
        session.flash = ("err", REPROCESS_REFUSED)
    else:
        message = REPROCESS_QUEUED if done.launched else REPROCESS_DEFERRED
        session.flash = ("ok", message.format(attempt=done.attempt))
    return RedirectResponse(f"/admin/jobs/{public_id}", status_code=303)


@protected.post("/jobs/{public_id}/deliveries/{delivery_id}/resend")
async def resend_delivery(
    request: Request,
    public_id: str,
    delivery_id: str,
    session: Annotated[AdminSession, Depends(require_session)],
    expected_config_version: Annotated[int | None, Form()] = None,
    expected_attempt: Annotated[int | None, Form()] = None,
) -> Response:
    """Entrega falha volta para pendente e o `deliver` é disparado de novo. Sem engine, o SDK insiste
    por minutos: a resposta não espera mais que HATCHET_TRIGGER_TIMEOUT_SECONDS. Se o disparo não
    chegar ao engine, o reconciliador encontra a entrega pendente sem run id. A regra é da ação
    `job_actions.resend_delivery`, a mesma da API."""
    try:
        target = uuid.UUID(delivery_id)
    except ValueError:
        return _error(request, 404, DELIVERY_NOT_FOUND)
    try:
        done = await job_actions.resend_delivery(
            get_deps(),
            public_id,
            target,
            actor=session.username,
            expected_config_version=expected_config_version,
            expected_attempt=expected_attempt,
        )
    except job_actions.JobNotFound:
        return _error(request, 404, JOB_NOT_FOUND)
    except job_actions.ResendRefused:
        session.flash = ("err", RESEND_REFUSED)
    else:
        session.flash = ("ok", RESEND_LAUNCHED if done.launched else RESEND_DEFERRED)
    return RedirectResponse(f"/admin/jobs/{public_id}", status_code=303)


@protected.get("/jobs/{public_id}/pdf")
def job_pdf(request: Request, public_id: str, version: str = "") -> Response:
    """PDF de uma tentativa (a mais recente se `version` não vier), sempre como anexo."""
    try:
        pdf = job_actions.pdf_file(
            get_deps(),
            public_id,
            int(version) if version.isdigit() else None,
            access=AdminPdfAccess(),
            signed_url=True,
        )
    except job_actions.NoPdf:
        return _error(request, 404, "Este pedido ainda não tem PDF.")
    except job_actions.PdfRemoved:
        return _error(request, 410, PDF_REMOVED)
    except (RetryableError, TerminalError, OSError):
        return _error(request, 502, "O PDF está indisponível. Tente novamente em instantes.")
    if pdf.url:
        return RedirectResponse(pdf.url, status_code=302)
    return Response(
        pdf.content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{pdf.filename}"'},
    )


# --- destinos do webhook ----------------------------------------------------------------------
# Cada ação existe para o destino padrão e para o de um formulário: a mesma função, com o slug
# (ou nenhum) tirado do caminho.

DESTINATION_NOT_FOUND = "Formulário não encontrado. Confira o endereço e tente de novo."
DESTINATION_CHANGED = (
    "O destino mudou desde que você abriu esta página (outra aba ou um clique repetido). Nada "
    "foi alterado: confira o estado atual abaixo."
)
SECRET_TAKEN_BY_OTHER_CLICK = (
    "O segredo já foi gerado por seu outro clique (ou por um recarregar da página), e ele só "
    "apareceu naquela resposta. Para ver um segredo, gere um segredo novo abaixo: o anterior "
    "deixa de valer."
)
DESTINATION_SAVED = "Endereço salvo. O segredo continua o mesmo."
DESTINATION_REMOVED = "Destino removido."
NO_CIPHER = (
    "Para guardar o segredo de um destino, defina WEBHOOK_ENCRYPTION_KEY no ambiente do app e "
    "reinicie a API e o worker (como gerar a chave: app/README.md)."
)
NOTHING_TO_TEST = "Crie o destino antes de mandar o evento de teste."
CONFIRM_ROTATE = "Confirme que entende que o segredo atual deixa de valer na hora."
CONFIRM_REMOVE = "Confirme que entende para onde os eventos passam a ir."


def _form_title(form_slug: str) -> str | None:
    deps = get_deps()
    with deps.sessions() as db:
        return views.form_title(db, form_slug)


def destination_target(request: Request) -> str | None:
    """O slug do formulário no caminho, ou None no destino padrão. Vale um formulário do
    registro ou um slug que saiu dele mas ainda tem destino guardado (para poder removê-lo);
    qualquer outro é `NotFound`."""
    try:
        with get_deps().sessions() as db:
            return destination_actions.target(db, request.path_params.get("slug")).form_slug
    except destination_actions.UnknownScope:
        raise NotFound(DESTINATION_NOT_FOUND) from None


Target = Annotated[str | None, Depends(destination_target)]


def _scope_label(form_slug: str | None) -> str:
    """Como os logs citam o destino: o slug ou o padrão."""
    return form_slug or "(padrão)"


def _version(value: str) -> int:
    """A versão que a pessoa viu, no texto canônico que o servidor deu (`"2"`, e não `"02"`); um valor
    estranho (um sobrescrito, um número sem fim, um zero à esquerda) nunca bate e vira `Conflict`, e
    não um erro."""
    return integration_actions.version_number(value)


def _back(session: AdminSession, form_slug: str | None, message: str) -> Response:
    """De volta à página do destino, com o aviso de por que nada mudou."""
    session.flash = ("err", message)
    return RedirectResponse(views.destination_url(form_slug), status_code=303)


def _secret_conflict(
    session: AdminSession,
    form_slug: str | None,
    conflict: destinations.Conflict,
    expected: int,
    *,
    makes_secret: bool = True,
) -> Response:
    """Criar e trocar o segredo recusam a página velha. Se quem passou na frente foi o próprio
    clique duplo, o segredo já existe, mas só apareceu na resposta do outro clique: a tela diz
    isso, em vez de "nada foi alterado"."""
    if destination_actions.secret_already_shown(
        conflict, actor=session.username, expected_version=expected, makes_secret=makes_secret
    ):
        return _back(session, form_slug, SECRET_TAKEN_BY_OTHER_CLICK)
    return _back(session, form_slug, DESTINATION_CHANGED)


def _destination_page(
    request: Request,
    *,
    status_code: int = 200,
    new_secret: str | None = None,
    test: views.ProbeView | None = None,
    test_error: str | None = None,
    errors: dict[str, str] | None = None,
    url: str | None = None,
    needs_confirmation: bool = False,
    selected_events: list[str] | None = None,
    selected_purpose: str | None = None,
) -> Response:
    """A página do destino. `new_secret` só vem na resposta da criação ou da troca: a única vez
    em que o segredo aparece. Num pedido do HTMX, uma recusa sai com 200: o HTMX não troca o
    trecho da página numa resposta 4xx, e a pessoa não veria o motivo."""
    form_slug = request.path_params.get("slug")
    deps = get_deps()
    title = _form_title(form_slug) if form_slug is not None else None
    with deps.sessions() as db:
        page = views.destination_page(db, form_slug, title, deps.cipher)
        effective = (
            destinations.serving(destinations.listing(db), form_slug)
            if form_slug
            else destinations.listing(db).get(None)
        )
    if page is None:
        return _error(request, 404, DESTINATION_NOT_FOUND)
    if url is None:
        url = page.stored.url if page.stored else ""
    if request.headers.get("hx-request") == "true":
        status_code = 200
    return _page(
        request,
        "admin/destination.html",
        {
            "page": page,
            "effective_events": effective.events if effective else (),
            "effective_purpose": effective.purpose if effective else None,
            "selected_purpose": selected_purpose
            or (page.stored.purpose if page.stored else "participant"),
            "selected_events": selected_events
            if selected_events is not None
            else (page.stored.events if page.stored else ("plano.concluido",)),
            "new_secret": new_secret,
            "test": test,
            "test_error": test_error,
            "errors": errors or {},
            "url": url,
            "needs_confirmation": needs_confirmation,
            "cipher_missing": deps.cipher is None,
            "max_url_length": destinations.MAX_URL_LENGTH,
        },
        status_code=status_code,
    )


@protected.get("/destinations")
def destination_list(request: Request) -> Response:
    deps = get_deps()
    with deps.sessions() as db:
        overview = views.destinations_overview(db, deps.forms.current_forms(), deps.cipher)
    return _page(
        request,
        "admin/destinations.html",
        {"overview": overview, "cipher_missing": deps.cipher is None},
    )


@protected.get("/destinations/default")
@protected.get("/destinations/forms/{slug}")
def destination_detail(request: Request) -> Response:
    return _destination_page(request)


@protected.post("/destinations/default/url")
@protected.post("/destinations/forms/{slug}/url")
def save_destination(
    request: Request,
    session: Annotated[AdminSession, Depends(require_session)],
    form_slug: Target,
    url: Annotated[str, Form()] = "",
    version: Annotated[str, Form()] = "",
    confirm_http: Annotated[str, Form()] = "",
    events: Annotated[list[str] | None, Form()] = None,
    events_present: Annotated[str, Form()] = "",
    purpose: Annotated[str | None, Form()] = None,
) -> Response:
    """Cria o destino, e o segredo novo aparece só nesta resposta, ou muda a URL dele."""
    expected = _version(version)
    try:
        saved = destination_actions.save(
            get_deps(),
            form_slug,
            url=url,
            actor=session.username,
            expected_version=expected,
            http_confirmed=bool(confirm_http),
            events=(events or []) if events_present else events,
            purpose=purpose,
        )
    except destinations.InvalidPurpose as exc:
        return _destination_page(
            request,
            status_code=422,
            errors={"purpose": exc.message},
            url=url,
            selected_events=events,
            selected_purpose=purpose,
        )
    except destinations.InvalidEvents as exc:
        return _destination_page(
            request,
            status_code=422,
            errors={"events": exc.message},
            url=url,
            selected_purpose=purpose,
            selected_events=(events or []) if events_present else events,
        )
    except destinations.InvalidUrl as exc:
        return _destination_page(
            request,
            status_code=422,
            errors={"url": exc.message},
            url=url,
            needs_confirmation=exc.needs_confirmation,
            selected_purpose=purpose,
            selected_events=events or [] if events_present else events,
        )
    except destinations.NoCipher:
        return _destination_page(request, status_code=422, errors={"url": NO_CIPHER}, url=url)
    except destinations.Conflict as conflict:
        # Só a criação gera segredo, e ele pode ter ido para o outro clique.
        return _secret_conflict(session, form_slug, conflict, expected, makes_secret=expected == 0)
    created = saved.secret is not None
    logger.info(
        "admin: destino do webhook %s formulario=%s host=%s",
        "criado" if created else "alterado",
        _scope_label(form_slug),
        saved.destination.host,
    )
    if created:
        return _destination_page(request, new_secret=saved.secret)
    session.flash = ("ok", DESTINATION_SAVED)
    return RedirectResponse(views.destination_url(form_slug), status_code=303)


@protected.post("/destinations/default/secret")
@protected.post("/destinations/forms/{slug}/secret")
def rotate_destination_secret(
    request: Request,
    session: Annotated[AdminSession, Depends(require_session)],
    form_slug: Target,
    version: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
) -> Response:
    """Gera um segredo novo, que aparece só nesta resposta; o antigo deixa de valer na hora. O
    `version` do formulário impede que um F5 neste POST troque o segredo de novo."""
    if not confirm:
        return _destination_page(request, status_code=422, errors={"confirm": CONFIRM_ROTATE})
    expected = _version(version)
    try:
        rotated = destination_actions.rotate(
            get_deps(), form_slug, actor=session.username, expected_version=expected
        )
    except destinations.NoCipher:
        return _destination_page(request, status_code=422, errors={"confirm": NO_CIPHER})
    except destinations.Conflict as conflict:
        return _secret_conflict(session, form_slug, conflict, expected)
    logger.info("admin: segredo do destino trocado formulario=%s", _scope_label(form_slug))
    return _destination_page(request, new_secret=rotated.secret)


@protected.post("/destinations/default/remove")
@protected.post("/destinations/forms/{slug}/remove")
def remove_destination(
    request: Request,
    session: Annotated[AdminSession, Depends(require_session)],
    form_slug: Target,
    version: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
) -> Response:
    if not confirm:
        return _destination_page(request, status_code=422, errors={"remove": CONFIRM_REMOVE})
    try:
        removed = destination_actions.remove(
            get_deps(), form_slug, actor=session.username, expected_version=_version(version)
        )
    except destinations.Conflict:
        return _back(session, form_slug, DESTINATION_CHANGED)
    logger.info("admin: destino do webhook removido formulario=%s", _scope_label(form_slug))
    session.flash = (
        "ok",
        DESTINATION_REMOVED
        + f" Registros de entrega de respostas apagados: {removed.capture_counts.total}; pendentes não foram redirecionadas.",
    )
    return RedirectResponse(views.DESTINATIONS_URL, status_code=303)


@protected.post("/destinations/default/test")
@protected.post("/destinations/forms/{slug}/test")
def send_test_event(
    request: Request,
    session: Annotated[AdminSession, Depends(require_session)],
    form_slug: Target,
    event: Annotated[str, Form()] = "webhook.teste",
) -> Response:
    """O evento de teste sai da API, sem passar pelo Hatchet (não leva dado de lead), com os
    timeouts do adaptador: a resposta pode levar até uns 20 s com o receptor mudo. As entregas
    saem do worker, que pode não alcançar o que a API alcança (a tela avisa)."""
    try:
        probe = destinations.send_test(get_deps(), form_slug, actor=session.username, event=event)
    except destinations.UnreadableSecret:
        return _destination_page(request, status_code=409, test=views.UNREADABLE_PROBE)
    except destinations.InvalidPurpose as exc:
        return _destination_page(request, status_code=422, test_error=exc.message)
    except destinations.UnsupportedEvent:
        return _back(session, form_slug, "O destino não declarou aceitar o evento de captura.")
    if probe is None:
        return _back(session, form_slug, NOTHING_TO_TEST)
    logger.info(
        "admin: evento de teste formulario=%s status=%s erro=%s",
        _scope_label(form_slug),
        probe.status,
        probe.error,
    )
    return _destination_page(request, test=views.probe_view(probe, event=event))


# --- formulários ------------------------------------------------------------------------------

FORM_IMPORT_URL = "/admin/forms/import"
FORM_NEW_URL = "/admin/forms/new"
FORM_NOT_FOUND = "Formulário não encontrado. Confira o endereço e tente de novo."
IMPORTED = "{title} publicado na versão {number}. Os envios novos já usam esta versão."
NO_DEFINITION = (
    "Esta versão não tem uma definição para exportar: só as versões publicadas dos formulários do "
    "banco têm. Confira o endereço."
)
FORM_ACTIVATED = "Formulário ativado: ele volta a receber envios no endereço público e na API."
FORM_DEACTIVATED = (
    "Formulário desativado: a página pública diz à lead que ele está indisponível, e a API recusa "
    "envios novos. Os pedidos que já existem seguem até o PDF e a entrega."
)
FORM_ALREADY_ACTIVE = "Nada mudou: o formulário já estava ativo (outra aba ou um clique repetido)."
FORM_ALREADY_INACTIVE = (
    "Nada mudou: o formulário já estava inativo (outra aba ou um clique repetido)."
)
CONFIRM_DEACTIVATE = "Confirme que entende que o formulário sai do ar para as leads."


@protected.get("/forms")
def form_list(request: Request) -> Response:
    deps = get_deps()
    with deps.sessions() as db:
        rows = views.form_list(
            db, deps.forms, public_base_url=deps.settings.public_base_url, now=_now(request)
        )
    return _page(
        request,
        "admin/forms.html",
        {"rows": rows, "import_url": FORM_IMPORT_URL, "new_url": FORM_NEW_URL},
    )


NOTHING_TO_IMPORT = Problem("", "cole a definição em JSON ou escolha o arquivo dela")


def _import_page(
    request: Request,
    *,
    status_code: int = 200,
    problems: Sequence[Problem] = (),
    contact: Sequence[str] = (),
    definition: str = "",
    note: str = "",
) -> Response:
    """A página da importação, com os problemas da última tentativa ou o pedido de confirmação
    para os dados de contato no prompt (e o texto dela, para a operadora corrigir ou confirmar)."""
    return _page(
        request,
        "admin/form_import.html",
        {
            "problems": problems,
            "contact": views.contact_roles(contact) if contact else None,
            "definition": definition,
            "note": note,
            "note_max_length": stored_forms.NOTE_MAX_LENGTH,
        },
        status_code=status_code,
    )


@protected.get("/forms/import")
def form_import_page(request: Request) -> Response:
    return _import_page(request)


@protected.post("/forms/import")
def import_form(
    request: Request,
    session: Annotated[AdminSession, Depends(require_session)],
    definition: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    confirm_contact: Annotated[str, Form()] = "",
    file: Annotated[UploadFile | None, File()] = None,
) -> Response:
    """Confere a definição, colada ou num arquivo (o arquivo vale mais), e publica a versão 1 do
    formulário, que abre na leitura dele. Recusada, ou à espera da confirmação dos dados de
    contato no prompt, a página volta com o texto, e nada é gravado."""
    text: str | bytes = definition
    if file is not None and file.filename:
        text = file.file.read()
    shown = text if isinstance(text, str) else text.decode("utf-8", errors="replace")
    if not shown.strip():
        return _import_page(request, status_code=422, problems=[NOTHING_TO_IMPORT], note=note)
    try:
        imported = stored_forms.import_definition(
            get_deps(),
            text,
            actor=session.username,
            note=note,
            contact_confirmed=bool(confirm_contact),
        )
    except DefinitionError as exc:
        return _import_page(
            request, status_code=422, problems=exc.problems, definition=shown, note=note
        )
    except stored_forms.ContactInPrompt as exc:
        return _import_page(
            request, status_code=422, contact=exc.roles, definition=shown, note=note
        )
    logger.info(
        "admin: formulário importado slug=%s versão=%s", imported.slug, imported.version_number
    )
    session.flash = ("ok", IMPORTED.format(title=imported.title, number=imported.version_number))
    return RedirectResponse(views.form_url(imported.slug), status_code=303)


FORM_CREATED = (
    "Rascunho de {title} criado. O formulário só vai ao ar na primeira publicação: edite, teste e "
    "publique a versão 1."
)


def _new_form_page(
    request: Request,
    *,
    title: str = "",
    slug: str = "",
    source: str = "",
    generation: str = "",
    suggest: bool = False,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    """A página do formulário novo, com o que a operadora mandou e os problemas, se houve. Com
    `suggest`, a origem escolhida sugere o título e o endereço da cópia."""
    deps = get_deps()
    with deps.sessions() as db:
        sources = views.form_sources(db)
        chosen = next((found for found in sources if found.slug == source), None)
        if suggest and chosen is not None:
            title, slug = views.copy_suggestion(db, chosen)
    page = views.NewFormPage(
        title=title,
        slug=slug,
        source=source,
        generation=generation if generation in ("pdf", "none") else "",
        sources=sources,
        errors=errors or {},
        address_prefix=form_queries.public_url(deps.settings.public_base_url, ""),
        slug_max_length=stored_forms.SLUG_MAX_LENGTH,
    )
    return _page(request, "admin/form_new.html", {"page": page}, status_code=status_code)


# Antes de /forms/{slug}: "new" é uma página do admin, e nunca um slug (`RESERVED_SLUGS`).
@protected.get("/forms/new")
def new_form_page(request: Request, source: Annotated[str, Query(alias="from")] = "") -> Response:
    """A página de criar; `?from=<slug>` já escolhe o formulário a duplicar (o "Duplicar…" da
    página dele)."""
    return _new_form_page(request, source=source, suggest=bool(source))


@protected.post("/forms/new")
def create_form(
    request: Request,
    session: Annotated[AdminSession, Depends(require_session)],
    title: Annotated[str, Form()] = "",
    slug: Annotated[str, Form()] = "",
    source: Annotated[str, Form()] = "",
    generation: Annotated[str, Form()] = "",
) -> Response:
    """Cria o formulário, sem versão publicada, e abre o rascunho dele: do modelo inicial ou da
    cópia de outro formulário. Recusado, a página volta com o que a operadora mandou e o problema
    de cada campo, e nada é gravado."""
    try:
        created = drafts.create(
            get_deps(),
            title=title,
            slug=slug,
            actor=session.username,
            source=source,
            generation=generation or None,
        )
    except DefinitionError as exc:
        return _new_form_page(
            request,
            title=title,
            slug=slug,
            source=source,
            generation=generation,
            errors=drafts.problems_by_field(exc.problems),
            status_code=422,
        )
    logger.info("admin: formulário criado slug=%s", created)
    session.flash = ("ok", FORM_CREATED.format(title=title.strip()))
    return RedirectResponse(views.draft_url(created), status_code=303)


def _form_page(
    request: Request, slug: str, *, errors: dict[str, str] | None = None, status_code: int = 200
) -> Response:
    """A página do formulário: a situação e a leitura da versão publicada."""
    deps = get_deps()
    with deps.sessions() as db:
        detail = views.form_detail(
            db,
            slug,
            deps.forms,
            public_base_url=deps.settings.public_base_url,
            retention_pdf_days=deps.settings.retention_pdf_days,
            retention_days=deps.settings.retention_days,
            now=_now(request),
        )
    if detail is None:
        return _error(request, 404, FORM_NOT_FOUND)
    return _page(
        request,
        "admin/form.html",
        {"form": detail, "errors": errors or {}},
        status_code=status_code,
    )


# Depois de /forms/import: o slug nunca é uma página do admin (`RESERVED_SLUGS`).
@protected.get("/forms/{slug}")
def form_detail(request: Request, slug: str) -> Response:
    return _form_page(request, slug)


def _set_active(request: Request, session: AdminSession, slug: str, active: bool) -> Response:
    """Muda a situação do formulário e volta à página dele, com o aviso do que mudou."""
    try:
        changed = stored_forms.set_active(get_deps().sessions, slug, active, actor=session.username)
    except UnknownForm:
        return _error(request, 404, FORM_NOT_FOUND)
    if changed:
        logger.info("admin: formulário %s slug=%s", "ativado" if active else "desativado", slug)
        session.flash = ("ok", FORM_ACTIVATED if active else FORM_DEACTIVATED)
    else:
        session.flash = ("err", FORM_ALREADY_ACTIVE if active else FORM_ALREADY_INACTIVE)
    return RedirectResponse(views.form_url(slug), status_code=303)


@protected.post("/forms/{slug}/activate")
def activate_form(
    request: Request, slug: str, session: Annotated[AdminSession, Depends(require_session)]
) -> Response:
    return _set_active(request, session, slug, True)


@protected.post("/forms/{slug}/deactivate")
def deactivate_form(
    request: Request,
    slug: str,
    session: Annotated[AdminSession, Depends(require_session)],
    confirm: Annotated[str, Form()] = "",
) -> Response:
    """Tira o formulário do ar para as leads, com a confirmação de quem desativa. Os pedidos que
    já existem seguem a versão deles."""
    if not confirm:
        return _form_page(request, slug, errors={"deactivate": CONFIRM_DEACTIVATE}, status_code=422)
    return _set_active(request, session, slug, False)


RETENTION_SAVED = (
    "Prazos de retenção salvos. Valem a partir da próxima retenção, também para os pedidos que "
    "já existem."
)
RETENTION_UNCHANGED = "Os prazos já eram esses: nada mudou."
RETENTION_CHANGED = (
    "Os prazos mudaram desde que você abriu esta página (outra aba ou outra pessoa). Nada foi "
    "alterado: confira os valores abaixo e tente de novo."
)


def _retention_page(
    request: Request,
    slug: str,
    *,
    status_code: int = 200,
    typed: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    reduction: views.ReductionView | None = None,
    message: str | None = None,
) -> Response:
    """A página dos prazos de retenção. `typed` são os campos como a operadora os digitou (os
    prazos guardados, se não vierem); `errors`, o motivo de cada campo recusado; `reduction`, a
    contagem do que a redução apagaria, à espera da confirmação."""
    deps = get_deps()
    with deps.sessions() as db:
        page = views.retention_page(db, slug, deps.settings)
    if page is None:
        return _error(request, 404, FORM_NOT_FOUND)
    if typed is None:
        typed = {
            "pdf_days": str(page.own.pdf_days or ""),
            "request_days": str(page.own.request_days or ""),
        }
    return _page(
        request,
        "admin/form_retention.html",
        {
            "page": page,
            "typed": typed,
            "errors": errors or {},
            "reduction": reduction,
            "message": message,
        },
        status_code=status_code,
    )


@protected.get("/forms/{slug}/retention")
def retention_form(request: Request, slug: str) -> Response:
    return _retention_page(request, slug)


@protected.post("/forms/{slug}/retention")
def save_retention(
    request: Request,
    slug: str,
    session: Annotated[AdminSession, Depends(require_session)],
    pdf_days: Annotated[str, Form()] = "",
    request_days: Annotated[str, Form()] = "",
    expected: Annotated[str, Form()] = "",
    seen: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
) -> Response:
    """Grava os prazos de retenção do formulário, sem publicação. Recusados, ou à espera da
    confirmação de uma redução (com a contagem do que ela apagaria), a página volta com os campos
    como ela os digitou, e nada é gravado. A confirmação só vale com a contagem que ela viu."""
    typed = {"pdf_days": pdf_days.strip(), "request_days": request_days.strip()}
    parsed: dict[str, int | None] = {}
    errors: dict[str, str] = {}
    for name, value in typed.items():
        try:
            parsed[name] = form_retention.parse_days(value)
        except ValueError as exc:
            errors[name] = str(exc)
    if errors:
        return _retention_page(request, slug, status_code=422, typed=typed, errors=errors)
    deps = get_deps()
    try:
        saved = form_retention.save(
            deps.sessions,
            deps.settings,
            slug,
            new=form_retention.Periods(parsed["pdf_days"], parsed["request_days"]),
            expected=expected,
            actor=session.username,
            now=_now(request),
            confirmed=form_retention.parse_seen(seen) if confirm else None,
        )
    except UnknownForm:
        return _error(request, 404, FORM_NOT_FOUND)
    except form_retention.Changed:
        return _retention_page(request, slug, status_code=409, message=RETENTION_CHANGED)
    except form_retention.Refused as refused:
        return _retention_page(request, slug, status_code=422, typed=typed, errors=refused.problems)
    except form_retention.ConfirmationNeeded as needed:
        return _retention_page(
            request,
            slug,
            status_code=422,
            typed=typed,
            reduction=views.reduction_view(needed.due, needed.today),
        )
    if saved.changed:
        logger.info(
            "admin: prazos de retenção alterados formulario=%s pdf=%s pedido=%s",
            slug,
            saved.periods.pdf_days,
            saved.periods.request_days,
        )
        session.flash = ("ok", RETENTION_SAVED)
    else:
        session.flash = ("ok", RETENTION_UNCHANGED)
    return RedirectResponse(views.form_url(slug), status_code=303)


def _number(value: str) -> int | None:
    """O número de uma versão no caminho; None se não for um (o banco guarda um integer)."""
    return version_actions.version_number(value)


@protected.get("/forms/{slug}/versions/{number}/export")
def export_form_version(request: Request, slug: str, number: str) -> Response:
    """A definição da versão como o banco a guarda, num arquivo JSON para importar em outro
    ambiente ou guardar fora do banco. As chaves dos objetos vêm na ordem do banco."""
    version = _number(number)
    with get_deps().sessions() as db:
        found = stored_forms.stored_version(db, slug, version) if version is not None else None
    if found is None:
        return _error(request, 404, NO_DEFINITION)
    body = json.dumps(found.definition, ensure_ascii=False, indent=2) + "\n"
    filename = f"{found.slug}-v{found.number}.json"
    return Response(
        body.encode("utf-8"),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- rascunho, teste e publicação -------------------------------------------------------------
# O rascunho de um formulário (`app.forms.drafts`).

DRAFT_NOT_FOUND = "Este formulário não tem rascunho. Confira o endereço."
DRAFT_SAVED = "Rascunho salvo. O que está no ar só muda quando você publicar."
DRAFT_SAVED_EQUAL = "Rascunho salvo igual à versão publicada: não há mudança para publicar."
DRAFT_CHANGED = (
    "O rascunho mudou desde que você abriu esta página (outra aba ou uma publicação). Nada foi "
    "salvo. Os campos abaixo têm o seu texto: salvar de novo substitui o rascunho atual."
)


def _draft(slug: str) -> drafts.Draft:
    """O rascunho do formulário, ou a página de erro (`NotFound`) se o formulário não existe."""
    with get_deps().sessions() as db:
        draft = drafts.load(db, slug)
    if draft is None:
        raise NotFound(DRAFT_NOT_FOUND)
    return draft


def _editor_page(
    request: Request, draft: drafts.Draft, *, status_code: int = 200, **kwargs: Any
) -> Response:
    page = draft_pages.editor(draft, **kwargs)
    return _page(request, "admin/form_draft.html", {"page": page}, status_code=status_code)


@protected.get("/forms/{slug}/draft")
def draft_editor(request: Request, slug: str) -> Response:
    return _editor_page(request, _draft(slug))


def _text_of(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def _posted(form: FormData) -> tuple[FormEdit, drafts.DraftTexts]:
    """O editor como a página o mandou: o editor estruturado e os textos do rascunho."""
    edit = FormEdit.of_form(form)
    texts = drafts.DraftTexts(
        instructions=_text_of(form, "instructions"),
        prompt=_text_of(form, "prompt"),
        definition=_text_of(form, "definition"),
        screen=edit.screen(),
        example_answers=edit.example_answers(),
        problems=tuple(edit.problems()),
    )
    return edit, texts


@protected.post("/forms/{slug}/draft")
def save_draft(
    request: Request,
    slug: str,
    session: Annotated[AdminSession, Depends(require_session)],
    form: PostedForm,
) -> Response:
    """Guarda o rascunho, se ele compila. Recusado, a página volta com o que a operadora mandou
    e os problemas, cada um no campo dele, e nada é gravado. Antes da primeira publicação, o
    endereço (`slug`) muda junto."""
    edit, texts = _posted(form)
    new_slug = form.get("slug")
    try:
        saved = drafts.save(
            get_deps(),
            slug,
            texts,
            actor=session.username,
            expected=_text_of(form, "expected"),
            new_slug=new_slug if isinstance(new_slug, str) else None,
        )
    except UnknownForm:
        return _error(request, 404, DRAFT_NOT_FOUND)
    except DefinitionError as exc:
        return _editor_page(
            request,
            _draft(slug),
            status_code=422,
            texts=texts,
            edit=edit,
            problems=exc.problems,
            slug=_text_of(form, "slug") if "slug" in form else None,
        )
    except drafts.DraftChanged:
        return _editor_page(
            request, _draft(slug), status_code=409, texts=texts, edit=edit, message=DRAFT_CHANGED
        )
    logger.info("admin: rascunho salvo slug=%s pendente=%s", saved.slug, saved.pending)
    session.flash = ("ok", DRAFT_SAVED if saved.pending else DRAFT_SAVED_EQUAL)
    return RedirectResponse(views.draft_url(saved.slug), status_code=303)


# O trecho do editor que cada tipo de operação desenha de novo, com o HTMX.
EDITOR_FRAGMENTS = {
    "structure": "admin/_form_structure.html",
    "plan": "admin/_plan_editor.html",
    "data": "admin/_data_editor.html",
    "scoring": "admin/_scoring_editor.html",
}


@protected.post("/forms/{slug}/draft/edit")
def edit_draft(request: Request, slug: str, form: PostedForm) -> Response:
    """Uma operação do editor estruturado (incluir, remover ou reordenar uma etapa, uma pergunta,
    uma opção, um campo do plano, uma regra, um bloco de dados, uma janela, uma nota, um nível de
    faixa ou uma regra da pontuação; ou só desenhar de novo, depois de mudar um tipo), aplicada ao
    que a página mandou, sem gravar nada. Com o HTMX, volta só o trecho que a operação muda; sem
    ele, a página inteira, com os textos como estavam."""
    draft = _draft(slug)
    edit, texts = _posted(form)
    focus = edit.apply(_text_of(form, "op"))
    page = draft_pages.editor(
        draft,
        texts=texts,
        edit=edit,
        focus=focus,
        token=_text_of(form, "expected") or None,
        slug=_text_of(form, "slug") if "slug" in form else None,
        unsaved=True,
    )
    if request.headers.get("hx-request") == "true":
        fragment = EDITOR_FRAGMENTS[edit.section_of(_text_of(form, "op"))]
        return templates.TemplateResponse(request, fragment, {"page": page})
    return _page(request, "admin/form_draft.html", {"page": page})


DRAFT_DOES_NOT_COMPILE = (
    "O rascunho não compila com o código deste deploy, e a página dele não monta. Abra o editor: "
    "salvar mostra cada problema no campo dele."
)


@protected.get("/forms/{slug}/draft/preview")
def draft_preview(request: Request, slug: str) -> Response:
    """A página pública do rascunho guardado, numa moldura na largura de um celular."""
    try:
        page = draft_pages.preview_page(_draft(slug))
    except DefinitionError:
        raise NotFound(DRAFT_DOES_NOT_COMPILE) from None
    return _page(request, "admin/form_preview.html", {"page": page})


@protected.get("/forms/{slug}/draft/completion-preview")
def draft_completion_preview(request: Request, slug: str) -> Response:
    """Recibo de exemplo, sem link externo utilizável nem envio de respostas."""
    try:
        html = preview_actions.completion_preview(get_deps(), slug)
    except UnknownForm:
        raise NotFound(DRAFT_NOT_FOUND) from None
    except DefinitionError:
        raise NotFound(DRAFT_DOES_NOT_COMPILE) from None
    return HTMLResponse(html)


@protected.get("/forms/{slug}/draft/preview/frame")
def draft_preview_frame(
    request: Request, slug: str, step: Annotated[str, Query(alias="etapa")] = "1"
) -> Response:
    """A página pública do rascunho inteira, como a lead a veria, para o quadro da prévia: com o
    JavaScript público, as perguntas e as opções condicionais aparecem e somem, e as etapas andam
    pelos botões. Sem `<form>` (nada é enviado) e com `data-preview` (o JavaScript não guarda nada
    no aparelho). `etapa` é a etapa em que ela abre. Só o próprio admin pode emoldurá-la
    (`FRAMEABLE_HEADERS`). O HTML é o da ação que a API também chama (`actions.previews`)."""
    try:
        page = preview_actions.public_page(get_deps(), slug, draft_pages.step_of(step))
    except UnknownForm:
        raise NotFound(DRAFT_NOT_FOUND) from None
    except DefinitionError:
        raise NotFound(DRAFT_DOES_NOT_COMPILE) from None
    request.state.frameable = True
    return HTMLResponse(preview_actions.render_public_page(page))


# --- o PDF e a prévia da página de resultado -------------------------------------------------


def _template_texts(form: FormData) -> drafts.TemplateTexts:
    """O PDF, a prévia e as notas como a página do editor do PDF os mandou."""
    return drafts.TemplateTexts(
        pdf_html=_text_of(form, "pdf_html"),
        pdf_css=_text_of(form, "pdf_css"),
        preview=_text_of(form, "preview"),
        scores=tuple(value for value in form.getlist("pdf_scores") if isinstance(value, str)),
    )


def _recent_tests(request: Request, draft: drafts.Draft) -> list[draft_tests.DraftTestResult]:
    with get_deps().sessions() as db:
        return draft_tests.recent(db, draft, now=_now(request))


def _pdf_page(
    request: Request,
    draft: drafts.Draft,
    *,
    status_code: int = 200,
    source: str = draft_previews.EXAMPLE,
    **kwargs: Any,
) -> Response:
    with get_deps().sessions() as db:
        images = sorted(form_assets.visible(db, draft.slug).values(), key=lambda a: a.name)
    from app.forms.definition import compile_definition

    compile_definition(draft.document).require_generation()
    page = draft_pages.pdf_editor(
        draft, tests=_recent_tests(request, draft), source=source, images=images, **kwargs
    )
    return _page(request, "admin/form_pdf.html", {"page": page}, status_code=status_code)


@protected.get("/forms/{slug}/draft/pdf")
def draft_pdf_editor(request: Request, slug: str) -> Response:
    return _pdf_page(request, _draft(slug))


@protected.post("/forms/{slug}/draft/pdf")
def save_draft_pdf(
    request: Request,
    slug: str,
    session: Annotated[AdminSession, Depends(require_session)],
    form: PostedForm,
) -> Response:
    """Guarda o PDF, a prévia e as notas do rascunho, se tudo compila e o PDF só pede arquivos do
    app. Recusado, a página volta com o que a operadora mandou e os problemas, cada um com a linha
    do template, e nada é gravado."""
    texts = _template_texts(form)
    source = _text_of(form, "plan")
    try:
        saved = drafts.save_templates(
            get_deps(), slug, texts, actor=session.username, expected=_text_of(form, "expected")
        )
    except UnknownForm:
        return _error(request, 404, DRAFT_NOT_FOUND)
    except DefinitionError as exc:
        return _pdf_page(
            request,
            _draft(slug),
            status_code=422,
            texts=texts,
            problems=exc.problems,
            source=source,
        )
    except drafts.DraftChanged:
        return _pdf_page(
            request,
            _draft(slug),
            status_code=409,
            texts=texts,
            message=DRAFT_CHANGED,
            source=source,
        )
    logger.info("admin: PDF do rascunho salvo slug=%s pendente=%s", saved.slug, saved.pending)
    session.flash = ("ok", DRAFT_SAVED if saved.pending else DRAFT_SAVED_EQUAL)
    return RedirectResponse(f"{views.draft_url(saved.slug)}/pdf", status_code=303)


def _frame_error(
    request: Request, status_code: int, *, message: str | None, problems: Sequence[Problem] = ()
) -> Response:
    """O que o quadro da prévia mostra no lugar do PDF ou da página: o motivo, com as linhas. O erro
    em si é dado (`DefinitionError`, `PreviewUnavailable`, `PdfFailed`, da ação); aqui ele vira HTML."""
    request.state.frameable = True
    shown = [
        draft_pages.ShownProblem(
            draft_pages.where(problem, FormEdit(), slug_field=False), problem.message, ""
        )
        for problem in problems
    ]
    return templates.TemplateResponse(
        request,
        "admin/form_pdf_frame_error.html",
        {"message": message, "problems": shown},
        status_code=status_code,
    )


def _template_preview(
    request: Request, slug: str, form: FormData
) -> draft_previews.TemplatePreview | Response:
    """A prévia do PDF e da página de resultado com o que a página do editor mandou, sem gravar, ou
    a resposta do quadro com o motivo (a linha do template, ou o plano do último teste que falta)."""
    try:
        return preview_actions.template_preview(
            get_deps(),
            slug,
            preview_actions.TemplateChanges.of(_template_texts(form)),
            source=_text_of(form, "plan"),
            now=_now(request),
        )
    except UnknownForm:
        raise NotFound(DRAFT_NOT_FOUND) from None
    except DefinitionError as exc:
        return _frame_error(request, 422, message=None, problems=exc.problems)
    except draft_previews.PreviewUnavailable as unavailable:
        return _frame_error(request, 409, message=str(unavailable))


@protected.post("/forms/{slug}/draft/pdf/preview")
def draft_pdf_preview(request: Request, slug: str, form: PostedForm) -> Response:
    """O PDF do rascunho como está na página (salvo ou não), com o plano de exemplo ou o do último
    teste, para o quadro da página. É o PDF do adaptador do pedido: só abre arquivos do app. Não
    grava nada."""
    found = _template_preview(request, slug, form)
    if isinstance(found, Response):
        return found
    try:
        rendered = preview_actions.render_pdf(get_deps(), found)
    except preview_actions.PdfFailed as exc:
        return _frame_error(request, 422, message=f"O PDF não foi gerado: {exc.detail}.")
    request.state.frameable = "pdf"
    return Response(
        rendered.content,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="previa.pdf"'},
    )


@protected.post("/forms/{slug}/draft/pdf/result")
def draft_result_preview(request: Request, slug: str, form: PostedForm) -> Response:
    """A página de resultado do rascunho como a lead a vê, com a prévia da página (salva ou não) e o
    plano de exemplo ou o do último teste, para o quadro de celular. Não grava nada, e o arquivo
    para baixar é de mentira."""
    found = _template_preview(request, slug, form)
    if isinstance(found, Response):
        return found
    try:
        html = preview_actions.render_result(found)
    except DefinitionError as exc:
        return _frame_error(request, 422, message=None, problems=exc.problems)
    request.state.frameable = True
    return HTMLResponse(html)


def _now(request: Request) -> datetime:
    """O agora do admin, do relógio do app (os testes trocam o relógio)."""
    return instant(request.app.state.admin_clock)


def _tests_page(
    request: Request,
    slug: str,
    *,
    answers: str | None = None,
    day: str = "",
    message: str | None = None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    scoring: bool = False,
    prompt_only: bool = False,
) -> Response:
    """A página do teste: as respostas (as de exemplo, se `answers` vier vazia) e a data, o prompt
    montado com elas ou, com `scoring`, a pontuação delas (ou os problemas delas) e os testes
    recentes. O que ela junta é da ação (`draft_test_actions.testing`), a mesma da API."""
    try:
        state = draft_test_actions.testing(
            get_deps(),
            slug,
            answers=answers,
            day=day,
            scoring=scoring,
            prompt_only=prompt_only,
            now=_now(request),
        )
    except UnknownForm:
        raise NotFound(DRAFT_NOT_FOUND) from None
    if state.draft.document.get("generation") == "none":
        return _capture_tests_page(
            request, state.draft, answers=answers, day=day, status_code=status_code
        )
    page = draft_pages.testing_page(
        state.draft,
        answers=answers or "",
        day=day,
        assembled=state.assembled,
        scored=state.scored,
        recent=state.recent,
        problems=state.problems,
        message=message,
    )
    response = _page(
        request,
        "admin/form_draft_tests.html",
        {"page": page},
        status_code=422 if state.problems else status_code,
    )
    response.headers.update(headers or {})
    return response


def _capture_tests_page(
    request: Request,
    draft: drafts.Draft,
    *,
    answers: str | None = None,
    day: str = "",
    status_code: int = 200,
) -> Response:
    """O teste local usa a mesma validação de modo, respostas e conclusão da API/MCP."""
    result = None
    problems = ()
    try:
        result = drafts.test_capture(draft, answers=answers, day=day, now=_now(request))
    except DefinitionError as exc:
        problems = exc.problems
    errors = {
        field: tuple(
            problem for problem in problems if (problem.path == "date") == (field == "date")
        )
        for field in ("answers", "date")
    }
    return _page(
        request,
        "admin/capture_test.html",
        {
            "draft": draft,
            "result": result,
            "answers": answers or "",
            "day": day,
            "problems": problems,
            "errors": errors,
        },
        status_code=422 if problems else status_code,
    )


def _given(answers: str) -> str | None:
    """As respostas do formulário do teste; em branco, as de exemplo."""
    return answers if answers.strip() else None


@protected.get("/forms/{slug}/draft/tests")
def draft_tests_page(request: Request, slug: str) -> Response:
    return _tests_page(request, slug)


@protected.post("/forms/{slug}/draft/prompt")
def assemble_prompt(
    request: Request,
    slug: str,
    answers: Annotated[str, Form()] = "",
    date: Annotated[str, Form()] = "",
) -> Response:
    """O prompt montado com as respostas e a data do formulário, sem gravar nada. É um POST para
    as respostas não irem para a URL nem para o log de acesso."""
    return _tests_page(request, slug, answers=_given(answers), day=date, prompt_only=True)


@protected.post("/forms/{slug}/draft/score")
def score_draft(
    request: Request,
    slug: str,
    answers: Annotated[str, Form()] = "",
    date: Annotated[str, Form()] = "",
) -> Response:
    """A pontuação do rascunho com as respostas do formulário (as de exemplo, em branco), sem
    gravar nada. Um POST, como o prompt: as respostas não vão para a URL nem para o log."""
    return _tests_page(request, slug, answers=_given(answers), day=date, scoring=True)


@protected.post("/forms/{slug}/draft/tests")
async def start_draft_test(
    request: Request,
    slug: str,
    session: Annotated[AdminSession, Depends(require_session)],
    answers: Annotated[str, Form()] = "",
    date: Annotated[str, Form()] = "",
) -> Response:
    """Põe o teste do rascunho na fila e dispara a task `draft_test`, que recebe só o id; a página
    do teste acompanha o resultado. Recusado (as respostas, a data, um teste em andamento ou a
    pausa), a página do teste volta com o motivo, e nada sai para o LLM."""
    try:
        started = await draft_test_actions.start(
            get_deps(),
            request.app.state.test_trigger,
            slug,
            actor=session.username,
            answers=_given(answers),
            day=date,
            now=_now(request),
        )
    except UnknownForm:
        return _error(request, 404, DRAFT_NOT_FOUND)
    except DefinitionError:
        return await run_in_threadpool(
            _tests_page, request, slug, answers=_given(answers), day=date
        )
    except draft_tests.DraftTestRefused as refused:
        return await run_in_threadpool(
            _tests_page,
            request,
            slug,
            answers=_given(answers),
            day=date,
            message=refused.message,
            status_code=429 if refused.paused else 409,
            headers={"Retry-After": str(refused.retry_after)} if refused.paused else None,
        )
    return RedirectResponse(views.draft_test_url(slug, started.test_id), status_code=303)


TEST_NOT_FOUND = (
    "Teste não encontrado: só os mais recentes ficam guardados, por poucos dias. Rode outro na "
    "página do teste."
)


def _test_result(
    request: Request, slug: str, test_id: str
) -> tuple[drafts.Draft, draft_tests.DraftTestResult]:
    """O rascunho e o teste do caminho, ou a página de erro (`NotFound`). A busca é da ação
    (`draft_test_actions.find`), a mesma da API."""
    try:
        found = draft_test_actions.find(get_deps(), slug, test_id, now=_now(request))
    except UnknownForm:
        raise NotFound(DRAFT_NOT_FOUND) from None
    except draft_test_actions.UnknownTest:
        raise NotFound(TEST_NOT_FOUND) from None
    return found.draft, found.result


@protected.get("/forms/{slug}/draft/tests/{test_id}")
def draft_test(request: Request, slug: str, test_id: str) -> Response:
    draft, run = _test_result(request, slug, test_id)
    return _page(
        request, "admin/form_draft_test.html", {"page": draft_pages.result_page(draft, run)}
    )


@protected.get("/forms/{slug}/draft/tests/{test_id}/status")
def draft_test_status(request: Request, slug: str, test_id: str) -> Response:
    """O trecho que o HTMX troca enquanto o teste roda. Terminou: 286 para o polling, e o
    `HX-Refresh` recarrega a página, que mostra o resultado."""
    draft, run = _test_result(request, slug, test_id)
    if run.finished:
        return Response(status_code=STOP_POLLING, headers={"HX-Refresh": "true"})
    return templates.TemplateResponse(
        request, "admin/_draft_test_status.html", {"page": draft_pages.result_page(draft, run)}
    )


@protected.get("/forms/{slug}/draft/tests/{test_id}/pdf")
def draft_test_pdf(request: Request, slug: str, test_id: str) -> Response:
    try:
        pdf = draft_test_actions.pdf(get_deps(), slug, test_id, now=_now(request))
    except UnknownForm:
        raise NotFound(DRAFT_NOT_FOUND) from None
    except draft_test_actions.UnknownTest:
        raise NotFound(TEST_NOT_FOUND) from None
    except draft_test_actions.NoPdf:
        return _error(request, 404, "Este teste não tem PDF: ele não chegou até lá.")
    return Response(
        pdf.content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{pdf.filename}"'},
    )


PUBLISHED = (
    "Versão {number} publicada. Os envios novos já usam esta versão; os pedidos que já existem "
    "seguem a versão deles."
)
PUBLISH_CHANGED = (
    "O rascunho mudou desde que você abriu esta página (outra aba ou outra publicação). Nada foi "
    "publicado: confira o que muda agora e publique de novo."
)


def _publish_page(
    request: Request,
    draft: drafts.Draft,
    *,
    status_code: int = 200,
    **kwargs: Any,
) -> Response:
    with get_deps().sessions() as db:
        found = drafts.review(db, draft)
    page = draft_pages.publish_page(found, **kwargs)
    return _page(request, "admin/form_publish.html", {"page": page}, status_code=status_code)


@protected.get("/forms/{slug}/draft/publish")
def publish_review(request: Request, slug: str) -> Response:
    return _publish_page(request, _draft(slug))


@protected.post("/forms/{slug}/draft/publish")
def publish_draft(
    request: Request,
    slug: str,
    session: Annotated[AdminSession, Depends(require_session)],
    note: Annotated[str, Form()] = "",
    expected: Annotated[str, Form()] = "",
    confirm_contact: Annotated[str, Form()] = "",
) -> Response:
    """Publica o rascunho como a versão seguinte, que abre na página do formulário. Recusada, a
    página da publicação volta com o que falta, e nada é gravado."""
    try:
        published = drafts.publish(
            get_deps(),
            slug,
            actor=session.username,
            note=note,
            expected=expected,
            contact_confirmed=bool(confirm_contact),
        )
    except UnknownForm:
        return _error(request, 404, DRAFT_NOT_FOUND)
    except drafts.DraftChanged:
        return _publish_page(
            request, _draft(slug), note=note, message=PUBLISH_CHANGED, status_code=409
        )
    except drafts.PublishRefused as refused:
        return _publish_page(
            request,
            _draft(slug),
            reasons=refused.reasons,
            problems=refused.problems,
            note=note,
            status_code=422,
        )
    logger.info("admin: formulário publicado slug=%s versão=%s", slug, published.number)
    session.flash = ("ok", PUBLISHED.format(number=published.number))
    return RedirectResponse(views.form_url(slug), status_code=303)


VERSION_NOT_FOUND = "Versão não encontrada. Confira o histórico do formulário."
NOTHING_TO_COMPARE = (
    "Este formulário ainda não tem versão publicada para comparar: a primeira publicação cria a "
    "versão 1."
)
CONFIRM_REVERT = "Confirme que o rascunho passa a ser uma cópia da versão {number}."
REVERTED = (
    "A versão {number} foi copiada para o rascunho. Confira, teste se precisar e publique: a "
    "publicação cria uma versão nova, igual a ela."
)
REVERTED_EQUAL = "O rascunho voltou a ser igual à versão publicada: não há mudança pendente."
REVERT_CHANGED = (
    "O rascunho mudou desde que você abriu esta página (outra aba ou uma publicação). Nada foi "
    "copiado: confira o histórico e tente de novo."
)
REVERT_BROKEN = "A versão {number} não compila mais com o código deste deploy: {problems}"


@protected.get("/forms/{slug}/versions/compare")
def compare_versions(
    request: Request,
    slug: str,
    old: Annotated[str, Query(alias="from")] = "",
    new: Annotated[str, Query(alias="to")] = "",
) -> Response:
    """Duas versões do formulário, ou uma e o rascunho, seção por seção. Sem escolha, a versão
    atual contra a anterior (ou contra o rascunho, se há um pendente). Um formulário ainda sem
    versão publicada não tem o que comparar."""
    try:
        with get_deps().sessions() as db:
            result = version_actions.compare(db, slug, old=old, new=new)
    except UnknownForm:
        raise NotFound(DRAFT_NOT_FOUND) from None
    except version_actions.NothingToCompare:
        raise NotFound(NOTHING_TO_COMPARE) from None
    except version_actions.UnknownVersion:
        return _error(request, 404, VERSION_NOT_FOUND)
    page = draft_pages.compare_page(
        result.draft,
        result.numbers,
        old=result.old,
        new=result.new,
        sections=result.sections,
    )
    return _page(request, "admin/form_compare.html", {"page": page})


@protected.post("/forms/{slug}/versions/{number}/revert")
def revert_version(
    request: Request,
    slug: str,
    number: str,
    session: Annotated[AdminSession, Depends(require_session)],
    expected: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
) -> Response:
    """Copia uma versão publicada para o rascunho, com a confirmação de quem volta. O rascunho
    segue o caminho normal até a publicação."""
    version = _number(number)
    if version is None:
        return _error(request, 404, VERSION_NOT_FOUND)
    if not confirm:
        errors = {"revert": CONFIRM_REVERT.format(number=version), "revert_number": str(version)}
        return _form_page(request, slug, errors=errors, status_code=422)
    try:
        reverted = drafts.revert(
            get_deps(), slug, version, actor=session.username, expected=expected
        )
    except UnknownForm:
        return _error(request, 404, VERSION_NOT_FOUND)
    except drafts.DraftChanged:
        session.flash = ("err", REVERT_CHANGED)
        return RedirectResponse(views.form_url(slug), status_code=303)
    except DefinitionError as exc:
        listed = "; ".join(str(problem) for problem in exc.problems[:5])
        errors = {
            "revert": REVERT_BROKEN.format(number=version, problems=listed),
            "revert_number": str(version),
        }
        return _form_page(request, slug, errors=errors, status_code=422)
    logger.info("admin: versão copiada para o rascunho slug=%s versão=%s", slug, version)
    message = REVERTED.format(number=version) if reverted.pending else REVERTED_EQUAL
    session.flash = ("ok", message)
    return RedirectResponse(views.draft_url(slug), status_code=303)


# --- integração com a planilha (Baserow) ------------------------------------------------------

INTEGRATION_CHANGED = (
    "A integração mudou desde que você abriu esta página (outra aba ou outra pessoa). Nada foi "
    "alterado: confira o que está abaixo e tente de novo."
)
INTEGRATION_CONNECTED = (
    "Conectado ao Baserow. Agora escolha o que preenche cada campo da tabela e confira o relatório."
)
INTEGRATION_RECONNECTED = "Conexão salva."
MAPPING_SAVED = "Mapeamento salvo. Confira o relatório abaixo antes de ativar."
MAPPING_SAVED_STILL_ACTIVE = "Mapeamento salvo. A integração segue ativa."
MAPPING_SAVED_DEACTIVATED = (
    "Mapeamento salvo, mas a integração foi desativada: o relatório tem pendências. Corrija-as "
    "e ative de novo."
)
NOTHING_MAPPED = "Salve o mapeamento antes de ativar: nenhum campo da tabela está mapeado ainda."
REPORT_PENDING = (
    "O relatório ainda tem pendências, e uma integração só ativa com ele limpo. Corrija-as no "
    "mapeamento ou no Baserow e tente de novo."
)
INTEGRATION_ACTIVATED = (
    "Integração ativada: os pedidos que ficarem prontos daqui em diante viram uma linha na tabela. "
    "Os que já estavam prontos não são sincronizados; para isso, reprocesse o pedido."
)
INTEGRATION_DEACTIVATED = (
    "Integração desativada. As sincronizações que ainda estavam repetindo esperam a reativação."
)
CONFIRM_REMOVE_INTEGRATION = (
    "Confirme que entende o que acontece com as linhas da tabela e com o estado das sincronizações."
)
INTEGRATION_REMOVED = "Integração removida."
SYNC_NOT_FOUND = "Sincronização não encontrada."
SYNC_RESENT = "Reenvio disparado. Acompanhe o estado da sincronização abaixo."
SYNC_RESENT_DEFERRED = (
    "A sincronização voltou para a fila; o envio será disparado automaticamente em instantes."
)
SYNC_RESEND_REFUSED = "Só dá para reenviar uma sincronização que falhou. Confira o estado atual."
FAILED_SYNCS_RESENT = "{count} sincronizações voltaram para a fila e estão sendo reenviadas."
NO_FAILED_SYNCS = "Nenhuma sincronização falhada para reenviar."
UNREADABLE_SAMPLE = integration_views.SampleView(
    ok=False,
    headline="O teste não saiu: o token guardado não abre com WEBHOOK_ENCRYPTION_KEY.",
    note="Confira a chave no ambiente do app, ou cole o token de novo na conexão.",
)


def integration_form(request: Request) -> str:
    """O slug de um formulário que existe, ou `NotFound`."""
    slug: str = request.path_params["slug"]
    if _form_title(slug) is None:
        raise NotFound(FORM_NOT_FOUND)
    return slug


IntegrationSlug = Annotated[str, Depends(integration_form)]


def _integration_page(
    request: Request,
    slug: str,
    *,
    status_code: int = 200,
    profile: str | None = None,
    errors: dict[str, str] | None = None,
    problems: Sequence[str] = (),
    typed: dict[str, str] | None = None,
    selected: dict[int, str] | None = None,
    sample: integration_views.SampleView | None = None,
) -> Response:
    """A página da integração do formulário. Lê a tabela no Baserow a cada abertura: os campos, os
    tipos e as opções valem como estão lá agora. `typed` e `selected` são o que a pessoa acabou de
    digitar e escolher, devolvidos numa recusa (o token nunca volta). Num pedido do HTMX, uma recusa
    sai com 200, para o HTMX trocar o trecho."""
    deps = get_deps()
    profile = profile or request.query_params.get("profile")
    if profile not in (None, "pdf", "capture"):
        return _error(request, 422, "Selecione o perfil pdf ou capture.")
    detail = integration_actions.read_detail(deps, slug, typed=selected, profile=profile)
    if detail is None:
        return _error(request, 404, FORM_NOT_FOUND)
    stored = detail.stored
    page = integration_views.integration_page(detail)
    values = {
        "base_url": stored.base_url if stored else "",
        "table_id": str(stored.table_id) if stored else "",
        "purpose": stored.purpose if stored else "",
        **(typed or {}),
    }
    if request.headers.get("hx-request") == "true":
        status_code = 200
    return _page(
        request,
        "admin/form_integration.html",
        {
            "page": page,
            "errors": errors or {},
            "problems": list(problems),
            "typed": values,
            "sample": sample,
            "cipher_missing": deps.cipher is None,
            "max_url_length": integration_config.MAX_URL_LENGTH,
        },
        status_code=status_code,
    )


def _integration_back(
    session: AdminSession, slug: str, message: str, kind: str = "err"
) -> Response:
    """De volta à página da integração, com o aviso de por que nada mudou."""
    session.flash = (kind, message)
    return RedirectResponse(integration_views.integration_url(slug), status_code=303)


@protected.get("/forms/{slug}/baserow")
def integration_detail(request: Request, slug: IntegrationSlug) -> Response:
    return _integration_page(request, slug)


@protected.post("/forms/{slug}/baserow/connect")
def connect_integration(
    request: Request,
    slug: IntegrationSlug,
    session: Annotated[AdminSession, Depends(require_session)],
    base_url: Annotated[str, Form()] = "",
    table_id: Annotated[str, Form()] = "",
    token: Annotated[str, Form()] = "",
    version: Annotated[str, Form()] = "",
    purpose: Annotated[str | None, Form()] = None,
) -> Response:
    """Conecta o formulário a uma tabela do Baserow, ou muda a conexão. Antes de guardar, lê os
    campos da tabela com o token: uma recusa mostra o motivo e não grava nada. Um token em branco,
    numa integração que existe, mantém o guardado. Mudar o endereço ou a tabela apaga o mapeamento.
    As conferências são da ação (`app.admin.actions.integrations.connect`), que a API também usa."""
    expected = integration_actions.version_number(version)
    typed = {"base_url": base_url.strip(), "table_id": table_id.strip(), "purpose": purpose or ""}
    try:
        connected = integration_actions.connect(
            get_deps(),
            slug,
            base_url=base_url,
            purpose=purpose,
            table_id=integration_actions.table_number(typed["table_id"]),
            token=token,
            expected_version=expected,
            actor=session.username,
        )
    except integration_actions.NotPublished as exc:
        return _integration_page(
            request, slug, status_code=422, errors={"connect": str(exc)}, typed=typed
        )
    except integration_actions.InvalidConnection as exc:
        return _integration_page(request, slug, status_code=422, errors=exc.errors, typed=typed)
    except integration_actions.TableNotRead as exc:
        return _integration_page(
            request, slug, status_code=422, errors={"connect": str(exc)}, typed=typed
        )
    except integration_config.Conflict:
        return _integration_back(session, slug, INTEGRATION_CHANGED)
    return _integration_back(
        session,
        slug,
        INTEGRATION_CONNECTED if connected.created else INTEGRATION_RECONNECTED,
        "ok",
    )


def _chosen_sources(form: FormData) -> dict[int, str]:
    """As fontes que a pessoa escolheu, pelo id do campo (`source_<id>`); "" (nada) fica de fora."""
    chosen = {}
    for key, value in form.multi_items():
        field_id = integration_actions.field_number(key.removeprefix("source_"))
        if not key.startswith("source_") or field_id is None:
            continue
        if isinstance(value, str) and value.strip():
            chosen[field_id] = value.strip()
    return chosen


@protected.post("/forms/{slug}/baserow/mapping")
def save_integration_mapping(
    request: Request,
    slug: IntegrationSlug,
    session: Annotated[AdminSession, Depends(require_session)],
    form: PostedForm,
) -> Response:
    """Guarda o mapeamento: o que preenche cada campo da tabela, conferido contra a tabela como
    ela está agora. Recusado, a página volta com as escolhas e o que corrigir. Se o relatório do
    mapeamento novo tem pendências, uma integração ativa é desativada."""
    expected = integration_actions.version_number(str(form.get("version") or ""))
    chosen = _chosen_sources(form)
    try:
        saved = integration_actions.save_mapping(
            get_deps(),
            slug,
            chosen=chosen,
            expected_version=expected,
            actor=session.username,
            profile=str(form.get("profile")) if form.get("profile") in ("pdf", "capture") else None,
        )
    except integration_actions.NotPublished as exc:
        return _integration_back(session, slug, str(exc))
    except integration_actions.ReadFailed as exc:
        return _integration_page(
            request, slug, status_code=422, errors={"mapping": str(exc)}, selected=chosen
        )
    except integration_mapping.InvalidMapping as exc:
        return _integration_page(
            request, slug, status_code=422, problems=exc.problems, selected=chosen
        )
    except integration_config.Conflict:
        return _integration_back(session, slug, INTEGRATION_CHANGED)
    if saved.deactivated:
        return _integration_back(session, slug, MAPPING_SAVED_DEACTIVATED)
    return _integration_back(
        session, slug, MAPPING_SAVED_STILL_ACTIVE if saved.was_active else MAPPING_SAVED, "ok"
    )


@protected.post("/forms/{slug}/baserow/activate")
def activate_integration(
    request: Request,
    slug: IntegrationSlug,
    session: Annotated[AdminSession, Depends(require_session)],
    version: Annotated[str, Form()] = "",
) -> Response:
    """Liga a integração, se o relatório do mapeamento contra a tabela de agora está limpo. As
    conferências são da ação (`app.admin.actions.integrations.activate`), que a API também usa."""
    try:
        integration_actions.activate(
            get_deps(),
            slug,
            expected_version=integration_actions.version_number(version),
            actor=session.username,
        )
    except integration_actions.NotPublished:
        return _integration_back(session, slug, NOT_PUBLISHED)
    except integration_actions.IntegrationMissing:
        return _integration_back(session, slug, INTEGRATION_MISSING)
    except integration_actions.ReadFailed as exc:
        return _integration_page(request, slug, status_code=422, errors={"activate": str(exc)})
    except integration_actions.ReportPending as pending:
        message = NOTHING_MAPPED if pending.nothing_mapped else REPORT_PENDING
        return _integration_page(request, slug, status_code=422, errors={"activate": message})
    except integration_config.Conflict:
        return _integration_back(session, slug, INTEGRATION_CHANGED)
    return _integration_back(session, slug, INTEGRATION_ACTIVATED, "ok")


@protected.post("/forms/{slug}/baserow/deactivate")
def deactivate_integration(
    request: Request,
    slug: IntegrationSlug,
    session: Annotated[AdminSession, Depends(require_session)],
    version: Annotated[str, Form()] = "",
) -> Response:
    try:
        integration_actions.deactivate(
            get_deps(),
            slug,
            expected_version=integration_actions.version_number(version),
            actor=session.username,
        )
    except integration_config.Conflict:
        return _integration_back(session, slug, INTEGRATION_CHANGED)
    return _integration_back(session, slug, INTEGRATION_DEACTIVATED, "ok")


@protected.post("/forms/{slug}/baserow/remove")
def remove_integration(
    request: Request,
    slug: IntegrationSlug,
    session: Annotated[AdminSession, Depends(require_session)],
    version: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
) -> Response:
    if not confirm:
        return _integration_page(
            request, slug, status_code=422, errors={"remove": CONFIRM_REMOVE_INTEGRATION}
        )
    try:
        removed = integration_actions.remove(
            get_deps(),
            slug,
            expected_version=integration_actions.version_number(version),
            actor=session.username,
        )
    except integration_config.Conflict:
        return _integration_back(session, slug, INTEGRATION_CHANGED)
    session.flash = (
        "ok",
        INTEGRATION_REMOVED
        + f" Registros de entrega de respostas apagados: {removed.capture_counts.total}; pendentes não serão enviadas.",
    )
    return RedirectResponse(views.form_url(slug), status_code=303)


@protected.post("/forms/{slug}/baserow/test")
def send_integration_test(
    request: Request,
    slug: IntegrationSlug,
    session: Annotated[AdminSession, Depends(require_session)],
    profile: Annotated[Literal["pdf", "capture"] | None, Form()] = None,
) -> Response:
    """Manda uma linha sintética (a lead de exemplo, no domínio `exemplo`) para a tabela, pelo mesmo
    caminho de um pedido de verdade, e mostra o que aconteceu. Sai da API, sem passar pelo Hatchet, e
    não toca dado de lead. A linha fica na tabela; testar de novo atualiza a mesma. O envio é da ação
    (`app.admin.actions.integrations.send_test`), que a API também usa."""
    profile = profile or request.query_params.get("profile")
    if profile not in (None, "pdf", "capture"):
        return _error(request, 422, "Selecione o perfil pdf ou capture.")
    try:
        result = integration_actions.send_test(
            get_deps(), slug, actor=session.username, profile=profile
        )
    except integration_actions.TokenUnreadable:
        return _integration_page(request, slug, status_code=409, sample=UNREADABLE_SAMPLE)
    except integration_actions.IntegrationMissing:
        return _integration_back(session, slug, INTEGRATION_MISSING)
    return _integration_page(
        request, slug, profile=profile, sample=integration_views.sample_view(result)
    )


@protected.post("/forms/{slug}/baserow/resend-failed")
def resend_failed_syncs(
    request: Request,
    slug: IntegrationSlug,
    session: Annotated[AdminSession, Depends(require_session)],
) -> Response:
    """Reenvia de uma vez as sincronizações que falharam (até `launching.RESEND_FAILED_LIMIT`): o que se
    faz depois de corrigir o token, a tabela ou o mapeamento. Elas voltam para a fila na hora, e o
    disparo segue em segundo plano. A regra é da ação
    (`app.admin.actions.integrations.resend_failed`), a mesma da API."""
    try:
        done = integration_actions.resend_failed(get_deps(), slug, actor=session.username)
    except integration_actions.IntegrationMissing:
        return _integration_back(session, slug, INTEGRATION_MISSING)
    if not done.sync_ids:
        return _integration_back(session, slug, NO_FAILED_SYNCS)
    launching.launch_syncs_later(done.sync_ids)
    return _integration_back(
        session, slug, FAILED_SYNCS_RESENT.format(count=len(done.sync_ids)), "ok"
    )


@protected.post("/jobs/{public_id}/syncs/{sync_id}/resend")
async def resend_sync(
    request: Request,
    public_id: str,
    sync_id: str,
    session: Annotated[AdminSession, Depends(require_session)],
) -> Response:
    """Uma sincronização que falhou volta para pendente e é disparada de novo, como o reenvio de uma
    entrega: a resposta não espera mais que HATCHET_TRIGGER_TIMEOUT_SECONDS, e se o disparo não
    chegar ao engine o reconciliador o encontra. A regra é da ação `job_actions.resend_sync`, a mesma
    da API."""
    try:
        target = uuid.UUID(sync_id)
    except ValueError:
        return _error(request, 404, SYNC_NOT_FOUND)
    try:
        done = await job_actions.resend_sync(get_deps(), public_id, target, actor=session.username)
    except job_actions.JobNotFound:
        return _error(request, 404, JOB_NOT_FOUND)
    except job_actions.ResendRefused:
        session.flash = ("err", SYNC_RESEND_REFUSED)
    else:
        session.flash = ("ok", SYNC_RESENT if done.launched else SYNC_RESENT_DEFERRED)
    return RedirectResponse(f"/admin/jobs/{public_id}", status_code=303)


@protected.get("/captures")
def captures_page(
    request: Request,
    form: str = "",
    form_version: str = "",
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Response:
    """Lista paginada por chegada, com os mesmos filtros de formulário e versão da API."""
    from app.admin.api.common import decode_keyset, encode_keyset
    from app.admin.api.errors import ApiError
    from app.admin.queries import captures

    try:
        after = decode_keyset(cursor) if cursor else None
    except ApiError:
        return _error(request, 422, "Página inválida. Abra novamente a lista de capturas.")
    deps = get_deps()
    with deps.sessions() as db:
        items = captures.listing(
            db,
            deps.forms,
            form=form or None,
            version=int(form_version) if form_version.isdecimal() else None,
            after=after,
            limit=limit + 1,
        )
    more = len(items) > limit
    items = items[:limit]
    next_url = None
    if more:
        following = encode_keyset(items[-1]["created_at"], items[-1]["submission_id"])
        query = {"form": form, "cursor": following, "limit": limit}
        if form_version is not None:
            query["form_version"] = form_version
        next_url = "/admin/captures?" + urlencode(query)
    return _page(
        request,
        "admin/captures.html",
        {
            "items": items,
            "detail": None,
            "next_url": next_url,
            "form": form,
            "form_version": form_version,
        },
    )


@protected.get("/captures/{submission_id}")
def capture_detail_page(submission_id: str, request: Request) -> Response:
    """A operadora autenticada lê respostas pela mesma consulta da API."""
    from app.admin.queries import captures

    deps = get_deps()
    with deps.sessions() as db:
        found = captures.detail(db, deps.forms, submission_id, with_lead=True)
    return _page(
        request,
        "admin/captures.html",
        {"items": [], "detail": found},
        status_code=200 if found else 404,
    )


@protected.post("/forms/{slug}/draft/capture-test")
def capture_test_page(
    slug: str,
    request: Request,
    answers: Annotated[str, Form()] = "",
    date: Annotated[str, Form()] = "",
) -> Response:
    return _capture_tests_page(request, _draft(slug), answers=_given(answers), day=date)


router.include_router(open_routes)
router.include_router(protected)


@protected.post("/captures/{submission_id}/deliveries/{delivery_id}/retry")
def retry_capture_delivery_page(
    submission_id: str,
    delivery_id: str,
    request: Request,
    session: Annotated[AdminSession, Depends(require_session)],
    expected_dispatch: Annotated[int, Form()],
    expected_config_version: Annotated[int, Form()],
    confirm: Annotated[str, Form()] = "",
) -> Response:
    from app.admin.actions import captures

    if confirm != "on":
        return Response("Confirme o envio externo antes de repetir.", status_code=422)
    try:
        captures.retry(
            get_deps(),
            submission_id,
            delivery_id,
            expected_dispatch=expected_dispatch,
            expected_config_version=expected_config_version,
            actor=session.username,
        )
    except captures.Missing:
        return Response("Captura ou entrega não encontrada.", status_code=404)
    except captures.Conflict:
        return Response(
            "Entrega ou configuração mudou; confira a revisão e a finalidade.", status_code=409
        )
    return RedirectResponse(f"/admin/captures/{submission_id}", status_code=303)


@protected.post("/jobs/{public_id}/release")
async def release_job_pdf(
    request: Request, public_id: str, session: Annotated[AdminSession, Depends(require_session)]
) -> Response:
    from pydantic import ValidationError

    from app.deployment_contract import DeploymentContractError
    from app.plan_release import ReleasePdfCommand, ReleaseRefused

    posted = await request.form()
    allowed = {
        "expected_attempt",
        "expected_artifact_id",
        "expected_form_version_number",
        "expected_release_version",
        "idempotency_key",
        "mode",
        "reason_code",
    }
    fields = {key: posted.get(key, "") for key in allowed}
    try:
        command = ReleasePdfCommand.model_validate(
            {**fields, "confirm": bool(posted.get("confirm"))}
        )
        done = await job_actions.release_pdf(
            get_deps(),
            request.app.state.trigger,
            public_id,
            actor=session.username,
            command=command,
        )
    except ValidationError:
        return await run_in_threadpool(
            _detail_page,
            request,
            public_id,
            errors={"release": "Confira os campos e confirme a liberação desta tentativa."},
            release_form=fields,
            status_code=422,
        )
    except DeploymentContractError:
        return await run_in_threadpool(
            _detail_page,
            request,
            public_id,
            errors={"release": "A liberação aguarda a verificação da atualização."},
            release_form=fields,
            status_code=503,
        )
    except ReleaseRefused as error:
        if error.status == 404:
            return _error(request, 404, JOB_NOT_FOUND)
        return await run_in_threadpool(
            _detail_page,
            request,
            public_id,
            errors={
                "release": "A tentativa, a versão ou o PDF mudou. Confira a situação antes de liberar."
            },
            release_form=fields,
            status_code=error.status,
        )
    session.flash = (
        "ok",
        "Acesso liberado; notificação não enviada."
        if done.commit.notification_decision != "outbox"
        else "Acesso liberado; notificação registrada para envio.",
    )
    return RedirectResponse(f"/admin/jobs/{public_id}", status_code=303)
