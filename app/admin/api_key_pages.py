"""A tela das chaves de API no admin (`/admin/api-keys`): a lista, o formulário de criar e a chave
nova, que aparece uma vez.

O que a tela mostra vem pronto daqui, com os rótulos em português e as datas no horário de
Brasília; o template (`admin/api_keys.html`) só dispõe. A regra de negócio é de
`app.admin.api.keys`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.admin.api import keys
from app.admin.api.reference import REFERENCE_PATH
from app.admin.views import Badge, Moment, moment

KEYS_URL = "/admin/api-keys"
# A validade sugerida: a chave não vence por padrão, mas a tela já vem com 90 dias, que a operadora
# pode esvaziar. Cobre quem esquece a chave num arquivo, sem obrigar quem precisa de uma longa.
DEFAULT_VALIDITY_DAYS = 90
MAX_VALIDITY_DAYS = 3650  # dez anos: mais que isso é erro de digitação, e estoura a data

ROLE_LABELS = {"leitura": "Leitura", "total": "Total"}
ROLE_HELP = {
    "leitura": "só consulta, e vê configuração, contagens e estados, nunca dado de lead",
    "total": "faz tudo o que o admin logado faz, inclusive criar outras chaves",
}
STATE_BADGES = {
    "active": Badge("Ativa", "check", "ok"),
    "expired": Badge("Vencida", "clock", "warn"),
    "revoked": Badge("Revogada", "x", "err"),
}

VALIDITY_INVALID = (
    f"Informe um número inteiro de dias, de 1 a {MAX_VALIDITY_DAYS}, "
    "ou deixe em branco para uma chave sem validade."
)
CONFIRM_REVOKE = "Confirme que entende que a chave deixa de valer agora."
KEY_NOT_FOUND = "Chave não encontrada. Confira o endereço e tente de novo."
REVOKED = "Chave «{name}» revogada. Ela deixou de valer agora."
ALREADY_REVOKED = "A chave «{name}» já estava revogada, por {by}, em {when}. Nada mudou."


def expires_at(text: str, now: datetime) -> datetime | None:
    """A validade que a operadora pediu, em dias a partir de agora; em branco, sem validade.
    Levanta `KeyRefused` no campo `validity_days` se não for um número de dias válido."""
    text = text.strip()
    if not text:
        return None
    if re.fullmatch(r"[0-9]{1,5}", text) is None or not 1 <= int(text) <= MAX_VALIDITY_DAYS:
        raise keys.KeyRefused("validity_days", VALIDITY_INVALID)
    return now + timedelta(days=int(text))


@dataclass(frozen=True)
class KeyRow:
    """Uma chave na lista. Nunca o segredo: ele só existe na resposta da criação."""

    key_id: str
    name: str
    role: str
    role_label: str
    state: Badge
    revocable: bool
    created_by: str
    created: Moment | None
    expires: Moment | None
    last_used: Moment | None
    revoked: Moment | None
    revoked_by: str | None
    revoke_action: str


def row_of(info: keys.KeyInfo, now: datetime) -> KeyRow:
    state = info.state(now)
    return KeyRow(
        key_id=info.key_id,
        name=info.name,
        role=info.role,
        role_label=ROLE_LABELS[info.role],
        state=STATE_BADGES[state],
        revocable=state == "active",
        created_by=info.created_by,
        created=moment(info.created_at),
        expires=moment(info.expires_at),
        last_used=moment(info.last_used_at),
        revoked=moment(info.revoked_at),
        revoked_by=info.revoked_by,
        revoke_action=f"{KEYS_URL}/{info.key_id}/revoke",
    )


@dataclass(frozen=True)
class NewKey:
    """A chave que acabou de ser criada, com o segredo: aparece só na resposta da criação."""

    key_id: str  # o id público: a tela do MCP escolhe a chave nova por ele
    name: str
    role_label: str
    key: str = field(repr=False)  # o segredo não vai a um repr
    expires: Moment | None


def new_key(created: keys.CreatedKey) -> NewKey:
    return NewKey(
        key_id=created.info.key_id,
        name=created.info.name,
        role_label=ROLE_LABELS[created.info.role],
        key=created.key,
        expires=moment(created.info.expires_at),
    )


@dataclass(frozen=True)
class RoleOption:
    value: str
    label: str
    help: str


@dataclass(frozen=True)
class KeysPage:
    """A lista das chaves e o formulário de criar, com o que a operadora digitou e o problema de
    cada campo (`name`, `role`, `validity_days`), se a criação foi recusada. `revoke_errors` diz,
    por chave, por que a revogação não valeu."""

    rows: list[KeyRow]
    name: str
    role: str
    validity_days: str
    errors: Mapping[str, str] = field(default_factory=dict)
    revoke_errors: Mapping[str, str] = field(default_factory=dict)
    new_key: NewKey | None = None
    roles: tuple[RoleOption, ...] = tuple(
        RoleOption(value, ROLE_LABELS[value], ROLE_HELP[value]) for value in ROLE_LABELS
    )
    action: str = KEYS_URL
    reference_url: str = REFERENCE_PATH  # a referência da API (Scalar), que descreve cada operação
    name_hint: str = keys.NAME_HINT
    name_max_length: int = 60
    default_validity_days: int = DEFAULT_VALIDITY_DAYS
    max_validity_days: int = MAX_VALIDITY_DAYS


def keys_page(
    infos: Sequence[keys.KeyInfo],
    now: datetime,
    *,
    name: str = "",
    role: str = "leitura",
    validity_days: str = str(DEFAULT_VALIDITY_DAYS),
    errors: Mapping[str, str] | None = None,
    revoke_errors: Mapping[str, str] | None = None,
    new_key: NewKey | None = None,
) -> KeysPage:
    return KeysPage(
        rows=[row_of(info, now) for info in infos],
        name=name,
        role=role,
        validity_days=validity_days,
        errors=errors or {},
        revoke_errors=revoke_errors or {},
        new_key=new_key,
    )
