"""API de serviço separada: autentica o header antes de interpretar qualquer corpo."""

import time

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import SecretStr
from starlette.concurrency import run_in_threadpool

from app import handoffs, integration_credentials
from app.admin.api.limits import RateLimiter
from app.admin.auth import instant
from app.deployment_contract import DeploymentContractError
from app.deps import get_deps
from app.handoff_types import HandoffError, ResolvedHandoff, ResolveHandoffRequest

PRIVATE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex",
}


def refusal(error):
    headers = dict(PRIVATE_HEADERS)
    if error.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse({"code": error.code}, status_code=error.status, headers=headers)


class AuthenticatedServiceRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            import sqlalchemy as sa

            clock = request.app.state.clock
            value = request.headers.get("Authorization", "")
            if not value.startswith("Bearer "):
                value = ""
            else:
                value = value[7:]

            def authenticate():
                with get_deps().sessions() as session:
                    return integration_credentials.authenticate_credential(
                        session, SecretStr(value), now=instant(clock)
                    )

            try:
                request.state.integration_principal = await run_in_threadpool(authenticate)
            except HandoffError as error:
                wait = request.app.state.invalid_limiter.take(
                    request.client.host if request.client else "unknown"
                )
                if wait:
                    return JSONResponse(
                        {"code": "rate_limited"},
                        status_code=429,
                        headers=PRIVATE_HEADERS | {"Retry-After": str(wait)},
                    )
                return refusal(error)
            except (sa.exc.SQLAlchemyError, DeploymentContractError):
                return JSONResponse(
                    {"code": "temporarily_unavailable"}, status_code=503, headers=PRIVATE_HEADERS
                )
            wait = request.app.state.limiter.take(
                str(request.state.integration_principal.credential_id)
            )
            if wait:
                return JSONResponse(
                    {"code": "rate_limited"},
                    status_code=429,
                    headers=PRIVATE_HEADERS | {"Retry-After": str(wait)},
                )
            return await original(request)

        return handler


router = APIRouter(route_class=AuthenticatedServiceRoute)


@router.post(
    "/handoffs/resolve",
    operation_id="resolve_handoff_reference",
    response_model=ResolvedHandoff,
    tags=["Encaminhamentos"],
    openapi_extra={"security": [{"IntegrationCredentialAuth": []}]},
)
def resolve_handoff_reference(body: ResolveHandoffRequest, request: Request):
    """Resolve correlation.v1; não identifica a participante nem concede acesso ao plano."""
    import sqlalchemy as sa

    try:
        with get_deps().sessions.begin() as session:
            result = handoffs.resolve_handoff(
                session,
                request.state.integration_principal,
                body.reference,
                now=instant(request.app.state.clock),
            )
        return JSONResponse(result.model_dump(mode="json"), headers=PRIVATE_HEADERS)
    except HandoffError as error:
        return refusal(error)
    except (sa.exc.SQLAlchemyError, DeploymentContractError):
        return JSONResponse(
            {"code": "temporarily_unavailable"}, status_code=503, headers=PRIVATE_HEADERS
        )


def create_integration_api(*, clock=time.time):
    app = FastAPI(
        title="API de integrações Leadkit",
        servers=[{"url": "/integrations/v1"}],
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.clock = clock
    app.state.limiter = RateLimiter(60, clock, burst=60)
    app.state.invalid_limiter = RateLimiter(10, clock, burst=10)
    app.include_router(router)
    from app.api.journey import router as journey_router

    app.include_router(journey_router)

    @app.exception_handler(RequestValidationError)
    async def invalid_body(request, error):
        return JSONResponse(
            {
                "code": "validation_failed",
                "errors": [
                    {
                        "field": "body"
                        if request.url.path.endswith("/journey/events")
                        else "reference",
                        "message": "Informe um objeto JSON válido conforme o contrato.",
                    }
                ],
            },
            status_code=422,
            headers=PRIVATE_HEADERS,
        )

    original = app.openapi

    def openapi():
        schema = original()
        schema.setdefault("components", {}).setdefault("securitySchemes", {})[
            "IntegrationCredentialAuth"
        ] = {
            "type": "http",
            "scheme": "bearer",
            "description": "Credencial própria lki_ com escopo handoffs:resolve; chaves administrativas não são aceitas.",
        }
        schema["components"]["securitySchemes"]["EventCredentialAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "description": "Credencial exclusiva lke_, escopo journey:events:write. Fontes pausadas ou credenciais inválidas recebem401 antes do corpo.",
        }
        return schema

    app.openapi = openapi
    return app
