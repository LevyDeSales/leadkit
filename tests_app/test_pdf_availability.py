from dataclasses import replace

import pytest
from test_intake import submit

from app import jobs, pipeline
from app.deps import Deps
from app.errors import TerminalError
from app.internal_links import AdminPdfAccess


def test_completion_rejects_a_pdf_prefix_without_a_readable_document(deps: Deps) -> None:
    class TruncatedRenderer:
        def render(self, spec, document, context):
            return replace(original.render(spec, document, context), content=b"%PDF-1.4\n%%EOF")

    original = deps.pdf
    deps.pdf = TruncatedRenderer()
    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "synthetic")
    plan, _ = pipeline.generate_plan(deps, job.id, 1)
    with pytest.raises(TerminalError, match="render_failed"):
        artifact, _ = pipeline.render_pdf(deps, job.id, 1, plan)
        pipeline.finalize(deps, job.id, 1, artifact)


def test_current_pdf_is_verified_and_storage_changes_do_not_rewrite_completion(deps: Deps) -> None:
    from app.models import Job
    from app.pdf_availability import current_pdf

    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "synthetic")
    plan, _ = pipeline.generate_plan(deps, job.id, 1)
    artifact, _ = pipeline.render_pdf(deps, job.id, 1, plan)
    pipeline.finalize(deps, job.id, 1, artifact)
    with deps.sessions() as session:
        state = current_pdf(session, deps.storage, session.get_one(Job, job.id))
    assert state.state == "available" and state.available and state.verified_at
    deps.storage.delete(f"plans/{job.id}/v1.pdf")
    with deps.sessions() as session:
        completed = session.get_one(Job, job.id)
        assert current_pdf(session, deps.storage, completed).state == "missing"
        assert completed.status == "completed"


def test_admin_read_exposes_current_pdf_without_exposing_or_regenerating_the_plan(
    deps: Deps,
) -> None:
    from datetime import UTC, datetime

    from app.admin.queries.jobs import read_job
    from app.models import Job

    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "synthetic")
    plan, _ = pipeline.generate_plan(deps, job.id, 1)
    artifact, _ = pipeline.render_pdf(deps, job.id, 1, plan)
    pipeline.finalize(deps, job.id, 1, artifact)
    with deps.sessions() as session:
        detail = read_job(
            session,
            job.public_id,
            forms=deps.forms,
            now=datetime.now(UTC),
            with_lead=False,
            storage=deps.storage,
        )
    assert detail.lead is None and detail.state.current_pdf.available
    with deps.sessions.begin() as session:
        assert (
            jobs.reprocess(
                session, job.id, actor="synthetic", reason="nova tentativa", expected_attempt=1
            )
            == 2
        )
    with deps.sessions() as session:
        detail = read_job(
            session,
            job.public_id,
            forms=deps.forms,
            now=datetime.now(UTC),
            with_lead=False,
            storage=deps.storage,
        )
        assert detail.state.current_pdf.attempt == 2
        assert not detail.state.current_pdf.available
        assert session.get_one(Job, job.id).status == "queued"


def test_default_download_never_falls_back_to_a_previous_attempt(deps: Deps) -> None:
    from app.admin.actions.jobs import NoPdf, pdf_file

    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "synthetic")
    plan, _ = pipeline.generate_plan(deps, job.id, 1)
    artifact, _ = pipeline.render_pdf(deps, job.id, 1, plan)
    pipeline.finalize(deps, job.id, 1, artifact)
    assert pdf_file(
        deps, job.public_id, None, access=AdminPdfAccess(), signed_url=False
    ).content.startswith(b"%PDF")
    with deps.sessions.begin() as session:
        jobs.reprocess(
            session, job.id, actor="synthetic", reason="nova tentativa", expected_attempt=1
        )
    with pytest.raises(NoPdf):
        pdf_file(deps, job.public_id, None, access=AdminPdfAccess(), signed_url=False)
    assert pdf_file(
        deps, job.public_id, 1, access=AdminPdfAccess(), signed_url=False
    ).content.startswith(b"%PDF")


def test_duplicate_render_keeps_the_winners_bytes_and_evidence(deps: Deps) -> None:
    from test_pipeline import VaryingPdf

    from app.models import Artifact
    from app.pdf_availability import current_pdf

    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "synthetic")
    plan, _ = pipeline.generate_plan(deps, job.id, 1)
    outer = VaryingPdf(deps.pdf)
    original = deps.pdf
    deps.pdf = outer
    winners = []

    def winner_finishes_first():
        deps.pdf = original
        winner, _ = pipeline.render_pdf(deps, job.id, 1, plan)
        pipeline.finalize(deps, job.id, 1, winner)
        winners.append(winner)

    outer.before_return = winner_finishes_first
    artifact, reused = pipeline.render_pdf(deps, job.id, 1, plan)
    assert reused and artifact == winners[0]
    with deps.sessions() as session:
        row = session.get_one(Artifact, artifact)
        from app.models import Job

        assert current_pdf(session, deps.storage, session.get_one(Job, job.id)).available
        assert deps.storage.get(row.storage_key).rstrip().endswith(b"%%EOF")


@pytest.mark.parametrize("condition", ["removed", "unknown", "invalid", "unavailable", "missing"])
def test_api_and_mcp_report_the_same_safe_availability_and_never_download_on_status(
    deps: Deps, api_client, tmp_path, monkeypatch, condition: str
) -> None:
    from admin_mcp_live import Live
    from test_admin import login
    from test_pipeline import CountingLlm

    from app.models import Artifact, Job, utcnow

    llm = CountingLlm(deps.llm)
    deps.llm = llm
    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "synthetic")
    plan, _ = pipeline.generate_plan(deps, job.id, 1)
    artifact_id, _ = pipeline.render_pdf(deps, job.id, 1, plan)
    pipeline.finalize(deps, job.id, 1, artifact_id)
    key = f"plans/{job.id}/v1.pdf"
    if condition in ("removed", "unknown"):
        with deps.sessions.begin() as session:
            artifact = session.get_one(Artifact, artifact_id)
            if condition == "removed":
                artifact.purged_at = utcnow()
            else:
                artifact.verified_at = artifact.storage_revision = None
    elif condition == "invalid":
        deps.storage.put(key, b"%PDF-1.4 modified", "application/pdf")
    elif condition == "missing":
        deps.storage.delete(key)
    else:

        def denied(key):
            raise PermissionError("PRIVATE-PROVIDER-DETAIL")

        monkeypatch.setattr(deps.storage, "head", denied)

    reads = []

    def no_download(key):
        reads.append(key)
        raise AssertionError("status must never download the PDF")

    monkeypatch.setattr(deps.storage, "get", no_download)
    reader = api_client("leitura")
    response = reader.get(f"/admin/api/v1/jobs/{job.public_id}")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "completed"
    state = result["current_pdf"]
    assert state["state"] == condition and state["available"] is False
    assert result["lead_data"] is None
    assert (
        "storage_revision" not in response.text and "PRIVATE-PROVIDER-DETAIL" not in response.text
    )
    assert reader.get(f"/admin/api/v1/jobs/{job.public_id}/pdf").status_code == 403
    live = Live(reader, tmp_path)
    try:
        assert live.ok("get_job", {"public_id": job.public_id})["current_pdf"] == state
    finally:
        live.close()
    assert login(reader).status_code == 303
    html = reader.get(f"/admin/jobs/{job.public_id}")
    assert html.status_code == 200 and "PDF da tentativa atual" in html.text
    if condition != "unknown":
        assert f'href="/admin/jobs/{job.public_id}/pdf"' not in html.text
    token = deps.signer.issue(job.public_id, job.token_version)
    public = reader.get(
        f"/v1/jobs/{job.public_id}", params={"t": token}, headers={"Authorization": ""}
    )
    assert public.status_code == 200 and public.json()["current_pdf"] == state
    assert "pdf_url" not in public.json()
    assert reads == []
    assert reader.get(f"/admin/jobs/{job.public_id}/pdf").status_code in (410, 502)
    assert reader.get(
        f"/v1/jobs/{job.public_id}/pdf", params={"t": token}, headers={"Authorization": ""}
    ).status_code in (410, 502)
    reader.get(f"/d/{job.public_id}", params={"t": token}, headers={"Authorization": ""})
    page = reader.get(f"/d/{job.public_id}")
    assert "Seu PDF está indisponível" in page.text
    assert "Organizar atividades do projeto" not in page.text
    with deps.sessions() as session:
        assert session.get_one(Job, job.id).status == "completed"
    assert llm.calls == 1


def test_authorized_download_verifies_a_historical_pdf_without_regenerating_it(deps: Deps) -> None:
    from test_pipeline import CountingLlm

    from app.admin.actions.jobs import pdf_file
    from app.models import Artifact, Job
    from app.pdf_availability import current_pdf

    llm = CountingLlm(deps.llm)
    deps.llm = llm
    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "synthetic")
    plan, _ = pipeline.generate_plan(deps, job.id, 1)
    artifact_id, _ = pipeline.render_pdf(deps, job.id, 1, plan)
    pipeline.finalize(deps, job.id, 1, artifact_id)
    with deps.sessions.begin() as session:
        artifact = session.get_one(Artifact, artifact_id)
        artifact.verified_at = artifact.storage_revision = None
    with deps.sessions() as session:
        assert current_pdf(session, deps.storage, session.get_one(Job, job.id)).state == "unknown"
    assert pdf_file(
        deps, job.public_id, None, access=AdminPdfAccess(), signed_url=False
    ).content.startswith(b"%PDF")
    with deps.sessions() as session:
        assert current_pdf(session, deps.storage, session.get_one(Job, job.id)).available
    assert llm.calls == 1


@pytest.mark.parametrize("surface", ["api", "web"])
@pytest.mark.parametrize("change", ["attempt", "token"])
def test_public_download_rechecks_authorized_snapshot_before_selecting_pdf(
    deps: Deps, api_client, surface: str, change: str
) -> None:
    import sqlalchemy as sa

    from app.models import Job

    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "synthetic")
    plan, _ = pipeline.generate_plan(deps, job.id, 1)
    artifact, _ = pipeline.render_pdf(deps, job.id, 1, plan)
    pipeline.finalize(deps, job.id, 1, artifact)
    client = api_client("total")
    token = deps.signer.issue(job.public_id, job.token_version)
    if surface == "web":
        assert client.get(f"/d/{job.public_id}", params={"t": token}).status_code == 303
    engine = deps.sessions.kw["bind"]
    crossed = []

    def cross_after_authorization_query(connection, cursor, statement, parameters, context, many):
        # O cursor já recebeu o snapshot anterior. A segunda conexão confirma a mudança antes
        # de a requisição sair da consulta de autorização; nenhum colaborador do app é simulado.
        if crossed or "FROM jobs" not in statement or "jobs.public_id =" not in statement:
            return
        crossed.append(change)
        with deps.sessions.begin() as session:
            if change == "token":
                session.get_one(Job, job.id).token_version += 1
            else:
                assert (
                    jobs.reprocess(
                        session,
                        job.id,
                        actor="synthetic",
                        reason="nova tentativa",
                        expected_attempt=1,
                    )
                    == 2
                )
                jobs.claim(session, job.id, 2, "synthetic-2")
        if change == "attempt":
            plan, _ = pipeline.generate_plan(deps, job.id, 2)
            artifact, _ = pipeline.render_pdf(deps, job.id, 2, plan)
            pipeline.finalize(deps, job.id, 2, artifact)

    sa.event.listen(engine, "after_cursor_execute", cross_after_authorization_query)
    try:
        if surface == "api":
            response = client.get(
                f"/v1/jobs/{job.public_id}/pdf", headers={"Authorization": f"Bearer {token}"}
            )
        else:
            response = client.get(f"/d/{job.public_id}/pdf")
    finally:
        sa.event.remove(engine, "after_cursor_execute", cross_after_authorization_query)
    assert crossed == [change]
    assert response.status_code in (401, 404, 409)
    assert b"%PDF" not in response.content and "location" not in response.headers


def test_storage_failure_during_completion_retries_without_another_paid_generation(
    deps: Deps, monkeypatch
) -> None:
    from test_pipeline import CountingLlm

    from app.errors import FailureCode, RetryableError
    from app.models import Job
    from app.pdf_availability import current_pdf

    llm = CountingLlm(deps.llm)
    deps.llm = llm
    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "synthetic")
    plan, _ = pipeline.generate_plan(deps, job.id, 1)
    artifact, _ = pipeline.render_pdf(deps, job.id, 1, plan)
    get = deps.storage.get

    def timeout(key):
        raise RetryableError(FailureCode.STORAGE_FAILED, "PRIVATE-PROVIDER-DETAIL")

    monkeypatch.setattr(deps.storage, "get", timeout)
    with pytest.raises(RetryableError) as caught:
        pipeline.finalize(deps, job.id, 1, artifact)
    assert "PRIVATE-PROVIDER-DETAIL" not in str(caught.value)
    with deps.sessions() as session:
        pending = session.get_one(Job, job.id)
        assert pending.status == "running"
        assert not current_pdf(session, deps.storage, pending).available
    assert pipeline.generate_plan(deps, job.id, 1) == (plan, True)
    assert pipeline.render_pdf(deps, job.id, 1, plan) == (artifact, True)
    monkeypatch.setattr(deps.storage, "get", get)
    pipeline.finalize(deps, job.id, 1, artifact)
    with deps.sessions() as session:
        assert current_pdf(session, deps.storage, session.get_one(Job, job.id)).available
    assert llm.calls == 1


@pytest.mark.parametrize("corruption", ["truncated", "empty", "encrypted"])
def test_pipeline_requires_readable_unencrypted_pages_and_sanitizes_parser_errors(
    deps: Deps, corruption: str, caplog
) -> None:
    from io import BytesIO

    from pypdf import PdfWriter

    from app.errors import as_hatchet_error

    original = deps.pdf

    class InvalidRenderer:
        def render(self, spec, document, context):
            result = original.render(spec, document, context)
            if corruption == "truncated":
                content = (
                    result.content[: len(result.content) // 2] + b"PRIVATE-DOCUMENT-DETAIL\n%%EOF"
                )
            else:
                writer = PdfWriter()
                if corruption == "encrypted":
                    writer.add_blank_page(width=100, height=100)
                    writer.encrypt("synthetic")
                buffer = BytesIO()
                writer.write(buffer)
                content = buffer.getvalue()
            return replace(result, content=content)

    deps.pdf = InvalidRenderer()
    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "synthetic")
    plan, _ = pipeline.generate_plan(deps, job.id, 1)
    with pytest.raises(TerminalError) as caught:
        pipeline.render_pdf(deps, job.id, 1, plan)
    assert "PRIVATE-DOCUMENT-DETAIL" not in str(as_hatchet_error(caught.value)) + caplog.text


def test_completion_rejects_different_bytes_even_when_head_and_size_match(
    deps: Deps, monkeypatch
) -> None:
    from app.models import Job
    from app.pdf_availability import current_pdf

    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "synthetic")
    plan, _ = pipeline.generate_plan(deps, job.id, 1)
    artifact, _ = pipeline.render_pdf(deps, job.id, 1, plan)
    original = deps.storage.get(f"plans/{job.id}/v1.pdf")
    monkeypatch.setattr(
        deps.storage, "get", lambda key: original.replace(b"Helvetica", b"PRIVATE-X")
    )
    with pytest.raises(TerminalError, match="diverge do artefato"):
        pipeline.finalize(deps, job.id, 1, artifact)
    with deps.sessions() as session:
        pending = session.get_one(Job, job.id)
        assert (
            pending.status == "running"
            and not current_pdf(session, deps.storage, pending).available
        )


def test_diagnostic_mcp_distinguishes_completed_processing_from_a_missing_pdf(
    deps: Deps, api_client, tmp_path
) -> None:
    from admin_mcp_live import Live

    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "synthetic")
    plan, _ = pipeline.generate_plan(deps, job.id, 1)
    artifact, _ = pipeline.render_pdf(deps, job.id, 1, plan)
    pipeline.finalize(deps, job.id, 1, artifact)
    deps.storage.delete(f"plans/{job.id}/v1.pdf")
    live = Live(api_client("leitura"), tmp_path)
    try:
        output = live.compose("diagnose_order", {"public_id": job.public_id})
    finally:
        live.close()
    assert not output.is_error
    assert "PDF atual: tentativa 1 · missing" in output.text
    assert "disponibilidade técnica não concede acesso" in output.text
    assert "não inicia uma geração" in output.text


def test_delivery_pdf_link_is_bound_to_its_bytes_and_a_stale_delivery_is_refused(
    deps: Deps, api_client
) -> None:
    from conftest import record_webhook
    from test_admin import finish

    webhook = record_webhook(deps)
    first = submit(deps).job
    (old_delivery,) = finish(deps, first.id)
    other = submit(deps, key="other").job
    (sent_delivery,) = finish(deps, other.id)
    pipeline.deliver(deps, sent_delivery)
    sent_url = webhook.calls[0][2]["pdf"]["url"]
    assert "&attempt=1" in sent_url
    client = api_client("total")
    assert client.get(sent_url, headers={"Authorization": ""}).status_code == 200
    for job in (first, other):
        with deps.sessions.begin() as session:
            jobs.reprocess(
                session, job.id, actor="synthetic", reason="nova tentativa", expected_attempt=1
            )
        finish(deps, job.id, 2)
    with pytest.raises(TerminalError, match="tentativa anterior"):
        pipeline.deliver(deps, old_delivery)
    assert len(webhook.calls) == 1
    assert client.get(sent_url, headers={"Authorization": ""}).status_code == 409


@pytest.mark.parametrize("corruption", ["invalid", "truncated"])
@pytest.mark.parametrize("historical", [False, True])
def test_corrupt_flate_page_cannot_be_certified_or_downloaded(
    deps: Deps, api_client, monkeypatch, caplog, corruption: str, historical: bool
) -> None:
    import hashlib
    import zlib
    from io import BytesIO

    import sqlalchemy as sa
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, EncodedStreamObject, NameObject
    from test_admin import finish

    from app.models import Artifact

    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    stream = EncodedStreamObject()
    stream._data = (
        b"PRIVATE-invalid-zlib-stream"
        if corruption == "invalid"
        else zlib.compress(b"q 1 0 0 1 0 0 cm Q")[:-2]
    )
    stream[NameObject("/Filter")] = NameObject("/FlateDecode")
    page[NameObject("/Contents")] = ArrayObject([writer._add_object(stream)])
    buffer = BytesIO()
    writer.write(buffer)
    content = buffer.getvalue()
    with pytest.raises(zlib.error):
        zlib.decompress(stream._data)
    job = submit(deps).job
    if historical:
        finish(deps, job.id)
        with deps.sessions.begin() as session:
            artifact = session.scalar(sa.select(Artifact).where(Artifact.job_id == job.id))
            deps.storage.put(artifact.storage_key, content, "application/pdf")
            artifact.byte_size = len(content)
            artifact.sha256 = hashlib.sha256(content).hexdigest()
            artifact.verified_at = artifact.storage_revision = None

        def forbidden(*args, **kwargs):
            raise AssertionError("histórico não pode gerar novamente")

        monkeypatch.setattr(deps.llm, "generate", forbidden)
        monkeypatch.setattr(deps.pdf, "render", forbidden)
        client = api_client("total")
        response = client.get(f"/admin/api/v1/jobs/{job.public_id}/pdf")
        assert response.status_code == 502
        result = client.get(f"/admin/api/v1/jobs/{job.public_id}").json()
        assert result["status"] == "completed"
        assert result["current_pdf"]["available"] is False
        assert result["current_pdf"]["verified_at"] is None
        assert "PRIVATE" not in response.text + caplog.text
    else:
        original = deps.pdf

        class CorruptRenderer:
            def render(self, spec, document, context):
                return replace(original.render(spec, document, context), content=content)

        deps.pdf = CorruptRenderer()
        with deps.sessions.begin() as session:
            jobs.claim(session, job.id, 1, "synthetic")
        plan, _ = pipeline.generate_plan(deps, job.id, 1)
        with pytest.raises(TerminalError, match="PDF estruturalmente inválido"):
            artifact, _ = pipeline.render_pdf(deps, job.id, 1, plan)
            pipeline.finalize(deps, job.id, 1, artifact)
        assert "PRIVATE" not in caplog.text


@pytest.mark.parametrize("chain", [False, True])
def test_valid_flate_content_remains_available_with_optional_filter_chain(deps: Deps, chain: bool):
    import zlib
    from base64 import a85encode
    from io import BytesIO

    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, EncodedStreamObject, NameObject

    from app.models import Job
    from app.pdf_availability import current_pdf

    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    stream = EncodedStreamObject()
    stream._data = zlib.compress(b"q 1 0 0 1 0 0 cm Q")
    filters = [NameObject("/FlateDecode")]
    if chain:
        stream._data = a85encode(stream._data) + b"~>"
        filters.insert(0, NameObject("/ASCII85Decode"))
    stream[NameObject("/Filter")] = ArrayObject(filters)
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = BytesIO()
    writer.write(buffer)
    original = deps.pdf

    class CompressedRenderer:
        def render(self, spec, document, context):
            return replace(original.render(spec, document, context), content=buffer.getvalue())

    deps.pdf = CompressedRenderer()
    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "synthetic")
    plan, _ = pipeline.generate_plan(deps, job.id, 1)
    artifact, _ = pipeline.render_pdf(deps, job.id, 1, plan)
    pipeline.finalize(deps, job.id, 1, artifact)
    with deps.sessions() as session:
        assert current_pdf(session, deps.storage, session.get_one(Job, job.id)).available


@pytest.mark.parametrize("probe_support", ["absent", "unsupported"])
def test_metadata_without_read_proof_keeps_access_unknown(deps: Deps, probe_support: str):
    from types import SimpleNamespace

    from test_admin import finish

    from app.models import Job
    from app.pdf_availability import current_pdf

    job = submit(deps).job
    finish(deps, job.id)
    storage = SimpleNamespace(head=deps.storage.head)
    if probe_support == "unsupported":

        def unsupported(key):
            raise NotImplementedError

        storage.probe = unsupported
    with deps.sessions() as session:
        state = current_pdf(session, storage, session.get_one(Job, job.id))
    assert state.state == "unknown" and state.accessible is None and not state.available
