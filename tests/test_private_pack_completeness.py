import sqlite3
import unittest
from tools.private_pack_completeness import inventory_pack


def fixture() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE cards (
            canonical_base_id TEXT, set_id TEXT, native_card_name TEXT,
            native_set_name TEXT, printed_collector_number TEXT,
            english_card_name TEXT, rarity TEXT, regulation_mark TEXT,
            release_date TEXT, image_thumbnail_url TEXT,
            image_display_url TEXT, mirror_permission_status TEXT,
            provider_card_id TEXT, provider_set_id TEXT, native_name_status TEXT
        );
        INSERT INTO cards VALUES
            ('a','set-a','피카츄','세트','001/100',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'source'),
            ('b','set-a','피카츄','세트','002/100',NULL,'Rare',NULL,NULL,'https://example.invalid/test',NULL,'unknown',NULL,NULL,'source'),
            ('c','set-b','皮卡丘',NULL,'003/100','Pikachu',NULL,NULL,NULL,NULL,NULL,'approved',NULL,NULL,'source');
    """)
    return db


class PrivatePackCompletenessTests(unittest.TestCase):
    def test_counts_gaps_without_exposing_card_rows(self):
        db = fixture()
        try:
            proof = inventory_pack(db)
        finally:
            db.close()
        self.assertEqual(proof["card_records"], 3)
        self.assertEqual(proof["distinct_referenced_sets"], 2)
        self.assertEqual(proof["sets_missing_all_native_names"], 1)
        self.assertEqual(proof["cards_with_image_reference"], 1)
        self.assertEqual(proof["cards_without_image_reference"], 2)
        self.assertEqual(proof["missing_metadata_field_counts"]["missing_rarity"], 2)
        self.assertEqual(proof["source_mirror_permission_approved_field_count"], 1)
        self.assertEqual(proof["worst_image_reference_gap_sets"][0]["set_id"], "set-a")
        self.assertEqual(proof["worst_image_reference_gap_sets"][0]["no_image_reference"], 1)
        self.assertIs(proof["artwork_downloaded"], False)
        self.assertIs(proof["safe_to_publish"], False)
        self.assertNotIn("피카츄", str(proof))
        self.assertNotIn("https://example.invalid/test", str(proof))

    def test_empty_pack_uses_zero_and_empty_gap_shortlist(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE cards(canonical_base_id TEXT,set_id TEXT,native_card_name TEXT,native_set_name TEXT,printed_collector_number TEXT)")
        try:
            proof = inventory_pack(db)
        finally:
            db.close()
        self.assertEqual(proof["card_records"], 0)
        self.assertEqual(proof["cards_without_image_reference"], 0)
        self.assertEqual(proof["worst_image_reference_gap_sets"], [])

    def test_missing_identity_schema_fails_closed(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE cards(native_card_name TEXT)")
        with self.assertRaisesRegex(ValueError,"missing_required_pack_columns"):
            inventory_pack(db)
        db.close()

if __name__ == "__main__":
    unittest.main()
