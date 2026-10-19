"""O que o admin faz com a integração do Baserow, sem página nem API (`app.admin.actions.integrations`):
conectar o formulário a uma tabela, ler a tabela de agora e guardar o mapeamento.

A página e a API chamam as mesmas funções (ADR 0015, "A página e a API dividem os serviços"). Aqui o
Baserow é o falso em memória do `conftest` (sobre `httpx.MockTransport`), e o que se confere é o que
cada ação decide: o que recusa, o que lê antes de gravar, o que grava e o que audita.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa
from conftest import (
    BASEROW_TABLE,
    BASEROW_TOKEN,
    BASEROW_URL,
    FakeBaserow,
    baserow_field,
)
from cryptography.fernet import Fernet
from test_admin_destinations import stored_text
from test_admin_integrations import failed_sync
from test_integration_mapping import FULL_MAPPING
from test_integrations import SLUG, connect_form, sync_state

from app.admin import launching
from app.admin.actions import integrations as actions
from app.deps import Deps
from app.destinations import SecretCipher
from app.forms import drafts
from app.integrations import config
from app.integrations.mapping import InvalidMapping
from app.models import Artifact, AuditLog, Integration, IntegrationSync, utcnow

ACTOR = "chave:ana-teste"


@pytest.fixture
def baserow(deps: Deps) -> FakeBaserow:
    return FakeBaserow().install(deps)


def connect(deps: Deps, **changes: object) -> actions.Connected:
    options: dict[str, object] = {
        "base_url": BASEROW_URL,
        "table_id": BASEROW_TABLE,
        "token": BASEROW_TOKEN,
        "expected_version": 0,
        "actor": ACTOR,
        **changes,
    }
    return actions.connect(deps, SLUG, **options)  # type: ignore[arg-type]


def stored(deps: Deps) -> config.StoredIntegration | None:
    with deps.sessions() as session:
        return config.load(session, SLUG)


def audit_actions(deps: Deps) -> list[str]:
    with deps.sessions() as session:
        return list(session.scalars(sa.select(AuditLog.action).order_by(AuditLog.at)))


# --- os números do formulário -------------------------------------------------------------------


HUGE = "9" * 5000  # `int()` de tanto algarismo levanta ValueError (500, se ninguém o segura)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("7", 7), ("2147483647", 2**31 - 1), ("0", None), ("2147483648", None), ("-3", None)]
    + [("abc", None), ("", None), ("1.5", None), (" 7", None), ("٣", None)]
    + [("12345678901", None), (HUGE, None)],
    ids=lambda value: str(value)[:12],  # o id de um teste não pode ter 5000 algarismos
)
def test_the_table_number_is_a_positive_integer_that_fits_the_database(
    text: str, expected: int | None
) -> None:
    assert actions.table_number(text) == expected
    assert actions.field_number(text) == expected  # o id de um campo é lido pela mesma regra


@pytest.mark.parametrize(
    ("text", "expected"),
    [("3", 3), ("0", 0), ("2147483647", 2**31 - 1)]
    + [("", -1), ("x", -1), ("-1", -1), ("٣", -1), ("1.0", -1), (" 3", -1)]
    + [("00", -1), ("002", -1), ("03", -1)]  # a versão é um texto canônico: sem zero à esquerda
    + [("2147483648", -1), ("12345678901", -1), (HUGE, -1)],
    ids=lambda value: str(value)[:12],  # o id de um teste não pode ter 5000 algarismos
)
def test_a_version_that_is_not_a_canonical_number_never_matches(text: str, expected: int) -> None:
    assert actions.version_number(text) == expected


# --- conectar -----------------------------------------------------------------------------------


def test_connecting_reads_the_table_with_the_typed_token_and_stores_the_connection_audited(
    deps: Deps, baserow: FakeBaserow
) -> None:
    done = connect(deps)

    assert done.created and done.integration.version == 1
    assert (done.integration.base_url, done.integration.table_id) == (BASEROW_URL, BASEROW_TABLE)
    assert baserow.calls == [("GET", f"fields/table/{BASEROW_TABLE}/")]
    assert baserow.requests[0].headers["Authorization"] == f"Token {BASEROW_TOKEN}"
    with deps.sessions() as session:
        opened = config.stored_connection(session, deps.cipher, SLUG)
        (entry,) = session.scalars(sa.select(AuditLog))
    assert opened is not None and opened.token == BASEROW_TOKEN
    assert (entry.action, entry.actor) == ("integration.create", ACTOR)
    assert BASEROW_TOKEN not in stored_text(deps)  # nem no banco, nem na auditoria


def test_a_connection_that_does_not_serve_collects_every_reason_and_calls_nothing(
    deps: Deps, baserow: FakeBaserow
) -> None:
    with pytest.raises(actions.InvalidConnection) as refused:
        actions.connect(
            deps,
            SLUG,
            base_url="baserow.example.test",
            table_id=None,
            token="  ",
            expected_version=0,
            actor=ACTOR,
        )

    assert set(refused.value.errors) == {"base_url", "table_id", "token"}
    assert refused.value.errors["table_id"] == actions.TABLE_INVALID
    assert refused.value.errors["token"] == actions.TOKEN_REQUIRED
    assert "https://" in refused.value.errors["base_url"]
    assert baserow.calls == []  # só lê a tabela depois de conferir o resto
    assert stored(deps) is None


def test_a_form_without_a_published_version_cannot_be_connected_whatever_else_is_wrong(
    deps: Deps, baserow: FakeBaserow
) -> None:
    """O estado do formulário vem antes do corpo: `NotPublished` (`409 state_conflict` na API), e
    nenhum campo é conferido nem o Baserow lido."""
    drafts.create(deps, title="Em rascunho", slug="em-rascunho", actor="testes")

    with pytest.raises(actions.NotPublished, match="Publique o formulário antes"):
        actions.connect(
            deps,
            "em-rascunho",
            base_url="baserow.example.test",
            table_id=None,
            token="",
            expected_version=0,
            actor=ACTOR,
        )

    assert baserow.calls == []
    assert not [name for name in audit_actions(deps) if name.startswith("integration.")]


def test_the_table_number_must_be_inside_the_range_the_database_keeps(
    deps: Deps, baserow: FakeBaserow
) -> None:
    for number in (0, -1, 2**31, None):
        with pytest.raises(actions.InvalidConnection) as refused:
            connect(deps, table_id=number)
        assert list(refused.value.errors) == ["table_id"]


def test_a_table_the_token_cannot_read_saves_nothing_and_says_why(
    deps: Deps, baserow: FakeBaserow
) -> None:
    baserow.token = "outro-token-do-servidor"

    with pytest.raises(actions.TableNotRead) as refused:
        connect(deps)

    assert "Não deu para ler a tabela" in str(refused.value) and "HTTP 401" in str(refused.value)
    assert BASEROW_TOKEN not in str(refused.value)
    assert stored(deps) is None and audit_actions(deps) == []


def test_a_baserow_that_is_down_is_a_table_not_read_too(deps: Deps, baserow: FakeBaserow) -> None:
    baserow.fail("GET", 503)

    with pytest.raises(actions.TableNotRead, match="HTTP 503"):
        connect(deps)

    assert stored(deps) is None


def test_a_blank_token_keeps_the_stored_one_and_reads_the_table_with_it(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    version = stored(deps).version  # type: ignore[union-attr]
    baserow.calls.clear()
    baserow.requests.clear()

    done = connect(deps, token="", expected_version=version, table_id=BASEROW_TABLE)

    assert not done.created and done.integration.version == version + 1
    assert baserow.requests[0].headers["Authorization"] == f"Token {BASEROW_TOKEN}"
    with deps.sessions() as session:
        opened = config.stored_connection(session, deps.cipher, SLUG)
    assert opened is not None and opened.token == BASEROW_TOKEN


def test_a_blank_token_on_creation_is_required(deps: Deps, baserow: FakeBaserow) -> None:
    with pytest.raises(actions.InvalidConnection) as refused:
        connect(deps, token="")

    assert refused.value.errors == {"token": actions.TOKEN_REQUIRED}


def test_a_stored_token_that_does_not_open_asks_for_the_token_again(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    version = stored(deps).version  # type: ignore[union-attr]
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))
    baserow.calls.clear()

    with pytest.raises(actions.InvalidConnection) as refused:
        connect(deps, token="", expected_version=version)

    assert refused.value.errors == {"token": actions.TOKEN_UNREADABLE}
    assert baserow.calls == []


def test_without_the_encryption_key_the_token_cannot_be_kept(
    deps: Deps, baserow: FakeBaserow
) -> None:
    deps.cipher = None

    with pytest.raises(actions.InvalidConnection) as refused:
        connect(deps)

    assert list(refused.value.errors) == ["token"]
    assert "WEBHOOK_ENCRYPTION_KEY" in refused.value.errors["token"]
    assert stored(deps) is None


def test_moving_the_connection_to_another_table_drops_the_mapping_and_deactivates(
    deps: Deps,
) -> None:
    connect_form(deps)  # mapeada e ativa
    FakeBaserow(table_id=9).install(deps)
    version = stored(deps).version  # type: ignore[union-attr]

    done = connect(deps, token="", table_id=9, expected_version=version)

    assert (done.integration.entries, done.integration.active) == ((), False)
    assert done.integration.table_id == 9
    assert audit_actions(deps)[-1] == "integration.connect"


def test_a_connection_the_person_saw_in_an_older_version_is_a_conflict_and_changes_nothing(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    before = stored(deps)
    assert before is not None

    with pytest.raises(config.Conflict) as conflict:
        connect(deps, token="", expected_version=before.version - 1)
    with pytest.raises(config.Conflict):
        connect(deps)  # criar de novo, com a integração já criada

    assert conflict.value.current is not None and conflict.value.current.version == before.version
    assert stored(deps) == before


@pytest.mark.parametrize(
    "address",
    [
        "https://outro-baserow.example.test",  # outro host
        "https://baserow.example.test:8443",  # outra porta
        "http://baserow.example.test",  # o token iria sem https
        "https://sub.baserow.example.test",
    ],
)
def test_the_stored_token_never_goes_to_another_server_so_a_new_address_needs_a_new_token(
    deps: Deps, baserow: FakeBaserow, address: str
) -> None:
    connect_form(deps, active=False)
    before = stored(deps)
    assert before is not None
    baserow.calls.clear()

    with pytest.raises(actions.InvalidConnection) as refused:
        connect(deps, base_url=address, token="", expected_version=before.version)

    assert list(refused.value.errors) == ["token"]
    assert "exige colar o token de novo" in refused.value.errors["token"]
    assert baserow.calls == [] and stored(deps) == before  # nada saiu, nada mudou
    assert BASEROW_TOKEN not in refused.value.errors["token"]


def test_a_new_address_with_a_new_token_is_connected_and_the_token_goes_only_to_the_new_server(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    version = stored(deps).version  # type: ignore[union-attr]
    other = "https://outro-baserow.example.test"
    baserow.calls.clear()
    baserow.requests.clear()

    done = connect(deps, base_url=other, token=BASEROW_TOKEN, expected_version=version)

    assert done.integration.base_url == other
    assert [str(r.url.host) for r in baserow.requests] == ["outro-baserow.example.test"]


@pytest.mark.parametrize(
    "address",
    [
        BASEROW_URL + "/",  # a barra do fim não é outro servidor
        "https://BASEROW.example.test",  # o host não diferencia maiúsculas
        "https://baserow.example.test:443",  # a porta padrão do https
    ],
)
def test_the_same_server_written_another_way_keeps_the_stored_token(
    deps: Deps, baserow: FakeBaserow, address: str
) -> None:
    connect_form(deps, active=False)
    version = stored(deps).version  # type: ignore[union-attr]
    baserow.requests.clear()

    done = connect(deps, base_url=address, token="", expected_version=version)

    assert done.integration.version == version + 1
    assert baserow.requests[0].headers["Authorization"] == f"Token {BASEROW_TOKEN}"


def test_another_path_on_the_same_server_keeps_the_stored_token(
    deps: Deps, baserow: FakeBaserow
) -> None:
    """O servidor é o mesmo (esquema, host e porta): o token vai ao mesmo lugar de sempre."""
    connect_form(deps, active=False)
    version = stored(deps).version  # type: ignore[union-attr]
    baserow.requests.clear()

    with pytest.raises(actions.TableNotRead):  # o Baserow de mentira só atende o caminho de sempre
        connect(deps, base_url=BASEROW_URL + "/baserow", token="", expected_version=version)

    assert baserow.requests[0].headers["Authorization"] == f"Token {BASEROW_TOKEN}"
    assert baserow.requests[0].url.host == "baserow.example.test"


@pytest.mark.parametrize(
    "token", ["", BASEROW_TOKEN], ids=["blank", "typed"]
)  # ids fixos: o token é sorteado por processo
def test_changing_a_connection_that_does_not_exist_is_a_conflict_before_baserow_is_read(
    deps: Deps, baserow: FakeBaserow, token: str
) -> None:
    """Quem espera uma versão (`expected_version` diferente de 0) viu uma integração que não existe
    mais: é conflito, e não uma leitura da tabela com um token vazio que o Baserow recusaria."""
    with pytest.raises(config.Conflict) as conflict:
        connect(deps, token=token, expected_version=3)

    assert conflict.value.current is None
    assert baserow.calls == [] and stored(deps) is None and audit_actions(deps) == []


def test_the_token_is_not_in_the_log_nor_in_a_refusal(
    deps: Deps,
    baserow: FakeBaserow,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # O alembic do conftest (`fileConfig`) desliga os loggers que já existiam: religa o da ação, ou
    # a ausência do token no log não provaria nada.
    monkeypatch.setattr(actions.logger, "disabled", False)
    with caplog.at_level(logging.DEBUG):
        connect(deps)
        baserow.token = "outro-token-do-servidor"
        with pytest.raises(actions.TableNotRead) as refused:
            connect(deps, token="token-errado-do-teste", expected_version=1)

    assert BASEROW_TOKEN not in caplog.text and "token-errado-do-teste" not in caplog.text
    assert "token-errado-do-teste" not in str(refused.value)
    assert "integração baserow conectada" in caplog.text and ACTOR in caplog.text


# --- ler a tabela de agora ----------------------------------------------------------------------


def test_reading_the_fields_gives_the_table_as_baserow_describes_it_now(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)

    first = actions.read_fields(deps, SLUG)
    baserow.fields.append(baserow_field(120, "Novo campo"))
    second = actions.read_fields(deps, SLUG)

    assert [item.id for item in first] == list(range(101, 111))
    assert [item.id for item in second][-1] == 120  # lida a cada chamada, sem cache
    assert baserow.requests[0].headers["Authorization"] == f"Token {BASEROW_TOKEN}"


def test_reading_the_fields_of_a_form_without_integration_says_there_is_none(
    deps: Deps, baserow: FakeBaserow
) -> None:
    with pytest.raises(actions.IntegrationMissing, match="ainda não tem integração"):
        actions.read_fields(deps, SLUG)

    assert baserow.calls == []


def test_a_stored_token_that_does_not_open_is_said_without_calling_baserow(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))

    with pytest.raises(actions.TokenUnreadable, match="não abre com a WEBHOOK_ENCRYPTION_KEY"):
        actions.read_fields(deps, SLUG)

    assert baserow.calls == []


@pytest.mark.parametrize(("status", "message"), [(401, "HTTP 401"), (500, "HTTP 500")])
def test_a_table_baserow_does_not_read_is_a_read_failure_with_the_status(
    deps: Deps, baserow: FakeBaserow, status: int, message: str
) -> None:
    connect_form(deps, active=False)
    baserow.fail("GET", status)

    with pytest.raises(actions.ReadFailed, match="Não deu para ler a tabela") as failed:
        actions.read_fields(deps, SLUG)

    assert message in str(failed.value) and BASEROW_TOKEN not in str(failed.value)


# --- o mapeamento -------------------------------------------------------------------------------

CHOSEN = {entry.field_id: entry.source for entry in FULL_MAPPING}


def mapped(
    deps: Deps, chosen: dict[int, str] | None = None, **changes: object
) -> actions.MappingSaved:
    version = stored(deps).version  # type: ignore[union-attr]
    options: dict[str, object] = {
        "chosen": CHOSEN if chosen is None else chosen,
        "expected_version": version,
        "actor": ACTOR,
        **changes,
    }
    return actions.save_mapping(deps, SLUG, **options)  # type: ignore[arg-type]


def test_saving_the_mapping_stores_the_entries_from_the_live_table_and_audits_it(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect(deps)

    done = mapped(deps)

    assert [(e.field_id, e.name, e.type, e.source) for e in done.integration.entries] == [
        (item.field_id, item.name, item.type, item.source) for item in FULL_MAPPING
    ]
    assert done.integration.key_field_id == 104 and not done.integration.active
    assert done.report.ok and not done.was_active and not done.deactivated
    with deps.sessions() as session:
        entry = session.scalars(
            sa.select(AuditLog).where(AuditLog.action == "integration.mapping")
        ).one()
    assert (entry.actor, entry.details["fields"]) == (ACTOR, len(FULL_MAPPING))


def test_a_source_left_blank_or_for_a_field_the_table_does_not_have_is_not_mapped(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect(deps)

    done = mapped(deps, {**CHOSEN, 105: "  ", 999: "lead.name"})

    assert 105 not in {e.field_id for e in done.integration.entries}
    assert 999 not in {e.field_id for e in done.integration.entries}
    assert len(done.integration.entries) == len(FULL_MAPPING) - 1


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({104: ""}, "código do pedido"),
        ({108: "lead.name"}, "não recebe"),
        ({108: "answer:nao_existe"}, "fonte que o formulário não tem"),
        ({107: "lead.email"}, "não recebe"),
    ],
)
def test_a_mapping_that_does_not_serve_says_what_to_fix_and_saves_nothing(
    deps: Deps, baserow: FakeBaserow, change: dict[int, str], message: str
) -> None:
    connect(deps)

    with pytest.raises(InvalidMapping) as refused:
        mapped(deps, {**CHOSEN, **change})

    assert any(message in problem for problem in refused.value.problems)
    assert stored(deps).entries == ()  # type: ignore[union-attr]
    assert "integration.mapping" not in audit_actions(deps)


def test_a_mapping_with_pending_options_deactivates_an_active_integration(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps)  # mapeada e ativa
    baserow.fields[7] = baserow_field(108, "Equipe", "single_select", options=("Só eu",))

    done = mapped(deps)

    assert not done.report.ok and done.was_active and done.deactivated
    assert not done.integration.active
    with deps.sessions() as session:
        entry = session.scalars(
            sa.select(AuditLog).where(AuditLog.action == "integration.mapping")
        ).all()[-1]
    assert entry.details["deactivated"] is True


def test_a_clean_remap_keeps_an_active_integration_active(deps: Deps, baserow: FakeBaserow) -> None:
    connect_form(deps)

    done = mapped(deps, {key: source for key, source in CHOSEN.items() if key != 105})

    assert done.report.ok and done.was_active and not done.deactivated
    assert done.integration.active and len(done.integration.entries) == len(FULL_MAPPING) - 1


def test_a_form_without_a_published_version_cannot_be_mapped(
    deps: Deps, baserow: FakeBaserow
) -> None:
    drafts.create(deps, title="Em rascunho", slug="em-rascunho", actor="testes")

    with pytest.raises(actions.NotPublished, match="Publique o formulário antes"):
        actions.save_mapping(deps, "em-rascunho", chosen=CHOSEN, expected_version=1, actor=ACTOR)

    assert baserow.calls == []


@pytest.mark.parametrize("version", [0, 1, 3], ids=["creation", "first", "later"])
def test_mapping_an_integration_that_is_gone_is_a_conflict_before_baserow_is_read(
    deps: Deps, baserow: FakeBaserow, version: int
) -> None:
    """Como em `connect`: quem espera uma versão viu uma integração que já não existe (ou nunca existiu)
    é conflito, com a versão atual `None`, e o Baserow nem é chamado."""
    with pytest.raises(config.Conflict) as conflict:
        actions.save_mapping(deps, SLUG, chosen=CHOSEN, expected_version=version, actor=ACTOR)

    assert conflict.value.current is None and baserow.calls == []
    assert audit_actions(deps) == []


def test_a_table_that_cannot_be_read_saves_no_mapping(deps: Deps, baserow: FakeBaserow) -> None:
    connect(deps)
    baserow.fail("GET", 503, where="fields")

    with pytest.raises(actions.TableNotRead, match="HTTP 503"):
        mapped(deps)

    assert stored(deps).entries == ()  # type: ignore[union-attr]


def test_a_stale_version_is_a_conflict_and_the_mapping_stays_as_it_was(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect(deps)
    mapped(deps)
    before = stored(deps)

    with pytest.raises(config.Conflict) as conflict:
        mapped(deps, expected_version=1)

    assert conflict.value.current == before and stored(deps) == before


def test_a_stale_version_is_a_conflict_even_when_baserow_is_down_and_it_is_never_called(
    deps: Deps, baserow: FakeBaserow
) -> None:
    """A versão esperada se compara no mesmo `load` que dá a atual, antes de ler a tabela: quem viu uma
    versão velha recebe o conflito (e `409`, na API), e não o erro do Baserow que estiver fora do ar."""
    connect_form(deps, active=False)
    current = stored(deps)
    assert current is not None
    baserow.calls.clear()
    baserow.fail("GET", 503)  # se o Baserow fosse lido, seria `TableNotRead`

    with pytest.raises(config.Conflict) as on_mapping:
        mapped(deps, expected_version=current.version - 1)
    with pytest.raises(config.Conflict) as on_connect:
        connect(deps, token="", expected_version=current.version - 1)
    with pytest.raises(config.Conflict) as on_creation:
        connect(deps)  # criar o que já existe

    for conflict in (on_mapping, on_connect, on_creation):
        assert conflict.value.current == current
    assert (
        baserow.calls == [] and len(baserow.failures) == 1
    )  # a falha esperava um GET que não veio
    assert stored(deps) == current


# --- a integração de um formulário, lida --------------------------------------------------------


def test_the_detail_of_a_form_that_does_not_exist_is_none(deps: Deps, baserow: FakeBaserow) -> None:
    assert actions.read_detail(deps, "nao-existe") is None
    assert baserow.calls == []


def test_a_form_without_an_integration_has_a_detail_that_offers_to_connect(
    deps: Deps, baserow: FakeBaserow
) -> None:
    detail = actions.read_detail(deps, SLUG)

    assert detail is not None and detail.slug == SLUG and detail.title
    assert detail.published and detail.stored is None and not detail.token_unreadable
    assert (detail.fields, detail.report, detail.read_error) == (None, None, None)
    assert (detail.counts.pending, detail.counts.failed, detail.failures) == (0, 0, [])
    assert baserow.calls == []  # sem conexão, nada a ler


def test_a_form_without_a_published_version_says_so_and_reads_no_table(
    deps: Deps, baserow: FakeBaserow
) -> None:
    drafts.create(deps, title="Em rascunho", slug="em-rascunho", actor="testes")

    detail = actions.read_detail(deps, "em-rascunho")

    assert detail is not None and not detail.published and detail.stored is None
    assert baserow.calls == []


def test_a_connected_form_reads_the_live_table_once_and_suggests_the_sources(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect(deps)
    baserow.calls.clear()

    detail = actions.read_detail(deps, SLUG)

    assert detail is not None and detail.stored is not None and detail.fields is not None
    assert baserow.calls == [("GET", f"fields/table/{BASEROW_TABLE}/")]
    by_id = {item.id: item for item in detail.fields}
    assert (by_id[101].selected, by_id[101].suggested) == ("lead.name", True)
    assert by_id[110].unsupported == "read_only"
    assert detail.report is None  # nada mapeado: o relatório só existe depois de salvar
    assert "answer:tamanho_equipe" in detail.sources


def test_a_saved_mapping_comes_with_its_report_against_the_live_table(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect(deps)
    mapped(deps)

    clean = actions.read_detail(deps, SLUG)
    baserow.fields[7] = baserow_field(108, "Equipe", "single_select", options=("Só eu",))
    pending = actions.read_detail(deps, SLUG)

    assert clean is not None and clean.report is not None and clean.report.ok
    assert pending is not None and pending.report is not None and not pending.report.ok
    assert pending.report.unmatched == 3
    assert clean.fields is not None and {item.selected for item in clean.fields} >= {"lead.name"}


def test_the_choices_just_sent_come_back_as_the_selection(deps: Deps, baserow: FakeBaserow) -> None:
    connect(deps)
    mapped(deps)

    detail = actions.read_detail(deps, SLUG, typed={101: "lead.email"})

    assert detail is not None and detail.fields is not None
    assert {item.id: item.selected for item in detail.fields}[101] == "lead.email"
    assert {item.id: item.selected for item in detail.fields}[102] == ""


def test_a_token_that_does_not_open_is_in_the_detail_and_baserow_is_not_called(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect(deps)
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))
    baserow.calls.clear()

    detail = actions.read_detail(deps, SLUG)

    assert detail is not None and detail.token_unreadable
    assert detail.read_error == actions.TOKEN_UNREADABLE
    assert (detail.fields, detail.report) == (None, None)
    assert detail.stored is not None  # a conexão aparece, sem o token
    assert baserow.calls == []


def test_a_baserow_that_stops_answering_leaves_the_detail_up_with_the_reason(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect(deps)
    mapped(deps)
    baserow.fail("GET", 500, where="fields")

    detail = actions.read_detail(deps, SLUG)

    assert detail is not None and detail.stored is not None
    assert detail.read_error is not None and "HTTP 500" in detail.read_error
    assert (detail.fields, detail.report) == (None, None)
    assert len(detail.stored.entries) == len(FULL_MAPPING)


def test_the_detail_counts_the_syncs_and_lists_the_ones_that_failed(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    public_id, sync_id = failed_sync(deps)

    detail = actions.read_detail(deps, SLUG)

    assert detail is not None
    assert (detail.counts.pending, detail.counts.failed, detail.counts.succeeded) == (0, 1, 0)
    (failure,) = detail.failures
    assert (failure.id, failure.job_public_id, failure.status) == (sync_id, public_id, "failed")


# --- ativar, desativar, remover, testar e reenviar ----------------------------------------------


def version_of(deps: Deps) -> int:
    found = stored(deps)
    assert found is not None
    return found.version


def activate(deps: Deps, **changes: object) -> config.StoredIntegration:
    options: dict[str, object] = {"expected_version": version_of(deps), "actor": ACTOR, **changes}
    return actions.activate(deps, SLUG, **options)  # type: ignore[arg-type]


def clone_failed_syncs(deps: Deps, sync_id: uuid.UUID, extra: int) -> list[uuid.UUID]:
    """`extra` sincronizações falhadas a mais da integração de `sync_id`, cada uma com o PDF (artefato)
    próprio, porque (integração, artefato) é único. Cada uma é mais nova que a anterior: o reenvio as
    pega da mais antiga para a mais nova."""
    with deps.sessions.begin() as session:
        sync = session.get_one(IntegrationSync, sync_id)
        artifact = session.get_one(Artifact, sync.artifact_id)
        sync.created_at = utcnow() - timedelta(hours=1)  # no passado, para o reconciliador enxergar
        created = []
        for number in range(1, extra + 1):
            pdf = Artifact(
                id=uuid.uuid4(),
                job_id=artifact.job_id,
                plan_id=artifact.plan_id,
                kind=artifact.kind,
                version=artifact.version + number,
                storage_key=f"{artifact.storage_key}-{number}",
                sha256=artifact.sha256,
                byte_size=artifact.byte_size,
                renderer=artifact.renderer,
            )
            session.add(pdf)
            session.flush()
            clone = IntegrationSync(
                id=uuid.uuid4(),
                integration_id=sync.integration_id,
                job_id=sync.job_id,
                artifact_id=pdf.id,
                status="failed",
                attempts=4,
                last_error=sync.last_error,
                created_at=sync.created_at + timedelta(seconds=number),
            )
            session.add(clone)
            created.append(clone.id)
        return created


def test_activating_checks_the_report_against_the_live_table_once_and_turns_the_integration_on(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    before = version_of(deps)
    baserow.calls.clear()

    done = activate(deps)

    assert done.active and done.version == before + 1
    assert baserow.calls == [
        ("GET", f"fields/table/{BASEROW_TABLE}/")
    ]  # a tabela de agora, uma vez
    with deps.sessions() as session:
        entry = session.scalars(
            sa.select(AuditLog).where(AuditLog.action == "integration.activate")
        ).one()
    assert entry.actor == ACTOR and BASEROW_TOKEN not in str(entry.details)


def test_activating_with_a_report_that_has_pending_options_says_which_and_changes_nothing(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    baserow.fields[7] = baserow_field(108, "Equipe", "single_select", options=("Só eu",))
    del baserow.fields[4]  # o campo 105 sumiu: um problema que não é de opção
    before = stored(deps)

    with pytest.raises(actions.ReportPending) as pending:
        activate(deps)

    assert not pending.value.nothing_mapped and not pending.value.report.ok
    reasons = pending.value.reasons()
    assert "O campo «Respostas» não existe mais na tabela." in reasons
    assert any("3 opções do formulário não têm par no Baserow" in reason for reason in reasons)
    assert stored(deps) == before and "integration.activate" not in audit_actions(deps)


def test_activating_with_nothing_mapped_says_so(deps: Deps, baserow: FakeBaserow) -> None:
    connect(deps)  # conectada, nada mapeado

    with pytest.raises(actions.ReportPending) as pending:
        activate(deps)

    assert pending.value.nothing_mapped
    assert pending.value.reasons() == [
        "Nenhum campo está mapeado: escolha o que preenche cada campo da tabela."
    ]
    assert not stored(deps).active  # type: ignore[union-attr]


def test_activating_a_form_without_a_published_version_is_not_published_and_reads_no_table(
    deps: Deps, baserow: FakeBaserow
) -> None:
    drafts.create(deps, title="Em rascunho", slug="em-rascunho", actor="testes")

    with pytest.raises(actions.NotPublished):
        actions.activate(deps, "em-rascunho", expected_version=1, actor=ACTOR)

    assert baserow.calls == []


def test_activating_a_form_without_an_integration_says_there_is_none(
    deps: Deps, baserow: FakeBaserow
) -> None:
    with pytest.raises(actions.IntegrationMissing):
        actions.activate(deps, SLUG, expected_version=1, actor=ACTOR)

    assert baserow.calls == [] and audit_actions(deps) == []


def test_activating_on_a_stale_version_is_a_conflict_before_baserow_is_read(
    deps: Deps, baserow: FakeBaserow
) -> None:
    """Como `connect` e `save_mapping`: a versão esperada se compara antes de ler a tabela, então quem viu
    uma versão velha recebe o conflito, e não o erro do Baserow que estiver fora do ar."""
    connect_form(deps, active=False)
    current = stored(deps)
    baserow.calls.clear()
    baserow.fail("GET", 503)  # se o Baserow fosse lido, seria `TableNotRead`

    with pytest.raises(config.Conflict) as conflict:
        activate(deps, expected_version=version_of(deps) - 1)

    assert conflict.value.current == current and stored(deps) == current
    assert baserow.calls == [] and len(baserow.failures) == 1


def test_activating_when_the_integration_changes_while_baserow_is_read_is_a_conflict(
    deps: Deps, baserow: FakeBaserow
) -> None:
    """A palavra final é do `set_active`, sob o lock: outra pessoa mudou a integração entre a leitura
    da tabela e a gravação."""
    connect_form(deps, active=False)
    seen = version_of(deps)

    def someone_else(request: object) -> None:
        baserow.before = None
        with deps.sessions.begin() as session:
            config.set_active(
                session, form_slug=SLUG, active=False, actor="outra", expected_version=seen
            )

    baserow.before = someone_else

    with pytest.raises(config.Conflict):
        activate(deps, expected_version=seen)

    found = stored(deps)
    assert found is not None and not found.active and found.updated_by == "outra"
    assert "integration.activate" not in audit_actions(deps)


def test_activating_with_a_token_that_does_not_open_is_unreadable_and_baserow_is_not_called(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))
    baserow.calls.clear()

    with pytest.raises(actions.TokenUnreadable):
        activate(deps)

    assert baserow.calls == [] and not stored(deps).active  # type: ignore[union-attr]


def test_activating_when_baserow_does_not_read_the_table_changes_nothing(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    baserow.fail("GET", 503, where="fields")

    with pytest.raises(actions.TableNotRead, match="HTTP 503"):
        activate(deps)

    assert not stored(deps).active  # type: ignore[union-attr]
    assert "integration.activate" not in audit_actions(deps)


def test_deactivating_turns_it_off_audited_and_never_calls_baserow(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps)  # mapeada e ativa
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))  # nem o token precisa abrir
    baserow.calls.clear()

    done = actions.deactivate(deps, SLUG, expected_version=version_of(deps), actor=ACTOR)

    assert not done.active and baserow.calls == []
    with deps.sessions() as session:
        entry = session.scalars(
            sa.select(AuditLog).where(AuditLog.action == "integration.deactivate")
        ).one()
    assert entry.actor == ACTOR


def test_deactivating_on_a_stale_version_or_without_an_integration_is_a_conflict(
    deps: Deps, baserow: FakeBaserow
) -> None:
    with pytest.raises(config.Conflict) as gone:
        actions.deactivate(deps, SLUG, expected_version=1, actor=ACTOR)
    connect_form(deps)
    before = stored(deps)
    with pytest.raises(config.Conflict) as stale:
        actions.deactivate(deps, SLUG, expected_version=version_of(deps) - 1, actor=ACTOR)

    assert gone.value.current is None and stale.value.current == before
    assert stored(deps) == before


def test_removing_deletes_the_integration_the_token_and_the_syncs_and_says_what_went_away(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    failed_sync(deps)
    baserow.calls.clear()
    before = stored(deps)

    done = actions.remove(deps, SLUG, expected_version=version_of(deps), actor=ACTOR)

    assert done.integration == before
    counts = done.sync_counts
    assert (counts.pending, counts.failed, counts.succeeded) == (0, 1, 0)
    assert stored(deps) is None and baserow.calls == []  # o app não apaga nada no Baserow
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(IntegrationSync)) == 0
        assert session.scalar(sa.select(sa.func.count()).select_from(Integration)) == 0
        entry = session.scalars(
            sa.select(AuditLog).where(AuditLog.action == "integration.remove")
        ).one()
    assert entry.actor == ACTOR


def test_removing_on_a_stale_version_or_without_an_integration_is_a_conflict_and_removes_nothing(
    deps: Deps, baserow: FakeBaserow
) -> None:
    with pytest.raises(config.Conflict) as gone:
        actions.remove(deps, SLUG, expected_version=1, actor=ACTOR)
    connect_form(deps)
    before = stored(deps)
    with pytest.raises(config.Conflict) as stale:
        actions.remove(deps, SLUG, expected_version=version_of(deps) - 1, actor=ACTOR)

    assert gone.value.current is None and stale.value.current == before
    assert stored(deps) == before and "integration.remove" not in audit_actions(deps)


def test_the_test_writes_the_sample_row_and_the_second_one_updates_the_same_row(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)

    first = actions.send_test(deps, SLUG, actor=ACTOR)
    second = actions.send_test(deps, SLUG, actor=ACTOR)

    assert (first.ok, first.outcome, first.public_id) == (True, "created", f"teste-{SLUG}")
    assert (second.ok, second.outcome, second.row_id) == (True, "updated", first.row_id)
    assert len(baserow.rows) == 1
    with deps.sessions() as session:
        entries = session.scalars(
            sa.select(AuditLog).where(AuditLog.action == "integration.test").order_by(AuditLog.at)
        ).all()
    assert [(e.actor, e.details["outcome"]) for e in entries] == [
        (ACTOR, "created"),
        (ACTOR, "updated"),
    ]


def test_a_test_baserow_refuses_is_a_result_that_did_not_pass_and_leaves_no_row(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    baserow.fail("GET", 401, where="fields")

    result = actions.send_test(deps, SLUG, actor=ACTOR)

    assert not result.ok and result.error is not None and "HTTP 401" in result.error
    assert baserow.rows == {}


def test_a_test_of_a_form_without_an_integration_says_there_is_none(
    deps: Deps, baserow: FakeBaserow
) -> None:
    with pytest.raises(actions.IntegrationMissing):
        actions.send_test(deps, SLUG, actor=ACTOR)

    assert baserow.calls == [] and audit_actions(deps) == []


def test_a_test_with_a_token_that_does_not_open_sends_nothing_and_is_unreadable(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))
    baserow.calls.clear()

    with pytest.raises(actions.TokenUnreadable):
        actions.send_test(deps, SLUG, actor=ACTOR)

    assert baserow.calls == [] and "integration.test" not in audit_actions(deps)


def test_resending_the_failed_syncs_puts_them_back_in_the_queue_audited_and_gives_their_ids(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    _, sync_id = failed_sync(deps)

    done = actions.resend_failed(deps, SLUG, actor=ACTOR)

    assert done.sync_ids == [sync_id] and done.still_failed == 0
    assert sync_state(deps, sync_id).status == "pending"
    with deps.sessions() as session:
        entry = session.scalars(
            sa.select(AuditLog).where(AuditLog.action == "sync.resend_failed")
        ).one()
    assert (entry.actor, entry.details) == (ACTOR, {"count": 1})


def test_resending_with_nothing_failed_gives_no_ids_and_audits_nothing(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps)

    done = actions.resend_failed(deps, SLUG, actor=ACTOR)

    assert (done.sync_ids, done.still_failed) == ([], 0)
    assert "sync.resend_failed" not in audit_actions(deps)


def test_resending_a_form_without_an_integration_says_there_is_none(
    deps: Deps, baserow: FakeBaserow
) -> None:
    with pytest.raises(actions.IntegrationMissing):
        actions.resend_failed(deps, SLUG, actor=ACTOR)

    assert audit_actions(deps) == []


def test_resending_takes_at_most_the_limit_and_says_how_many_are_still_failed(
    deps: Deps, baserow: FakeBaserow, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect_form(deps)
    _, first = failed_sync(deps)
    clone_failed_syncs(deps, first, 4)  # cinco falhadas ao todo
    monkeypatch.setattr(launching, "RESEND_FAILED_LIMIT", 3)

    done = actions.resend_failed(deps, SLUG, actor=ACTOR)

    assert len(done.sync_ids) == 3 and done.still_failed == 2
    assert first in done.sync_ids  # as mais antigas primeiro
    with deps.sessions() as session:
        entry = session.scalars(
            sa.select(AuditLog).where(AuditLog.action == "sync.resend_failed")
        ).one()
    assert entry.details == {"count": 3}
