"""Páginas de fontes e jornada com sessão, CSRF e serviços comuns à API/MCP."""

from typing import get_args
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from fastapi import Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app import event_sources, journey
from app.admin.api.common import PageParams
from app.admin.auth import instant
from app.admin.handoff_routes import _expires_at
from app.admin.views import moment
from app.deployment_contract import DeploymentContractError
from app.deps import get_deps
from app.handoff_types import HandoffError, Revoke
from app.journey_types import (
    CreateEventCredential,
    CreateEventSource,
    EraseJourney,
    EventType,
    RotateEventCredential,
    SaveEventSource,
    SetEventSourceEnabled,
)
from app.models import HandoffReceiver, Submission

TYPE_LABELS = {
    "correlation.linked": "Vínculos declarados",
    "invitation.sent": "Convite enviado",
    "meeting.state_reported": "Agendamento",
    "meeting.attendance_recorded": "Comparecimento",
    "notification.delivery_reported": "Transporte de notificação",
    "participant.receipt_acknowledged": "Confirmação explícita da participante",
    "entity.withdrawn": "Retirada de declaração",
}
STATE_LABELS = {
    "declared": "Declarado pela fonte",
    "withdrawn": "Declaração retirada",
    "erased": "Dados externos apagados",
    "scheduled": "Agendada",
    "cancelled": "Cancelada",
    "attended": "Compareceu",
    "no_show": "Não compareceu",
    "unknown": "Não informado",
    "accepted": "Aceito para envio",
    "confirmed": "Recebimento declarado",
    "failed": "Falhou",
    "held": "Aguardando liberação",
    "released": "Liberado",
    "persisted": "Salva",
    "queued": "Na fila",
    "pending": "Pendente",
    "automatic": "Automático",
}
EVIDENCE_LABELS = {
    "correlation_record": "Registro de vínculo",
    "dispatch_record": "Registro de despacho",
    "calendar_record": "Registro de agenda",
    "attendance_record": "Registro de comparecimento",
    "provider_delivery_receipt": "Comprovante do canal",
    "participant_ack": "Confirmação da participante",
    "operator_correction": "Correção pela responsável",
}
CHANNEL_LABELS = {
    "chat": "Conversa",
    "email": "E-mail",
    "sms": "SMS",
    "voice": "Voz",
    "system": "Sistema",
    "other": "Outro",
}


def sources_page(
    request, *, slug=None, source_id=None, status=200, message=None, values=None, created=None
):
    from app.admin.routes import _page

    with get_deps().sessions() as session:
        source = (
            event_sources.source_info(event_sources.source_row(session, source_id))
            if source_id
            else None
        )
        slug = source.form_slug if source else slug
        receiver = session.scalar(
            sa.select(HandoffReceiver).where(HandoffReceiver.form_slug == slug)
        )
        receiver_view = (
            {
                "id": receiver.id,
                "revision": receiver.revision,
                "name": receiver.name,
                "enabled": receiver.enabled,
            }
            if receiver
            else None
        )
        sources = event_sources.list_sources(session, slug)
        credentials = (
            event_sources.list_credentials(
                session, source_id, now=instant(request.app.state.admin_clock)
            )
            if source
            else []
        )
    return _page(
        request,
        "admin/event_sources.html",
        {
            "slug": slug,
            "source": source,
            "sources": sources,
            "receiver": receiver_view,
            "credentials": credentials,
            "values": values or {},
            "created": created,
            "message": message,
            "type_labels": TYPE_LABELS,
            "evidence_labels": EVIDENCE_LABELS,
            "channel_labels": CHANNEL_LABELS,
            "idempotency_key": str(uuid4()),
            "moment": moment,
        },
        status_code=status,
    )


def journey_page(request, submission_id, *, status=200, message=None):
    from app.admin.routes import _page

    with_lead = request.query_params.get("include_lead_data") == "true"
    with get_deps().sessions() as session:
        view = journey.read_journey(session, submission_id, with_lead=with_lead)
        source_id = request.query_params.get("source_id") or None
        event_type = request.query_params.get("type") or None
        try:
            source_id = UUID(source_id) if source_id else None
            if event_type and event_type not in get_args(EventType):
                raise ValueError
            page = PageParams(limit=20, cursor=request.query_params.get("cursor"))
            events, cursor = journey.list_events(
                session,
                submission_id,
                page=page,
                source_id=source_id,
                event_type=event_type,
                with_lead=with_lead,
            )
        except ValueError:
            raise HandoffError("validation_failed", status=422) from None
        submission = session.get(Submission, submission_id)
        slug = submission.form_slug
        sources = event_sources.list_sources(session, slug)

    def scheduled(value):
        if value.starts_at is None:
            return None
        return (
            value.starts_at.astimezone(ZoneInfo(value.timezone)).strftime("%d/%m/%Y às %H:%M")
            + " · "
            + value.timezone
        )

    return _page(
        request,
        "admin/journey.html",
        {
            "journey": view,
            "slug": slug,
            "sources": sources,
            "events": events,
            "next_cursor": cursor,
            "selected_source": str(source_id) if source_id else "",
            "selected_type": event_type or "",
            "with_lead": with_lead,
            "type_labels": TYPE_LABELS,
            "state_labels": STATE_LABELS,
            "moment": moment,
            "scheduled": scheduled,
            "message": message,
        },
        status_code=status,
    )


def register(protected):
    @protected.get("/forms/{slug}/event-sources")
    def source_list(slug: str, request: Request):
        return sources_page(request, slug=slug)

    @protected.get("/event-sources/{source_id}")
    def source_detail(source_id: UUID, request: Request):
        return sources_page(request, source_id=source_id)

    async def source_command(request, *, operation, slug=None, source_id=None, credential_id=None):
        from app.admin.routes import posted_form

        raw = await posted_form(request)

        def value(name):
            result = raw.get(name, "")
            return result if isinstance(result, str) else ""

        values = {
            name: value(name) for name in ("name", "namespace", "expiry_choice", "expires_at")
        }
        values["types"] = [item for item in raw.getlist("types") if item in TYPE_LABELS]
        values["channels"] = [item for item in raw.getlist("channels") if item in CHANNEL_LABELS]
        values["evidence_kinds"] = [
            item for item in raw.getlist("evidence_kinds") if item in EVIDENCE_LABELS
        ]
        actor, now = request.state.admin_session.username, instant(request.app.state.admin_clock)

        def apply():
            common = {
                "confirm": value("confirm") == "true",
                "expected_version": value("expected_version"),
            }
            with get_deps().sessions.begin() as session:
                if operation in ("create", "update"):
                    data = common | {
                        "name": value("name"),
                        "policy": {
                            key: values[key] for key in ("types", "channels", "evidence_kinds")
                        },
                    }
                    if operation == "create":
                        receiver = session.scalar(
                            sa.select(HandoffReceiver).where(HandoffReceiver.form_slug == slug)
                        )
                        if receiver is None:
                            raise HandoffError("not_found", status=404)
                        command = CreateEventSource(
                            **data,
                            receiver_id=receiver.id,
                            receiver_revision=int(value("receiver_revision")),
                            namespace=value("namespace"),
                            idempotency_key=value("idempotency_key"),
                        )
                        return event_sources.create_source(
                            session, slug, command=command, actor=actor, now=now
                        ), None
                    return event_sources.update_source(
                        session, source_id, command=SaveEventSource(**data), actor=actor, now=now
                    ), None
                if operation == "enabled":
                    return event_sources.set_enabled(
                        session,
                        source_id,
                        command=SetEventSourceEnabled(**common, enabled=value("enabled") == "true"),
                        actor=actor,
                        now=now,
                    ), None
                if operation in ("credential", "rotate"):
                    expiry = _expires_at(value("expiry_choice"), value("expires_at"))
                    data = {
                        "expires_at": expiry,
                        "idempotency_key": value("idempotency_key"),
                        "confirm": common["confirm"],
                    }
                    if operation == "credential":
                        command = CreateEventCredential(
                            **data,
                            name=value("name"),
                            expected_source_version=common["expected_version"],
                            source_revision=value("source_revision"),
                        )
                        result = event_sources.issue_credential(
                            session, source_id, command=command, actor=actor, now=now
                        )
                    else:
                        result = event_sources.rotate_credential(
                            session,
                            credential_id,
                            command=RotateEventCredential(
                                **common,
                                **{key: val for key, val in data.items() if key != "confirm"},
                            ),
                            actor=actor,
                            now=now,
                        )
                    return None, result
                event_sources.revoke_credential(
                    session, credential_id, command=Revoke(**common), actor=actor, now=now
                )
                return None, None

        try:
            source, created = await run_in_threadpool(apply)
            if created:
                return sources_page(request, source_id=source_id, created=created)
            return RedirectResponse(
                "/admin/event-sources/" + str(source.id if source else source_id), status_code=303
            )
        except (ValidationError, ValueError, HandoffError, DeploymentContractError) as error:
            status = (
                error.status
                if isinstance(error, HandoffError)
                else 503
                if isinstance(error, DeploymentContractError)
                else 422
            )
            message = (
                "Releia a página antes de confirmar novamente."
                if status == 409
                else "Confira os campos e confirme a ação. Nenhuma alteração foi aplicada."
            )
            return sources_page(
                request,
                slug=slug,
                source_id=source_id,
                status=status,
                message=message,
                values=values,
            )

    @protected.post("/forms/{slug}/event-sources")
    async def source_create(slug: str, request: Request):
        return await source_command(request, operation="create", slug=slug)

    @protected.post("/event-sources/{source_id}/{operation}")
    async def source_update(source_id: UUID, operation: str, request: Request):
        if operation not in ("update", "enabled", "credential"):
            raise HandoffError("not_found", status=404)
        return await source_command(request, operation=operation, source_id=source_id)

    @protected.post("/event-sources/{source_id}/credentials/{credential_id}/{operation}")
    async def credential_update(
        source_id: UUID, credential_id: UUID, operation: str, request: Request
    ):
        if operation not in ("rotate", "revoke"):
            raise HandoffError("not_found", status=404)
        with get_deps().sessions() as session:
            from app.models import EventCredential

            credential = session.get(EventCredential, credential_id)
            if credential is None or credential.source_id != source_id:
                raise HandoffError("not_found", status=404)
        return await source_command(
            request, operation=operation, source_id=source_id, credential_id=credential_id
        )

    @protected.get("/submissions/{submission_id}/journey")
    def submission_journey(submission_id: UUID, request: Request):
        return journey_page(request, submission_id)

    @protected.post("/submissions/{submission_id}/journey/erase")
    async def submission_erase(submission_id: UUID, request: Request):
        from app.admin.routes import posted_form

        raw = await posted_form(request)
        try:
            command = EraseJourney(
                expected_journey_version=raw.get("expected_journey_version", ""),
                expected_handoff_version=raw.get("expected_handoff_version", ""),
                confirm=raw.get("confirm") == "true",
            )

            def apply():
                with get_deps().sessions.begin() as session:
                    journey.erase_journey(
                        session,
                        submission_id,
                        command=command,
                        actor=request.state.admin_session.username,
                        now=instant(request.app.state.admin_clock),
                    )

            await run_in_threadpool(apply)
            return RedirectResponse(f"/admin/submissions/{submission_id}/journey", status_code=303)
        except (ValidationError, HandoffError) as error:
            return journey_page(
                request,
                submission_id,
                status=error.status if isinstance(error, HandoffError) else 422,
                message="Confira a confirmação e releia as versões antes de tentar novamente.",
            )
