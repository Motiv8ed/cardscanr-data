import json
from pathlib import Path
import tempfile
import unittest

from tools.private_image_identity_crosswalk import crosswalk_rows, write_report


def pack(card_id="base-1", print_id="printing-1", name="皮卡丘", number="001/414", img=None):
    return {
        "canonical_base_id":card_id,
        "canonical_printing_id":print_id,
        "set_id":"tcgdex:asia:set:SI",
        "language":"zh-tw",
        "printed_collector_number":number,
        "native_card_name":name,
        "image_thumbnail_url":img,
        "image_display_url":None,
    }


def source(cid="SI-001", name="皮卡丘", num="001", image="https://assets.tcgdex.net/zh-tw/S/SI/001"):
    return {"id":cid, "name":name, "localId":num, "image":image}


class SourceIdentityCrosswalkTests(unittest.TestCase):
    def test_exact_number_and_native_name_matches_unique_printing(self):
        result, report=crosswalk_rows([pack()],[source()],language="zh-tw",source_set_id="SI")
        self.assertEqual(result[0]["status"],"exact_unique")
        self.assertEqual(result[0]["canonical_printing_id"],"printing-1")
        self.assertTrue(result[0]["candidate_for_missing_pack_image_ref"])
        self.assertFalse(result[0]["artwork_rights_verified"])
        self.assertEqual(report["exact_matched_missing_image_url_with_source_candidate"],1)

    def test_existing_art_prevents_false_missing_candidate(self):
        results, _=crosswalk_rows([pack(img="https://some.example/existing")],[source()],language="zh-tw",source_set_id="SI")
        self.assertEqual(results[0]["status"],"exact_unique")
        self.assertFalse(results[0]["candidate_for_missing_pack_image_ref"])

    def test_ambiguity_never_assigns_printing(self):
        a=pack();b=pack(card_id="base-2",print_id="printing-2")
        results,report=crosswalk_rows([a,b],[source()],language="zh-tw",source_set_id="SI")
        self.assertEqual(results[0]["status"],"ambiguous")
        self.assertIsNone(results[0]["canonical_base_id"])
        self.assertEqual(report["status_counts"]["ambiguous"],1)
        self.assertFalse(results[0]["candidate_for_missing_pack_image_ref"])

    def test_number_and_name_mismatch_do_not_crosslink(self):
        results,_=crosswalk_rows([pack()],[source(num="002")],language="zh-tw",source_set_id="SI")
        self.assertEqual(results[0]["status"],"unmatched")
        results,_=crosswalk_rows([pack()],[source(name="Different")],language="zh-tw",source_set_id="SI")
        self.assertEqual(results[0]["status"],"unmatched")

    def test_source_duplicate_fails_closed(self):
        with self.assertRaisesRegex(ValueError,"duplicate_source_card_identity"):
            crosswalk_rows([pack()],[source(),source()],language="zh-tw",source_set_id="SI")

    def test_pack_set_language_mismatch_fails_closed(self):
        row=pack();row["language"]="ja"
        with self.assertRaisesRegex(ValueError,"pack_set_language_mismatch"):
            crosswalk_rows([row],[source()],language="zh-tw",source_set_id="SI")

    def test_private_archive_immutable(self):
        rows,report=crosswalk_rows([pack()],[source()],language="zh-tw",source_set_id="SI")
        with tempfile.TemporaryDirectory() as td:
            dest=Path(td)/"proof"
            body={"classification":"PRIVATE_IDENTITY_CROSSWALK_REVIEW_ONLY","safe_to_publish":False}
            write_report(dest,rows,body)
            self.assertEqual(len((dest/"crosswalk_candidates.jsonl").read_text().splitlines()),1)
            self.assertFalse(json.loads((dest/"receipt.json").read_text())["safe_to_publish"])
            with self.assertRaises(FileExistsError):
                write_report(dest,rows,report)


if __name__=="__main__":
    unittest.main()
