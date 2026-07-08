#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent

from cardscanr_search_index.publication import (
    MINIMUM_COMPATIBLE_APP_VERSION,
    PAGES_MAX_ASSET_BYTES,
    PublicationConfig,
    assert_pages_publish_safe,
    build_runtime_manifest,
    immutable_database_filename,
    publish_search_index,
    r2_object_key,
    r2_public_url,
    verify_local_database,
)
from cardscanr_search_index.builder import build_search_index


class PublicationHelpersTest(unittest.TestCase):
    def test_immutable_filename_generation(self) -> None:
        digest = "a" * 64
        self.assertEqual(
            immutable_database_filename(digest),
            f"catalog_search_v1.{digest}.sqlite",
        )

    def test_immutable_filename_rejects_invalid_digest(self) -> None:
        with self.assertRaises(ValueError):
            immutable_database_filename("not-a-hash")

    def test_manifest_r2_url_generation(self) -> None:
        digest = "b" * 64
        filename = immutable_database_filename(digest)
        key = r2_object_key(filename)
        url = r2_public_url("https://catalog.example.test", key)
        self.assertEqual(
            url,
            f"https://catalog.example.test/v1/catalog/pokemon/search/{filename}",
        )

    def test_minimum_compatible_version_requirement(self) -> None:
        manifest = build_runtime_manifest(
            current_sha256="c" * 64,
            current_byte_size=123,
            current_database_url="https://catalog.example.test/v1/catalog/pokemon/search/x.sqlite",
            current_database_filename="x.sqlite",
            generated_at="2026-07-08T00:00:00Z",
            previous_database_url=None,
            previous_sha256=None,
            total_card_count=1,
            per_language_counts={"en": 1, "jp": 0},
        )
        self.assertEqual(manifest["minimumCompatibleAppVersion"], "1.0.0+21")
        self.assertNotIn("minimumCompatibleAppVersionStatus", manifest)
        self.assertNotIn("sourceCatalogueHashes", manifest)

    def test_refuses_oversized_pages_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public_dir = root / "public"
            search_dir = public_dir / "v1" / "catalog" / "pokemon" / "search"
            search_dir.mkdir(parents=True)
            blocked = search_dir / "catalog_search_v1.sqlite"
            blocked.write_bytes(b"x" * (PAGES_MAX_ASSET_BYTES + 1))
            subprocess.run(["git", "init"], cwd=root, capture_output=True, check=False)
            subprocess.run(["git", "add", "public"], cwd=root, capture_output=True, check=False)
            issues = assert_pages_publish_safe(public_dir, root=root)
            self.assertTrue(any("oversized_tracked_search_index_asset" in issue for issue in issues))

    def test_refuses_oversized_tracked_public_assets_outside_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public_dir = root / "public"
            blocked = public_dir / "v1" / "images" / "cards-manifest.json"
            blocked.parent.mkdir(parents=True)
            blocked.write_bytes(b"x" * (PAGES_MAX_ASSET_BYTES + 1))
            subprocess.run(["git", "init"], cwd=root, capture_output=True, check=False)
            subprocess.run(["git", "add", "public"], cwd=root, capture_output=True, check=False)
            issues = assert_pages_publish_safe(public_dir, root=root)
            self.assertTrue(any("oversized_tracked_pages_asset" in issue for issue in issues))

    def test_refuses_manifest_publication_before_r2_object_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "public" / "v1" / "catalog" / "pokemon" / "search"
            output_dir.mkdir(parents=True)
            db_path = output_dir / "catalog_search_v1.sqlite"
            db_path.write_bytes(b"sqlite")
            (output_dir / "catalog_search_v1.sha256").write_text("f" * 64 + "\n", encoding="utf-8")
            config = PublicationConfig(
                account_id="acct",
                r2_bucket="cardscanr-catalog",
                r2_public_base_url="https://catalog.example.test",
                pages_base_url="https://pages.example.test",
                r2_s3_endpoint="https://acct.r2.cloudflarestorage.com",
                r2_access_key_id="key",
                r2_secret_access_key="secret",
            )
            local_verification = mock.Mock(
                database_path=db_path,
                sha256="f" * 64,
                byte_size=db_path.stat().st_size,
                total_cards=74578,
                per_language_counts={"en": 46417, "jp": 28161},
                passed=True,
                issues=[],
            )
            with mock.patch(
                "cardscanr_search_index.publication.verify_local_database",
                return_value=local_verification,
            ), mock.patch(
                "cardscanr_search_index.publication.ensure_r2_bucket",
                return_value=(True, "bucket_accessible"),
            ), mock.patch(
                "cardscanr_search_index.publication.upload_r2_object",
                return_value=(False, "upload_failed"),
            ):
                report = publish_search_index(
                    output_dir=output_dir,
                    public_dir=root / "public",
                    config=config,
                    root=root,
                    dry_run=False,
                    skip_tests=True,
                    skip_live_verification=True,
                )
            self.assertEqual(report.classification, "FAIL")
            self.assertIn("manifest_publication_blocked_pending_r2_verification", report.unresolved_issues)

    def test_rollback_metadata(self) -> None:
        digest = "d" * 64
        previous_digest = "e" * 64
        manifest = build_runtime_manifest(
            current_sha256=digest,
            current_byte_size=100,
            current_database_url=r2_public_url("https://catalog.example.test", r2_object_key(immutable_database_filename(digest))),
            current_database_filename=immutable_database_filename(digest),
            generated_at="2026-07-08T00:00:00Z",
            previous_database_url=r2_public_url(
                "https://catalog.example.test",
                r2_object_key(immutable_database_filename(previous_digest)),
            ),
            previous_sha256=previous_digest,
            total_card_count=10,
            per_language_counts={"en": 6, "jp": 4},
        )
        self.assertTrue(str(manifest["previousDatabaseUrl"]).startswith("https://"))
        self.assertEqual(manifest["previousSha256"], previous_digest)
        self.assertIn("rollbackPolicy", manifest)

    def test_checksum_mismatch_detected_locally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "public" / "v1" / "catalog" / "pokemon" / "search"
            output_dir.mkdir(parents=True)
            db_path = output_dir / "catalog_search_v1.sqlite"
            db_path.write_bytes(b"sqlite")
            sidecar = output_dir / "catalog_search_v1.sha256"
            sidecar.write_text("0" * 64 + "\n", encoding="utf-8")
            config = PublicationConfig(
                account_id="acct",
                r2_bucket="cardscanr-catalog",
                r2_public_base_url="https://catalog.example.test",
                pages_base_url="https://pages.example.test",
                r2_s3_endpoint="https://acct.r2.cloudflarestorage.com",
                r2_access_key_id="key",
                r2_secret_access_key="secret",
            )
            local_verification = mock.Mock(
                database_path=db_path,
                sha256="f" * 64,
                byte_size=db_path.stat().st_size,
                total_cards=74578,
                per_language_counts={"en": 46417, "jp": 28161},
                passed=True,
                issues=[],
            )
            with mock.patch(
                "cardscanr_search_index.publication.verify_local_database",
                return_value=local_verification,
            ):
                report = publish_search_index(
                    output_dir=output_dir,
                    public_dir=root / "public",
                    config=config,
                    root=root,
                    dry_run=True,
                    skip_tests=True,
                    skip_live_verification=True,
                )
            self.assertIn("sha256_sidecar_mismatch", report.unresolved_issues)

    def test_config_normalization_moves_s3_endpoint(self) -> None:
        from cardscanr_search_index.publication import _normalize_config_payload

        normalized = _normalize_config_payload(
            {
                "accountId": "acct123",
                "r2Bucket": "cardscanr-catalog",
                "r2PublicBaseUrl": "https://acct123.r2.cloudflarestorage.com",
                "r2PublicDevUrl": "https://pub-bucketid.r2.dev",
                "r2AccessKeyId": "key",
                "r2SecretAccessKey": "secret",
            }
        )
        self.assertEqual(normalized["r2_s3_endpoint"], "https://acct123.r2.cloudflarestorage.com")
        self.assertEqual(normalized["r2_public_base_url"], "https://pub-bucketid.r2.dev")

    def test_idempotent_existing_object_handling(self) -> None:
        from cardscanr_search_index.publication import upload_r2_object

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_path = root / "db.sqlite"
            local_path.write_bytes(b"sqlite-bytes")
            config = PublicationConfig(
                account_id="acct",
                r2_bucket="cardscanr-catalog",
                r2_public_base_url="https://catalog.example.test",
                pages_base_url="https://pages.example.test",
                r2_s3_endpoint="https://acct.r2.cloudflarestorage.com",
                r2_access_key_id="key",
                r2_secret_access_key="secret",
            )
            with mock.patch(
                "cardscanr_search_index.publication._r2_object_matches",
                return_value=(True, "verified_existing_object"),
            ) as matches:
                ok, message = upload_r2_object(
                    config=config,
                    local_path=local_path,
                    object_key="v1/catalog/pokemon/search/test.sqlite",
                    root=root,
                    dry_run=False,
                    expected_sha256="f" * 64,
                    expected_size=local_path.stat().st_size,
                )
            self.assertTrue(ok)
            self.assertIn("idempotent_existing_object", message)
            matches.assert_called_once()


def _write_min_catalogue(root: Path) -> None:
    en_sets = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": "2026-01-01T00:00:00Z",
        "sets": [{"id": "base1", "name": "Base", "total": 1, "printedTotal": 1}],
    }
    jp_sets = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": "2026-01-01T00:00:00Z",
        "sets": [{"id": "SV10", "name": "Test", "total": 1, "printedTotal": 1}],
    }
    en_cards = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": "2026-01-01T00:00:00Z",
        "setId": "base1",
        "setName": "Base",
        "cards": [
            {
                "canonicalBaseId": "pokemon|en|base1|4|charizard",
                "language": "en",
                "setId": "base1",
                "collectorNumber": "4",
                "name": "Charizard",
                "normalizedName": "charizard",
                "imageSource": "pokemon_tcg_api",
                "imageCached": False,
                "providerIds": {"pokemonTcgApi": "base1-4"},
            }
        ],
    }
    jp_cards = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": "2026-01-01T00:00:00Z",
        "setId": "SV10",
        "setName": "Test",
        "cards": [
            {
                "canonicalBaseId": "pokemon|jp|SV10|001|test",
                "language": "jp",
                "setId": "SV10",
                "collectorNumber": "001",
                "name": "テスト",
                "normalizedName": "test",
                "imageSource": "pokemon_tcg_api",
                "imageCached": False,
                "providerIds": {"pokemonTcgApi": "sv10-001"},
            }
        ],
    }
    (root / "catalog" / "pokemon" / "en").mkdir(parents=True)
    (root / "catalog" / "pokemon" / "jp").mkdir(parents=True)
    (root / "catalog" / "pokemon" / "en" / "sets.json").write_text(json.dumps(en_sets), encoding="utf-8")
    (root / "catalog" / "pokemon" / "jp" / "sets.json").write_text(json.dumps(jp_sets), encoding="utf-8")
    (root / "catalog" / "pokemon" / "en" / "cards").mkdir()
    (root / "catalog" / "pokemon" / "jp" / "cards").mkdir()
    (root / "catalog" / "pokemon" / "en" / "cards" / "base1.json").write_text(json.dumps(en_cards), encoding="utf-8")
    (root / "catalog" / "pokemon" / "jp" / "cards" / "SV10.json").write_text(json.dumps(jp_cards), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
