"""Fail-closed tests for private legacy-web-shard metadata projection."""
import unittest
from tools.private_shard_contract import (
    WEB_FIELDS, preview_metadata_row, validate_bounded_preview,
)

def row(language="ko"):
    return {
        "canonical_base_id": "ko-set-001",
        "canonical_printing_id": "ko-printing-001",
        "language": language,
        "set_id": "ko-set",
        "printed_collector_number": "001/100",
        "normalized_collector_number": "1/100",
        "native_card_name": "피카츄",
        "native_set_name": "한국어 세트",
        "rarity": "common",
        "thumbnail_url": "https://example.invalid/unapproved.png",
        "image_display_url": "https://example.invalid/unapproved-large.png",
        "mirror_permission_status": "link_only",
    }


class PrivateShardContractTests(unittest.TestCase):
    def test_metadata_identity_preserves_base_id_without_image(self):
        adapted=preview_metadata_row(row(), "ko")
        self.assertEqual(set(adapted), WEB_FIELDS)
        self.assertEqual(adapted["id"], "ko-set-001")
        self.assertEqual(adapted["lang"], "ko")
        self.assertEqual(adapted["name"], "피카츄")
        self.assertEqual(adapted["n"], "피카츄")
        self.assertEqual(adapted["num"], "001/100")
        self.assertIsNone(adapted["thumb"])
        self.assertNotIn("example.invalid", str(adapted))

    def test_language_mismatch_denied(self):
        with self.assertRaisesRegex(ValueError, "language_mismatch"):
            preview_metadata_row(row("ko"), "ja")

    def test_unknown_language_denied(self):
        with self.assertRaisesRegex(ValueError, "unapproved_private_language"):
            preview_metadata_row(row("fr"), "fr")

    def test_missing_or_invalid_identity_denied(self):
        bad=row();bad["canonical_base_id"]=""
        with self.assertRaisesRegex(ValueError, "missing_canonical"):
            preview_metadata_row(bad, "ko")

    def test_duplicate_ids_denied(self):
        with self.assertRaisesRegex(ValueError, "duplicate_base_id"):
            validate_bounded_preview([row(),row()], "ko")

    def test_preview_capped_at_three(self):
        with self.assertRaisesRegex(ValueError, "sample_not_bounded"):
            validate_bounded_preview([row() for _ in range(4)], "ko")

    def test_all_languages_can_be_previewed_privately(self):
        for language in ("en","ja","ko","zh-tw","th","id","zh-cn"):
            proof=validate_bounded_preview([row(language)],language)
            self.assertTrue(proof["no_images"])
            self.assertFalse(proof["production_enabled"])


if __name__=="__main__":
    unittest.main()
