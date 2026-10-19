"""As chaves de API do admin: criar, conferir um `Authorization: Bearer`, revogar e listar.

Uma chave tem nome e papel (`leitura` só consulta; `total` faz tudo o que o admin logado faz). A
página `/admin/api-keys` e a API (`/admin/api/v1`) criam e revogam; a API usa `authenticate` em cada
chamada.

O formato
---------
`gp_<id>_<segredo>`: o `id` tem 8 caracteres hexadecimais, é público e serve só para achar a linha;
o segredo são 32 bytes sorteados, em base64 do alfabeto da URL (`-` e `_` no lugar de `+` e `/`), sem
preenchimento: a chave viaja num cabeçalho e é copiada com o mouse. O prefixo `gp_` deixa uma chave
vazada reconhecível por quem busca segredos.

O que é guardado
----------------
Só o SHA-256 do segredo, em hexadecimal. Com 256 bits sorteados, sal e custo não protegem mais
nada, e o argon2 da senha do admin, feito para texto humano, imporia uma conta pesada em cada
chamada. A chave inteira só existe na resposta de `create` (`CreatedKey.key`): nem a lista, nem a
auditoria, nem o log a trazem, e o segredo não se recupera.

Conferir
--------
- Validade e revogação são lidas do banco a cada chamada, sem cache: revogar vale na hora.
- O hash é comparado em tempo constante, e uma chave que não existe gasta a mesma conta que uma de
  segredo errado.
- Só quem acertou o segredo vê o motivo (`key_expired`, `key_revoked`). Chave ausente, malformada,
  desconhecida ou de segredo errado recebe sempre `invalid_credentials`, que não diz se ela existe.
- `last_used_at` é gravado no máximo uma vez por minuto, num único UPDATE condicional.

O nome
------
De 3 a 60 caracteres (`a-z`, `0-9` e hífen), com ao menos uma letra ou um dígito (`---` não é nome).
Não muda e não se reaproveita, nem o de uma chave
revogada: a auditoria cita o autor como `chave:<nome>`, e é isso que mantém uma entrada antiga
dizendo quem foi. Por isso a linha revogada fica, e o nome continua dela.

A transação é de quem chama (`with deps.sessions.begin() as session`), como nos outros serviços; a de
`authenticate` tem um cuidado a mais, descrito nela. Todo `now` é um instante com fuso (UTC), para os
testes trocarem o relógio.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import API_KEY_ROLES, ApiKey, AuditLog, utcnow

PREFIX = "gp_"
KEY_ID_BYTES = 4  # 8 caracteres hexadecimais
SECRET_BYTES = 32
SECRET_LENGTH = 43  # 32 bytes em base64 sem preenchimento
KEY_ID_ATTEMPTS = 5  # o id público tem 32 bits: no raro choque, sorteia outro
LAST_USED_EVERY = timedelta(minutes=1)
NAME_PATTERN = re.compile(
    r"(?=.*[a-z0-9])[a-z0-9-]{3,60}"
)  # o lookahead: ao menos uma letra ou dígito
_KEY = re.compile(rf"{PREFIX}([0-9a-f]{{{KEY_ID_BYTES * 2}}})_([A-Za-z0-9_-]{{{SECRET_LENGTH}}})")
_BEARER = re.compile(r"(?i:bearer) +(\S+)")
# O hash contra o qual se compara quando o id não existe: a conta é a mesma de uma chave de segredo
# errado. Sorteado a cada processo, não é de ninguém.
_NOBODY = hashlib.sha256(secrets.token_bytes(SECRET_BYTES)).hexdigest()

NAME_HINT = (
    "de 3 a 60 caracteres: letras minúsculas sem acento, números e hífen, com ao menos uma letra "
    "ou um número"
)
NAME_INVALID = "Use " + NAME_HINT + "."
NAME_TAKEN = (
    "Já existe uma chave com este nome. O nome de uma chave não se repete nem se reaproveita, nem "
    "o de uma chave revogada: a auditoria cita a chave por ele."
)
ROLE_UNKNOWN = "Escolha o papel da chave: leitura ou total."
EXPIRES_IN_THE_PAST = "A validade tem de ser no futuro."
AUTH_MESSAGES = {
    "invalid_credentials": "Chave de API inválida ou ausente.",
    "key_expired": "A chave de API venceu.",
    "key_revoked": "A chave de API foi revogada.",
}

State = Literal["active", "expired", "revoked"]
# Do papel de menos poder para o de mais (a ordem de `API_KEY_ROLES`): `total` faz tudo o que `leitura`
# faz. A API pede o papel mínimo de cada operação e pergunta a `allows`.
_ROLE_RANK = {role: rank for rank, role in enumerate(API_KEY_ROLES)}


class KeyRefused(Exception):
    """A criação não vale: `field` é o campo com o problema (`name`, `role` ou `expires_at`) e
    `message`, o motivo, em português. Nada é gravado."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


class UnknownKey(LookupError):
    """Não há chave com este id."""


class AuthFailed(Exception):
    """A chave não vale. `code` é o que a API responde (`invalid_credentials`, `key_expired` ou
    `key_revoked`); `key_id` é o id público quando a chave chegou bem formada, para o log da
    aplicação (nunca o segredo). A mensagem não diz mais do que o `code`."""

    def __init__(self, code: str, key_id: str | None = None) -> None:
        super().__init__(AUTH_MESSAGES[code])
        self.code = code
        self.key_id = key_id


@dataclass(frozen=True)
class KeyInfo:
    """Uma chave como o admin e a API a veem. Não tem o segredo nem o hash."""

    key_id: str
    name: str
    role: str
    created_by: str
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    revoked_by: str | None
    last_used_at: datetime | None

    @property
    def actor(self) -> str:
        """O autor que a auditoria grava quando esta chave escreve."""
        return f"chave:{self.name}"

    def state(self, now: datetime) -> State:
        """Revogada vale mais que vencida: a revogação foi uma decisão de alguém."""
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at is not None and self.expires_at <= now:
            return "expired"
        return "active"


class AlreadyRevoked(Exception):
    """A chave já estava revogada: a primeira revogação segue valendo, e nada é gravado. `key` é a
    chave como está."""

    def __init__(self, key: KeyInfo) -> None:
        super().__init__(key.name)
        self.key = key


@dataclass(frozen=True)
class CreatedKey:
    """O resultado de `create`: a chave e a chave inteira (`key`), que só existe aqui."""

    info: KeyInfo
    key: str = field(repr=False)  # o segredo não vai a um repr, log de depuração nem traceback


def allows(role: str, required: str) -> bool:
    """Se uma chave de papel `role` pode o que pede o papel mínimo `required`. Um papel que o serviço
    não conhece não pode nada (negado por padrão); um `required` desconhecido é erro de quem chama."""
    if required not in _ROLE_RANK:
        raise ValueError(f"papel desconhecido: {required!r}")
    return role in _ROLE_RANK and _ROLE_RANK[role] >= _ROLE_RANK[required]


def format_key(key_id: str, secret: str) -> str:
    return f"{PREFIX}{key_id}_{secret}"


def digest(secret: str) -> str:
    """O SHA-256 do segredo, em hexadecimal: é o que se guarda."""
    return hashlib.sha256(secret.encode()).hexdigest()


def _info(row: ApiKey) -> KeyInfo:
    return KeyInfo(
        key_id=row.key_id,
        name=row.name,
        role=row.role,
        created_by=row.created_by,
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        revoked_by=row.revoked_by,
        last_used_at=row.last_used_at,
    )


def _audit(session: Session, actor: str, action: str, info: KeyInfo) -> None:
    """Nome, papel e validade; nunca o segredo nem o hash (o `KeyInfo` nem os tem)."""
    session.add(
        AuditLog(
            actor=actor,
            action=f"api_key.{action}",
            target_type="api_key",
            target_id=info.key_id,
            details={
                "name": info.name,
                "role": info.role,
                "expires_at": info.expires_at.astimezone(UTC).isoformat()
                if info.expires_at
                else None,
            },
        )
    )


def _name_taken(session: Session, name: str) -> bool:
    return session.scalar(sa.select(sa.exists().where(ApiKey.name == name))) is True


def create(
    session: Session,
    *,
    name: str,
    role: str,
    actor: str,
    expires_at: datetime | None = None,
    now: datetime | None = None,
) -> CreatedKey:
    """Cria a chave e devolve, junto, a chave inteira para mostrar uma vez. `expires_at` nulo é
    uma chave sem validade (o padrão do serviço; a página sugere 90 dias). Recusada, levanta
    `KeyRefused` e nada é gravado."""
    now = now or utcnow()
    if NAME_PATTERN.fullmatch(name) is None:
        raise KeyRefused("name", NAME_INVALID)
    if role not in API_KEY_ROLES:
        raise KeyRefused("role", ROLE_UNKNOWN)
    if expires_at is not None:
        if expires_at.tzinfo is None:
            raise ValueError("expires_at precisa de fuso")
        if expires_at <= now:
            raise KeyRefused("expires_at", EXPIRES_IN_THE_PAST)
    if _name_taken(session, name):
        raise KeyRefused("name", NAME_TAKEN)
    secret = secrets.token_urlsafe(SECRET_BYTES)
    for _ in range(KEY_ID_ATTEMPTS):
        row = ApiKey(
            key_id=secrets.token_hex(KEY_ID_BYTES),
            name=name,
            role=role,
            secret_hash=digest(secret),
            created_by=actor,
            created_at=now,
            expires_at=expires_at,
        )
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            if _name_taken(session, name):  # outro pedido criou o nome primeiro
                raise KeyRefused("name", NAME_TAKEN) from None
            continue  # o id público já existia: sorteia outro
        info = _info(row)
        _audit(session, actor, "create", info)
        return CreatedKey(info, format_key(row.key_id, secret))
    raise RuntimeError("não deu para sortear um id de chave livre")


def parse(authorization: str | None) -> tuple[str, str] | None:
    """O id e o segredo de `Bearer gp_<id>_<segredo>`, ou None se o cabeçalho não é isso."""
    bearer = _BEARER.fullmatch(authorization or "")
    key = _KEY.fullmatch(bearer.group(1)) if bearer else None
    return (key.group(1), key.group(2)) if key else None


def authenticate(
    session: Session, authorization: str | None, *, now: datetime | None = None
) -> KeyInfo:
    """Confere o cabeçalho `Authorization` e devolve a chave. Levanta `AuthFailed`:
    `invalid_credentials` para tudo o que não acertou o segredo; `key_revoked` e `key_expired` só
    para quem o acertou.

    A transação: `last_used_at` é gravado por um UPDATE na transação de quem chama, que precisa
    confirmá-la. Esse UPDATE segura o lock da linha da chave até o commit, e outra chamada da mesma
    chave que tente gravar o mesmo campo espera por ele. Por isso a dependência da API abre uma
    transação própria e curta só para isto (`with deps.sessions.begin() as session:`), confirmada
    antes de a operação rodar: nunca dentro da transação da operação, que pode demorar (renderizar
    um PDF, falar com o Baserow) e deixaria a chave travada enquanto isso."""
    now = now or utcnow()
    parsed = parse(authorization)
    if parsed is None:
        raise AuthFailed("invalid_credentials")
    key_id, secret = parsed
    row = session.scalar(sa.select(ApiKey).where(ApiKey.key_id == key_id))
    stored = row.secret_hash if row is not None else _NOBODY
    right = hmac.compare_digest(digest(secret).encode(), stored.encode())
    if row is None or not right:
        raise AuthFailed("invalid_credentials", key_id)
    info = _info(row)
    state = info.state(now)
    if state == "revoked":
        raise AuthFailed("key_revoked", key_id)
    if state == "expired":
        raise AuthFailed("key_expired", key_id)
    touched = session.execute(
        sa.update(ApiKey)
        .where(
            ApiKey.id == row.id,
            sa.or_(ApiKey.last_used_at.is_(None), ApiKey.last_used_at <= now - LAST_USED_EVERY),
        )
        .values(last_used_at=now)
        .execution_options(synchronize_session=False)
    )
    return replace(info, last_used_at=now) if touched.rowcount else info  # type: ignore[attr-defined]


def revoke(session: Session, key_id: str, *, actor: str, now: datetime | None = None) -> KeyInfo:
    """Revoga a chave, que deixa de valer na próxima chamada. Levanta `UnknownKey` se não existe e
    `AlreadyRevoked` se já estava revogada (a primeira revogação fica como está)."""
    now = now or utcnow()
    revoked = session.execute(
        sa.update(ApiKey)
        .where(ApiKey.key_id == key_id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=now, revoked_by=actor)
        .returning(ApiKey)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if revoked is None:
        row = session.scalar(sa.select(ApiKey).where(ApiKey.key_id == key_id))
        if row is None:
            raise UnknownKey(key_id)
        raise AlreadyRevoked(_info(row))
    info = _info(revoked)
    _audit(session, actor, "revoke", info)
    return info


def listing(session: Session) -> list[KeyInfo]:
    """Todas as chaves, as revogadas também, da mais nova para a mais antiga."""
    rows = session.scalars(sa.select(ApiKey).order_by(ApiKey.created_at.desc(), ApiKey.id))
    return [_info(row) for row in rows]
