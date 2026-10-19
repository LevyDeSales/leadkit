"""A consulta da auditoria (`app.admin.queries.audit`): a que a API de admin usa (`list_audit_log`), sem
rótulo e com os `details` das chaves permitidas.

A auditoria é escrita por vários serviços, cada um com os `details` dele, e nenhum garante o que o
próximo vai pôr ali. A consulta é a barreira: só passa a chave que está em `PUBLIC_DETAIL_KEYS`, com um
valor simples, e o que não passa sai sem aviso. A lista filtra pelo **nome** da chave, e não pelo
conteúdo: o valor sob uma chave permitida passa como foi escrito. Estes testes prendem a lista (nada com
nome de segredo, de hash ou de texto livre de pessoa), cobram que toda chave que o código escreve como
literal seja pública ou retida de propósito, e o valor aceito; a leitura pela API está em
`test_admin_api_audit.py`.
"""

from __future__ import annotations

import ast
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.admin.queries import audit as queries
from app.deps import Deps
from app.models import AuditLog

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def row(n: int, *, at: datetime = T0, **fields: Any) -> AuditLog:
    return AuditLog(
        id=uuid.UUID(int=n),
        at=at,
        actor=fields.pop("actor", "operadora"),
        action=fields.pop("action", "form.publish"),
        target_type=fields.pop("target_type", "form"),
        target_id=fields.pop("target_id", None),
        details=fields.pop("details", {}),
    )


def entries(
    deps: Deps, filters: queries.AuditFilter | None = None, **options: Any
) -> tuple[list[queries.AuditEntry], bool]:
    with deps.sessions() as session:
        return queries.entries_after(
            session, filters or queries.AuditFilter(), **{"limit": 50, **options}
        )


# --- os `details` -------------------------------------------------------------------------------


# As chaves públicas que têm cara de sensível e foram revisadas: o nome engana, o valor não é.
REVIEWED = {
    "token_changed": "um booleano: o token do Baserow mudou nesta conexão (o token nunca vai ao banco em claro)",
    "consent_version": "a versão do texto de consentimento que a publicação conferiu, e não a resposta da lead",
}


def test_the_public_detail_keys_leave_out_secrets_hashes_and_free_text() -> None:
    sensitive = re.compile(
        r"secret|hash|token|password|senha|authorization|api_key|cipher|reason|motivo|email|phone|"
        r"answers|plan|consent|score"
    )
    suspicious = {key for key in queries.PUBLIC_DETAIL_KEYS if sensitive.search(key)}

    # Uma chave pública com nome de segredo, de hash ou de texto livre exige estar em `REVIEWED`,
    # com o porquê, no mesmo PR: a revisão vê quem a pôs. Nenhuma outra tem cara de uma.
    assert suspicious == set(REVIEWED)
    assert "reason" not in queries.PUBLIC_DETAIL_KEYS  # o motivo livre de um reprocessamento
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", key) for key in queries.PUBLIC_DETAIL_KEYS)


# As chaves que os serviços gravam e a leitura não deixa passar, de propósito:
# - `reason`: o motivo que a operadora digita ao reprocessar um pedido, e que pode citar a lead (a
#   retenção o apaga junto com o pedido);
# - `error`: o texto de um erro de fora (o do Baserow na linha de teste da integração, ou o nome da
#   exceção no evento de teste de um destino). O projeto trata o texto de erro de fora como só do
#   `total` (o último erro de uma sincronização só sai no `lead_data.notes`, e `failure_code_of`
#   entrega à `leitura` só o código), e uma chave permitida pelo nome deixaria passar, sem revisão, o
#   `error=str(exc)` de uma ação futura sobre um pedido de verdade.
WITHHELD = frozenset({"reason", "error"})
APP = Path(__file__).resolve().parents[1] / "app"
# As variáveis em que os serviços montam os `details` antes de gravá-los (`details = {...}`,
# `details["images"] = ...`, `confirmed = {"http_confirmed": True}`).
BUILDERS = frozenset({"details", "confirmed"})


def _string_keys(node: ast.AST | None) -> set[str]:
    """As chaves-texto de um dicionário literal, e as dos dois lados de um `a if c else b`."""
    if isinstance(node, ast.IfExp):
        return _string_keys(node.body) | _string_keys(node.orelse)
    if isinstance(node, ast.Dict):
        return {
            key.value
            for key in node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
    return set()


def _name(node: ast.AST) -> str:
    return node.id if isinstance(node, ast.Name) else getattr(node, "attr", "")


def detail_keys_written(root: Path) -> set[str]:
    """As chaves de `details` que o código **escreve como texto literal**, nos arquivos que gravam na
    auditoria (os que citam `AuditLog` ou `record_test`): o dicionário de `AuditLog(details=...)`, os
    argumentos nomeados de `_audit(...)` e de `record_test(...)` (que viram `details`) e o que se monta
    nas variáveis `details` e `confirmed`. **Não vê** a chave montada por outro caminho: os campos que
    `report.model_dump()` espalha (a retenção) ou um `**details` repassado."""
    found: set[str] = set()
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "AuditLog" not in text and "record_test" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Call):
                if _name(node.func) == "AuditLog":
                    for keyword in node.keywords:
                        if keyword.arg == "details":
                            found |= _string_keys(keyword.value)
                elif _name(node.func) in ("_audit", "record_test"):
                    found |= {k.arg for k in node.keywords if k.arg and k.arg != "actor"}
            elif isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in BUILDERS:
                        found |= _string_keys(node.value)
                    elif isinstance(target, ast.Subscript) and _name(target.value) in BUILDERS:
                        found |= {
                            part.value
                            for part in ast.walk(target.slice)
                            if isinstance(part, ast.Constant) and isinstance(part.value, str)
                        }
    return found


def test_every_key_the_services_write_as_a_literal_is_public_or_withheld_on_purpose() -> None:
    """O que o código escreve, lido do código (`detail_keys_written`): uma chave nova num serviço reprova
    aqui até alguém decidir se a chave `leitura` pode vê-la (entra em `PUBLIC_DETAIL_KEYS`) ou não
    (entra em `WITHHELD`, com o porquê)."""
    seen = detail_keys_written(APP)

    # O teste olha o que diz olhar: as chaves de cada caminho de escrita aparecem.
    assert {"reason", "error", "attempt", "images", "http_confirmed", "outcome", "sha256"} <= seen
    undecided = seen - queries.PUBLIC_DETAIL_KEYS - WITHHELD
    assert undecided == set(), (
        f"chave(s) de `details` que um serviço grava e ninguém classificou: {sorted(undecided)}. "
        "Inclua em PUBLIC_DETAIL_KEYS (app/admin/queries/audit.py) se a chave `leitura` pode vê-la, "
        "ou em WITHHELD (este arquivo), com o motivo."
    )
    assert WITHHELD <= seen  # uma chave retida que nenhum serviço grava mais sai da lista
    assert not WITHHELD & queries.PUBLIC_DETAIL_KEYS


def test_the_extractor_reads_the_ways_a_service_writes_a_key(tmp_path: Path) -> None:
    (tmp_path / "service.py").write_text(
        "session.add(AuditLog(actor=a, action='x', details={'literal': 1, **more}))\n"
        "_audit(session, actor, 'y', row, as_keyword=2, actor=actor)\n"
        "record_test(session, stored, actor=actor, as_test_keyword=3)\n"
        "details = {'first': 1}\n"
        "details['second'] = 2\n"
        "details['third' if ok else 'fourth'] = 3\n"
        "confirmed = {'fifth': True} if insecure else {}\n"
        "other = {'not_a_detail': 1}\n",
        encoding="utf-8",
    )
    (tmp_path / "unrelated.py").write_text("details = {'ignored': 1}\n", encoding="utf-8")

    assert detail_keys_written(tmp_path) == {
        "literal",
        "as_keyword",
        "as_test_keyword",
        "first",
        "second",
        "third",
        "fourth",
        "fifth",
    }


def test_the_hand_written_list_of_the_keys_the_services_write_matches_the_public_ones() -> None:
    """Uma lista **escrita à mão** (cada grupo com o serviço de onde sai), comparada com a outra lista
    escrita à mão, `PUBLIC_DETAIL_KEYS`: **não lê o código**. Serve de conferência para o que o teste
    acima não vê, as chaves montadas por outro caminho (os campos que `report.model_dump()` espalha na
    retenção, o `**details` repassado). Uma chave nova desse tipo só entra se alguém editar as duas."""
    written = {
        "app/destinations.py": {"form_slug", "host", "previous_host", "http_confirmed", "source"},
        "app/destinations.py (teste)": {"status", "elapsed_ms", "error"},
        "app/jobs.py": {"attempt", "previous_status", "failure_code", "job_id"},
        "app/retention.py": {
            "requests_deleted",
            "leads_deleted",
            "pdfs_deleted",
            "storage_errors",
            "draft_tests_deleted",
            "retention_days",
            "retention_pdf_days",
        },
        "app/forms/*.py": {
            "version",
            "fingerprint",
            "contact_in_prompt",
            "images",
            "tested_at",
            "consent_version",
            "previous_slug",
            "source_version",
            "test",
            "simulated_date",
            "active",
        },
        "app/forms/retention.py": {
            "pdf_days",
            "request_days",
            "previous_pdf_days",
            "previous_request_days",
            "in_force_pdf_days",
            "in_force_request_days",
            "confirmed_requests",
            "confirmed_pdfs",
        },
        "app/forms/assets.py": {"name", "sha256", "previous_sha256", "bytes", "source_bytes"},
        "app/integrations/": {
            "kind",
            "table_id",
            "token_changed",
            "mapping_reset",
            "fields",
            "deactivated",
            "ok",
            "public_id",
            "outcome",
            "error",  # o texto do erro do Baserow (`push.send_sample`)
            "count",
        },
        "app/admin/api/keys.py": {"role", "expires_at"},
        "app/jobs.py (motivo)": {"reason"},
    }
    every = set().union(*written.values())

    assert every - WITHHELD == queries.PUBLIC_DETAIL_KEYS
    assert WITHHELD <= every


@pytest.mark.parametrize(
    "value",
    [
        "texto",
        "",
        7,
        0,
        -3,
        1.5,
        True,
        False,
        None,
        ["name", "email"],
        [],
        [1, 2],
        ["a", 1, None, True],
        {"logo": "e3b0c442"},
        {},
        {"largura": 100, "ativo": True, "nada": None},
    ],
)
def test_a_known_key_keeps_a_simple_value_as_it_was_written(value: Any) -> None:
    assert queries.public_details({"form_slug": value}) == {"form_slug": value}


@pytest.mark.parametrize(
    "value",
    [
        {"fundo": {"x": 1}},  # um objeto dentro do objeto
        {"lista": ["a"]},  # uma lista dentro do objeto
        [{"x": 1}],  # um objeto numa lista
        [["a"]],  # uma lista numa lista
        {"a": {"b": {"c": 1}}},
    ],
)
def test_a_known_key_with_a_value_deeper_than_one_level_is_dropped(value: Any) -> None:
    assert queries.public_details({"form_slug": "demo", "images": value}) == {"form_slug": "demo"}


def test_an_unknown_key_is_dropped_whatever_its_name_or_value() -> None:
    secret = uuid.uuid4().hex

    kept = queries.public_details(
        {
            "form_slug": "demo",
            "secret": secret,
            "reason": f"cite {secret}",
            "novo_campo_de_um_servico_futuro": 1,
            "Form_Slug": "demo",  # a chave é exata: maiúsculas não passam
        }
    )

    assert kept == {"form_slug": "demo"}


def test_details_that_are_not_an_object_come_out_empty() -> None:
    for odd in (None, [], "texto", 7):
        assert queries.public_details(odd) == {}  # type: ignore[arg-type]


# --- a leitura ----------------------------------------------------------------------------------


def seed(deps: Deps, *rows: AuditLog) -> None:
    with deps.sessions.begin() as session:
        session.add_all(rows)


def test_an_entry_comes_out_as_a_dataclass_with_the_safe_details_only(deps: Deps) -> None:
    seed(
        deps,
        row(
            1,
            actor="chave:painel",
            action="job.reprocess",
            target_type="job",
            target_id="j-1",
            details={"attempt": 2, "reason": "cite a lead", "failure_code": "llm_unavailable"},
        ),
    )

    (found,), more = entries(deps)

    assert more is False
    assert found == queries.AuditEntry(
        id=uuid.UUID(int=1),
        at=T0,
        actor="chave:painel",
        action="job.reprocess",
        target_type="job",
        target_id="j-1",
        details={"attempt": 2, "failure_code": "llm_unavailable"},
    )


def test_the_filters_are_equalities_and_add_up(deps: Deps) -> None:
    seed(
        deps,
        row(1, actor="a", action="x.one", target_type="form", target_id="1"),
        row(2, actor="a", action="x.two", target_type="job", target_id="1"),
        row(3, actor="b", action="x.one", target_type="job", target_id="2"),
    )

    def found(**filters: str) -> set[int]:
        rows, _ = entries(deps, queries.AuditFilter(**filters))
        return {entry.id.int for entry in rows}

    assert found() == {1, 2, 3}
    assert found(actor="a") == {1, 2}
    assert found(action="x.one") == {1, 3}
    assert found(target_type="job") == {2, 3}
    assert found(target_id="1") == {1, 2}
    assert found(actor="a", target_type="job", target_id="1", action="x.two") == {2}
    assert found(actor="A") == found(action="x.%") == found(target_id="_") == set()


def test_the_cursor_walks_the_entries_with_the_same_instant_by_id_without_a_gap(
    deps: Deps,
) -> None:
    rows = [row(n, at=T0 if n <= 3 else T0 + timedelta(hours=1)) for n in (2, 3, 1, 5, 4)]
    seed(deps, *rows)

    seen: list[int] = []
    cursor = None
    while True:
        page, more = entries(deps, limit=2, after=cursor)
        seen += [entry.id.int for entry in page]
        if not more:
            break
        cursor = queries.AuditCursor(page[-1].at, page[-1].id)

    assert seen == [5, 4, 3, 2, 1]  # o instante mais novo primeiro; no mesmo instante, o `id` maior


def test_there_are_more_only_when_an_entry_is_left_after_the_page(deps: Deps) -> None:
    seed(deps, row(1), row(2), row(3, at=T0 + timedelta(hours=1)))

    assert entries(deps, limit=3)[1] is False  # cabe certo: não há página vazia depois
    assert entries(deps, limit=2)[1] is True
    assert entries(deps, limit=1)[1] is True
    assert entries(deps, limit=50) == (entries(deps, limit=3)[0], False)
