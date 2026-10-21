"""Página mínima de gestão, protegida pela sessão e CSRF existentes do admin."""

from uuid import UUID, uuid4

from fastapi import Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from app import handoff_receivers, handoffs, integration_credentials
from app.admin.auth import instant
from app.deployment_contract import DeploymentContractError
from app.deps import get_deps
from app.destinations import SecretCipher
from app.handoff_types import (
    CreateCredential,
    HandoffError,
    Revoke,
    RotateCredential,
    SaveReceiver,
    SetReceiverEnabled,
)


def _render(request, slug, *, status=200, message=None, created=None, values=None):
    from app.admin.routes import _page

    receiver, credentials, reference = None, [], None
    with get_deps().sessions() as session:
        try:
            receiver = handoff_receivers.get_receiver(session, slug)
            credentials = integration_credentials.list_credentials(
                session, slug, now=instant(request.app.state.admin_clock)
            )
        except HandoffError:
            pass
        supplied = request.query_params.get("submission_id")
        if supplied:
            try:
                reference = handoffs.get_handoff(session, UUID(supplied))
                if receiver is None or reference.receiver_id != receiver.id:
                    reference = None
            except (ValueError, HandoffError):
                message = "Encaminhamento não encontrado neste formulário."
    return _page(
        request,
        "admin/form_handoff.html",
        {
            "slug": slug,
            "receiver": receiver,
            "credentials": credentials,
            "handoff": reference,
            "message": message,
            "created": created,
            "values": values or {},
            "idempotency_key": str(uuid4()),
        },
        status_code=status,
    )


def register(protected):
    @protected.get("/forms/{slug}/handoff")
    def handoff_page(slug: str, request: Request):
        return _render(request, slug)

    async def handoff_command(slug: str, operation: str, request: Request):
        from starlette.concurrency import run_in_threadpool

        from app.admin.routes import posted_form

        raw = await posted_form(request)

        def first(key, default=""):
            value = raw.get(key, default)
            return value if isinstance(value, str) else default

        confirmed = first("confirm") == "true"
        expected = first("expected_version")
        actor = request.state.admin_session.username
        moment = instant(request.app.state.admin_clock)
        values = {
            key: first(key)
            for key in ("name", "entry_url", "reference_ttl_seconds", "expires_at", "expiry_choice")
        }

        def apply():
            with get_deps().sessions.begin() as session:
                if operation == "save":
                    return handoff_receivers.save_receiver(
                        session,
                        slug,
                        command=SaveReceiver(
                            expected_version=expected,
                            name=first("name"),
                            entry_url=first("entry_url"),
                            reference_ttl_seconds=first("reference_ttl_seconds", "604800"),
                            confirm=confirmed,
                        ),
                        actor=actor,
                        now=moment,
                    )
                if operation == "enabled":
                    return handoff_receivers.set_receiver_enabled(
                        session,
                        slug,
                        command=SetReceiverEnabled(
                            expected_version=expected,
                            enabled=first("enabled") == "true",
                            confirm=confirmed,
                        ),
                        actor=actor,
                        now=moment,
                        cipher=SecretCipher.from_settings(get_deps().settings),
                    )
                if operation == "create":
                    return integration_credentials.create_credential(
                        session,
                        slug,
                        command=CreateCredential(
                            name=first("name"),
                            expected_receiver_version=expected,
                            receiver_revision=first("receiver_revision"),
                            expires_at=None
                            if first("expiry_choice", "none") == "none"
                            else first("expires_at"),
                            idempotency_key=first("idempotency_key"),
                            confirm=confirmed,
                        ),
                        actor=actor,
                        now=moment,
                    )
                if operation == "rotate":
                    return integration_credentials.rotate_credential(
                        session,
                        UUID(first("credential_id")),
                        command=RotateCredential(
                            expected_version=expected,
                            expires_at=None
                            if first("expiry_choice", "none") == "none"
                            else first("expires_at"),
                            idempotency_key=first("idempotency_key"),
                            confirm=confirmed,
                        ),
                        actor=actor,
                        now=moment,
                    )
                if operation == "revoke":
                    return integration_credentials.revoke_credential(
                        session,
                        UUID(first("credential_id")),
                        command=Revoke(expected_version=expected, confirm=confirmed),
                        actor=actor,
                        now=moment,
                    )
                if operation == "revoke-reference":
                    return handoffs.revoke_handoff(
                        session,
                        UUID(first("submission_id")),
                        command=Revoke(expected_version=expected, confirm=confirmed),
                        actor=actor,
                        now=moment,
                    )
                raise HandoffError("not_found", status=404)

        try:
            result = await run_in_threadpool(apply)
        except ValidationError:
            return _render(
                request,
                slug,
                status=422,
                values=values,
                message="Confira os campos informados. Datas precisam de fuso, como 2027-01-01T12:00:00Z.",
            )
        except ValueError:
            return _render(
                request,
                slug,
                status=422,
                values=values,
                message="Confira o identificador informado.",
            )
        except HandoffError as error:
            messages = {
                "confirmation_required": "Confirme a ação antes de continuar.",
                "version_conflict": "A configuração mudou. Confira a versão atual e tente novamente.",
                "credential_secret_already_issued": "Este segredo já foi emitido e não pode ser exibido novamente. Revogue ou gire a credencial explicitamente.",
                "receiver_not_ready": "Crie uma credencial válida desta revisão antes de habilitar o receptor.",
                "invalid_entry_url": "Use HTTPS sem query, fragmento, usuário ou senha.",
                "invalid_expiry": "Escolha uma data futura ou Sem vencimento.",
            }
            return _render(
                request,
                slug,
                status=error.status,
                values=values,
                message=messages.get(
                    error.code, "A ação não foi aplicada. Confira a configuração e a revisão."
                ),
            )
        except DeploymentContractError:
            return _render(
                request,
                slug,
                status=503,
                values=values,
                message="A atualização ainda não habilitou esta operação. Aguarde a conclusão.",
            )
        if operation in ("create", "rotate"):
            return _render(request, slug, created=result)
        return RedirectResponse(f"/admin/forms/{slug}/handoff", status_code=303)

    def command_for(operation):
        async def action(slug: str, request: Request):
            return await handoff_command(slug, operation, request)

        action.__name__ = f"handoff_{operation.replace('-', '_')}"
        return action

    for operation in ("save", "enabled", "create", "rotate", "revoke", "revoke-reference"):
        protected.add_api_route(
            f"/forms/{{slug}}/handoff/{operation}", command_for(operation), methods=["POST"]
        )
