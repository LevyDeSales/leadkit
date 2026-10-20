"""A configuração da integração de um formulário: a conexão com a tabela, o mapeamento e a situação.

- No máximo uma integração de cada tipo por formulário, pelo slug (`integrations`). Hoje o único tipo
  é o `baserow`.
- O token do Baserow é cifrado com WEBHOOK_ENCRYPTION_KEY, como o segredo de um destino do webhook
  (`app.destinations.SecretCipher`), e nunca sai daqui para o log, a auditoria nem a tela: quem lê a
  configuração recebe `StoredIntegration`, que não o traz, e só `open_connection` o abre, na hora de
  falar com o Baserow. Trocar a chave sem perder o token funciona como nos destinos (`prepare`).
- Toda mudança fica na auditoria em nome de quem a fez, com o host da URL e o id da tabela, sem o
  token e sem o caminho da URL.
- Cada mudança sobe a `version`: o admin recusa uma página aberta antes dela (`Conflict`), como no
  destino do webhook.
"""

from __future__ import annotations

import ipaddress
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.destinations import NoCipher, SecretCipher, UnreadableSecret, host_of
from app.forms import FormSpec, UnknownForm
from app.forms import stored as stored_forms
from app.integrations.baserow import Connection
from app.integrations.mapping import Entry, entries_of, key_entry
from app.models import AuditLog, Integration, utcnow

if TYPE_CHECKING:
    from app.deps import Deps

logger = logging.getLogger(__name__)

KIND = "baserow"
MAX_URL_LENGTH = 2000


class Conflict(Exception):
    """A integração mudou desde que a página foi aberta (outra aba, um clique repetido).
    `current` é como ela está agora (None se não existe mais)."""

    def __init__(self, current: StoredIntegration | None = None) -> None:
        super().__init__()
        self.current = current


class InvalidBaseUrl(ValueError):
    """O endereço do Baserow não serve. A mensagem é para a tela."""


class MissingToken(ValueError):
    """Conectar pela primeira vez pede o token."""


@dataclass(frozen=True)
class StoredIntegration:
    """Uma integração como o admin a mostra: nunca traz o token."""

    id: uuid.UUID
    form_slug: str
    kind: str
    base_url: str
    table_id: int
    entries: tuple[Entry, ...]
    key_field_id: int | None
    active: bool
    version: int
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime
    token_changed_at: datetime
    capture_entries: tuple[Entry, ...] = ()
    capture_key_field_id: int | None = None

    @property
    def host(self) -> str:
        return host_of(self.base_url)


def _stored(row: Integration) -> StoredIntegration:
    return StoredIntegration(
        id=row.id,
        form_slug=row.form_slug,
        kind=row.kind,
        base_url=row.base_url,
        table_id=row.table_id,
        entries=tuple(entries_of(row.mapping)),
        key_field_id=row.key_field_id,
        active=row.active,
        version=row.version,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
        token_changed_at=row.token_changed_at,
        capture_entries=tuple(entries_of(row.capture_mapping)),
        capture_key_field_id=row.capture_key_field_id,
    )


def check_base_url(url: str, *, production: bool) -> str:
    """O endereço do Baserow, sem espaços nas pontas nem barra no fim; senão `InvalidBaseUrl` com o
    motivo. Só http ou https, com host, sem usuário e senha, sem `?` nem `#` e sem IP link-local.
    Em produção só https: o token vai no cabeçalho de cada chamada."""
    url = url.strip()
    if not url:
        raise InvalidBaseUrl("Informe o endereço do Baserow.")
    if len(url) > MAX_URL_LENGTH:
        raise InvalidBaseUrl(f"O endereço passa de {MAX_URL_LENGTH} caracteres.")
    if any(char.isspace() or not char.isprintable() for char in url):
        raise InvalidBaseUrl("O endereço não pode ter espaços nem caracteres de controle.")
    try:
        parts = urlsplit(url)
    except ValueError:  # colchete sem par (`https://[::1`), host que o NFKC quebra: era um 500
        raise InvalidBaseUrl("O endereço não é válido: confira o que foi digitado.") from None
    if parts.scheme not in ("http", "https"):
        raise InvalidBaseUrl("Use um endereço que comece com https:// (ou http://).")
    if "@" in parts.netloc:
        raise InvalidBaseUrl("Tire o usuário e a senha do endereço: o acesso é pelo token.")
    try:
        parts.port  # noqa: B018 (só valida: uma porta fora do intervalo levanta ValueError)
    except ValueError:
        raise InvalidBaseUrl("A porta do endereço não é válida.") from None
    if not parts.hostname:
        raise InvalidBaseUrl("Falta o endereço do servidor.")
    if parts.query or parts.fragment:
        raise InvalidBaseUrl(
            "Use só o endereço do Baserow (como https://baserow.example.test), sem o que vem "
            "depois de ? ou de #."
        )
    if _link_local(parts.hostname):
        raise InvalidBaseUrl(
            "Endereços link-local (169.254.x.x, fe80::) são de metadados do servidor e não servem."
        )
    if parts.scheme == "http" and production:
        raise InvalidBaseUrl(
            "Em produção, use https: o token do Baserow vai no cabeçalho de cada chamada."
        )
    return url.rstrip("/")


def server_of(url: str) -> tuple[str, str, int]:
    """O servidor de um endereço já conferido (`check_base_url`): o esquema, o host em minúsculas e a
    porta (a padrão do esquema, se o endereço não a diz). O caminho não conta: é o mesmo servidor.
    Dois endereços com servidores diferentes não dividem o token."""
    parts = urlsplit(url)
    default = 443 if parts.scheme == "https" else 80
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port or default


def _link_local(hostname: str) -> bool:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_link_local


def _audit(
    session: Session,
    actor: str,
    action: str,
    row: Integration | StoredIntegration,
    **details: object,
) -> None:
    session.add(
        AuditLog(
            actor=actor,
            action=f"integration.{action}",
            target_type="integration",
            target_id=str(row.id),
            details={
                "form_slug": row.form_slug,
                "kind": row.kind,
                "host": host_of(row.base_url),
                "table_id": row.table_id,
                **details,
            },
        )
    )


def load(session: Session, form_slug: str) -> StoredIntegration | None:
    """A integração do formulário, ou None se ele não tem."""
    row = session.scalar(
        sa.select(Integration).where(Integration.form_slug == form_slug, Integration.kind == KIND)
    )
    return _stored(row) if row else None


def active_ids(session: Session, form_slug: str) -> list[uuid.UUID]:
    """As integrações ativas do formulário: as que recebem cada pedido que ele conclui."""
    return list(
        session.scalars(
            sa.select(Integration.id).where(
                Integration.form_slug == form_slug, Integration.active.is_(True)
            )
        )
    )


def form_spec(deps: Deps, form_slug: str) -> FormSpec | None:
    """A versão publicada mais recente do formulário, ativo ou não (o mapeamento segue as perguntas
    dela); None se ele não tem versão publicada ou ela não compila mais."""
    with deps.sessions() as session:
        published = stored_forms.stored_version(session, form_slug)
    if published is None:
        return None
    try:
        return deps.forms.get(form_slug, published.number)
    except UnknownForm:
        return None


def open_connection(cipher: SecretCipher | None, row: Integration) -> Connection:
    """A conexão com o token aberto, para a hora de falar com o Baserow. Levanta `UnreadableSecret`
    se o token não abre com a chave atual."""
    if cipher is None:
        raise UnreadableSecret
    return Connection(row.base_url, row.table_id, cipher.open(row.token_sealed))


def stored_connection(
    session: Session, cipher: SecretCipher | None, form_slug: str
) -> Connection | None:
    """A conexão da integração do formulário com o token aberto, ou None se ele não tem. Levanta
    `UnreadableSecret` se o token não abre."""
    row = session.scalar(
        sa.select(Integration).where(Integration.form_slug == form_slug, Integration.kind == KIND)
    )
    return open_connection(cipher, row) if row else None


def token_unreadable(session: Session, cipher: SecretCipher | None, form_slug: str) -> bool:
    """O token guardado não abre com a chave atual: as sincronizações falham até alguém colar o
    token de novo."""
    sealed = session.scalar(
        sa.select(Integration.token_sealed).where(
            Integration.form_slug == form_slug, Integration.kind == KIND
        )
    )
    return sealed is not None and (cipher is None or not cipher.opens(sealed))


def connect(
    session: Session,
    cipher: SecretCipher | None,
    *,
    form_slug: str,
    base_url: str,
    table_id: int,
    token: str,
    actor: str,
    expected_version: int,
    production: bool,
) -> StoredIntegration:
    """Cria a integração (`expected_version` 0; o token é obrigatório) ou muda a conexão da que
    existe. Um `token` vazio, numa que existe, mantém o guardado. Mudar o endereço ou a tabela
    apaga o mapeamento e desativa a integração: os campos eram de outra tabela. Se a versão mudou
    desde que a pessoa a viu, levanta `Conflict` e nada muda. A conexão já foi conferida com o
    Baserow por quem chama."""
    if cipher is None:
        raise NoCipher
    from app.integration_readiness import configuration_lock

    configuration_lock(session)
    base_url = check_base_url(base_url, production=production)
    token = token.strip()
    if expected_version == 0:
        if not token:
            raise MissingToken
        return _create(session, cipher, form_slug, base_url, table_id, token, actor)
    row = _lock_for_change(session, form_slug, expected_version, actor)
    moved = (row.base_url, row.table_id) != (base_url, table_id)
    previous_host = host_of(row.base_url)
    row.base_url, row.table_id = base_url, table_id
    if token:
        row.token_sealed = cipher.seal(token)
        row.token_changed_at = row.updated_at
    if moved:
        row.mapping, row.key_field_id, row.active = [], None, False
        row.capture_mapping, row.capture_key_field_id = [], None
    _audit(
        session,
        actor,
        "connect",
        row,
        previous_host=previous_host,
        token_changed=bool(token),
        mapping_reset=moved,
    )
    return _stored(row)


def _create(
    session: Session,
    cipher: SecretCipher,
    form_slug: str,
    base_url: str,
    table_id: int,
    token: str,
    actor: str,
) -> StoredIntegration:
    now = utcnow()
    row = Integration(
        id=uuid.uuid4(),
        form_slug=form_slug,
        kind=KIND,
        base_url=base_url,
        table_id=table_id,
        token_sealed=cipher.seal(token),
        mapping=[],
        key_field_id=None,
        active=False,
        version=1,
        created_by=actor,
        created_at=now,
        updated_by=actor,
        updated_at=now,
        token_changed_at=now,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except sa.exc.IntegrityError:  # outro pedido criou a integração primeiro
        raise Conflict(load(session, form_slug)) from None
    _audit(session, actor, "create", row, token_changed=True)
    return _stored(row)


def save_mapping(
    session: Session,
    *,
    form_slug: str,
    entries: list[Entry],
    actor: str,
    expected_version: int,
    deactivate: bool,
    profile: str = "pdf",
) -> StoredIntegration:
    """Guarda o mapeamento, já conferido por `mapping.check_entries`. `deactivate`: o relatório
    do mapeamento novo tem pendências, e uma integração ativa não pode seguir enviando com elas."""
    row = _lock_for_change(session, form_slug, expected_version, actor)
    key = key_entry(entries, profile=profile)
    if profile == "capture":
        row.capture_mapping = [entry.as_json() for entry in entries]
        row.capture_key_field_id = key.field_id if key else None
    else:
        row.mapping = [entry.as_json() for entry in entries]
        row.key_field_id = key.field_id if key else None
    was_active = row.active
    if deactivate:
        row.active = False
    _audit(
        session,
        actor,
        "mapping",
        row,
        fields=len(entries),
        profile=profile,
        deactivated=bool(deactivate and was_active),
    )
    return _stored(row)


def set_active(
    session: Session, *, form_slug: str, active: bool, actor: str, expected_version: int
) -> StoredIntegration:
    """Liga ou desliga a integração. Quem liga conferiu o relatório antes (`mapping.build_report`)."""
    row = _lock_for_change(session, form_slug, expected_version, actor)
    row.active = active
    _audit(session, actor, "activate" if active else "deactivate", row)
    return _stored(row)


def remove(session: Session, *, form_slug: str, actor: str, expected_version: int) -> None:
    """Apaga a integração e o estado das sincronizações dela. As linhas que já foram para a planilha
    ficam lá: o app não apaga nada no Baserow."""
    row = _lock_for_change(session, form_slug, expected_version, actor)
    _audit(session, actor, "remove", row)
    session.delete(row)


def record_test(
    session: Session, integration: StoredIntegration, *, actor: str, **details: object
) -> None:
    """O teste na auditoria: o resultado e o código do pedido de teste, sem dado de lead."""
    _audit(session, actor, "test", integration, **details)


def _lock_for_change(
    session: Session, form_slug: str, expected_version: int, actor: str
) -> Integration:
    """A integração travada para mudar, se ainda está na versão que a pessoa viu (senão
    `Conflict`), já com a versão nova e a autoria da mudança."""
    from app.integration_readiness import configuration_lock

    configuration_lock(session)
    row = session.scalar(
        sa.select(Integration)
        .where(
            Integration.form_slug == form_slug,
            Integration.kind == KIND,
            Integration.version == expected_version,
        )
        .with_for_update()
    )
    if row is None:
        raise Conflict(load(session, form_slug))
    row.version += 1
    row.updated_by = actor
    row.updated_at = utcnow()
    return row


# --- subida da API e do worker ----------------------------------------------------------------


def prepare(deps: Deps) -> None:
    """Roda na subida da API e do worker, depois de `destinations.prepare`. Os tokens cifrados com
    uma chave que não é a primeira de WEBHOOK_ENCRYPTION_KEY são cifrados de novo com ela (a troca
    de chave em dois deploys, como nos segredos dos destinos), e os que nenhuma chave abre vão para
    o log, só com o slug: o admin avisa, e as sincronizações falham até alguém colar o token de novo.

    Banco ainda sem a tabela (no primeiro deploy da stack do Swarm, a API e o worker sobem antes das
    migrations): não faz nada."""
    with deps.sessions() as session:
        migrated = sa.inspect(session.connection()).has_table(Integration.__tablename__)
    if not migrated:
        return
    with deps.sessions.begin() as session:
        rows = session.execute(
            sa.select(Integration.id, Integration.form_slug, Integration.token_sealed)
        ).all()
        if not rows:
            return
        if deps.cipher is None:
            logger.warning("integrações: sem WEBHOOK_ENCRYPTION_KEY, nenhum token abre")
            return
        for integration_id, form_slug, sealed in rows:
            if not deps.cipher.opens(sealed):
                logger.warning(
                    "integração formulario=%s: o token não abre com WEBHOOK_ENCRYPTION_KEY; cole o "
                    "token de novo no admin",
                    form_slug,
                )
                continue
            resealed = deps.cipher.reseal(sealed)
            if resealed is not None:  # só se ninguém trocou o token no meio do caminho
                session.execute(
                    sa.update(Integration)
                    .where(Integration.id == integration_id, Integration.token_sealed == sealed)
                    .values(token_sealed=resealed)
                )
