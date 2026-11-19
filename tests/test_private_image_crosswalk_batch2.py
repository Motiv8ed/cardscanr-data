"""Offline tests for second-source-wave exact printing reconciliation."""
import json
from pathlib import Path
import tempfile
import unittest

from tools.private_image_crosswalk_batch2 import (
    EXPECTED_TOTAL_GAP, LANGUAGES, TARGETS, checked_source_set, save,
)
from tools.private_image_identity_crosswalk import crosswalk_rows


class SecondWaveCrosswalkTests(unittest.TestCase):
    def test_target_gap_budget_is_six_explicit_unique_sets(self):
        self.assertEqual(len(TARGETS), 6)
        self.assertEqual(len({(lang,sid) for lang,sid,_ in TARGETS}), 6)
        self.assertEqual(sum(gap for _,_,gap in TARGETS), EXPECTED_TOTAL_GAP)
        self.assertEqual(EXPECTED_TOTAL_GAP, 1439)
        self.assertEqual(set(lang for lang,_,_ in TARGETS), set(LANGUAGES))

    def test_source_set_identity_cannot_be_substituted(self):
        with self.assertRaisesRegex(ValueError, "provider_set_identity_mismatch"):
            checked_source_set({"id":"wrong","cards":[{"id":"SV4a-1"}]}, "zh-tw","SV4a")
        with self.assertRaisesRegex(ValueError, "source_set_has_no_cards"):
            checked_source_set({"id":"SV4a","cards":[]}, "zh-tw","SV4a")

    def test_duplicate_source_cards_refused(self):
        source = {"id":"SV4a","cards":[{"id":"SV4a-1"},{"id":"SV4a-1"}]}
        with self.assertRaisesRegex(ValueError,"duplicate_or_malformed_source_cards"):
            checked_source_set(source, "zh-tw","SV4a")

    def test_six_sets_preserve_exact_native_and_printed_number(self):
        for lang, sid, gap in TARGETS:
            with self.subTest(language=lang, set_id=sid):
                name="ชุดทดสอบ" if lang=="th" else ("皮卡丘" if lang=="zh-tw" else "Pikachu")
                card = {
                    "canonical_base_id": lang+sid+":base",
                    "canonical_printing_id": lang+sid+":printing",
                    "set_id":"tcgdex:asia:set:"+sid,
                    "language":lang,"printed_collector_number":"001/200",
                    "native_card_name":name,
                    "image_thumbnail_url":None,"image_display_url":None,
                }
                result,summary=crosswalk_rows(
                    [card],
                    [{"id":sid+"-1","name":name,"localId":"001",
                      "image":"https://assets.tcgdex.net/example"}],
                    language=lang,source_set_id=sid,
                )
                self.assertEqual(summary["status_counts"]["exact_unique"], 1)
                self.assertEqual(result[0]["canonical_printing_id"],lang+sid+":printing")
                self.assertTrue(result[0]["candidate_for_missing_pack_image_ref"])
                self.assertFalse(result[0]["artwork_rights_verified"])

    def test_wrong_name_not_assigned(self):
        lang, sid, _ = TARGETS[0]
        pack = [{"canonical_base_id":"base","canonical_printing_id":"printing",
                 "set_id":"tcgdex:asia:set:"+sid,"language":lang,
                 "printed_collector_number":"001/200","native_card_name":"正確",
                 "image_thumbnail_url":None,"image_display_url":None}]
        result,_=crosswalk_rows(pack,[{"id":sid+"-1","localId":"001",
                                     "name":"不同","image":"https://assets.tcgdex.net/x"}],
                                 language=lang,source_set_id=sid)
        self.assertEqual(result[0]["status"],"unmatched")
        self.assertIsNone(result[0]["canonical_printing_id"])

    def test_private_receipt_and_overwrite_denied(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"safe"
            proof={"safe_to_publish":False,"production_writes":0}
            save(path,[],[],proof)
            self.assertFalse(json.loads((path/"receipt.json").read_text())["safe_to_publish"])
            self.assertTrue((path/"printing_matches.jsonl").exists())
            with self.assertRaises(FileExistsError):
                save(path,[],[],proof)


if __name__ == "__main__":
    unittest.main()
