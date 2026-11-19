"""Bounded, no-network exact-set provider reference acquisition tests."""
import json
from pathlib import Path
import tempfile
import unittest

from tools.private_targeted_image_references import API, PRIORITY, research, save_private_archive


def fixture_source(url: str):
    for target in PRIORITY:
        lang, sid = target["language"], target["set"]
        base = f"{API}/{lang}/"
        if url == f"{base}sets/{sid}":
            data = {
                "id": sid, "name": target["set_name"],
                "cardCount": {"official": 1, "total": 2},
                "cards": [
                    {"id": sid+"-1", "name": "Sample", "localId": "001",
                     "image": "https://assets.tcgdex.net/example-source-reference"},
                    {"id": sid+"-2", "name": "Second", "localId": "002"},
                ],
            }
            raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
            return data, raw
        for suffix in ("1", "2"):
            if url == f"{base}cards/{sid}-{suffix}":
                data = {"id": sid+"-"+suffix, "name": "Synthetic", "set": {"id": sid}, "hp": 60,
                        "image": "https://assets.tcgdex.net/example-source-reference"
                        if suffix == "1" else None}
                return data, json.dumps(data).encode("utf-8")
    raise AssertionError("unexpected URL: " + url)


class TargetedProviderImageCandidateTests(unittest.TestCase):
    def test_all_four_real_source_id_targets_covered_by_fake_data(self):
        sets, briefs, full, receipt = research(get=fixture_source, pace_seconds=0,
                                              max_details_per_set=2)
        self.assertEqual(len(sets), 4)
        self.assertEqual(len(briefs), 8)
        self.assertEqual(len(full), 8)
        self.assertEqual(receipt["requests"], 12)
        self.assertEqual(receipt["max_requests"], 12)
        self.assertEqual(receipt["provider_image_url_candidates"], 4)
        self.assertEqual(receipt["total_target_gap_rows"], 1051)
        self.assertEqual(receipt["status"], "pass")
        self.assertEqual(receipt["image_binaries_downloaded"], 0)
        self.assertEqual(receipt["production_writes"], 0)
        self.assertFalse(receipt["public_release_allowed"])
        self.assertFalse(receipt["artwork_legal_rights_cleared"])
        self.assertTrue(all(x["canonical_cardscanr_id"] is None for x in briefs))

    def test_source_identity_collision_fails_a_set_closed(self):
        def duplicate_source(url):
            payload, _ = fixture_source(url)
            if "/sets/" in url:
                payload["cards"].append(dict(payload["cards"][0]))
            return payload, json.dumps(payload, ensure_ascii=False).encode("utf-8")
        sets, briefs, full, receipt = research(get=duplicate_source, pace_seconds=0,
                                              max_details_per_set=1)
        self.assertEqual((len(sets), len(briefs), len(full)), (0, 0, 0))
        self.assertEqual(len(receipt["warnings"]), 4)
        self.assertTrue(all(item["error"] == "ValueError" for item in receipt["warnings"]))

    def test_wrong_card_set_does_not_cross_link(self):
        def wrong_source(url):
            data, raw = fixture_source(url)
            if "/cards/" in url:
                data["set"] = {"id": "another-set"}
            return data, json.dumps(data, ensure_ascii=False).encode("utf-8")
        sets, briefs, full, receipt = research(get=wrong_source, pace_seconds=0,
                                              max_details_per_set=1)
        self.assertEqual(len(sets), 4)
        self.assertEqual(len(briefs), 8)
        self.assertEqual(len(full), 0)
        self.assertEqual(len(receipt["warnings"]), 4)

    def test_archive_rejects_overwrite(self):
        sets, briefs, full, receipt = research(get=fixture_source, pace_seconds=0,
                                              max_details_per_set=1)
        with tempfile.TemporaryDirectory() as d:
            output=Path(d) / "private-targeted"
            saved=save_private_archive(output, sets, briefs, full, receipt)
            self.assertEqual(saved["candidate_card_image_references.jsonl"]["count"], 8)
            self.assertEqual(saved["source_full_cards.jsonl"]["count"], 4)
            self.assertEqual(json.loads((output/"receipt.json").read_text())["production_writes"],0)
            with self.assertRaises(FileExistsError):
                save_private_archive(output, sets, briefs, full, receipt)

    def test_limits_fail_closed(self):
        for number in (-1, 7):
            with self.assertRaises(ValueError):
                research(get=fixture_source, max_details_per_set=number)
        with self.assertRaises(ValueError):
            research(get=fixture_source, pace_seconds=-1)


if __name__ == "__main__":
    unittest.main()
