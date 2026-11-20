"""Offline tests for 10k external-source evidence without PC or user data."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.private_cardcorpus_10k_source_archive import (
    GOAL, brief, export, select_records, verify_source,
)


class SourceTenKTests(unittest.TestCase):
    def test_bounded_card_id_and_rights_guards(self):
        good = {
            "$kind": "card",
            "set": "jp-M6a",
            "card": "001",
            "name": "Example",
            "language": "ja",
            "number": {"printed": "001/100", "numerator": "001"},
            "picture": {"front": {"image": "0123456789abcdef0123456789abcdef"}},
        }
        row = brief(good)
        self.assertEqual(row["provider_card_id"], "001")
        self.assertEqual(row["picture_id_only"], "0123456789abcdef0123456789abcdef")
        self.assertIsNone(row["canonical_printing_id"])
        self.assertFalse(row["artwork_rights_verified"])
        self.assertFalse(row["exact_match_verified"])
        self.assertFalse(row["image_binary_downloaded"])
        self.assertFalse(row["public_release_allowed"])
        self.assertIsNone(brief({**good, "language": "fr"}))
        self.assertIsNone(brief({**good, "card": "../\nunsafe"}))
        self.assertIsNone(brief({**good, "name": 123}))
        self.assertIsNone(brief({**good, "picture": {"front": {"image": "x"}}})["picture_id_only"])

    def test_hard_pinned_source_version(self):
        raw = json.dumps({"cardCount": 3, "cards": [{}, {}, {}]}).encode()
        sha = hashlib.sha1(("blob " + str(len(raw)) + "\0").encode() + raw).hexdigest()
        self.assertEqual(verify_source(raw, byte_count=len(raw), blob=sha, expected_cards=3)["cardCount"],3)
        with self.assertRaises(ValueError):
            verify_source(raw, byte_count=len(raw), blob="0" * 40, expected_cards=3)
        with self.assertRaises(ValueError):
            verify_source(raw, byte_count=len(raw) + 1, blob=sha, expected_cards=3)

    def test_exact_10000_distinct_source_cards(self):
        cards = []
        for lang, amount in (("ja", 6000), ("zh-Hans", 3000), ("en", 3000)):
            for i in range(amount):
                cards.append({
                    "$kind": "card", "set": f"{lang}-fake-set",
                    "card": f"{i:05d}", "name": "Fixture",
                    "language": lang,
                })
        chosen, counts = select_records({"cards": cards})
        self.assertEqual(len(chosen), GOAL)
        self.assertEqual(counts["selected_by_language"], {"en":2500,"ja":5000,"zh-Hans":2500})
        self.assertEqual(len(set((x["language"],x["provider_set_id"],x["provider_card_id"]) for x in chosen)), GOAL)
        self.assertTrue(all(not x["verified_new_card"] for x in chosen))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence"
            receipt = export(path, chosen, counts, b"test source")
            data = (path / "source_card_briefs_10000.jsonl").read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), receipt["briefs_sha256"])
            self.assertEqual(receipt["exact_new_card_additions"], 0)
            self.assertEqual(receipt["production_writes"], 0)
            self.assertEqual(receipt["image_binaries_downloaded"], 0)
            self.assertFalse(receipt["rights_granted_for_artwork"])
            with self.assertRaises(FileExistsError):
                export(path, chosen, counts, b"test source")

    def test_source_duplicate_key_rejected(self):
        obj = {"$kind": "card", "language": "ja", "set": "X", "card": "001", "name": "A"}
        with self.assertRaises(ValueError):
            select_records({"cards": [obj, dict(obj)]})


if __name__ == "__main__":
    unittest.main()
