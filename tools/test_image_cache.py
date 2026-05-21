#!/usr/bin/env python3
"""Lightweight tests for CardScanR image manifest/cache tooling."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_image_cache as image_cache  # noqa: E402
import report_dataset_coverage as coverage  # noqa: E402
import validate_cache as validator  # noqa: E402


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def write_sample_catalog(v1_dir: Path) -> None:
    payload = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": "2026-05-22T00:00:00Z",
        "game": "pokemon",
        "language": "en",
        "setId": "base1",
        "setName": "Base",
        "source": "pokemon_tcg_api",
        "catalogueStatus": "built",
        "cardCount": 1,
        "cards": [
            {
                "canonicalBaseId": "pokemon|en|base1|1|alakazam",
                "game": "pokemon",
                "language": "en",
                "setId": "base1",
                "setName": "Base",
                "collectorNumber": "1",
                "name": "Alakazam",
                "normalizedName": "alakazam",
                "rarity": "Rare Holo",
                "supertype": "Pokemon",
                "subtypes": ["Stage 2"],
                "types": ["Psychic"],
                "hp": "80",
                "artist": "Ken Sugimori",
                "imageSmall": "https://images.example.test/base1/1.png",
                "imageLarge": "https://images.example.test/base1/1_hires.png",
                "imageSource": "pokemon_tcg_api",
                "imageCached": False,
                "externalIds": {
                    "pokemonTcgApiId": "base1-1",
                    "tcgdexCardId": None,
                    "tcgplayerProductId": None,
                    "pricechartingId": None,
                },
                "availableVariants": [],
            }
        ],
    }
    write_json(v1_dir / "catalog" / "pokemon" / "en" / "cards" / "base1.json", payload)


def write_sample_price(v1_dir: Path) -> None:
    payload = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": "2026-05-22T00:00:00Z",
        "game": "pokemon",
        "language": "en",
        "setId": "base1",
        "setName": "Base",
        "source": "pokemon_tcg_api",
        "currency": "USD",
        "status": "ok",
        "priceCount": 1,
        "lastSuccessfulPriceUpdateAtUtc": "2026-05-22T00:00:00Z",
        "nextExpectedPriceUpdateAtUtc": "2026-05-22T01:00:00Z",
        "expectedUpdateIntervalMinutes": 60,
        "isLivePricing": False,
        "staleness": {"status": "fresh", "ageSeconds": 0, "freshForSeconds": 86400, "staleAfterSeconds": 172800},
        "prices": [
            {
                "canonicalId": "pokemon|en|base1|1|alakazam|holo|near_mint",
                "canonicalCardId": "pokemon|en|base1|1|alakazam",
                "setId": "base1",
                "setName": "Base",
                "collectorNumber": "1",
                "normalizedName": "alakazam",
                "variant": "holo",
                "condition": "near_mint",
                "currency": "USD",
                "source": "pokemon_tcg_api",
                "fetchedAtUtc": "2026-05-22T00:00:00Z",
                "marketPrice": 1.23,
                "nextExpectedPriceUpdateAtUtc": "2026-05-22T01:00:00Z",
                "staleness": {"status": "fresh", "ageSeconds": 0},
            }
        ],
    }
    write_json(v1_dir / "prices" / "current" / "pokemon" / "en" / "base1.json", payload)


def test_manifest_only_generation_does_not_download_images() -> None:
    original_get = image_cache.requests.get
    try:
        calls: list[str] = []

        def fake_get(*args, **kwargs):
            calls.append("called")
            raise AssertionError("manifest-only generation must not download")

        image_cache.requests.get = fake_get
        with TemporaryDirectory() as tmp_dir:
            v1_dir = Path(tmp_dir) / "public" / "v1"
            write_sample_catalog(v1_dir)
            manifest = image_cache.build_manifest(v1_dir=v1_dir, game="pokemon", language="en")
            assert manifest["recordCount"] == 1
            assert calls == []
    finally:
        image_cache.requests.get = original_get


def test_cdn_url_generation_when_env_is_set() -> None:
    original = os.environ.get("CARDSCANR_IMAGE_CDN_BASE_URL")
    try:
        os.environ["CARDSCANR_IMAGE_CDN_BASE_URL"] = "https://cdn.example.test/assets/"
        with TemporaryDirectory() as tmp_dir:
            v1_dir = Path(tmp_dir) / "public" / "v1"
            write_sample_catalog(v1_dir)
            manifest = image_cache.build_manifest(
                v1_dir=v1_dir,
                game="pokemon",
                language="en",
                cdn_base_url=os.getenv("CARDSCANR_IMAGE_CDN_BASE_URL"),
            )
            record = manifest["records"][0]
            assert record["imageSmallUrl"] == (
                "https://cdn.example.test/assets/cards/pokemon/en/base1/"
                "pokemon-en-base1-1-alakazam/small.webp"
            )
            assert record["imageLargeUrl"].endswith("/large.webp")
            assert record["sourceImageSmallUrl"] == "https://images.example.test/base1/1.png"
            assert record["cacheStatus"] == "cdn_ready"
    finally:
        if original is None:
            os.environ.pop("CARDSCANR_IMAGE_CDN_BASE_URL", None)
        else:
            os.environ["CARDSCANR_IMAGE_CDN_BASE_URL"] = original


def test_provider_fallback_urls_when_cdn_base_url_is_absent() -> None:
    with TemporaryDirectory() as tmp_dir:
        v1_dir = Path(tmp_dir) / "public" / "v1"
        write_sample_catalog(v1_dir)
        manifest = image_cache.build_manifest(v1_dir=v1_dir, game="pokemon", language="en", cdn_base_url=None)
        record = manifest["records"][0]
        assert record["imageSmallUrl"] == "https://images.example.test/base1/1.png"
        assert record["imageLargeUrl"] == "https://images.example.test/base1/1_hires.png"
        assert record["cacheStatus"] == "remote_only"


def test_validate_cache_accepts_remote_only_manifest() -> None:
    original_errors = list(validator.errors)
    try:
        validator.errors.clear()
        with TemporaryDirectory() as tmp_dir:
            v1_dir = Path(tmp_dir) / "public" / "v1"
            write_sample_catalog(v1_dir)
            manifest = image_cache.build_manifest(v1_dir=v1_dir, game="pokemon", language="en", cdn_base_url=None)
            validator.validate_image_manifest_data(manifest, "test-manifest.json", Path(tmp_dir))
            assert validator.errors == []
    finally:
        validator.errors[:] = original_errors


def test_bad_manifest_fails_validation() -> None:
    original_errors = list(validator.errors)
    original_err = validator.err
    try:
        validator.errors.clear()
        validator.err = lambda msg: validator.errors.append(f"ERROR: {msg}")
        with TemporaryDirectory() as tmp_dir:
            v1_dir = Path(tmp_dir) / "public" / "v1"
            write_sample_catalog(v1_dir)
            manifest = image_cache.build_manifest(v1_dir=v1_dir, game="pokemon", language="en")
            manifest["records"][0]["cacheStatus"] = "not_a_status"
            manifest["records"][0]["imageSmallUrl"] = "not-a-url"
            validator.validate_image_manifest_data(manifest, "bad-manifest.json", Path(tmp_dir))
            assert any("cacheStatus" in item for item in validator.errors)
            assert any("imageSmallUrl" in item for item in validator.errors)
    finally:
        validator.err = original_err
        validator.errors[:] = original_errors


def test_coverage_report_runs() -> None:
    with TemporaryDirectory() as tmp_dir:
        v1_dir = Path(tmp_dir) / "public" / "v1"
        manifest_path = v1_dir / "images" / "cards-manifest.json"
        write_sample_catalog(v1_dir)
        write_sample_price(v1_dir)
        manifest = image_cache.build_manifest(v1_dir=v1_dir, game="pokemon", language="en")
        write_json(manifest_path, manifest)
        report = coverage.build_report(v1_dir=v1_dir, manifest_path=manifest_path)
        assert report["catalogueCardCount"] == 1
        assert report["imageManifestRecordCount"] == 1
        assert report["cardsWithSmallImageUrl"] == 1
        assert report["cardsWithLargeImageUrl"] == 1
        assert report["priceSourceCounts"]["pokemon_tcg_api"] == 1
        assert report["APP_TEST_READY"] == "yes"


if __name__ == "__main__":
    test_manifest_only_generation_does_not_download_images()
    test_cdn_url_generation_when_env_is_set()
    test_provider_fallback_urls_when_cdn_base_url_is_absent()
    test_validate_cache_accepts_remote_only_manifest()
    test_bad_manifest_fails_validation()
    test_coverage_report_runs()
    print("Image cache tests passed.")
