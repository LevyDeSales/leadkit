"""As chaves de API do admin: o serviço (criar, conferir um `Authorization: Bearer`, revogar, listar) e a
página delas no painel (`/admin/api-keys`), com a sessão, o CSRF e a auditoria do resto do admin.

As chaves destes testes são sorteadas na execução, nunca literais: o GitGuardian do PR acusa valor com
cara de segredo, e nada com essa cara entra no repositório."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import itertools
import logging
import re
import secrets
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from test_admin import ORIGIN, FakeClock, csrf_token, login
from test_admin_destinations import ADMIN_ENV

from app.admin import api_key_pages as pages
from app.admin.api import keys
from app.api.main import create_app
from app.deps import Deps
from app.models import ApiKey, AuditLog
from app.settings import Settings

KEYS = "/admin/api-keys"
ACTOR = "operadora"
KEY_SHAPE = re.compile(r"gp_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")
T0 = datetime(2027, 1, 15, 12, 0, tzinfo=UTC)


def make(
    deps: Deps,
    name: str = "backup-noturno",
    role: str = "leitura",
    *,
    expires_at: datetime | None = None,
    now: datetime = T0,
) -> keys.CreatedKey:
    with deps.sessions.begin() as session:
        return keys.create(
            session, name=name, role=role, actor=ACTOR, expires_at=expires_at, now=now
        )


def check(deps: Deps, key: str | None, *, now: datetime = T0) -> keys.KeyInfo:
    """Confere como a API vai conferir: o cabeçalho `Authorization: Bearer <chave>`."""
    header = None if key is None else f"Bearer {key}"
    with deps.sessions.begin() as session:
        return keys.authenticate(session, header, now=now)


def refusal(deps: Deps, key: str | None, *, now: datetime = T0) -> keys.AuthFailed:
    with pytest.raises(keys.AuthFailed) as refused:
        check(deps, key, now=now)
    return refused.value


def wrong_secret(key: str) -> str:
    """A mesma chave com outro segredo: o id acha a linha, o segredo não confere."""
    return keys.format_key(key.split("_")[1], secrets.token_urlsafe(32))


def unknown_key() -> str:
    return keys.format_key(secrets.token_hex(4), secrets.token_urlsafe(32))


def row_of(deps: Deps, name: str = "backup-noturno") -> ApiKey:
    with deps.sessions() as session:
        return session.scalars(sa.select(ApiKey).where(ApiKey.name == name)).one()


def audited(deps: Deps, action: str) -> list[AuditLog]:
    with deps.sessions() as session:
        found = session.scalars(sa.select(AuditLog).where(AuditLog.action == action))
        return list(found)


def everything_stored(deps: Deps) -> str:
    """Tudo o que o banco guarda sobre chaves e auditoria, num texto só, para procurar o que não
    pode estar lá."""
    with deps.sessions() as session:
        tables = ("api_keys", "audit_log")
        return "\n".join(
            str(row)
            for table in tables
            for row in session.execute(sa.text(f"SELECT * FROM {table}"))
        )


# --- o formato e o que é gravado ----------------------------------------------------------------


def test_a_key_is_gp_then_a_public_id_then_32_random_bytes_in_base64_without_padding(
    deps: Deps,
) -> None:
    created = make(deps)

    assert KEY_SHAPE.fullmatch(created.key)
    _, key_id, secret = created.key.split("_", 2)
    assert key_id == created.info.key_id and re.fullmatch(r"[0-9a-f]{8}", key_id)
    assert "=" not in secret
    assert len(base64.urlsafe_b64decode(secret + "=")) == 32


def test_every_key_is_drawn_again(deps: Deps) -> None:
    first, second = make(deps, "chave-um"), make(deps, "chave-dois")

    assert first.key != second.key
    assert first.info.key_id != second.info.key_id


def test_only_the_sha256_of_the_secret_is_stored(deps: Deps) -> None:
    created = make(deps)
    secret = created.key.split("_", 2)[2]

    assert row_of(deps).secret_hash == hashlib.sha256(secret.encode()).hexdigest()
    stored = everything_stored(deps)
    assert secret not in stored and created.key not in stored


def test_the_audit_and_the_listing_never_carry_the_secret_nor_the_hash(deps: Deps) -> None:
    created = make(deps, expires_at=T0 + timedelta(days=90))
    with deps.sessions.begin() as session:
        keys.revoke(session, created.info.key_id, actor=ACTOR, now=T0)
    secret = created.key.split("_", 2)[2]
    digest = row_of(deps).secret_hash

    with deps.sessions() as session:
        listed = repr(keys.listing(session))
        audit = repr([entry.details for entry in session.scalars(sa.select(AuditLog))])
    for shown in (listed, audit):
        assert secret not in shown and digest not in shown
    assert not {"secret_hash", "key"} & {field.name for field in dataclasses.fields(keys.KeyInfo)}


# --- conferir a chave ---------------------------------------------------------------------------


def test_the_right_key_identifies_who_is_calling(deps: Deps) -> None:
    created = make(deps, "painel-de-vendas", "total")

    found = check(deps, created.key)

    assert (found.name, found.role) == ("painel-de-vendas", "total")
    assert found.key_id == created.info.key_id
    assert found.actor == "chave:painel-de-vendas"


def test_a_wrong_secret_and_an_unknown_key_get_the_same_answer(deps: Deps) -> None:
    created = make(deps)

    wrong = refusal(deps, wrong_secret(created.key))
    unknown = refusal(deps, unknown_key())

    assert (wrong.code, str(wrong)) == (unknown.code, str(unknown))
    assert wrong.code == "invalid_credentials"


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Basic {basic}",
        "Bearer não-é-uma-chave",
        "Bearer gp_",
        "Bearer gp__",
        "Token {key}",
        "Bearer {key} sobrando",
        "Bearer {upper}",
        "Bearer {other_prefix}",
    ],
)
def test_a_header_that_is_not_a_well_formed_bearer_key_is_invalid_credentials(
    deps: Deps, header: str | None
) -> None:
    created = make(deps)
    if header is not None:
        header = header.format(
            key=created.key,
            upper=created.key.upper(),
            other_prefix=created.key.replace("gp_", "xx_", 1),
            basic=base64.b64encode(
                f"{secrets.token_hex(4)}:{secrets.token_hex(8)}".encode()
            ).decode(),
        )

    with deps.sessions.begin() as session, pytest.raises(keys.AuthFailed) as refused:
        keys.authenticate(session, header, now=T0)

    assert refused.value.code == "invalid_credentials"


def test_the_scheme_is_case_insensitive_but_the_key_is_not(deps: Deps) -> None:
    created = make(deps)

    with deps.sessions.begin() as session:
        found = keys.authenticate(session, f"bearer {created.key}", now=T0)

    assert found.name == "backup-noturno"


def test_only_who_has_the_secret_learns_that_the_key_expired(deps: Deps) -> None:
    created = make(deps, expires_at=T0 + timedelta(days=90))
    after = T0 + timedelta(days=90)

    assert check(deps, created.key, now=after - timedelta(seconds=1)).name == "backup-noturno"
    assert refusal(deps, created.key, now=after).code == "key_expired"
    assert refusal(deps, wrong_secret(created.key), now=after).code == "invalid_credentials"


def test_only_who_has_the_secret_learns_that_the_key_was_revoked(deps: Deps) -> None:
    created = make(deps)
    with deps.sessions.begin() as session:
        keys.revoke(session, created.info.key_id, actor=ACTOR, now=T0)

    assert refusal(deps, created.key).code == "key_revoked"
    assert refusal(deps, wrong_secret(created.key)).code == "invalid_credentials"


def test_a_key_that_is_both_revoked_and_expired_says_revoked(deps: Deps) -> None:
    created = make(deps, expires_at=T0 + timedelta(days=1))
    with deps.sessions.begin() as session:
        keys.revoke(session, created.info.key_id, actor=ACTOR, now=T0)

    assert refusal(deps, created.key, now=T0 + timedelta(days=2)).code == "key_revoked"


def test_a_key_without_a_validity_never_expires(deps: Deps) -> None:
    created = make(deps)

    assert check(deps, created.key, now=T0 + timedelta(days=36500)).name == "backup-noturno"


def test_revoking_counts_on_the_next_call_with_no_cache_in_between(deps: Deps) -> None:
    created = make(deps)
    assert check(deps, created.key).name == "backup-noturno"

    with deps.sessions.begin() as session:
        keys.revoke(session, created.info.key_id, actor=ACTOR, now=T0)

    assert refusal(deps, created.key).code == "key_revoked"


def test_the_validity_is_read_from_the_database_on_every_call(deps: Deps) -> None:
    created = make(deps, expires_at=T0 + timedelta(days=90))
    assert check(deps, created.key).name == "backup-noturno"

    with deps.sessions.begin() as session:  # alguém encurta a validade direto no banco
        session.execute(sa.update(ApiKey).values(expires_at=T0))

    assert refusal(deps, created.key).code == "key_expired"


def test_last_used_is_written_at_most_once_a_minute(deps: Deps) -> None:
    created = make(deps)
    assert row_of(deps).last_used_at is None

    check(deps, created.key, now=T0)
    assert row_of(deps).last_used_at == T0
    check(deps, created.key, now=T0 + timedelta(seconds=59))
    assert row_of(deps).last_used_at == T0
    check(deps, created.key, now=T0 + timedelta(seconds=61))
    assert row_of(deps).last_used_at == T0 + timedelta(seconds=61)
    check(deps, created.key, now=T0 + timedelta(seconds=90))
    assert row_of(deps).last_used_at == T0 + timedelta(seconds=61)


def test_a_refused_call_does_not_count_as_a_use(deps: Deps) -> None:
    created = make(deps)

    refusal(deps, wrong_secret(created.key))
    assert row_of(deps).last_used_at is None
    with deps.sessions.begin() as session:
        keys.revoke(session, created.info.key_id, actor=ACTOR, now=T0)
    refusal(deps, created.key)

    assert row_of(deps).last_used_at is None


# --- criar --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["abc", "a-b", "123", "a" * 60, "painel-2027", "--a", "9--", "-x-"]
)
def test_names_of_3_to_60_letters_digits_and_hyphens_are_accepted(deps: Deps, name: str) -> None:
    assert make(deps, name).info.name == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "ab",
        "a" * 61,
        "Painel",
        "painel_vendas",
        "painel vendas",
        "pâinel",
        "painel.vendas",
        " ",
        "---",  # só hífen: sem uma letra ou um dígito, não é nome
        "-" * 60,
        "painel\n",  # o fim de linha que um `$` deixaria passar
    ],
)
def test_other_names_are_refused_and_nothing_is_stored(deps: Deps, name: str) -> None:
    with pytest.raises(keys.KeyRefused) as refused:
        make(deps, name)

    assert refused.value.field == "name"
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(ApiKey)) == 0
    assert audited(deps, "api_key.create") == []


def test_a_name_is_never_used_twice(deps: Deps) -> None:
    make(deps)

    with pytest.raises(keys.KeyRefused) as refused:
        make(deps)

    assert refused.value.field == "name"
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(ApiKey)) == 1


def test_the_name_of_a_revoked_key_is_not_reused_either(deps: Deps) -> None:
    created = make(deps)
    with deps.sessions.begin() as session:
        keys.revoke(session, created.info.key_id, actor=ACTOR, now=T0)

    with pytest.raises(keys.KeyRefused) as refused:
        make(deps)

    assert refused.value.field == "name"
    assert "revogada" in refused.value.message


def test_a_role_other_than_leitura_or_total_is_refused(deps: Deps) -> None:
    with pytest.raises(keys.KeyRefused) as refused:
        make(deps, role="administrador")

    assert refused.value.field == "role"


@pytest.mark.parametrize("role", ["leitura", "total"])
def test_both_roles_are_accepted(deps: Deps, role: str) -> None:
    assert make(deps, role=role).info.role == role


def test_a_validity_in_the_past_is_refused(deps: Deps) -> None:
    for expires_at in (T0, T0 - timedelta(days=1)):
        with pytest.raises(keys.KeyRefused) as refused:
            make(deps, expires_at=expires_at)
        assert refused.value.field == "expires_at"


def test_a_key_has_no_validity_unless_one_is_given(deps: Deps) -> None:
    assert make(deps).info.expires_at is None
    assert make(deps, "com-validade", expires_at=T0 + timedelta(days=90)).info.expires_at == (
        T0 + timedelta(days=90)
    )


def test_creating_records_who_and_when(deps: Deps) -> None:
    created = make(deps)

    row = row_of(deps)
    assert (row.created_by, row.created_at) == (ACTOR, T0)
    assert (row.revoked_at, row.revoked_by, row.last_used_at) == (None, None, None)
    assert created.info.created_by == ACTOR


def test_creating_is_audited_with_the_name_the_role_and_the_validity(deps: Deps) -> None:
    created = make(deps, role="total", expires_at=T0 + timedelta(days=90))

    (entry,) = audited(deps, "api_key.create")
    assert entry.actor == ACTOR
    assert (entry.target_type, entry.target_id) == ("api_key", created.info.key_id)
    assert entry.details == {
        "name": "backup-noturno",
        "role": "total",
        "expires_at": (T0 + timedelta(days=90)).isoformat(),
    }


def test_a_key_without_validity_is_audited_with_a_null_validity(deps: Deps) -> None:
    make(deps)

    (entry,) = audited(deps, "api_key.create")
    assert entry.details["expires_at"] is None


# --- revogar e listar ---------------------------------------------------------------------------


def test_revoking_records_who_and_when_and_is_audited(deps: Deps) -> None:
    created = make(deps, role="total")

    with deps.sessions.begin() as session:
        revoked = keys.revoke(session, created.info.key_id, actor="outra-pessoa", now=T0)

    row = row_of(deps)
    assert (row.revoked_at, row.revoked_by) == (T0, "outra-pessoa")
    assert revoked.revoked_by == "outra-pessoa"
    (entry,) = audited(deps, "api_key.revoke")
    assert (entry.actor, entry.target_id) == ("outra-pessoa", created.info.key_id)
    assert entry.details == {"name": "backup-noturno", "role": "total", "expires_at": None}


def test_the_validity_is_audited_in_utc_on_the_creation_and_on_the_revocation(deps: Deps) -> None:
    created = make(deps, expires_at=T0 + timedelta(days=90))
    with deps.sessions.begin() as session:
        keys.revoke(session, created.info.key_id, actor=ACTOR, now=T0)

    (on_create,) = audited(deps, "api_key.create")
    (on_revoke,) = audited(deps, "api_key.revoke")
    assert on_create.details["expires_at"] == "2027-04-15T12:00:00+00:00"
    assert on_revoke.details == on_create.details


def test_revoking_twice_keeps_the_first_revocation_and_audits_only_once(deps: Deps) -> None:
    created = make(deps)
    with deps.sessions.begin() as session:
        keys.revoke(session, created.info.key_id, actor=ACTOR, now=T0)

    with pytest.raises(keys.AlreadyRevoked), deps.sessions.begin() as session:
        keys.revoke(session, created.info.key_id, actor="outra-pessoa", now=T0 + timedelta(hours=1))

    row = row_of(deps)
    assert (row.revoked_at, row.revoked_by) == (T0, ACTOR)
    assert len(audited(deps, "api_key.revoke")) == 1


def test_revoking_an_unknown_key_changes_nothing(deps: Deps) -> None:
    make(deps)

    with pytest.raises(keys.UnknownKey), deps.sessions.begin() as session:
        keys.revoke(session, secrets.token_hex(4), actor=ACTOR, now=T0)

    assert row_of(deps).revoked_at is None
    assert audited(deps, "api_key.revoke") == []


def test_revoking_one_key_does_not_touch_the_others(deps: Deps) -> None:
    keep, drop = make(deps, "fica"), make(deps, "sai")

    with deps.sessions.begin() as session:
        keys.revoke(session, drop.info.key_id, actor=ACTOR, now=T0)

    assert check(deps, keep.key).name == "fica"
    assert refusal(deps, drop.key).code == "key_revoked"


def test_the_listing_has_every_key_newest_first_revoked_ones_included(deps: Deps) -> None:
    old = make(deps, "primeira", now=T0)
    make(deps, "segunda", now=T0 + timedelta(hours=1))
    make(deps, "terceira", now=T0 + timedelta(hours=2))
    with deps.sessions.begin() as session:
        keys.revoke(session, old.info.key_id, actor=ACTOR, now=T0 + timedelta(hours=3))

    with deps.sessions() as session:
        listed = keys.listing(session)

    assert [entry.name for entry in listed] == ["terceira", "segunda", "primeira"]
    assert [entry.revoked_by for entry in listed] == [None, None, ACTOR]


def test_a_key_is_active_expired_or_revoked_as_of_a_moment(deps: Deps) -> None:
    created = make(deps, expires_at=T0 + timedelta(days=1))

    assert created.info.state(T0) == "active"
    assert created.info.state(T0 + timedelta(days=1)) == "expired"
    with deps.sessions.begin() as session:
        revoked = keys.revoke(session, created.info.key_id, actor=ACTOR, now=T0)
    assert revoked.state(T0) == "revoked"


# --- a chave fora do repr, a comparação, o sorteio do id, a corrida e os papéis ---------------------


def test_the_whole_key_is_kept_out_of_every_repr(deps: Deps) -> None:
    created = make(deps)
    secret = created.key.split("_", 2)[2]
    new = pages.new_key(created)

    shown = [
        repr(created),
        str(created),
        repr(new),
        repr(pages.keys_page([created.info], T0, new_key=new)),
    ]

    for text in shown:
        assert created.key not in text and secret not in text
    assert (
        created.key.startswith("gp_") and new.key == created.key
    )  # o campo existe, só não aparece


class SpyHmac:
    """O `hmac` do serviço, com um `compare_digest` que anota o que compara e delega ao real."""

    def __init__(self) -> None:
        self.calls: list[tuple[bytes, bytes]] = []

    def compare_digest(self, left: bytes, right: bytes) -> bool:
        self.calls.append((left, right))
        return hmac.compare_digest(left, right)


def test_the_hash_is_compared_in_constant_time_for_an_existing_id_and_for_an_unknown_one(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = make(deps)
    stored = row_of(deps).secret_hash.encode()
    spy = SpyHmac()
    monkeypatch.setattr(keys, "hmac", spy)

    check(deps, created.key)  # o id existe e o segredo está certo
    refusal(deps, wrong_secret(created.key))  # o id existe e o segredo está errado
    refusal(deps, unknown_key())  # o id não existe

    assert len(spy.calls) == 3
    right, wrong, unknown = spy.calls
    assert right == (stored, stored)
    assert wrong[1] == stored and wrong[0] != stored
    assert unknown[0] != unknown[1] and unknown[1] != stored  # contra um hash que não é de ninguém
    assert all(len(left) == len(other) == 64 for left, other in spy.calls)  # a mesma conta nos três


class FixedIds:
    """O `secrets` do serviço, com um `token_hex` (o id público) que devolve os ids dados, na ordem."""

    def __init__(self, ids: Iterator[str]) -> None:
        self.ids = ids
        self.drawn = 0

    def token_hex(self, nbytes: int) -> str:
        self.drawn += 1
        return next(self.ids)

    def __getattr__(self, name: str) -> object:
        return getattr(secrets, name)


def test_a_public_id_that_already_exists_is_drawn_again(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = make(deps, "primeira")
    ids = FixedIds(iter([first.info.key_id, "0f0f0f0f"]))
    monkeypatch.setattr(keys, "secrets", ids)

    second = make(deps, "segunda")

    assert ids.drawn == 2 and second.info.key_id == "0f0f0f0f"
    assert check(deps, second.key).name == "segunda"
    assert check(deps, first.key).name == "primeira"
    assert len(audited(deps, "api_key.create")) == 2


def test_when_every_draw_collides_creating_gives_up_and_the_session_stays_usable(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = make(deps, "primeira")
    ids = FixedIds(itertools.repeat(first.info.key_id))
    monkeypatch.setattr(keys, "secrets", ids)

    with deps.sessions.begin() as session:
        with pytest.raises(RuntimeError):
            keys.create(session, name="segunda", role="leitura", actor=ACTOR, now=T0)
        assert [entry.name for entry in keys.listing(session)] == ["primeira"]

    assert ids.drawn == keys.KEY_ID_ATTEMPTS
    assert count_keys(deps) == 1 and len(audited(deps, "api_key.create")) == 1


def test_two_creations_of_the_same_name_at_once_leave_one_key_and_refuse_the_other(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As duas olham se o nome está livre antes de qualquer uma gravar: quem decide é o banco.
    both_looked = threading.Barrier(2, timeout=20)
    asked: set[int] = set()
    questions: list[str] = []
    looked = keys._name_taken

    def name_taken_once_both_looked(session: object, name: str) -> bool:
        found = looked(session, name)  # type: ignore[arg-type]
        questions.append(name)
        if threading.get_ident() not in asked:  # só a primeira pergunta de cada pedido
            asked.add(threading.get_ident())
            both_looked.wait()
        return found

    monkeypatch.setattr(keys, "_name_taken", name_taken_once_both_looked)

    def create_one(actor: str) -> keys.CreatedKey | keys.KeyRefused:
        try:
            with deps.sessions.begin() as session:
                return keys.create(session, name="mesma-chave", role="leitura", actor=actor, now=T0)
        except keys.KeyRefused as refused:
            return refused

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(create_one, ["um", "dois"]))

    created = [outcome for outcome in outcomes if isinstance(outcome, keys.CreatedKey)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, keys.KeyRefused)]
    assert len(created) == 1 and len(refused) == 1
    assert (
        len(questions) == 3
    )  # as duas perguntas livres, e a do perdedor depois de o banco recusar
    assert (refused[0].field, refused[0].message) == ("name", keys.NAME_TAKEN)
    assert count_keys(deps) == 1 and len(audited(deps, "api_key.create")) == 1


@pytest.mark.parametrize(
    ("role", "required", "allowed"),
    [
        ("leitura", "leitura", True),
        ("leitura", "total", False),
        ("total", "leitura", True),
        ("total", "total", True),
    ],
)
def test_a_role_can_do_what_a_role_no_stronger_than_it_asks(
    role: str, required: str, allowed: bool
) -> None:
    assert keys.allows(role, required) is allowed


def test_an_unknown_role_can_do_nothing_and_an_unknown_requirement_is_an_error() -> None:
    assert keys.allows("administrador", "leitura") is False
    assert keys.allows("", "leitura") is False
    with pytest.raises(ValueError):
        keys.allows("total", "administrador")


def test_the_role_of_the_authenticated_key_feeds_allows(deps: Deps) -> None:
    reader, writer = make(deps, "so-leio", "leitura"), make(deps, "faco-tudo", "total")

    assert keys.allows(check(deps, reader.key).role, "total") is False
    assert keys.allows(check(deps, writer.key).role, "total") is True


# --- a página -----------------------------------------------------------------------------------

OTHER_ORIGIN = {"Origin": "https://outro.example.test"}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(deps: Deps, clock: FakeClock) -> TestClient:
    deps.settings = Settings(_env_file=None, **ADMIN_ENV)  # type: ignore[call-arg]
    current = TestClient(create_app(trigger=None, clock=clock), follow_redirects=False)
    assert login(current).status_code == 303
    return current


def moment_of(clock: FakeClock) -> datetime:
    return datetime.fromtimestamp(clock(), UTC)


def post_key(
    client: TestClient,
    name: str = "backup-noturno",
    role: str = "leitura",
    validity: str = "90",
    *,
    token: bool = True,
    headers: dict[str, str] | None = ORIGIN,
) -> httpx.Response:
    data = {"name": name, "role": role, "validity_days": validity}
    if token:
        data["csrf_token"] = csrf_token(client)
    return client.post(KEYS, data=data, headers=headers)


def shown_key(html: str) -> str:
    """A chave que a página mostra uma vez, como a operadora a copiaria."""
    match = re.search(r'id="chave-nova"[^>]*>([^<]+)<', html)
    assert match is not None, html
    return match.group(1)


def post_revoke(
    client: TestClient,
    key_id: str,
    *,
    confirm: bool = True,
    token: bool = True,
    headers: dict[str, str] | None = ORIGIN,
) -> httpx.Response:
    data = {"confirm": "on"} if confirm else {}
    if token:
        data["csrf_token"] = csrf_token(client)
    return client.post(f"{KEYS}/{key_id}/revoke", data=data, headers=headers)


def count_keys(deps: Deps) -> int:
    with deps.sessions() as session:
        return session.scalar(sa.select(sa.func.count()).select_from(ApiKey)) or 0


def test_the_page_needs_a_session(client: TestClient, deps: Deps) -> None:
    created = make(deps)
    anonymous = TestClient(client.app, follow_redirects=False)

    for response in (
        anonymous.get(KEYS),
        anonymous.post(KEYS, data={"name": "abc", "role": "total"}, headers=ORIGIN),
        anonymous.post(
            f"{KEYS}/{created.info.key_id}/revoke", data={"confirm": "on"}, headers=ORIGIN
        ),
    ):
        assert response.status_code == 303
        assert response.headers["location"].startswith("/admin/login")
    assert row_of(deps).revoked_at is None
    assert count_keys(deps) == 1


def test_a_post_without_the_csrf_token_or_from_another_origin_is_refused(
    client: TestClient, deps: Deps
) -> None:
    created = make(deps)
    key_id = created.info.key_id

    assert post_key(client, "sem-token", token=False).status_code == 403
    assert post_key(client, "outra-origem", headers=OTHER_ORIGIN).status_code == 403
    assert post_revoke(client, key_id, token=False).status_code == 403
    assert post_revoke(client, key_id, headers=OTHER_ORIGIN).status_code == 403

    assert row_of(deps).revoked_at is None
    assert count_keys(deps) == 1
    assert audited(deps, "api_key.revoke") == []


def test_the_menu_links_to_the_page_and_marks_it_when_it_is_open(client: TestClient) -> None:
    other = client.get("/admin/assets").text
    own = client.get(KEYS).text

    assert '<a href="/admin/api-keys">' in other
    assert '<a href="/admin/api-keys" aria-current="page">' in own


def test_the_form_comes_with_90_days_the_operator_can_empty_and_the_leitura_role(
    client: TestClient,
) -> None:
    page = client.get(KEYS).text

    validity = re.search(r'<input[^>]*name="validity_days"[^>]*>', page)
    assert validity is not None
    assert 'value="90"' in validity.group(0) and "required" not in validity.group(0)
    assert '<option value="leitura" selected>' in page
    assert '<option value="total">' in page
    assert 'name="name"' in page and "Nenhuma chave criada ainda" in page


def test_the_key_appears_once_in_the_response_of_the_creation_and_never_again(
    client: TestClient, deps: Deps, clock: FakeClock
) -> None:
    created = post_key(client, role="total")

    assert created.status_code == 200
    assert created.headers["Cache-Control"] == "no-store"
    key = shown_key(created.text)
    assert KEY_SHAPE.fullmatch(key)
    assert check(deps, key, now=moment_of(clock)).role == "total"
    secret = key.split("_", 2)[2]
    for url in (KEYS, "/admin/assets", "/admin"):
        later = client.get(url)
        assert key not in later.text and secret not in later.text, url
    assert "backup-noturno" in client.get(KEYS).text


def test_the_creation_does_not_redirect_and_repeating_it_shows_no_key(
    client: TestClient, deps: Deps
) -> None:
    first = post_key(client)
    second = post_key(client)  # o F5 do navegador, ou um segundo clique sem o HTMX

    assert first.status_code == 200 and second.status_code == 422
    assert shown_key(first.text)
    assert 'id="chave-nova"' not in second.text and "gp_" not in second.text
    assert count_keys(deps) == 1


def test_the_listing_shows_name_role_validity_and_state_but_never_a_secret(
    client: TestClient, deps: Deps
) -> None:
    key = shown_key(post_key(client, "painel-de-vendas", "total", "30").text)
    secret = key.split("_", 2)[2]
    digest = row_of(deps, "painel-de-vendas").secret_hash

    page = client.get(KEYS).text

    assert "painel-de-vendas" in page and "Total" in page and "Ativa" in page
    assert "Vence em" in page and "14/02/2027 05:00" in page  # 30 dias depois do relógio falso
    assert "Nunca usada" in page
    assert f"id {key.split('_')[1]}" in page  # o id público ajuda a achar a chave
    assert "gp_" not in page and secret not in page and digest not in page


def test_the_box_of_the_new_key_says_who_it_is_and_until_when_it_is_valid(
    client: TestClient,
) -> None:
    page = post_key(client, "painel-de-vendas", "total", "90").text

    assert "Chave «painel-de-vendas» criada" in page
    assert "total" in page and "15/04/2027 05:00" in page
    assert "Authorization: Bearer" in page


def test_an_empty_validity_creates_a_key_that_never_expires(client: TestClient, deps: Deps) -> None:
    key = shown_key(post_key(client, validity="").text)

    assert row_of(deps).expires_at is None
    assert check(deps, key, now=T0 + timedelta(days=36500)).name == "backup-noturno"
    assert "Sem validade" in client.get(KEYS).text


def test_a_validity_in_days_counts_from_now(
    client: TestClient, deps: Deps, clock: FakeClock
) -> None:
    post_key(client, validity=" 90 ")

    assert row_of(deps).expires_at == moment_of(clock) + timedelta(days=90)


@pytest.mark.parametrize("validity", ["0", "-5", "abc", "1.5", "3651", "99999999999", "²"])
def test_a_validity_that_is_not_a_number_of_days_from_1_to_3650_is_refused(
    client: TestClient, deps: Deps, validity: str
) -> None:
    response = post_key(client, validity=validity)

    assert response.status_code == 422
    assert 'aria-invalid="true"' in response.text and "número inteiro de dias" in response.text
    assert "gp_" not in response.text
    assert count_keys(deps) == 0


def test_the_last_day_of_the_range_is_accepted(
    client: TestClient, deps: Deps, clock: FakeClock
) -> None:
    assert post_key(client, validity="3650").status_code == 200
    assert row_of(deps).expires_at == moment_of(clock) + timedelta(days=3650)


def test_a_refused_creation_returns_the_form_with_what_was_typed_and_the_problem(
    client: TestClient, deps: Deps
) -> None:
    response = post_key(client, "Nome Inválido", "total", "45")

    assert response.status_code == 422
    assert 'value="Nome Inválido"' in response.text
    assert re.search(r'<input[^>]*name="validity_days"[^>]*value="45"', response.text)
    assert '<option value="total" selected>' in response.text
    assert 'aria-invalid="true"' in response.text and "minúsculas" in response.text
    assert count_keys(deps) == 0 and audited(deps, "api_key.create") == []


def test_a_role_that_does_not_exist_is_refused(client: TestClient, deps: Deps) -> None:
    response = post_key(client, role="administrador")

    assert response.status_code == 422
    assert "Escolha o papel" in response.text and 'aria-invalid="true"' in response.text
    assert count_keys(deps) == 0


def test_a_name_in_use_is_refused_even_when_that_key_was_revoked(
    client: TestClient, deps: Deps
) -> None:
    post_key(client)
    post_revoke(client, row_of(deps).key_id)

    response = post_key(client)

    assert response.status_code == 422
    assert "revogada" in response.text and "gp_" not in response.text
    assert count_keys(deps) == 1


def test_under_htmx_a_refusal_comes_back_as_200_so_the_page_swaps_in_the_reason(
    client: TestClient,
) -> None:
    response = client.post(
        KEYS,
        data={
            "name": "X",
            "role": "total",
            "validity_days": "90",
            "csrf_token": csrf_token(client),
        },
        headers={**ORIGIN, "HX-Request": "true"},
    )

    assert response.status_code == 200 and 'aria-invalid="true"' in response.text


def test_revoking_needs_the_confirmation_and_then_counts_at_once(
    client: TestClient, deps: Deps, clock: FakeClock
) -> None:
    key = shown_key(post_key(client).text)
    key_id = row_of(deps).key_id

    refused = post_revoke(client, key_id, confirm=False)
    assert refused.status_code == 422 and "Confirme que entende" in refused.text
    assert check(deps, key, now=moment_of(clock)).name == "backup-noturno"

    done = post_revoke(client, key_id)
    assert done.status_code == 303 and done.headers["location"] == KEYS
    assert "Chave «backup-noturno» revogada" in client.get(KEYS).text
    assert refusal(deps, key, now=moment_of(clock)).code == "key_revoked"


def test_a_revoked_key_shows_who_revoked_it_and_offers_no_new_revocation(
    client: TestClient, deps: Deps
) -> None:
    post_key(client, "ativa-uma")
    post_key(client, "revogada-uma")
    revoked_id = row_of(deps, "revogada-uma").key_id
    post_revoke(client, revoked_id)

    page = client.get(KEYS).text

    assert "Revogada" in page and f"Revogada por {ACTOR}" in page
    assert f"/admin/api-keys/{revoked_id}/revoke" not in page
    assert f"/admin/api-keys/{row_of(deps, 'ativa-uma').key_id}/revoke" in page


def test_an_expired_key_is_labeled_as_such_and_cannot_be_revoked_again(
    client: TestClient, deps: Deps, clock: FakeClock
) -> None:
    post_key(client, validity="1")
    clock.advance(2 * 86400)
    assert login(client).status_code == 303  # a sessão caiu com o tempo

    page = client.get(KEYS).text

    assert "Vencida" in page and "Venceu em" in page
    assert f"/admin/api-keys/{row_of(deps).key_id}/revoke" not in page


def test_revoking_a_key_that_was_already_revoked_says_so_and_changes_nothing(
    client: TestClient, deps: Deps
) -> None:
    post_key(client)
    key_id = row_of(deps).key_id
    post_revoke(client, key_id)
    first = row_of(deps)

    again = post_revoke(client, key_id)

    assert again.status_code == 303
    assert "já estava revogada" in client.get(KEYS).text
    assert row_of(deps).revoked_at == first.revoked_at
    assert len(audited(deps, "api_key.revoke")) == 1


def test_revoking_a_key_that_does_not_exist_is_a_404(client: TestClient) -> None:
    for key_id in ("00000000", "nao-e-um-id"):
        response = post_revoke(client, key_id)

        assert response.status_code == 404
        assert "Chave não encontrada" in response.text


def test_the_page_audits_the_creation_and_the_revocation_in_the_name_of_the_operator(
    client: TestClient, deps: Deps
) -> None:
    post_key(client, "painel-de-vendas", "total", "90")
    stored = row_of(deps, "painel-de-vendas")
    post_revoke(client, stored.key_id)

    (created,) = audited(deps, "api_key.create")
    (revoked,) = audited(deps, "api_key.revoke")
    assert (created.actor, revoked.actor) == (ACTOR, ACTOR)
    assert (created.target_id, revoked.target_id) == (stored.key_id, stored.key_id)
    assert created.details["name"] == "painel-de-vendas" and created.details["role"] == "total"
    assert stored.expires_at is not None
    assert datetime.fromisoformat(created.details["expires_at"]) == stored.expires_at
    assert stored.created_by == ACTOR


def test_no_secret_reaches_the_logs_the_database_or_the_audit(
    client: TestClient,
    deps: Deps,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("app.admin.api_key_routes", "app.admin.api.keys"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)
    caplog.set_level(logging.DEBUG)

    key = shown_key(post_key(client, role="total").text)
    secret = key.split("_", 2)[2]
    client.get(KEYS)
    post_revoke(client, row_of(deps).key_id)

    assert "admin: chave de API criada" in caplog.text  # os logs existem, só sem o segredo
    assert "admin: chave de API revogada" in caplog.text
    assert key not in caplog.text and secret not in caplog.text
    stored = everything_stored(deps)
    assert key not in stored and secret not in stored


def test_every_response_of_the_page_is_marked_not_to_be_cached(
    client: TestClient, deps: Deps
) -> None:
    post_key(client)
    key_id = row_of(deps).key_id

    for response in (
        client.get(KEYS),
        post_key(client, "outra-chave"),
        post_key(client, "x"),
        post_revoke(client, key_id),
        post_revoke(client, "00000000"),
    ):
        assert response.headers["Cache-Control"] == "no-store"


def test_the_api_is_a_sub_application_mounted_next_to_the_page(client: TestClient) -> None:
    """A página das chaves segue no app principal (e no `/openapi.json` dele); as operações da API
    moram na sub-aplicação montada em `/admin/api/v1`, com o OpenAPI próprio."""
    paths = client.get("/openapi.json").json()["paths"]  # fora de produção o app o expõe

    assert not [path for path in paths if path.startswith("/admin/api/")]
    assert set(paths[KEYS]) == {"get", "post"}
    assert set(paths[f"{KEYS}/{{key_id}}/revoke"]) == {"post"}
    # as operações das chaves moram lá (as fases de recurso acrescentam as delas ao lado)
    in_the_api = set(client.app.state.admin_api.openapi()["paths"])  # type: ignore[attr-defined]
    assert {"/me", "/api-keys", "/api-keys/{key_id}/revoke"} <= in_the_api
    # Montada: a chamada sem chave é recusada pela API, no formato dela, e não pelo login do admin.
    refused = client.get("/admin/api/v1/me")
    assert refused.status_code == 401 and refused.json()["code"] == "invalid_credentials"
