"""Ingestão de eventos com autenticação anterior ao corpo e erros sem eco."""

import sqlalchemy as sa
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import SecretStr
from starlette.concurrency import run_in_threadpool

from app import event_sources
from app.admin.auth import instant
from app.api.handoffs import PRIVATE_HEADERS, refusal
from app.deployment_contract import DeploymentContractError
from app.deps import get_deps
from app.handoff_types import HandoffError
from app.journey_types import JourneyEventInput, JourneyEventReceipt


class EventServiceRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            authorization = request.headers.get("Authorization", "")
            value = authorization[7:] if authorization.startswith("Bearer ") else ""

            def authenticate():
                with get_deps().sessions() as session:
                    return event_sources.authenticate(
                        session, SecretStr(value), now=instant(request.app.state.clock)
                    )

            try:
                request.state.event_principal = await run_in_threadpool(authenticate)
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
                return refusal(HandoffError("temporarily_unavailable", status=503))
            wait = request.app.state.limiter.take(str(request.state.event_principal.credential_id))
            if wait:
                return JSONResponse(
                    {"code": "rate_limited"},
                    status_code=429,
                    headers=PRIVATE_HEADERS | {"Retry-After": str(wait)},
                )
            if (
                request.headers.get("Content-Type", "").split(";")[0].strip().lower()
                != "application/json"
            ):
                return refusal(HandoffError("unsupported_media_type", status=415))
            parts, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 32768:
                    return refusal(HandoffError("payload_too_large", status=413))
                parts.append(chunk)
            request._body = b"".join(parts)
            return await original(request)

        return handler


router = APIRouter(route_class=EventServiceRoute)


@router.post(
    "/journey/events",
    operation_id="ingest_journey_event",
    response_model=JourneyEventReceipt,
    tags=["Jornada"],
    summary="Registrar declaração externa",
    description="Persiste um snapshot autenticado; não confirma sua verdade comercial nem libera plano ou envia mensagens.",
    responses={
        code: {"description": description}
        for code, description in {
            200: "Repetição exata.",
            201: "Declaração persistida.",
            401: "invalid_credentials",
            403: "source_not_authorized",
            404: "correlation_unavailable",
            409: "event_conflict, entity_revision_conflict ou source_revision_conflict",
            413: "payload_too_large",
            415: "unsupported_media_type",
            422: "validation_failed",
            429: "rate_limited",
            503: "temporarily_unavailable",
        }.items()
    },
    openapi_extra={"security": [{"EventCredentialAuth": []}]},
)
def ingest_journey_event(body: JourneyEventInput, request: Request):
    from app.journey import ingest_event

    try:
        with get_deps().sessions.begin() as session:
            receipt = ingest_event(
                session, request.state.event_principal, body, now=instant(request.app.state.clock)
            )
        return JSONResponse(
            receipt.model_dump(mode="json"),
            status_code=200 if receipt.replay else 201,
            headers=PRIVATE_HEADERS,
        )
    except HandoffError as error:
        return refusal(error)
    except (sa.exc.SQLAlchemyError, DeploymentContractError):
        return refusal(HandoffError("temporarily_unavailable", status=503))
