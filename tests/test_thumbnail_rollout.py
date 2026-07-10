from __future__ import annotations

import unittest
from unittest.mock import patch

from cardscanr_image_pipeline.config import ImagePipelineConfig
from cardscanr_image_pipeline.identity import sha256_hex
from cardscanr_image_pipeline.models import CardImageIdentity, ProcessedImageVariant, ProviderImageCandidate
from cardscanr_image_pipeline.paths import build_storage_paths
from cardscanr_image_pipeline.processing import process_downloaded_image
from cardscanr_image_pipeline.thumbnail_rollout import (
    ENGLISH_BATCH_BUCKETS,
    estimate_thumbnail_storage,
    is_pokewallet_auth_url,
)


def _tiny_portrait_png_bytes() -> bytes:
    # 10x14 RGB PNG via Pillow
    from io import BytesIO

    from PIL import Image

    image = Image.new("RGB", (100, 140), color=(20, 40, 60))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class ThumbnailOnlyPathTests(unittest.TestCase):
    def test_thumb_only_storage_path_omits_display(self) -> None:
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
            content_hash_sha256=sha256_hex(b"thumb"),
            import_display=False,
        )
        self.assertTrue(thumb_path.endswith("/thumb.webp"))
        self.assertIsNone(display_path)
        self.assertEqual(
            thumb_path,
            f"pokemon/en/base1/4/v/{sha256_hex(b'thumb')[:16]}/thumb.webp",
        )

    def test_process_downloaded_image_thumb_only_hashes_thumb_bytes(self) -> None:
        candidate = ProviderImageCandidate(
            provider="pokemon_tcg_api",
            source_url_thumb="https://images.pokemontcg.io/base1/4.png",
            source_url_display="https://images.pokemontcg.io/base1/4_hires.png",
            provider_card_id="base1-4",
            provider_set_id="base1",
            match_basis="test",
        )
        processed = process_downloaded_image(
            _tiny_portrait_png_bytes(),
            candidate,
            fallback_provider=None,
            thumb_max_px=245,
            display_max_px=1000,
            import_display=False,
        )
        self.assertIsNone(processed.display)
        self.assertFalse(processed.import_display)
        self.assertEqual(processed.content_hash_sha256, sha256_hex(processed.thumb.data))
        self.assertLessEqual(max(processed.thumb.width, processed.thumb.height), 245)

    def test_pokewallet_auth_url_detection(self) -> None:
        self.assertTrue(is_pokewallet_auth_url("https://api.pokewallet.io/images/pk_abc?size=low"))
        self.assertFalse(is_pokewallet_auth_url("https://images.pokemontcg.io/base1/4.png"))

    def test_english_batch_bucket_totals(self) -> None:
        self.assertEqual(sum(ENGLISH_BATCH_BUCKETS.values()), 500)
        self.assertNotIn("en_tcgdex", ENGLISH_BATCH_BUCKETS)

    def test_storage_estimates(self) -> None:
        estimates = estimate_thumbnail_storage(matchable_en=23375, matchable_all=36542)
        self.assertEqual(estimates["estimated500BatchBytes"], 13_009 * 500)
        self.assertEqual(estimates["estimatedFullEnglishMatchableBytes"], 13_009 * 23375)

    def test_config_defaults_import_display_false(self) -> None:
        with patch.dict(
            "os.environ",
            {"SUPABASE_URL": "https://example.supabase.co", "SUPABASE_SECRET_KEY": "secret"},
            clear=False,
        ):
            config = ImagePipelineConfig.from_env(dry_run=True)
            self.assertFalse(config.import_display)


if __name__ == "__main__":
    unittest.main()
