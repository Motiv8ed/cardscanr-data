from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from cardscanr_image_pipeline.identity import (
    identity_from_catalogue_card,
    parse_collector_number,
    sha256_hex,
)
from cardscanr_image_pipeline.models import CardImageIdentity
from cardscanr_image_pipeline.paths import (
    build_storage_paths,
    content_hash_from_display_bytes,
    safe_path_segment,
    version_directory_name,
)
from cardscanr_image_pipeline.matching import resolve_provider_image
from cardscanr_image_pipeline.pipeline import ImageIngestionPipeline, PipelineRunSummary
from cardscanr_image_pipeline.processing import ImageValidationError, decode_image, resize_to_webp
from cardscanr_image_pipeline.providers.pokemon_tcg_api import PokemonTcgApiImageProvider
from cardscanr_image_pipeline.providers.pokewallet import (
    PokeWalletAmbiguousMatchError,
    PokeWalletImageProvider,
    extract_pokewallet_identity,
)
from cardscanr_image_pipeline.providers.tcgdex import TcgdexImageProvider, build_tcgdex_image_url
from cardscanr_image_pipeline.retry import RetryableError, retry_call


class ImagePipelinePathTests(unittest.TestCase):
    def test_safe_path_segment_replaces_unsafe_characters(self) -> None:
        self.assertEqual(safe_path_segment("001/081"), "001-081")

    def test_version_directory_uses_hash_prefix(self) -> None:
        digest = "a" * 64
        self.assertEqual(version_directory_name(digest), "a" * 16)

    def test_build_storage_paths_are_immutable_layout(self) -> None:
        identity = CardImageIdentity(
            canonical_base_id="pokemon|en|base1|4|charizard",
            game="pokemon",
            language="en",
            set_id="base1",
            set_code="base1",
            collector_number="4",
            printed_card_number="4",
            local_card_number="4",
            set_total=102,
            printed_total=102,
            provider_set_id=None,
            provider_ids={},
            image_source="pokemon_tcg_api",
            catalogue_image_small="https://images.pokemontcg.io/base1/4.png",
            catalogue_image_large="https://images.pokemontcg.io/base1/4_hires.png",
        )
        thumb_path, display_path = build_storage_paths(
            identity,
            content_hash_sha256=sha256_hex(b"display"),
            import_display=True,
        )
        self.assertTrue(thumb_path.endswith("/thumb.webp"))
        assert display_path is not None
        self.assertTrue(display_path.endswith("/display.webp"))
        self.assertIn("/v/", thumb_path)
        self.assertIn("pokemon/en/base1/4/", thumb_path)

    def test_content_hash_from_display_bytes(self) -> None:
        data = b"webp-bytes"
        self.assertEqual(content_hash_from_display_bytes(data), sha256_hex(data))


class ImagePipelineMatchingTests(unittest.TestCase):
    def test_tcgdex_matches_by_provider_card_id_not_name(self) -> None:
        identity = identity_from_catalogue_card(
            {
                "canonicalBaseId": "pokemon|jp|SV10|001|wrong-name",
                "game": "pokemon",
                "language": "jp",
                "setId": "SV10",
                "collectorNumber": "001",
                "name": "Wrong Name",
                "providerIds": {"tcgdex": "SV10-001"},
                "externalIds": {"tcgdexCardId": "SV10-001"},
                "imageSource": "tcgdex",
                "imageSmall": "https://assets.tcgdex.net/ja/SV/SV10/001/low.webp",
                "imageLarge": "https://assets.tcgdex.net/ja/SV/SV10/001/high.webp",
            },
            set_meta={"printedTotal": 98, "total": 98},
        )
        candidate, fallback = resolve_provider_image(identity, providers=[TcgdexImageProvider()])
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.provider, "tcgdex")
        self.assertIn("SV10", candidate.source_url_display)
        self.assertIsNone(fallback)

    def test_pokemon_tcg_api_is_en_fallback(self) -> None:
        identity = identity_from_catalogue_card(
            {
                "canonicalBaseId": "pokemon|en|zsv10|62|arrokuda",
                "game": "pokemon",
                "language": "en",
                "setId": "zsv10",
                "collectorNumber": "62",
                "name": "Arrokuda",
                "providerIds": {"pokemonTcgApi": "zsv10-62"},
                "externalIds": {"pokemonTcgApiId": "zsv10-62"},
                "imageSource": "pokemon_tcg_api",
                "imageSmall": "https://images.pokemontcg.io/zsv10/62.png",
                "imageLarge": "https://images.pokemontcg.io/zsv10/62_hires.png",
            },
            set_meta={"printedTotal": 100, "total": 100},
        )
        candidate, fallback = resolve_provider_image(
            identity,
            providers=[TcgdexImageProvider(), PokemonTcgApiImageProvider()],
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.provider, "pokemon_tcg_api")
        self.assertEqual(fallback, "pokemon_tcg_api")

    def test_tcgdex_skips_pokemon_tcg_api_source_without_tcgdex_id(self) -> None:
        identity = identity_from_catalogue_card(
            {
                "canonicalBaseId": "pokemon|en|base1|4|charizard",
                "game": "pokemon",
                "language": "en",
                "setId": "base1",
                "collectorNumber": "4",
                "name": "Charizard",
                "providerIds": {"pokemonTcgApi": "base1-4"},
                "externalIds": {"pokemonTcgApiId": "base1-4"},
                "imageSource": "pokemon_tcg_api",
                "imageSmall": "https://images.pokemontcg.io/base1/4.png",
                "imageLarge": "https://images.pokemontcg.io/base1/4_hires.png",
            },
            set_meta={"printedTotal": 102, "total": 102},
        )
        tcg_candidate = TcgdexImageProvider().resolve(identity)
        self.assertIsNone(tcg_candidate)

    def test_classify_en_pokemon_tcg_api_bucket_respects_image_source(self) -> None:
        from cardscanr_image_pipeline.matching import classify_sample_bucket

        card = {
            "canonicalBaseId": "pokemon|en|base1|4|charizard",
            "game": "pokemon",
            "language": "en",
            "setId": "base1",
            "collectorNumber": "4",
            "name": "Charizard",
            "providerIds": {"pokemonTcgApi": "base1-4"},
            "externalIds": {"pokemonTcgApiId": "base1-4"},
            "imageSource": "pokemon_tcg_api",
            "imageSmall": "https://images.pokemontcg.io/base1/4.png",
            "imageLarge": "https://images.pokemontcg.io/base1/4_hires.png",
        }
        identity = identity_from_catalogue_card(card, set_meta={"printedTotal": 102, "total": 102})
        bucket = classify_sample_bucket(identity, source_card=card)
        self.assertEqual(bucket, "en_pokemon_tcg_api")

    def test_fraction_collector_number_parsing(self) -> None:
        printed, local, total = parse_collector_number("001/081", set_total=81)
        self.assertEqual(printed, "001/081")
        self.assertEqual(local, "1")
        self.assertEqual(total, 81)

    def test_tcgdex_url_builder(self) -> None:
        url = build_tcgdex_image_url(
            language="jp",
            serie_id="SV",
            set_id="SV10",
            local_id="001",
            quality="high",
        )
        self.assertEqual(url, "https://assets.tcgdex.net/ja/SV/SV10/001/high.webp")


class ImagePipelinePokeWalletTests(unittest.TestCase):
    def test_extract_pokewallet_identity(self) -> None:
        card = {
            "setId": "1430",
            "collectorNumber": "1/130",
            "language": "en",
            "imageSource": "pokewallet",
            "providerIds": {"pokewallet": "pk_a1b2c3d4e5f6789012345678901234567890"},
            "promotionMetadata": {"providerSetId": "1430", "providerCardId": "pk_a1b2c3d4e5f6789012345678901234567890"},
        }
        extracted = extract_pokewallet_identity(card)
        self.assertEqual(extracted["providerCardId"], "pk_a1b2c3d4e5f6789012345678901234567890")
        self.assertEqual(extracted["providerSetId"], "1430")

    def test_pokewallet_promoted_en_set_matches_without_name(self) -> None:
        card = {
            "canonicalBaseId": "pokemon|en|1430|1/130|dialga",
            "game": "pokemon",
            "language": "en",
            "setId": "1430",
            "collectorNumber": "1/130",
            "name": "Dialga",
            "normalizedName": "dialga",
            "imageSource": "pokewallet",
            "imageSmall": "https://api.pokewallet.io/images/pk_a1b2c3d4e5f6789012345678901234567890?size=low",
            "imageLarge": "https://api.pokewallet.io/images/pk_a1b2c3d4e5f6789012345678901234567890?size=high",
            "providerIds": {"pokewallet": "pk_a1b2c3d4e5f6789012345678901234567890"},
            "promotionMetadata": {
                "providerSetId": "1430",
                "providerCardId": "pk_a1b2c3d4e5f6789012345678901234567890",
                "identityKey": "en|1430|1/130|dialga|normal",
            },
        }
        identity = identity_from_catalogue_card(card)
        candidate = PokeWalletImageProvider().resolve(identity)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.provider, "pokewallet")
        self.assertIn("pk_a1b2c3d4e5f6789012345678901234567890", candidate.source_url_display)

    def test_pokewallet_rejects_conflicting_provider_card_id(self) -> None:
        card = {
            "canonicalBaseId": "pokemon|en|1430|2/130|dusknoir",
            "game": "pokemon",
            "language": "en",
            "setId": "1430",
            "collectorNumber": "2/130",
            "imageSource": "pokewallet",
            "imageSmall": "https://api.pokewallet.io/images/pk_a1b2c3d4e5f6789012345678901234567890?size=low",
            "imageLarge": "https://api.pokewallet.io/images/pk_a1b2c3d4e5f6789012345678901234567890?size=high",
            "providerIds": {"pokewallet": "pk_a1b2c3d4e5f6789012345678901234567890"},
            "promotionMetadata": {"providerSetId": "1430", "providerCardId": "pk_b2c3d4e5f6789012345678901234567890ab"},
        }
        identity = identity_from_catalogue_card(card)
        with self.assertRaises(PokeWalletAmbiguousMatchError):
            PokeWalletImageProvider().resolve(identity)

    def test_pokewallet_path_generation_uses_immutable_layout(self) -> None:
        identity = identity_from_catalogue_card(
            {
                "canonicalBaseId": "pokemon|jp|23598|001/073|tropius",
                "game": "pokemon",
                "language": "jp",
                "setId": "23598",
                "collectorNumber": "001/073",
                "imageSource": "pokewallet",
                "providerIds": {"pokewallet": "pk_jp"},
                "imageSmall": "https://api.pokewallet.io/images/pk_jp?size=low",
                "imageLarge": "https://api.pokewallet.io/images/pk_jp?size=high",
            }
        )
        thumb_path, _ = build_storage_paths(identity, content_hash_sha256=sha256_hex(b"display"))
        self.assertIn("001-073", thumb_path)


class ImagePipelineHashingTests(unittest.TestCase):
    def test_sha256_is_stable(self) -> None:
        self.assertEqual(sha256_hex(b"abc"), sha256_hex(b"abc"))
        self.assertNotEqual(sha256_hex(b"abc"), sha256_hex(b"abd"))


class ImagePipelineResumeTests(unittest.TestCase):
    def test_completed_record_is_skipped_on_resume(self) -> None:
        config = MagicMock()
        config.dry_run = False
        config.thumb_max_px = 245
        config.display_max_px = 1000
        config.timeout_seconds = 5
        config.max_retries = 1
        config.retry_base_seconds = 0.1
        config.cache_control = "public, max-age=31536000, immutable"
        config.bucket_name = "pokemon-card-images"
        config.languages = ("en",)
        config.sample_limit = None
        config.supabase_url = "https://example.supabase.co"
        config.supabase_secret_key = "test-key"
        pipeline = ImageIngestionPipeline(config)
        identity = identity_from_catalogue_card(
            {
                "canonicalBaseId": "pokemon|en|base1|4|charizard",
                "game": "pokemon",
                "language": "en",
                "setId": "base1",
                "collectorNumber": "4",
                "providerIds": {"pokemonTcgApi": "base1-4"},
                "imageSource": "pokemon_tcg_api",
                "imageSmall": "https://images.pokemontcg.io/base1/4.png",
                "imageLarge": "https://images.pokemontcg.io/base1/4_hires.png",
            }
        )
        pipeline.db.get_record = MagicMock(return_value={"status": "verified", "retry_count": 0})
        result = pipeline.process_identity(identity)
        self.assertEqual(result.status, "skipped")


class ImagePipelineFailureTests(unittest.TestCase):
    def test_retry_call_retries_retryable_errors(self) -> None:
        attempts = {"count": 0}

        def flaky() -> str:
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise RetryableError("temporary")
            return "ok"

        value = retry_call(flaky, max_retries=3, base_seconds=0.0)
        self.assertEqual(value, "ok")
        self.assertEqual(attempts["count"], 3)

    def test_decode_image_rejects_invalid_bytes(self) -> None:
        with self.assertRaises(ImageValidationError):
            decode_image(b"not-an-image")

    def test_dry_run_does_not_hit_storage(self) -> None:
        config = MagicMock()
        config.dry_run = True
        config.thumb_max_px = 245
        config.display_max_px = 1000
        config.timeout_seconds = 5
        config.max_retries = 1
        config.retry_base_seconds = 0.1
        config.cache_control = "public, max-age=31536000, immutable"
        config.bucket_name = "pokemon-card-images"
        config.languages = ("en",)
        config.sample_limit = None
        config.supabase_url = "https://example.supabase.co"
        config.supabase_secret_key = "test-key"
        pipeline = ImageIngestionPipeline(config)
        identity = identity_from_catalogue_card(
            {
                "canonicalBaseId": "pokemon|en|base1|4|charizard",
                "game": "pokemon",
                "language": "en",
                "setId": "base1",
                "collectorNumber": "4",
                "providerIds": {"pokemonTcgApi": "base1-4"},
                "imageSource": "pokemon_tcg_api",
                "imageSmall": "https://images.pokemontcg.io/base1/4.png",
                "imageLarge": "https://images.pokemontcg.io/base1/4_hires.png",
            }
        )
        with patch.object(pipeline.storage, "upload_if_absent") as upload_mock:
            result = pipeline.process_identity(identity)
        upload_mock.assert_not_called()
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.dry_run)


class ImagePipelineMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parent.parent
        cls.sql = (root / "supabase" / "migrations" / "20260708000000_pokemon_card_image_pipeline.sql").read_text(
            encoding="utf-8"
        )

    def test_migration_creates_image_records_table(self) -> None:
        self.assertIn("create table if not exists public.pokemon_card_image_records", self.sql.lower())

    def test_migration_creates_storage_bucket(self) -> None:
        self.assertIn("pokemon-card-images", self.sql)

    def test_migration_tracks_verification_and_retry_fields(self) -> None:
        for field in ("verified_at", "retry_count", "failure_reason", "content_hash_sha256"):
            self.assertIn(field, self.sql)


if __name__ == "__main__":
    unittest.main()
