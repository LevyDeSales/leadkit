"""Storage em S3 (STORAGE_MODE=s3) sem rede: o bucket é emulado pelo moto e erros do provedor vêm
do Stubber do botocore."""

from __future__ import annotations

import hashlib
import socket
import traceback
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
import requests
from botocore.config import Config
from botocore.stub import Stubber
from conftest import record_webhook
from fastapi.testclient import TestClient
from moto import mock_aws
from test_pipeline import run_until_finalize

from app import pipeline, retention
from app.adapters.pdf import minimal_pdf
from app.adapters.s3_storage import S3Storage
from app.api.main import create_app
from app.deps import Deps, build_storage
from app.errors import FailureCode, RetryableError, TerminalError
from app.models import Job, utcnow
from app.settings import Settings

BUCKET = "planos-teste"
REGION = "sa-east-1"
KEY = "plans/0b7c6a52-3f7e-4f7e-9a59-3c1f6f0f2a10/v1-0123456789ab.pdf"
PDF = minimal_pdf("Plano de Contadora Exemplo")


def s3_settings(**env: str) -> Settings:
    values = {
        "STORAGE_MODE": "s3",
        "S3_BUCKET": BUCKET,
        "S3_REGION": REGION,
        "S3_ACCESS_KEY_ID": "AKIATESTE",
        "S3_SECRET_ACCESS_KEY": "segredo-de-teste",
        **env,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def s3_client(**options: Any) -> Any:
    return boto3.client(
        "s3",
        region_name=REGION,
        aws_access_key_id="AKIATESTE",
        aws_secret_access_key="segredo-de-teste",
        **options,
    )


@pytest.fixture
def bucket() -> Iterator[Any]:
    """Bucket emulado; devolve um cliente boto3 para conferir o que chegou nele."""
    with mock_aws():
        client = s3_client()
        client.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        yield client


def test_s3_mode_stores_the_pdf_in_the_bucket_with_its_content_type(bucket: Any) -> None:
    storage = build_storage(s3_settings())  # o que a API e o worker montam com STORAGE_MODE=s3

    storage.put(KEY, PDF, "application/pdf")

    assert storage.get(KEY) == PDF
    assert bucket.head_object(Bucket=BUCKET, Key=KEY)["ContentType"] == "application/pdf"


def test_presigned_url_downloads_the_pdf_as_named_attachment(bucket: Any) -> None:
    storage = S3Storage.from_settings(s3_settings())
    # Metadado genérico de propósito: o link sempre entrega como PDF.
    storage.put(KEY, PDF, "binary/octet-stream")

    url = storage.presigned_url(KEY, ttl_seconds=300, filename="plano-Ab3_x-9.pdf")

    assert url is not None
    download = requests.get(url, timeout=5)  # o moto atende o GET do link
    assert download.status_code == 200 and download.content == PDF
    assert download.headers["Content-Type"] == "application/pdf"
    assert download.headers["Content-Disposition"] == 'attachment; filename="plano-Ab3_x-9.pdf"'


def test_aws_links_are_sigv4_on_the_regional_endpoint_and_expire_as_requested() -> None:
    # O moto aceita qualquer assinatura; um bucket real não: SigV2 é recusada por buckets novos e
    # o endpoint global responde 307 para buckets recém-criados fora de us-east-1, o que invalida
    # a assinatura (o host é assinado).
    url = S3Storage.from_settings(s3_settings()).presigned_url(
        KEY, ttl_seconds=300, filename="plano.pdf"
    )

    assert url is not None
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert parts.netloc == "planos-teste.s3.sa-east-1.amazonaws.com"
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Expires"] == ["300"]
    assert query["X-Amz-Credential"][0].startswith("AKIATESTE/")


def test_custom_endpoint_links_point_at_the_endpoint_with_the_bucket_in_the_path() -> None:
    storage = S3Storage.from_settings(
        s3_settings(S3_ENDPOINT_URL="http://minio:9000", S3_ACCESS_KEY_ID="minio-chave")
    )

    url = storage.presigned_url(KEY, ttl_seconds=60, filename="plano.pdf")

    assert url is not None
    assert url.startswith(f"http://minio:9000/{BUCKET}/{KEY}?")
    assert parse_qs(urlsplit(url).query)["X-Amz-Credential"][0].startswith("minio-chave/")


def test_empty_credentials_fall_back_to_the_default_aws_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIADACADEIA")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "segredo-da-cadeia")
    # Como no deploy/.env.example: variáveis presentes, mas vazias.
    storage = S3Storage.from_settings(
        s3_settings(S3_ACCESS_KEY_ID="", S3_SECRET_ACCESS_KEY="", S3_ENDPOINT_URL="")
    )

    url = storage.presigned_url(KEY, ttl_seconds=60, filename="plano.pdf")

    assert url is not None
    parts = urlsplit(url)
    assert parts.netloc == "planos-teste.s3.sa-east-1.amazonaws.com"
    assert parse_qs(parts.query)["X-Amz-Credential"][0].startswith("AKIADACADEIA/")


def test_missing_bucket_or_object_is_a_terminal_storage_failure(bucket: Any) -> None:
    with pytest.raises(TerminalError) as missing_bucket:
        S3Storage.from_settings(s3_settings(S3_BUCKET="nao-existe")).put(
            KEY, PDF, "application/pdf"
        )
    with pytest.raises(TerminalError) as missing_object:
        S3Storage.from_settings(s3_settings()).get(KEY)

    assert missing_bucket.value.code is FailureCode.STORAGE_FAILED
    assert missing_object.value.code is FailureCode.STORAGE_FAILED


@pytest.mark.parametrize(
    ("code", "status", "expected"),
    [
        ("SlowDown", 503, RetryableError),
        ("InternalError", 500, RetryableError),
        ("TooManyRequests", 429, RetryableError),
        ("RequestTimeout", 400, RetryableError),
        ("AccessDenied", 403, TerminalError),
    ],
)
def test_provider_errors_become_storage_failed(
    code: str, status: int, expected: type[RetryableError | TerminalError]
) -> None:
    client = s3_client()
    with Stubber(client) as stubber:
        stubber.add_client_error("put_object", service_error_code=code, http_status_code=status)
        with pytest.raises(expected) as caught:
            S3Storage(client, BUCKET).put(KEY, PDF, "application/pdf")

    assert caught.value.code is FailureCode.STORAGE_FAILED
    assert code in str(caught.value)


def test_delete_removes_the_pdf_and_repeating_it_is_harmless(bucket: Any) -> None:
    storage = build_storage(s3_settings())
    storage.put(KEY, PDF, "application/pdf")

    storage.delete(KEY)
    storage.delete(KEY)  # a retenção pode repetir depois de uma falha

    assert bucket.list_objects_v2(Bucket=BUCKET)["KeyCount"] == 0


def test_a_key_without_delete_permission_is_a_terminal_storage_failure() -> None:
    client = s3_client()
    with Stubber(client) as stubber:
        stubber.add_client_error(
            "delete_object", service_error_code="AccessDenied", http_status_code=403
        )
        with pytest.raises(TerminalError) as caught:
            S3Storage(client, BUCKET).delete(KEY)

    assert caught.value.code is FailureCode.STORAGE_FAILED
    assert "delete_object: AccessDenied" in str(caught.value)


def test_missing_credentials_are_a_terminal_storage_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nao/existe")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nao/existe")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    storage = S3Storage.from_settings(s3_settings(S3_ACCESS_KEY_ID="", S3_SECRET_ACCESS_KEY=""))

    with pytest.raises(TerminalError) as on_put:
        storage.put(KEY, PDF, "application/pdf")
    with pytest.raises(TerminalError) as on_link:
        storage.presigned_url(KEY, ttl_seconds=60, filename="plano.pdf")

    assert on_put.value.code is FailureCode.STORAGE_FAILED
    assert on_link.value.code is FailureCode.STORAGE_FAILED


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_network_failure_is_retryable_and_the_recorded_error_has_no_pdf_content() -> None:
    client = s3_client(
        endpoint_url=f"http://127.0.0.1:{_closed_port()}",
        config=Config(retries={"max_attempts": 1}),
    )

    with pytest.raises(RetryableError) as caught:
        S3Storage(client, BUCKET).put(KEY, PDF, "application/pdf")

    assert caught.value.code is FailureCode.STORAGE_FAILED
    recorded = "".join(traceback.format_exception(caught.value))  # o que o Hatchet grava
    assert "Contadora Exemplo" not in recorded


def test_lead_pdf_link_redirects_to_a_short_presigned_download(deps: Deps, bucket: Any) -> None:
    deps.storage = S3Storage.from_settings(s3_settings())
    job_id, _ = run_until_finalize(deps)
    with deps.sessions() as session:
        job = session.get_one(Job, job_id)
    client = TestClient(create_app(trigger=None))

    response = client.get(pipeline.lead_links(deps, job)["pdf_url"], follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    location = response.headers["location"]
    assert parse_qs(urlsplit(location).query)["X-Amz-Expires"] == ["300"]
    download = requests.get(location, timeout=5)
    assert download.status_code == 200 and download.content.startswith(b"%PDF")
    assert download.headers["Content-Type"] == "application/pdf"
    assert download.headers["Content-Disposition"] == (
        f'attachment; filename="plano-{job.public_id}.pdf"'
    )


def test_webhook_payload_carries_a_24h_direct_link_to_the_same_pdf(deps: Deps, bucket: Any) -> None:
    deps.storage = S3Storage.from_settings(s3_settings())
    webhook = record_webhook(deps)
    _, deliveries = run_until_finalize(deps)

    pipeline.deliver(deps, deliveries[0])

    pdf = webhook.calls[0][2]["pdf"]
    assert pdf["url"].startswith("http://testserver/v1/jobs/")  # o link estável segue no app
    assert pdf["direct_url_expires_in"] == 86400
    assert parse_qs(urlsplit(pdf["direct_url"]).query)["X-Amz-Expires"] == ["86400"]
    download = requests.get(pdf["direct_url"], timeout=5)
    assert hashlib.sha256(download.content).hexdigest() == pdf["sha256"]


def test_retention_empties_the_bucket_and_a_later_delivery_has_no_direct_link(
    deps: Deps, bucket: Any
) -> None:
    deps.storage = S3Storage.from_settings(s3_settings())
    webhook = record_webhook(deps)
    _, deliveries = run_until_finalize(deps)

    report = retention.purge_expired(deps, now=utcnow() + timedelta(days=31))  # só o PDF saiu
    pipeline.deliver(deps, deliveries[0])  # reenvio feito semanas depois

    assert report.pdfs_deleted == 1 and bucket.list_objects_v2(Bucket=BUCKET)["KeyCount"] == 0
    pdf = webhook.calls[0][2]["pdf"]
    assert (pdf["direct_url"], pdf["direct_url_expires_in"]) == (None, None)
