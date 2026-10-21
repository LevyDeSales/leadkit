"""O que o admin faz com o PDF de um pedido (`app.admin.actions.jobs.pdf_file`): a tentativa pedida, o
404 de quem não tem PDF, o 410 de quem a retenção apagou, e a URL assinada ou os bytes.

A página usa a URL (o storage entrega direto ao navegador) e a API usa os bytes (uma URL assinada
seria um segundo segredo, fora da chave e da auditoria).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from jobs_world import build_jobs_world
from test_admin_destinations import admin_client

from app.admin.actions import jobs as actions
from app.deps import Deps
from app.internal_links import AdminPdfAccess
from app.models import Artifact

SIGNED = "https://armazenamento.exemplo.invalid/plano.pdf?assinatura=1"


def sign_from_now_on(deps: Deps) -> list[tuple[str, int, str]]:
    """O storage dos testes é local e não assina; a partir daqui ele assina, e guarda o que lhe
    pedem (a chave, a duração e o nome do arquivo). Vale depois de montar os pedidos: o pipeline
    também pede URLs (as do webhook, de 24 h)."""
    asked: list[tuple[str, int, str]] = []

    def presigned_url(key: str, *, ttl_seconds: int, filename: str) -> str | None:
        asked.append((key, ttl_seconds, filename))
        return SIGNED

    deps.storage.presigned_url = presigned_url  # type: ignore[method-assign]
    return asked


def test_the_pdf_of_the_latest_attempt_comes_with_its_bytes_and_a_name_that_says_the_attempt(
    deps: Deps,
) -> None:
    world = build_jobs_world(deps)

    pdf = actions.pdf_file(
        deps, world.beatriz.public_id, None, access=AdminPdfAccess(), signed_url=False
    )

    assert pdf.filename == f"plano-{world.beatriz.public_id}-v2.pdf"
    assert pdf.url is None and pdf.content is not None and pdf.content.startswith(b"%PDF")


def test_a_version_asks_for_that_attempt_and_one_that_does_not_exist_is_missing(
    deps: Deps,
) -> None:
    world = build_jobs_world(deps)
    public_id = world.beatriz.public_id

    first = actions.pdf_file(deps, public_id, 1, access=AdminPdfAccess(), signed_url=False)
    latest = actions.pdf_file(deps, public_id, None, access=AdminPdfAccess(), signed_url=False)

    assert first.filename.endswith("-v1.pdf") and latest.filename.endswith("-v2.pdf")
    with pytest.raises(actions.NoPdf):
        actions.pdf_file(deps, public_id, 9, access=AdminPdfAccess(), signed_url=False)


def test_a_job_that_does_not_exist_and_a_job_without_a_pdf_are_both_missing(deps: Deps) -> None:
    world = build_jobs_world(deps)

    with pytest.raises(actions.NoPdf):
        actions.pdf_file(deps, "nao-existe", None, access=AdminPdfAccess(), signed_url=False)
    with pytest.raises(actions.NoPdf):  # na fila, ainda sem PDF
        actions.pdf_file(
            deps, world.debora.public_id, None, access=AdminPdfAccess(), signed_url=False
        )


def test_a_pdf_the_retention_removed_is_gone_and_the_storage_is_not_asked(deps: Deps) -> None:
    world = build_jobs_world(deps)
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(Artifact)
            .where(Artifact.job_id == world.ana.id)
            .values(purged_at=datetime(2027, 1, 2, tzinfo=UTC))
        )

    with pytest.raises(actions.PdfRemoved):
        actions.pdf_file(deps, world.ana.public_id, None, access=AdminPdfAccess(), signed_url=True)


def test_with_a_signed_url_allowed_the_page_gets_the_url_and_not_the_bytes(deps: Deps) -> None:
    world = build_jobs_world(deps)
    asked = sign_from_now_on(deps)

    pdf = actions.pdf_file(
        deps, world.ana.public_id, None, access=AdminPdfAccess(), signed_url=True
    )

    assert (pdf.url, pdf.content) == (SIGNED, None)
    assert [(ttl, name) for _, ttl, name in asked] == [(300, f"plano-{world.ana.public_id}-v1.pdf")]


def test_the_api_never_gets_a_signed_url_even_when_the_storage_offers_one(deps: Deps) -> None:
    world = build_jobs_world(deps)
    asked = sign_from_now_on(deps)

    pdf = actions.pdf_file(
        deps, world.ana.public_id, None, access=AdminPdfAccess(), signed_url=False
    )

    assert pdf.url is None and pdf.content is not None and pdf.content.startswith(b"%PDF")
    assert asked == []  # nem chegou a pedir a assinatura


def test_the_page_redirects_to_the_signed_url_when_the_storage_signs(deps: Deps) -> None:
    world = build_jobs_world(deps)
    sign_from_now_on(deps)
    admin = admin_client(deps)

    response = admin.get(f"/admin/jobs/{world.ana.public_id}/pdf")

    assert response.status_code == 302 and response.headers["location"] == SIGNED
