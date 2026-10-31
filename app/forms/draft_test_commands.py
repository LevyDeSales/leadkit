"""Identidade e recibo específicos do teste de rascunho; não concedem autorização."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import load_only

from app.forms import UnknownForm, draft_tests, drafts
from app.forms.definition import Clock, DefinitionError, Problem, compile_definition
from app.models import ApiKey, DraftTest, DraftTestCommand, Form, WebhookIssuer

COMMAND_V1_WINDOW = timedelta(days=3)  # Contrato dt1 fixo: não segue TEST_DAYS futuro.
_IDENTITY = re.compile(r"dt1_([0-9a-f]{32})_([0-9]{13})_([0-9a-f]{32})\Z")


class CommandError(Exception):
    """Recusa segura sem repetir a entrada nem revelar recibos de outra autoridade."""

    def __init__(self, code: str):
        self.code = code
        self.message = {
            "command_not_found": "Não há comando de teste com esta identidade.",
            "command_forbidden": "Este comando não está disponível para esta autoridade.",
            "command_conflict": "A identidade já está vinculada a outro escopo, revisão ou argumentos.",
            "command_expired": "A identidade do comando expirou. Para outro teste, escolha Novo teste e confirme novamente.",
            "command_in_future": "A identidade está no futuro. Use a identidade emitida pelo servidor.",
            "validation_failed": "Informe uma identidade válida e a revisão esperada do rascunho.",
            "confirmation_required": "Confirme a chamada real de IA e o possível custo.",
            "unavailable": "O contexto de teste não está disponível nesta instalação.",
        }[code]
        super().__init__(self.message)


@dataclass(frozen=True)
class Principal:
    kind: str
    id: str


@dataclass(frozen=True)
class CommandInput:
    expected_version: str
    answers: Any = None
    day: str = ""
    input_kind: str = "json"


class CommandContext(BaseModel):
    command_id: str
    expected_version: str
    server_time: datetime
    create_until: datetime


class CommandReceipt(BaseModel):
    command_id: str
    operation: str = "start_form_draft_test/v1"
    namespace: uuid.UUID
    form_id: uuid.UUID
    form_slug: str
    expected_version: str
    principal_kind: str
    principal_id: str
    channel: str
    confirmation_evidence: str = "client_reported"
    issued_at: datetime
    committed_at: datetime
    expires_at: datetime
    test_id: uuid.UUID
    audit_id: uuid.UUID
    dispatch_state: str
    dispatch_at: datetime | None
    test_status: str | None
    failure_code: str | None
    result_state: str


class ReceiptResponse(BaseModel):
    receipt: CommandReceipt


@dataclass(frozen=True)
class Committed:
    receipt: CommandReceipt
    created: bool


def _namespace(session):
    namespace = session.scalar(sa.select(WebhookIssuer.namespace).where(WebhookIssuer.id == 1))
    if namespace is None:
        raise CommandError("unavailable")
    return namespace


def issue_context(deps, slug: str, *, now: datetime) -> CommandContext:
    with deps.sessions() as session:
        draft = drafts.load(session, slug)
        if draft is None:
            raise UnknownForm(slug)
        namespace = _namespace(session)
    millis = int(now.timestamp() * 1000)
    issued = datetime.fromtimestamp(millis / 1000, UTC)
    return CommandContext(
        command_id=f"dt1_{namespace.hex}_{millis:013d}_{uuid.uuid4().hex}",
        expected_version=draft.token,
        server_time=issued,
        create_until=issued + COMMAND_V1_WINDOW,
    )


def _identity(session, identity: str, now: datetime) -> datetime:
    match = _IDENTITY.fullmatch(identity)
    if match is None:
        raise CommandError("validation_failed")
    namespace, millis, random = match.groups()
    if uuid.UUID(hex=random).version != 4:
        raise CommandError("validation_failed")
    if uuid.UUID(hex=namespace) != _namespace(session):
        raise CommandError("command_conflict")
    try:
        issued = datetime.fromtimestamp(int(millis) / 1000, UTC)
    except (ValueError, OverflowError, OSError):
        raise CommandError("validation_failed") from None
    if issued > now:
        raise CommandError("command_in_future")
    return issued


def _digest(value) -> str:
    try:
        raw = json.dumps(
            value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError):
        raise CommandError("validation_failed") from None
    return hashlib.sha256(raw.encode()).hexdigest()


def _input(command: CommandInput) -> tuple[str, Any, str]:
    if not command.expected_version or command.input_kind not in ("json", "html_fields"):
        raise CommandError("validation_failed")
    answers = command.answers
    if command.input_kind == "json" and isinstance(answers, str):
        try:
            answers = json.loads(answers)
        except ValueError:
            raise DefinitionError(
                [Problem("answers", "Informe respostas em JSON válido.")]
            ) from None
    day = command.day.strip()
    if day:
        day = drafts._simulated(day).isoformat()
    digest = _digest(["v1", command.expected_version, command.input_kind, answers, day])
    return digest, answers, day


def _authorize(session, principal: Principal, now: datetime, *, writing=False):
    if principal.kind == "admin" and principal.id:
        return
    if principal.kind != "api_key":
        raise CommandError("command_forbidden")
    key = session.scalar(
        sa.select(ApiKey)
        .where(ApiKey.key_id == principal.id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if (
        key is None
        or key.revoked_at is not None
        or (key.expires_at is not None and key.expires_at <= now)
        or (writing and key.role != "total")
    ):
        raise CommandError("command_forbidden")


def _receipt(session, row: DraftTestCommand, now: datetime) -> CommandReceipt:
    test = session.scalar(
        sa.select(DraftTest)
        .options(
            load_only(
                DraftTest.status,
                DraftTest.failure_code,
                DraftTest.started_at,
                DraftTest.claimed_at,
            )
        )
        .where(DraftTest.id == row.test_id)
    )
    retained = test is not None and now <= test.started_at + timedelta(days=draft_tests.TEST_DAYS)
    status, code = (
        draft_tests._status(test.status, test.failure_code, test.started_at, test.claimed_at, now)
        if retained
        else (None, None)
    )
    return CommandReceipt(
        **{
            name: getattr(row, name)
            for name in (
                "command_id",
                "namespace",
                "form_id",
                "form_slug",
                "expected_version",
                "principal_kind",
                "principal_id",
                "channel",
                "issued_at",
                "committed_at",
                "expires_at",
                "test_id",
                "audit_id",
                "dispatch_at",
            )
        },
        dispatch_state="uncertain" if row.dispatch_state == "dispatching" else row.dispatch_state,
        test_status=status,
        failure_code=code,
        result_state="retained" if retained else "expired",
    )


def _lookup(session, slug, identity, principal, command, now):
    row = session.get(DraftTestCommand, identity)
    if row is None:
        return None
    if (row.principal_kind, row.principal_id) != (principal.kind, principal.id):
        raise CommandError("command_forbidden")
    if now >= row.expires_at:
        raise CommandError("command_expired")
    form_id = session.scalar(sa.select(Form.id).where(Form.slug == slug))
    if row.form_id != form_id or row.form_slug != slug:
        raise CommandError("command_conflict")
    if command is not None and row.input_digest != _input(command)[0]:
        raise CommandError("command_conflict")
    return _receipt(session, row, now)


def resolve(deps, slug, identity, *, principal: Principal, command=None, now: datetime):
    with deps.sessions.begin() as session:
        _authorize(session, principal, now)
        issued = _identity(session, identity, now)
        found = _lookup(session, slug, identity, principal, command, now)
        if found is not None:
            return found
        if now >= issued + COMMAND_V1_WINDOW:
            raise CommandError("command_expired")
        raise CommandError("command_not_found")


def commit(deps, slug, identity, *, principal, actor, command, confirmed, channel, clock: Clock):
    try:
        with deps.sessions.begin() as session:
            now = clock()
            _authorize(session, principal, now, writing=True)
            issued = _identity(session, identity, now)
            found = _lookup(session, slug, identity, principal, command, now)
            if found is not None:
                return Committed(found, False)
            form_id = session.scalar(sa.select(Form.id).where(Form.slug == slug).with_for_update())
            if form_id is None:
                raise UnknownForm(slug)
            now = clock()  # A espera de lock não renova uma janela já vencida.
            _authorize(session, principal, now, writing=True)
            found = _lookup(session, slug, identity, principal, command, now)
            if found is not None:
                return Committed(found, False)
            if now >= issued + COMMAND_V1_WINDOW:
                raise CommandError("command_expired")
            if confirmed is not True:
                raise CommandError("confirmation_required")
            digest, answers, day = _input(command)
            draft = drafts.load(session, slug)
            draft_tests.check_version(draft, command.expected_version)
            if command.input_kind == "html_fields":
                from starlette.datastructures import FormData

                from app.admin import simulation

                spec = compile_definition(draft.document, version_number=draft.next_number)
                answers = simulation.read_answers(spec, FormData(answers))
            assembled = drafts.assemble(draft, answers=answers, day=day, now=now)
            from app.forms.continuation_contract import assert_definition_contract

            assert_definition_contract(session, draft.document)
            committed_at = clock()
            _authorize(session, principal, committed_at, writing=True)
            if committed_at >= issued + COMMAND_V1_WINDOW:
                raise CommandError("command_expired")
            test_id = uuid.uuid4()
            audit_id = draft_tests._persist_start(
                session, draft, assembled, actor=actor, now=now, test_id=test_id
            )
            row = DraftTestCommand(
                command_id=identity,
                namespace=_namespace(session),
                form_id=form_id,
                form_slug=slug,
                expected_version=command.expected_version,
                principal_kind=principal.kind,
                principal_id=principal.id,
                input_digest=digest,
                effective_digest=_digest(
                    [draft.token, assembled.answers.model_dump(mode="json"), day, now.isoformat()]
                ),
                issued_at=issued,
                committed_at=committed_at,
                expires_at=committed_at + COMMAND_V1_WINDOW,
                test_id=test_id,
                audit_id=audit_id,
                channel=channel,
                dispatch_state="committed",
            )
            session.add(row)
            session.flush()
            receipt = _receipt(session, row, now)
        return Committed(receipt, True)
    except IntegrityError as error:
        if (
            getattr(getattr(error.orig, "diag", None), "constraint_name", None)
            != "pk_draft_test_commands"
        ):
            raise
        # A PK global arbitra a mesma identidade disputada até por formulários distintos.
        receipt = resolve(deps, slug, identity, principal=principal, command=command, now=clock())
        return Committed(receipt, False)


def begin_dispatch(deps, identity: str, *, now: datetime) -> bool:
    """Somente a criadora chama; um replay nunca tenta assumir despacho abandonado."""
    with deps.sessions.begin() as session:
        return (
            session.execute(
                sa.update(DraftTestCommand)
                .where(
                    DraftTestCommand.command_id == identity,
                    DraftTestCommand.dispatch_state == "committed",
                    DraftTestCommand.expires_at > now,
                )
                .values(dispatch_state="dispatching", dispatch_at=now)
            ).rowcount
            == 1
        )


def record_dispatch(deps, identity: str, state: str) -> None:
    """Conserva somente o fato de transporte, separado da execução atual do teste."""
    if state not in ("enqueued", "not_started", "uncertain"):
        raise ValueError("estado de despacho inválido")
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(DraftTestCommand)
            .where(
                DraftTestCommand.command_id == identity,
                DraftTestCommand.dispatch_state == "dispatching",
            )
            .values(dispatch_state=state)
        )
