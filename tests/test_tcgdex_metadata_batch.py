"""Offline fixture tests for the bounded, read-only TCGdex metadata source."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from tools.tcgdex_metadata_batch import API, _choose_sets, _records, _korean_index_records, collect, write_snapshot


def payload(value):
    return value, json.dumps(value, ensure_ascii=False).encode("utf-8")


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        if url.endswith("/ko/cards"):
            return payload([{"id": "latest-002", "localId": "002", "name": "피카츄"},
                            {"id": "latest-003", "localId": "003", "name": "카드"}])
        if url.endswith("/sets"):
            return payload([
                {"id": "older", "releaseDate": "2024-05-02"},
                {"id": "latest", "releaseDate": "2026-11-01"},
                {"id": "latest", "releaseDate": "2026-11-01"},
                {"id": "../../escape", "releaseDate": "2027-01-01"},
            ])
        return payload({"id": "latest", "cards": [
            {"id": "latest-002", "localId": "002", "name": "ピカチュウ", "image": "https://assets.tcgdex.net/example"},
            {"id": "latest-002", "localId": "002", "name": "duplicate"},
            {"id": "latest-003", "localId": "003", "name": "カード"},
            {"id": "missing-name", "localId": "004"},
        ]})

    def test_unique_latest_sets_and_valid_identity_only(self):
        ids = _choose_sets([{"id": "bad/..", "releaseDate": "2026-11-30"}, {"id": "ok", "releaseDate": "2026-01-01"}], 2)
        self.assertEqual(ids, ["ok"])
        rows, invalid = _records("ja", "latest", self.get(API + "/ja/sets/latest")[0], "1"*64, 10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(invalid, 2)
        self.assertTrue(all(r["artwork_downloaded"] is False and r["production_published"] is False for r in rows))
        self.assertEqual(rows[0]["source_language"], "ja")

    def test_live_shape_fixed_budget_provenance_private(self):
        rows, m = collect(["ja", "zh-tw", "ko"], max_sets=1, max_cards=2, get=self.get, pace_seconds=0)
        self.assertEqual(len(rows), 6)
        self.assertEqual(len(self.urls), 7)
        self.assertTrue(all(url.startswith(API + "/") and "/assets/" not in url for url in self.urls))
        self.assertTrue(all(row["source_response_sha256"] for row in rows))
        self.assertFalse(m["public_artwork_rights_cleared"])
        self.assertEqual(m["image_files_downloaded"], 0)
        self.assertLessEqual(m["requests_completed"], m["request_budget"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "batch"
            receipt = write_snapshot(root, rows, m)
            data = (root / "card_candidates.jsonl").read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), receipt["candidate_sha256"])
            self.assertEqual(len(data.splitlines()), 6)
            with self.assertRaises(FileExistsError):
                write_snapshot(root, rows, m)

    def test_reject_unbounded_or_unsupported_requests(self):
        for langs in (["ja", "ja"], ["invalid"], []):
            with self.assertRaises(ValueError):
                collect(langs, max_sets=1, max_cards=2, get=self.get, pace_seconds=0)
        for cap in (0, 4):
            with self.assertRaises(ValueError):
                collect(["ja"], max_sets=cap, max_cards=2, get=self.get, pace_seconds=0)
        with self.assertRaises(ValueError):
            collect(["ja"], max_sets=1, max_cards=26, get=self.get, pace_seconds=0)

    def test_empty_language_not_reported_as_complete(self):
        def empty_ko(url):
            if url.endswith("/ko/sets/latest"):
                return payload({"id": "latest", "cards": []})
            return self.get(url)
        rows, m = collect(["ja", "ko"], max_sets=1, max_cards=2, get=empty_ko, pace_seconds=0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(m["status"], "partial")
        self.assertEqual(m["candidate_cards_by_language"]["ko"], 0)
        self.assertTrue(any(w["reason"] == "no_valid_card_candidates" for w in m["warnings"]))

    def test_korean_card_index_recovers_empty_set_pages(self):
        def ko_source(url):
            if url == API + "/ko/sets":
                return payload([{"id": "sv5K"}, {"id": "SV6"}])
            if url == API + "/ko/cards":
                return payload([
                    {"id": "sv5K-001", "localId": "001", "name": "피카츄"},
                    {"id": "SV6-002", "localId": "002", "name": "이상해씨"},
                ])
            if url.endswith("/ko/sets/sv5K") or url.endswith("/ko/sets/SV6"):
                return payload({"id": url.rsplit("/", 1)[-1], "cards": []})
            raise AssertionError("Unexpected URL " + url)
        rows, manifest = collect(["ko"], max_sets=2, max_cards=20, get=ko_source, pace_seconds=0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(manifest["candidate_cards_by_language"]["ko"], 2)
        self.assertEqual(manifest["status"], "pass")
        self.assertEqual(manifest["requests_completed"], 4)
        self.assertEqual(manifest["request_budget"], 4)
        self.assertTrue(all(r["metadata_route"] == "korean_card_index_fallback" for r in rows))
        self.assertTrue(all(r["variant_status"] == "unverified" for r in rows))
        self.assertTrue(all(r["artwork_downloaded"] is False for r in rows))

    def test_unmatched_korean_set_ids_do_not_create_fake_printings(self):
        rows, rejected, capped = _korean_index_records([
            {"id": "unknown-1", "localId": "1", "name": "Unknown"},
            {"id": "sv5K-001", "localId": "001", "name": "피카츄"},
            {"id": "sv5K-001", "localId": "001", "name": "Duplicate"},
        ], [{"id": "sv5K"}], "a" * 64)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rejected, 2)
        self.assertEqual(capped, 0)

    def test_korean_index_is_capped_even_with_many_records(self):
        cards = [{"id": f"sv5K-{i:03d}", "localId": f"{i:03d}", "name": "card"}
                 for i in range(320)]
        rows, rejected, unprocessed = _korean_index_records(cards, [{"id": "sv5K"}], "f" * 64)
        self.assertEqual(len(rows), 300)
        self.assertEqual(rejected, 0)
        self.assertEqual(unprocessed, 20)

    def test_undated_provider_set_index_order_is_not_lexical_recency(self):
        self.assertEqual(_choose_sets([
            {"id": "sv5K"}, {"id": "AA"}, {"id": "ZZ"},
        ], 2), ["sv5K", "AA"])

    def test_partial_fetch_failures_not_misreported_as_success(self):
        def broken(url):
            if "/ko/" in url:
                raise OSError("fake network outage")
            return self.get(url)
        rows, m = collect(["ja", "ko"], max_sets=1, max_cards=1, get=broken, pace_seconds=0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(m["status"], "partial")
        self.assertEqual(m["warnings"][0]["language"], "ko")
        self.assertEqual(m["warnings"][0]["reason"], "OSError")


if __name__ == "__main__":
    unittest.main()
