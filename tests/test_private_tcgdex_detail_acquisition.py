"""Offline tests: provider JSON only; zero network, binary assets or DB writes."""
import json
import tempfile
import unittest
from pathlib import Path

from tools.private_tcgdex_detail_acquisition import API, gather, save_snapshot


def api_response(payload):
    return payload, json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def source(url):
    data = {
        API + "/ja/sets": [{"id": "set-ja", "releaseDate": "2026-11-01"}],
        API + "/ja/sets/set-ja": {
            "id": "set-ja", "name": "テスト", "releaseDate": "2026-11-01",
            "logo": "https://assets.tcgdex.net/ja/test/logo",
            "cardCount": {"official": 2, "total": 2},
            "cards": [
                {"id": "set-ja-1", "name": "ピカチュウ", "localId": "001"},
                {"id": "set-ja-2", "name": "イーブイ", "localId": "002"},
            ],
        },
        API + "/ja/cards/set-ja-1": {
            "id": "set-ja-1", "localId": "001", "name": "ピカチュウ",
            "set": {"id": "set-ja"}, "hp": 60,
            "attacks": [{"name": "こうげき", "damage": "20"}],
            "image": "https://assets.tcgdex.net/ja/test/1",
        },
        API + "/ja/cards/set-ja-2": {
            "id": "set-ja-2", "localId": "002", "name": "イーブイ",
            "set": {"id": "set-ja"}, "description": "sample",
        },
    }
    if url not in data:
        raise AssertionError("unexpected outbound URL: " + url)
    return api_response(data[url])


class TcgdexDetailAcquisitionTests(unittest.TestCase):
    def test_full_card_and_set_details_image_reference_only(self):
        sets, cards, report = gather(
            ["ja"], max_sets=1, cards_per_set=2, get=source, pace_seconds=0
        )
        self.assertEqual(len(sets), 1)
        self.assertEqual(len(cards), 2)
        self.assertEqual(report["requests"], 4)
        self.assertEqual(sets[0]["source_set_details"]["cardCount"]["official"], 2)
        self.assertEqual(cards[0]["source_card_details"]["attacks"][0]["damage"], "20")
        self.assertTrue(cards[0]["image_reference_present"])
        self.assertFalse(cards[1]["image_reference_present"])
        self.assertEqual(report["by_language"]["ja"]["card_image_references"], 1)
        self.assertEqual(report["image_files_downloaded"], 0)
        self.assertFalse(report["safe_to_publish"])
        self.assertFalse(report["canonical_identity_assigned"])
        self.assertTrue(all(r["url"].startswith(API + "/ja/") for r in report["response_receipts"]))
        self.assertTrue(all(c["image_file_downloaded"] is False for c in cards))

    def test_empty_set_offset_stays_bounded(self):
        sets, cards, report = gather(
            ["ja"], max_sets=1, cards_per_set=1,
            set_offset=10, get=source, pace_seconds=0
        )
        self.assertEqual((sets, cards), ([], []))
        self.assertEqual(report["requests"], 1)
        self.assertEqual(report["status"], "no_results")
        self.assertTrue(report["warnings"])

    def test_offsets_and_request_budget_fail_closed(self):
        for kwargs in (
            {"max_sets": 3}, {"cards_per_set": 7},
            {"set_offset": -1}, {"card_offset": 1001},
            {"pace_seconds": -1},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    gather(["ja"], get=source, **kwargs)
        with self.assertRaises(ValueError):
            gather(["ko", "ko"], get=source)

    def test_snapshot_is_complete_reproducible_and_overwrite_denied(self):
        sets, cards, proof = gather(
            ["ja"], max_sets=1, cards_per_set=2, get=source, pace_seconds=0
        )
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "candidate"
            saved = save_snapshot(out, sets, cards, proof)
            self.assertEqual(saved["card_details.jsonl"]["records"], 2)
            self.assertEqual(saved["set_details.jsonl"]["records"], 1)
            self.assertEqual(len((out / "card_details.jsonl").read_text().splitlines()), 2)
            self.assertEqual(json.loads((out / "receipt.json").read_text())["production_writes"], 0)
            with self.assertRaises(FileExistsError):
                save_snapshot(out, sets, cards, proof)


if __name__ == "__main__":
    unittest.main()
