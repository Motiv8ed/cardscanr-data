"""Bounded private 113-set gap refresh regression tests."""
import json
import tempfile
import unittest
from pathlib import Path

from tools.private_113_set_artwork_refresh import (
    TARGETS, LANGUAGES, EXPECTED_REMAINING_UNLINKED_GAPS,
    validate_targets, source_summary, save
)
from tools.private_image_identity_crosswalk import crosswalk_rows


class Gap113Tests(unittest.TestCase):
    def test_exact_113_sets_8077_owner_vault_gaps(self):
        self.assertEqual(len(TARGETS),113)
        self.assertEqual(len({(l,s) for l,s,_ in TARGETS}),113)
        self.assertEqual(sum(v for _,_,v in TARGETS),8077)
        self.assertEqual(EXPECTED_REMAINING_UNLINKED_GAPS,8077)
        self.assertEqual({l for l,_,_ in TARGETS},set(LANGUAGES))
        validate_targets()

    def test_duplicate_and_bad_target_rejected(self):
        with self.assertRaisesRegex(ValueError,"duplicate_or_too_many_targets"):
            validate_targets((("th","S1",10),("th","S1",10)))
        with self.assertRaisesRegex(ValueError,"invalid_target"):
            validate_targets((("th","../unsafe",10),))
        with self.assertRaisesRegex(ValueError,"invalid_target"):
            validate_targets((("xx","S1",10),))

    def test_source_set_id_and_duplicate_rejected(self):
        raw=b'{"id":"SV6s","cards":[{"id":"SV6s-001"}]}'
        payload=json.loads(raw)
        summary=source_summary(payload,"id","SV6s",raw)
        self.assertEqual(summary["source_provider"],"tcgdex")
        self.assertFalse(summary["artwork_rights_granted"])
        with self.assertRaisesRegex(ValueError,"provider_source_set_mismatch"):
            source_summary(payload,"id","different",raw)
        with self.assertRaisesRegex(ValueError,"bad_or_duplicate_source_cards"):
            source_summary({"id":"SV6s","cards":[{"id":"SV6s-001"},{"id":"SV6s-001"}]},
                           "id","SV6s",raw)

    def test_name_mismatch_never_invents_canonical_id(self):
        source=[{"id":"SV6s-001","localId":"001","name":"Other name",
                 "image":"https://assets.tcgdex.net/id/SV/SV6s/001"}]
        pack=[{"canonical_base_id":"test","canonical_printing_id":"test-printing",
               "set_id":"tcgdex:asia:set:SV6s","language":"id",
               "printed_collector_number":"001/167","native_card_name":"Correct name",
               "image_thumbnail_url":None,"image_display_url":None}]
        matches,proof=crosswalk_rows(pack,source,language="id",source_set_id="SV6s")
        self.assertEqual(matches[0]["status"],"unmatched")
        self.assertIsNone(matches[0]["canonical_printing_id"])
        self.assertEqual(proof["exact_matched_missing_image_url_with_source_candidate"],0)

    def test_private_artifact_output(self):
        with tempfile.TemporaryDirectory() as td:
            output=Path(td)/"private"
            receipt={"rights_granted":False,"safe_for_public_use":False,"production_writes":0}
            save(output,[],[],receipt)
            self.assertEqual(json.loads((output/"receipt.json").read_text())["matching_results.jsonl"]["records"],0)
            with self.assertRaises(FileExistsError):
                save(output,[],[],receipt)


if __name__=="__main__":
    unittest.main()
