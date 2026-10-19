"""As imagens dos formulários no storage: chave pelo conteúdo, conferência do sha256 e cache."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from app.adapters.assets import AssetStore, AssetUnavailable, _Cache
from app.adapters.s3_storage import S3Storage
from app.adapters.storage import LocalStorage
from app.errors import FailureCode, TerminalError
from app.forms.definition.images import AssetRef

BUCKET = "imagens-teste"
REGION = "sa-east-1"
CONTENT = b"RIFF....WEBPimagem de teste"
REF = AssetRef(hashlib.sha256(CONTENT).hexdigest(), "image/webp")


def store(tmp_path: Path, cache: _Cache | None = None) -> tuple[AssetStore, LocalStorage]:
    storage = LocalStorage(tmp_path / "bucket")
    return AssetStore(storage, cache or _Cache()), storage


def test_the_image_is_stored_under_the_key_of_its_content(tmp_path: Path) -> None:
    assets, storage = store(tmp_path)

    assets.put(REF, CONTENT)

    assert REF.key == f"assets/{REF.sha256}.webp"
    assert storage.get(REF.key) == CONTENT


def test_storing_the_same_image_twice_is_harmless(tmp_path: Path) -> None:
    assets, storage = store(tmp_path)

    assets.put(REF, CONTENT)
    assets.put(REF, CONTENT)

    assert storage.get(REF.key) == CONTENT
    assert len(list((tmp_path / "bucket" / "assets").iterdir())) == 1


def test_bytes_that_are_not_the_ones_of_the_reference_are_never_stored(tmp_path: Path) -> None:
    assets, _ = store(tmp_path)

    with pytest.raises(ValueError, match="referência"):
        assets.put(REF, CONTENT + b"x")

    assert not list((tmp_path / "bucket").rglob("*"))


def test_reading_gives_back_what_was_stored(tmp_path: Path) -> None:
    assets, _ = store(tmp_path)
    assets.put(REF, CONTENT)

    assert AssetStore(LocalStorage(tmp_path / "bucket"), _Cache()).read(REF) == CONTENT


def test_an_image_the_storage_does_not_have_is_unavailable(tmp_path: Path) -> None:
    assets, _ = store(tmp_path)

    with pytest.raises(AssetUnavailable):
        assets.read(REF)


def test_a_file_that_does_not_match_its_key_is_refused(tmp_path: Path) -> None:
    assets, storage = store(tmp_path)
    storage.put(REF.key, b"outra coisa", REF.content_type)

    with pytest.raises(AssetUnavailable, match="não é o da imagem"):
        assets.read(REF)


def test_the_cache_serves_an_image_that_was_read_before(tmp_path: Path) -> None:
    assets, storage = store(tmp_path)
    assets.put(REF, CONTENT)
    assets.read(REF)
    storage.delete(REF.key)

    assert assets.read(REF) == CONTENT


def test_the_cache_is_shared_by_content_between_stores_that_use_it(tmp_path: Path) -> None:
    shared = _Cache()
    first, _ = store(tmp_path / "um", shared)
    second = AssetStore(LocalStorage(tmp_path / "dois"), shared)
    first.put(REF, CONTENT)

    assert second.read(REF) == CONTENT  # o segundo storage está vazio: veio do cache


def test_the_cache_drops_the_least_used_image_when_it_passes_its_limit() -> None:
    cache = _Cache(max_bytes=10)
    cache.add("a", b"12345")
    cache.add("b", b"12345")
    assert cache.get("a") == b"12345"  # a mais usada agora é a "a"

    cache.add("c", b"12345")

    assert cache.get("b") is None and cache.get("a") == b"12345" and cache.get("c") == b"12345"


def test_an_image_bigger_than_the_whole_cache_is_not_kept() -> None:
    cache = _Cache(max_bytes=4)

    cache.add("a", b"12345")

    assert cache.get("a") is None


def test_discarding_deletes_the_file_and_forgets_it(tmp_path: Path) -> None:
    assets, _ = store(tmp_path)
    assets.put(REF, CONTENT)

    assets.discard(REF)

    with pytest.raises(AssetUnavailable):
        assets.read(REF)
    assets.discard(REF)  # apagar de novo não é erro


# --- no S3 --------------------------------------------------------------------------------------


@pytest.fixture
def bucket() -> Iterator[Any]:
    with mock_aws():
        client = boto3.client(
            "s3",
            region_name=REGION,
            aws_access_key_id="AKIATESTE",
            aws_secret_access_key="segredo-de-teste",
        )
        client.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        yield client


def test_in_the_bucket_the_image_has_its_type_and_an_absent_one_is_a_storage_failure(
    bucket: Any,
) -> None:
    assets = AssetStore(S3Storage(bucket, BUCKET), _Cache())

    with pytest.raises(TerminalError) as missing:
        assets.read(REF)
    assets.put(REF, CONTENT)

    assert missing.value.code == FailureCode.STORAGE_FAILED
    assert bucket.head_object(Bucket=BUCKET, Key=REF.key)["ContentType"] == "image/webp"
    assert assets.read(REF) == CONTENT
