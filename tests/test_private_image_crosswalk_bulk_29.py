"""Tests for 29-set source discovery, identity precision and fail-closed safety."""
import json
import tempfile
import unittest
from pathlib import Path
from tools.private_image_crosswalk_bulk_29 import TARGETS, LANGUAGES, validate_targets, set_brief, save
from tools.private_image_identity_crosswalk import crosswalk_rows
from tools.private_artwork_takedown_controls import _safe_image_base


class Bulk29ImageSourceTests(unittest.TestCase):
    def test_29_exact_targets_and_gap_sum(self):
        self.assertEqual(len(TARGETS),29)
        self.assertEqual(sum(g for _,_,g in TARGETS),4636)
        self.assertEqual({x[0] for x in TARGETS},set(LANGUAGES))
        self.assertEqual(len({(l,s) for l,s,_ in TARGETS}),29)
        validate_targets(TARGETS)

    def test_duplicate_target_and_bad_language_rejected(self):
        with self.assertRaisesRegex(ValueError,"invalid_target_count_or_duplicate"):
            validate_targets((TARGETS[0],TARGETS[0]))
        with self.assertRaisesRegex(ValueError,"bad_target_language_set_or_gap"):
            validate_targets((("zh-cn","ABC",50),))
        with self.assertRaisesRegex(ValueError,"bad_target_language_set_or_gap"):
            validate_targets((("th","/../../secret",50),))

    def test_source_set_brief_preserves_native_metadata(self):
        obj={"id":"SV9s","name":"สายใยแห่งโชคชะตา","releaseDate":"2025-05-01",
             "cardCount":{"official":180,"total":185},
             "cards":[{"id":"SV9s-1","name":"ทดสอบ","localId":"001","image":"https://assets.tcgdex.net/th/SV/SV9s/001"}]}
        result=set_brief(obj,"th","SV9s")
        self.assertEqual(result["source_set_card_index_rows"],1)
        self.assertEqual(result["total_card_count"],185)
        self.assertEqual(result["release_date"],"2025-05-01")
        self.assertFalse(result["rights_cleared"])
        with self.assertRaisesRegex(ValueError,"invalid_provider_set_identity_or_cards"):
            set_brief(obj,"th","wrong")

    def test_source_collision_rejected(self):
        with self.assertRaisesRegex(ValueError,"duplicate_or_malformed_provider_cards"):
            set_brief({"id":"SV9s","cards":[{"id":"SV9s-1"},{"id":"SV9s-1"}]},"th","SV9s")

    def test_strict_card_match_and_host_validity(self):
        base={"canonical_base_id":"base","canonical_printing_id":"print",
              "set_id":"tcgdex:asia:set:SV9s","language":"th",
              "printed_collector_number":"001/185","native_card_name":"ตัวอย่าง",
              "image_thumbnail_url":None,"image_display_url":None}
        url="https://assets.tcgdex.net/th/SV/SV9s/001"
        cards=[{"id":"SV9s-1","name":"ตัวอย่าง","localId":"001","image":url}]
        matched,proof=crosswalk_rows([base],cards,language="th",source_set_id="SV9s")
        self.assertEqual(proof["status_counts"],{"exact_unique":1})
        self.assertEqual(matched[0]["canonical_printing_id"],"print")
        self.assertTrue(matched[0]["candidate_for_missing_pack_image_ref"])
        self.assertTrue(_safe_image_base(url))
        self.assertFalse(_safe_image_base("https://evil.example/th/SV/SV9s/001"))
        self.assertFalse(_safe_image_base("https://assets.tcgdex.net/th/SV/SV9s/001?token=bad"))

    def test_private_output_and_no_overwrite(self):
        result={"safe_to_publish":False,"production_writes":0,"image_binaries_downloaded":0}
        item={"status":"exact_unique","candidate_for_missing_pack_image_ref":True,
              "canonical_printing_id":"p1","source_image_base_url":"https://assets.tcgdex.net/th/SV/SV9s/001"}
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/"private"
            save(root,[item],result)
            self.assertEqual(len((root/"valid_image_candidates.jsonl").read_text().splitlines()),1)
            self.assertFalse(json.loads((root/"receipt.json").read_text())["safe_to_publish"])
            with self.assertRaises(FileExistsError):
                save(root,[item],result)


if __name__ == "__main__":
    unittest.main()
