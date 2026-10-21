"""Leituras internas com capacidade própria; nenhum parâmetro público promove audiência."""

from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.admin.actions.jobs import NoPdf, PdfRemoved, pdf_file
from app.deployment_contract import DeploymentContractError
from app.deps import get_deps
from app.errors import RetryableError, TerminalError
from app.internal_links import InternalLinkSigner, InternalPdfAccess, validate_internal_access
from app.models import utcnow
from app.pdf_availability import availability_dict, current_pdf
from app.tokens import TokenError
from app.web.public import PUBLIC_HEADERS, JobDetails, PublicRoute, _preview

router = APIRouter(route_class=PublicRoute)
COOKIE = "leadkit_internal_pdf"


def _claims(request, deps, public_id):
    token = request.query_params.get("k") or request.cookies.get(COOKIE, "")
    claims = InternalLinkSigner(deps.signer.secrets_).verify(token, now=utcnow())
    if claims.job_public_id != public_id:
        raise TokenError("invalid")
    return claims, token


def _refused():
    return JSONResponse(
        {"code": "internal_access_unavailable"}, status_code=404, headers=PUBLIC_HEADERS
    )


def _exchange(request, public_id, claims, token):
    response = RedirectResponse(request.url.path, status_code=303, headers=PUBLIC_HEADERS)
    response.set_cookie(
        COOKIE,
        token,
        max_age=max(0, claims.expires_at - int(utcnow().timestamp())),
        path=f"/i/jobs/{public_id}",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/i/jobs/{public_id}/status", include_in_schema=False)
def internal_status(public_id: str, request: Request):
    deps = get_deps()
    try:
        claims, token = _claims(request, deps, public_id)
        with deps.sessions.begin() as session:
            job, _ = validate_internal_access(session, deps, claims)
            body = {
                "public_id": job.public_id,
                "attempt": job.attempt,
                "status": job.status,
                "current_pdf": availability_dict(current_pdf(session, deps.storage, job)),
            }
        if "k" in request.query_params:
            return _exchange(request, public_id, claims, token)
        return JSONResponse(body, headers=PUBLIC_HEADERS)
    except (TokenError, DeploymentContractError):
        return _refused()


@router.get("/i/jobs/{public_id}", include_in_schema=False)
def internal_page(public_id: str, request: Request):
    from app.models import Plan, Submission

    deps = get_deps()
    try:
        claims, token = _claims(request, deps, public_id)
        with deps.sessions.begin() as session:
            job, artifact = validate_internal_access(session, deps, claims)
            if "k" in request.query_params:
                return _exchange(request, public_id, claims, token)
            submission = session.get_one(Submission, job.submission_id)
            plan = session.get_one(Plan, artifact.plan_id)
            details = JobDetails(
                submission.form_slug,
                submission.form_version_number,
                None,
                plan.document,
                artifact,
                scores=submission.scores or {},
            )
            preview = _preview(deps, details) or ""
            content = (
                '<!doctype html><html lang="pt-BR"><meta charset="utf-8"><title>Inspeção do plano</title><main><h1>Inspeção do plano</h1>'
                + str(preview)
                + '<a href="/i/jobs/'
                + escape(public_id, quote=True)
                + '/pdf">Baixar PDF</a></main></html>'
            )
            return HTMLResponse(content, headers=PUBLIC_HEADERS)
    except (TokenError, DeploymentContractError):
        return _refused()


@router.get("/i/jobs/{public_id}/pdf", include_in_schema=False)
def internal_pdf(public_id: str, request: Request):
    deps = get_deps()
    try:
        claims, _ = _claims(request, deps, public_id)
        pdf = pdf_file(deps, public_id, None, signed_url=True, access=InternalPdfAccess(claims))
    except (
        TokenError,
        DeploymentContractError,
        NoPdf,
        PdfRemoved,
        RetryableError,
        TerminalError,
        OSError,
    ):
        return _refused()
    if pdf.url:
        return RedirectResponse(pdf.url, status_code=302, headers=PUBLIC_HEADERS)
    return Response(
        pdf.content,
        media_type="application/pdf",
        headers={
            **PUBLIC_HEADERS,
            "Content-Disposition": f'attachment; filename="plano-{public_id}.pdf"',
        },
    )
