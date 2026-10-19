"""Um banco com pedidos em todos os estados, para os testes que leem pedidos (a consulta, a página e a
API de admin). Não é um arquivo de teste (o pytest não o coleta); os `test_admin_*jobs*.py` o importam.

Cada pedido tem o nome e o e-mail de uma pessoa fictícia, para o teste procurar esses textos nas
respostas que não podem trazê-los.
"""

from __future__ import annotations

import dataclasses
import typing
from dataclasses import dataclass

import sqlalchemy as sa
from conftest import newer_demo_definition, publish, record_webhook
from test_admin import failed_delivery, finish, new_lead_job, new_mcc_job, reprocess_order
from test_integrations import connect_form, sync_ids

from app import jobs, pipeline
from app.deps import Deps
from app.forms import FormSpec
from app.forms.definition import fingerprint
from app.integrations import syncs
from app.models import Artifact, Form, FormVersion, IntegrationSync, Job, Submission, utcnow

REASON = "Prompt corrigido pela equipe depois da falha"
FAILURE_DETAIL = "A IA devolveu HTTP 503 do provedor"
SYNC_ERROR = "[integration_unavailable] criar a linha: HTTP 503"

# Os nomes que só podem viver no `lead_data`: o dado pessoal da lead e, como diz a ADR 0015, a
# pontuação e o consentimento também.
PERSONAL = {
    "name",
    "email",
    "phone",
    "answers",
    "scores",
    "consent",
    "plan",
    "persona",
    "priority",
}


def field_names(kind: type) -> set[str]:
    """Os nomes dos campos de uma dataclass e, recursivamente, os das dataclasses que ela guarda
    (`tuple[Brief, ...]`, `Brief | None`)."""
    found: set[str] = set()
    for name, hint in typing.get_type_hints(kind).items():
        found.add(name)
        stack = [hint]
        while stack:
            item = stack.pop()
            if dataclasses.is_dataclass(item) and isinstance(item, type):
                found |= field_names(item)
            stack.extend(typing.get_args(item))
    return found


PEOPLE = {
    "ana": ("Ana Souza", "ana@example.test"),
    "beatriz": ("Beatriz Lima", "beatriz@example.test"),
    "carla": ("Carla Dias", "carla@example.test"),
    "debora": ("Debora Melo", "debora@example.test"),
    "elisa": ("Elisa Reis", "elisa@example.test"),
}


FERNANDA = ("Fernanda Rocha", "fernanda@example.test")  # a lead do MCC


@dataclass
class JobsWorld:
    """Os cinco pedidos, do mais antigo ao mais novo."""

    ana: Job  # concluído, entregue ao webhook e sincronizado com a planilha
    beatriz: Job  # a entrega falhou, foi reprocessado (2 tentativas) e a sincronização falhou
    carla: Job  # falhou (llm_unavailable), com o detalhe da falha
    debora: Job  # na fila
    elisa: Job  # gerando (montando o PDF)

    @property
    def all(self) -> list[Job]:
        return [self.ana, self.beatriz, self.carla, self.debora, self.elisa]


def add_mcc_order(deps: Deps) -> Job:
    """O sexto pedido, o mais novo e de outro formulário (o MCC): os cinco do mundo são do demo. Fica
    fora de `build_jobs_world` porque os testes que o usam contam cinco pedidos, de um formulário só;
    quem filtra por formulário o acrescenta. Na fila, como o `debora`."""
    return new_mcc_job(deps, *FERNANDA)


def build_jobs_world(deps: Deps) -> JobsWorld:
    connect_form(deps)  # a integração ativa dá uma sincronização a cada pedido concluído
    ana = new_lead_job(deps, *PEOPLE["ana"])
    record_webhook(deps)
    (delivery_id,) = finish(deps, ana.id)
    pipeline.deliver(deps, delivery_id)
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(IntegrationSync)
            .where(IntegrationSync.job_id == ana.id)
            .values(status="succeeded", attempts=1, outcome="created", row_id=7, synced_at=utcnow())
        )

    beatriz = new_lead_job(deps, *PEOPLE["beatriz"])
    failed_delivery(deps, beatriz.id)
    with deps.sessions.begin() as session:
        assert jobs.reprocess(
            session, beatriz.id, actor="operadora", reason=REASON, expected_attempt=1
        )
    finish(deps, beatriz.id, attempt=2)
    with deps.sessions.begin() as session:
        for sync_id in sync_ids(deps, beatriz.id)[-1:]:
            session.execute(
                sa.update(IntegrationSync)
                .where(IntegrationSync.id == sync_id)
                .values(attempts=4, last_error=SYNC_ERROR)
            )
            syncs.mark_failed(session, sync_id, "integration_unavailable")

    carla = new_lead_job(deps, *PEOPLE["carla"])
    with deps.sessions.begin() as session:
        jobs.claim(session, carla.id, 1, "run-carla")
        jobs.fail(session, carla.id, 1, "llm_unavailable", FAILURE_DETAIL)

    debora = new_lead_job(deps, *PEOPLE["debora"])

    elisa = new_lead_job(deps, *PEOPLE["elisa"])
    with deps.sessions.begin() as session:
        jobs.claim(session, elisa.id, 1, "run-elisa")
        jobs.set_stage(session, elisa.id, 1, "rendering")

    return JobsWorld(ana, beatriz, carla, debora, elisa)


# A situação da sincronização com a planilha que a lista distingue, na ordem em que `jobs_by_sync`
# cria os pedidos (o par de `DELIVERY_CASES`, em `test_admin`).
SYNC_CASES = (
    "failed_now",  # concluído; a sincronização da tentativa 1 falhou
    "pending_now",  # concluído; a sincronização da tentativa 1 está pendente
    "synced",  # concluído; a sincronização da tentativa 1 deu certo
    "retried_ok",  # a da tentativa 1 falhou; reprocessado, a da tentativa 2 deu certo
    "regressed",  # a da tentativa 1 deu certo; reprocessado, a da tentativa 2 falhou
    "queued",  # na fila, ainda sem PDF nem sincronização
    "no_integration",  # concluído, mas de um formulário (o MCC) sem integração
)
# O que cada opção de "Planilha" mostra: a situação das sincronizações da tentativa **atual**.
# Calculado à mão, não pelo código.
SYNC_EXPECTED = {
    "": set(SYNC_CASES),
    "failed": {"failed_now", "regressed"},
    "pending": {"pending_now"},
}
# O que a lista diz da sincronização da tentativa atual de cada pedido de `jobs_by_sync`.
SYNC_SHOWN = {
    "failed_now": ["failed"],
    "pending_now": ["pending"],
    "synced": ["succeeded"],
    "retried_ok": ["succeeded"],  # a falha da tentativa 1 já foi refeita
    "regressed": ["failed"],  # a da tentativa 1 deu certo, mas a atual falhou
    "queued": [],
    "no_integration": [],
}


def settle_sync(deps: Deps, job: Job, attempt: int, outcome: str) -> None:
    """Leva a sincronização do PDF da tentativa `attempt` ao estado `outcome`, como o Hatchet faria:
    `succeeded` (a linha está na planilha), `failed` (esgotou as tentativas) ou `pending` (fica
    como nasceu, na fila)."""
    with deps.sessions.begin() as session:
        found = list(
            session.scalars(
                sa.select(IntegrationSync.id)
                .join(Artifact, Artifact.id == IntegrationSync.artifact_id)
                .where(IntegrationSync.job_id == job.id, Artifact.version == attempt)
            )
        )
        assert len(found) == 1, (job.public_id, attempt)
        if outcome == "succeeded":
            session.execute(
                sa.update(IntegrationSync)
                .where(IntegrationSync.id == found[0])
                .values(
                    status="succeeded", attempts=1, outcome="created", row_id=7, synced_at=utcnow()
                )
            )
        elif outcome == "failed":
            session.execute(
                sa.update(IntegrationSync)
                .where(IntegrationSync.id == found[0])
                .values(attempts=4, last_error=SYNC_ERROR)
            )
            syncs.mark_failed(session, found[0], "integration_unavailable")


def jobs_by_sync(deps: Deps) -> dict[str, Job]:
    """Um pedido do demo (que tem integração) em cada situação de `SYNC_CASES`, pelo nome dela, mais
    um do MCC (que não tem). Todos com o webhook ligado, então os concluídos têm também uma entrega
    pendente. Os do fim da lista são os mais novos."""
    connect_form(deps)
    record_webhook(deps)
    made = {
        name: new_lead_job(deps, f"Lead {name}", f"lead-{number}@example.test")
        for number, name in enumerate(SYNC_CASES)
        if name != "no_integration"
    }
    made["no_integration"] = new_mcc_job(deps, "Lead no_integration", "lead-no@example.test")
    finish(deps, made["failed_now"].id)
    settle_sync(deps, made["failed_now"], 1, "failed")
    finish(deps, made["pending_now"].id)
    finish(deps, made["synced"].id)
    settle_sync(deps, made["synced"], 1, "succeeded")
    finish(deps, made["retried_ok"].id)
    settle_sync(deps, made["retried_ok"], 1, "failed")
    reprocess_order(deps, made["retried_ok"])
    finish(deps, made["retried_ok"].id, 2)
    settle_sync(deps, made["retried_ok"], 2, "succeeded")
    finish(deps, made["regressed"].id)
    settle_sync(deps, made["regressed"], 1, "succeeded")
    reprocess_order(deps, made["regressed"])
    finish(deps, made["regressed"].id, 2)
    settle_sync(deps, made["regressed"], 2, "failed")
    finish(deps, made["no_integration"].id)
    return made


# O texto de `form_version` de cada pedido de `jobs_by_version`, na ordem em que ele os cria.
# `demo-2026.10` é o que o código gravava antes da migration 0008 (versão 1 no registro); `demo-v1`,
# `demo-v2` e `mcc-v1` são os `<endereço>-v<número>` dos envios novos.
VERSION_OF = {
    "legacy": "demo-2026.10",
    "v1": "demo-v1",
    "v2": "demo-v2",
    "v2_again": "demo-v2",
    "mcc": "mcc-v1",
}
# O que o filtro "Versão" deixa passar para cada par (formulário, versão): os pedidos cujo formulário
# e cujo texto de versão são esses. Calculado à mão, não pelo código. A versão de outro formulário e a
# que ninguém tem dão a lista vazia.
VERSION_EXPECTED = {
    ("demo", ""): {"legacy", "v1", "v2", "v2_again"},
    ("demo", "demo-v2"): {"v2", "v2_again"},
    ("demo", "demo-v1"): {"v1"},
    ("demo", "demo-2026.10"): {"legacy"},
    ("demo", "mcc-v1"): set(),  # a versão do MCC, com o formulário demo
    ("demo", "demo-v9"): set(),  # nenhum pedido tem
    ("mcc", ""): {"mcc"},
    ("mcc", "mcc-v1"): {"mcc"},
    ("mcc", "demo-v2"): set(),
}
# As versões do demo que a lista oferece, da mais nova para a mais antiga: a de maior número primeiro
# e, na mesma, a que chegou por último (`demo-v1` chegou depois do texto legado, também versão 1).
DEMO_VERSIONS = ["demo-v2", "demo-v1", "demo-2026.10"]


def jobs_by_version(deps: Deps, newer_demo: FormSpec) -> dict[str, Job]:
    """Pedidos do demo em três textos de versão (o legado, `demo-v1` e, depois de publicada a versão
    2, `demo-v2` duas vezes) e um do MCC (`mcc-v1`), pelo nome de `VERSION_OF`. Os do fim da lista são
    os mais novos."""
    made = {
        name: new_lead_job(deps, f"Lead {name}", f"lead-{name}@example.test")
        for name in ("legacy", "v1")
    }
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(Submission)
            .where(Submission.id == made["legacy"].submission_id)
            .values(form_version=VERSION_OF["legacy"])
        )
    # Este mundo testa consultas históricas no banco: publicar só no FixedRegistry deixaria
    # a versão 2 sem definição persistida, diferente de uma publicação real (ADR 0013).
    definition = newer_demo_definition()
    with deps.sessions.begin() as session:
        form_id = session.scalar(sa.select(Form.id).where(Form.slug == newer_demo.slug))
        session.add(
            FormVersion(
                form_id=form_id,
                number=newer_demo.version_number,
                definition=definition,
                fingerprint=fingerprint(definition),
                note="Versão 2 da fixture",
                published_by="testes",
            )
        )
    publish(deps, newer_demo)  # a partir daqui, o demo recebe na versão 2
    for name in ("v2", "v2_again"):
        made[name] = new_lead_job(deps, f"Lead {name}", f"lead-{name}@example.test")
    made["mcc"] = new_mcc_job(deps, "Lead mcc", "lead-mcc@example.test")
    return made
