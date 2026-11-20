"""Offline regression tests for cross-language source-only gap collection."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.private_all_language_source_gap_discovery import (
    PRIORITY, MAX_SETS_PER_BATCH, validate_index, select_batches,
    summarize_set, save,
)


class GapDiscoveryTests(unittest.TestCase):
    def test_language_priority_not_random(self):
        self.assertEqual(PRIORITY,("ja","zh-tw","ko","en","th","id","zh-cn"))
        self.assertEqual(len(set(PRIORITY)),7)

    def test_index_validation_rejects_duplicate_and_unsafe_provider_ids(self):
        valid=[{"id":"S12a","name":"set","cardCount":{"total":250}},
               {"id":"SV4a","cardCount":{"total":314}}]
        rows=validate_index("zh-tw",valid)
        self.assertEqual(rows[0]["index_card_count_total"],250)
        self.assertEqual(rows[1]["provider_set_id"],"SV4a")
        self.assertEqual(rows[1]["index_position"],1)
        for bad in ([{"id":"../../x"}],
                    [{"id":"x","cardCount":7}],
                    [None]):
            with self.assertRaises(ValueError):
                validate_index("zh-tw",bad)
        conflicts=[]
        self.assertEqual(
            validate_index("zh-cn",[
                {"id":"CSV1C","name":"First conflicting set","cardCount":{"total":127}},
                {"id":"CSV1C","name":"Second conflicting set","cardCount":{"total":9}},
            ],quarantined=conflicts),[])
        self.assertEqual(conflicts,[{
            "language":"zh-cn","provider_set_id":"CSV1C","occurrences":2,
            "status":"quarantined_conflicting_provider_set_id",
        }])
        with self.assertRaises(ValueError):
            validate_index("wrong",valid)

    def test_only_unmatched_exact_language_set_ids_are_selected(self):
        indexed={l:[] for l in PRIORITY}
        indexed["ja"]=validate_index("ja",[
            {"id":"S1","cardCount":{"total":80}},
            {"id":"S2","cardCount":{"total":90}},
            {"id":"S3","cardCount":{"total":100}},
        ])
        indexed["zh-tw"]=validate_index("zh-tw",[
            {"id":"S2","cardCount":{"total":110}},
        ])
        selected,report=select_batches(
            indexed,{("ja","S2")},batch_index=0,batch_size=2)
        self.assertEqual([(x["language"],x["provider_set_id"]) for x in selected],
                         [("ja","S3"),("ja","S1")])
        self.assertEqual(report["total_source_unmatched_set_ids"],3)
        self.assertEqual(report["source_reported_card_positions_unmatched"],290)
        self.assertEqual(report["total_batches"],2)
        second,_=select_batches(indexed,{("ja","S2")},batch_index=1,batch_size=2)
        self.assertEqual([(x["language"],x["provider_set_id"]) for x in second],
                         [("zh-tw","S2")])
        with self.assertRaises(ValueError):
            select_batches(indexed,set(),batch_index=-1,batch_size=2)
        with self.assertRaises(ValueError):
            select_batches(indexed,set(),batch_index=0,batch_size=MAX_SETS_PER_BATCH+1)

    def test_cross_provider_code_collision_is_quarantined_as_possible_alias(self):
        idx={l:[] for l in PRIORITY}
        idx["en"]=validate_index("en",[
            {"id":"new1","cardCount":{"total":55}},
            {"id":"base1","cardCount":{"total":102}},
        ])
        page,proof=select_batches(
            idx,set(),batch_index=0,batch_size=8,
            other_provider_suffix_keys={("en","base1")})
        self.assertEqual([x["provider_set_id"] for x in page],["new1","base1"])
        self.assertEqual(proof["total_source_unmatched_set_ids"],2)
        self.assertEqual(proof["possible_cross_provider_alias_codes"],1)
        self.assertEqual(proof["source_set_codes_without_any_pack_suffix_match"],1)
        byid={r["provider_set_id"]:r for r in page}
        self.assertEqual(byid["base1"]["candidate_status"],"possible_cross_provider_alias")
        self.assertEqual(byid["new1"]["candidate_status"],"provider_set_id_not_in_pack")
        self.assertTrue(byid["base1"]["cross_provider_set_code_collision"])
        self.assertFalse(byid["new1"]["cross_provider_set_code_collision"])

    def test_source_briefs_are_provisional_not_canonical_new_cards(self):
        payload={"id":"SV4a","name":"Known source set",
                 "releaseDate":"2025-02-21",
                 "cardCount":{"total":2,"official":2},
                 "cards":[
                    {"id":"SV4a-001","localId":"001","name":"Sample A",
                     "image":"https://assets.tcgdex.net/id/SV/SV4a/001"},
                    {"id":"SV4a-002","localId":"002","name":"Sample B",
                     "image":"https://evil.example/id/SV/SV4a/002"},
                 ]}
        raw=json.dumps(payload).encode()
        source,cards=summarize_set("id","SV4a",payload,raw)
        self.assertEqual(source["source_response_sha256"],hashlib.sha256(raw).hexdigest())
        self.assertEqual(source["source_card_index_rows"],2)
        self.assertEqual(source["image_base_references"],1)
        self.assertEqual(len(cards),2)
        self.assertEqual(cards[0]["provider_card_id"],"SV4a-001")
        self.assertTrue(cards[0]["source_image_ref_present"])
        self.assertIsNone(cards[1]["source_image_base_url"])
        self.assertFalse(source["artwork_rights_verified"])
        self.assertEqual(source["new_CardScanR_cards_confirmed"],0)
        self.assertTrue(all(r["canonical_printing_id"] is None and
                            not r["exact_match_verified"] and
                            not r["image_binary_downloaded"] for r in cards))

    def test_source_duplicate_or_foreign_card_ids_refused(self):
        good={"id":"SI","cards":[{"id":"SI-001","name":"One","localId":"001"}]}
        source,cards=summarize_set("zh-tw","SI",good,b"safe")
        self.assertEqual(len(cards),1)
        for cards in (
            [{"id":"SI-001"},{"id":"SI-001"}],
            [{"id":"SXX-001"}],
            [{"id":"SI/../../bad"}],
            [123],
        ):
            with self.assertRaises(ValueError):
                summarize_set("zh-tw","SI",{"id":"SI","cards":cards},b"bad")
        with self.assertRaises(ValueError):
            summarize_set("zh-tw","DIFFERENT",good,b"wrong")

    def test_private_provenance_written_and_reimport_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"fresh"
            report={"rights_granted":False,"production_writes":0}
            save(path,[],[],report)
            self.assertFalse(json.loads((path/"receipt.json").read_text())["rights_granted"])
            self.assertEqual(json.loads((path/"receipt.json").read_text())["unmatched_source_sets.jsonl"]["records"],0)
            with self.assertRaises(FileExistsError):
                save(path,[],[],report)


if __name__=="__main__":
    unittest.main()
