"""PDFs num bucket S3-compatível (AWS S3, Cloudflare R2, MinIO). Carregado com STORAGE_MODE=s3.

A chave vem do pipeline (job, tentativa e hash). Os links pré-assinados forçam download
(`attachment`) como `application/pdf`; com credencial temporária (role, SSO) um link expira junto
com a credencial, antes do prazo pedido.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, HTTPClientError
from botocore.exceptions import ConnectionError as BotocoreConnectionError

from app.errors import FailureCode, RetryableError, TerminalError
from app.settings import Settings


def _is_transient(code: str, status: int) -> bool:
    # 5xx, 408 e 429 (inclui o SlowDown do S3) e o RequestTimeout (400 do S3 quando o upload
    # para no meio). O resto (AccessDenied, NoSuchBucket...) é configuração.
    return status >= 500 or status in (408, 429) or code == "RequestTimeout"


@contextmanager
def _provider_errors(operation: str) -> Iterator[None]:
    """Traduz falhas do S3; a mensagem leva só a operação, o código e o status HTTP."""
    try:
        yield
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "?")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0
        message = f"{operation}: {code} (HTTP {status})"
        if _is_transient(code, status):
            raise RetryableError(FailureCode.STORAGE_FAILED, message) from exc
        raise TerminalError(FailureCode.STORAGE_FAILED, message) from exc
    except BotoCoreError as exc:
        message = f"{operation}: {type(exc).__name__}"
        if isinstance(exc, BotocoreConnectionError | HTTPClientError):  # conexão, timeout, TLS
            raise RetryableError(FailureCode.STORAGE_FAILED, message) from exc
        raise TerminalError(FailureCode.STORAGE_FAILED, message) from exc  # sem credenciais etc.


class S3Storage:
    def __init__(self, client: Any, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    @classmethod
    def from_settings(cls, settings: Settings) -> S3Storage:
        if not settings.s3_bucket:
            raise ValueError("STORAGE_MODE=s3 exige S3_BUCKET")
        endpoint = settings.s3_endpoint_url or None
        secret = settings.s3_secret_access_key
        config = Config(
            # Sem isto o botocore assina links com SigV2 (recusada por buckets novos) e usa o
            # endpoint global. R2/MinIO: bucket no caminho (`bucket.minio` não resolve no compose).
            signature_version="s3v4",
            s3={"addressing_style": "path" if endpoint else "virtual"},
            connect_timeout=5,
            read_timeout=15,
            retries={"max_attempts": 3, "mode": "standard"},
        )
        client = boto3.session.Session().client(
            "s3",
            region_name=settings.s3_region,
            endpoint_url=endpoint,
            aws_access_key_id=settings.s3_access_key_id or None,
            aws_secret_access_key=secret.get_secret_value() if secret else None,
            config=config,
        )
        return cls(client, settings.s3_bucket)

    def put(self, key: str, content: bytes, content_type: str) -> None:
        with _provider_errors("put_object"):
            self.client.put_object(
                Bucket=self.bucket, Key=key, Body=content, ContentType=content_type
            )

    def get(self, key: str) -> bytes:
        with _provider_errors("get_object"):
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        """Chave inexistente não é erro no S3. Em bucket com versionamento isto só cria um
        marcador de exclusão: as versões antigas precisam de regra de ciclo de vida."""
        with _provider_errors("delete_object"):
            self.client.delete_object(Bucket=self.bucket, Key=key)

    def presigned_url(self, key: str, *, ttl_seconds: int, filename: str) -> str | None:
        with _provider_errors("presign_get_object"):
            return self.client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self.bucket,
                    "Key": key,
                    "ResponseContentDisposition": f'attachment; filename="{filename}"',
                    "ResponseContentType": "application/pdf",
                },
                ExpiresIn=ttl_seconds,
            )
