import json
from pathlib import Path

import pytest

from cardscanr_search_index.worldwide_publication import (
    ACTIVE_MANIFEST_KEY,
    PublishResult,
    activate_version,
    build_manifest,
    immutable_database_key,
    immutable_manifest_key,
    public_url,
    rollback_manifest_key,
)


class _Body:
    def __init__(self, value: bytes) -> None:
        self.value = value

    def read(self) -> bytes:
        return self.value


class _FakeClient:
    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def get_object(self, *, Bucket: str, Key: str):  # noqa: N803
        del Bucket
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey()
        return {"Body": _Body(self.objects[Key])}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **kwargs):  # noqa: N803
        del Bucket, kwargs
        self.objects[Key] = Body


def test_worldwide_publication_keys_are_immutable_and_versioned() -> None:
    digest = "a" * 64
    assert immutable_database_key(digest) == f"v2/catalog/pokemon/search/versions/{digest}/catalogue.sqlite"
    assert immutable_manifest_key(digest) == f"v2/catalog/pokemon/search/versions/{digest}/manifest.json"
    assert rollback_manifest_key(digest) == f"v2/catalog/pokemon/search/rollbacks/{digest}/manifest.json"
    assert ACTIVE_MANIFEST_KEY == "v2/catalog/pokemon/search/catalogue.manifest.json"


def test_worldwide_publication_rejects_non_https_and_invalid_digest() -> None:
    with pytest.raises(ValueError):
        immutable_database_key("bad")
    with pytest.raises(ValueError):
        public_url("http://example.test", "catalogue.sqlite")


def test_worldwide_manifest_includes_products_and_rollback(tmp_path: Path) -> None:
    database = tmp_path / "worldwide.sqlite"
    database.write_bytes(b"sqlite")
    summary = {
        "sha256": "b" * 64,
        "byteSize": 6,
        "records": 5,
        "perLanguageCounts": {"en": 3, "ja": 2},
        "products": 4,
        "productContents": 7,
        "perLanguageRegionProductCounts": {"en:US": 4},
        "cardImages": 3,
        "productImages": 4,
    }
    previous = {"databaseUrl": "https://example.test/old.sqlite", "sha256": "c" * 64}
    manifest = build_manifest(
        database_path=database,
        database_summary=summary,
        r2_public_base_url="https://catalog.example.test",
        generated_at="2026-08-02T00:00:00Z",
        previous_manifest=previous,
    )
    assert manifest["searchIndexSchemaVersion"] == "2.1.0"
    assert manifest["totalCardCount"] == 5
    assert manifest["totalSealedProductCount"] == 4
    assert manifest["totalSealedProductContentCount"] == 7
    assert manifest["previousDatabaseUrl"] == previous["databaseUrl"]
    assert manifest["previousSha256"] == previous["sha256"]


def test_first_v2_activation_retains_supplied_v1_manifest() -> None:
    digest = "d" * 64
    previous = {"databaseUrl": "https://example.test/old.sqlite", "sha256": "e" * 64}
    published = PublishResult(
        classification="PASS",
        manifest={"databaseUrl": "https://example.test/new.sqlite", "sha256": digest},
        database_key=immutable_database_key(digest),
        manifest_key=immutable_manifest_key(digest),
        database_upload="uploaded",
        manifest_upload="uploaded_and_verified",
        public_verification={"classification": "PASS"},
        previous_manifest=previous,
    )
    client = _FakeClient()
    activated = activate_version(published=published, client=client, bucket="bucket")
    assert activated.classification == "PASS"
    assert activated.active_manifest_key == ACTIVE_MANIFEST_KEY
    assert activated.rollback_manifest_key == rollback_manifest_key(previous["sha256"])
    retained = json.loads(client.objects[activated.rollback_manifest_key].decode("utf-8"))
    assert retained == previous
