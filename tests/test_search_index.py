#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from cardscanr_search_index.builder import build_search_index, sha256_file
from cardscanr_search_index.catalogue_reader import iter_catalogue_cards
from cardscanr_search_index.normalization import (
    normalize_collector_number,
    normalize_search_text,
)
from cardscanr_search_index.search import SearchRequest, connect_readonly, lookup_exact_identity, search_cards
from cardscanr_search_index.verify import verify_search_index


def _write_min_catalogue(root: Path) -> None:
    en_sets = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": "2026-01-01T00:00:00Z",
        "sets": [
            {
                "id": "base1",
                "name": "Base",
                "total": 102,
                "printedTotal": 102,
                "releaseDate": "1999/01/09",
                "ptcgoCode": "BS",
            },
            {
                "id": "xy12",
                "name": "Evolutions",
                "total": 108,
                "printedTotal": 108,
                "releaseDate": "2016/11/02",
                "ptcgoCode": "EVO",
            },
        ],
    }
    jp_sets = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": "2026-01-01T00:00:00Z",
        "sets": [
            {
                "id": "SV10",
                "name": "ロケット団の栄光",
                "total": 98,
                "printedTotal": 98,
                "releaseDate": "2025/01/01",
            }
        ],
    }
    en_base1 = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": "2026-01-01T00:00:00Z",
        "setId": "base1",
        "setName": "Base",
        "cards": [
            {
                "canonicalBaseId": "pokemon|en|base1|4|charizard",
                "language": "en",
                "setId": "base1",
                "collectorNumber": "4",
                "name": "Charizard",
                "normalizedName": "charizard",
                "imageSource": "pokemon_tcg_api",
                "imageCached": False,
                "imageSmall": "https://example.test/base1/4.png",
                "imageLarge": "https://example.test/base1/4_hires.png",
                "providerIds": {"pokemonTcgApi": "base1-4"},
            },
            {
                "canonicalBaseId": "pokemon|en|base1|004|energy",
                "language": "en",
                "setId": "base1",
                "collectorNumber": "004",
                "name": "Energy",
                "normalizedName": "energy",
                "imageSource": "pokemon_tcg_api",
                "imageCached": False,
                "imageSmall": "https://example.test/base1/004.png",
                "imageLarge": "https://example.test/base1/004_hires.png",
                "providerIds": {"pokemonTcgApi": "base1-004"},
            },
        ],
    }
    en_xy12 = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": "2026-01-01T00:00:00Z",
        "setId": "xy12",
        "setName": "Evolutions",
        "cards": [
            {
                "canonicalBaseId": "pokemon|en|xy12|11|charizard",
                "language": "en",
                "setId": "xy12",
                "collectorNumber": "11",
                "name": "Charizard",
                "normalizedName": "charizard",
                "imageSource": "pokemon_tcg_api",
                "imageCached": False,
                "imageSmall": "https://example.test/xy12/11.png",
                "imageLarge": "https://example.test/xy12/11_hires.png",
                "providerIds": {"pokemonTcgApi": "xy12-11"},
            }
        ],
    }
    jp_sv10 = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": "2026-01-01T00:00:00Z",
        "setId": "SV10",
        "setName": "ロケット団の栄光",
        "cards": [
            {
                "canonicalBaseId": "pokemon|jp|SV10|001|クヌギダマ",
                "language": "jp",
                "setId": "SV10",
                "collectorNumber": "001",
                "name": "クヌギダマ",
                "normalizedName": "クヌギダマ",
                "imageSource": "tcgdex",
                "imageCached": False,
                "imageSmall": "https://example.test/sv10/001.webp",
                "imageLarge": "https://example.test/sv10/001-high.webp",
                "providerIds": {"tcgdex": "SV10-001"},
            }
        ],
    }
    en_root = root / "catalog" / "pokemon" / "en"
    jp_root = root / "catalog" / "pokemon" / "jp"
    (en_root / "cards").mkdir(parents=True, exist_ok=True)
    (jp_root / "cards").mkdir(parents=True, exist_ok=True)
    (en_root / "sets.json").write_text(json.dumps(en_sets), encoding="utf-8")
    (jp_root / "sets.json").write_text(json.dumps(jp_sets), encoding="utf-8")
    (en_root / "cards" / "base1.json").write_text(json.dumps(en_base1), encoding="utf-8")
    (en_root / "cards" / "xy12.json").write_text(json.dumps(en_xy12), encoding="utf-8")
    (jp_root / "cards" / "SV10.json").write_text(json.dumps(jp_sv10), encoding="utf-8")


class NormalizationTests(unittest.TestCase):
    def test_leading_zero_collector(self) -> None:
        self.assertEqual(normalize_collector_number("004"), "4")

    def test_fraction_collector(self) -> None:
        self.assertEqual(normalize_collector_number("001/081"), "1/81")

    def test_fraction_leading_zeros_both_sides(self) -> None:
        self.assertEqual(normalize_collector_number("024/086"), "24/86")

    def test_prefixed_fraction_collector(self) -> None:
        self.assertEqual(normalize_collector_number("TG01/TG30"), "tg1/tg30")

    def test_letter_prefix_collector(self) -> None:
        self.assertEqual(normalize_collector_number("SV001"), "sv1")
        self.assertEqual(normalize_collector_number("SWSH001"), "swsh1")

    def test_collector_query_detection(self) -> None:
        from cardscanr_search_index.normalization import is_collector_number_query

        self.assertTrue(is_collector_number_query("4"))
        self.assertTrue(is_collector_number_query("001/081"))
        self.assertTrue(is_collector_number_query("H3"))
        self.assertFalse(is_collector_number_query("charizard"))
        self.assertFalse(is_collector_number_query("zzznomatch999"))

    def test_punctuation_and_case(self) -> None:
        self.assertEqual(normalize_search_text("Ethan's Pinsir"), "ethan s pinsir")


class SearchIndexBuildTests(unittest.TestCase):
    def test_build_verify_and_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalogue_root = Path(tmp) / "v1"
            output_dir = Path(tmp) / "search"
            _write_min_catalogue(catalogue_root)
            first = build_search_index(catalogue_root=catalogue_root, output_dir=output_dir, root=ROOT)
            second = build_search_index(catalogue_root=catalogue_root, output_dir=output_dir, root=ROOT)
            verify = verify_search_index(
                output_dir=output_dir,
                catalogue_root=catalogue_root,
                expected_fingerprint=first.content_fingerprint,
            )
            self.assertTrue(verify.passed, verify.issues)
            self.assertEqual(first.content_fingerprint, second.content_fingerprint)
            self.assertTrue((output_dir / "catalog_search_v1.previous.sqlite").exists())

            conn = connect_readonly(str(output_dir / "catalog_search_v1.sqlite"))
            try:
                hits = search_cards(conn, SearchRequest(query_text="charizard", language="en", limit=10))
                self.assertGreaterEqual(len(hits), 2)
                exact = lookup_exact_identity(conn, language="en", set_id="base1", collector_number="4")
                assert exact is not None
                self.assertEqual(exact.canonical_base_id, "pokemon|en|base1|4|charizard")
                jp_hits = search_cards(conn, SearchRequest(query_text="クヌギダマ", language="jp", limit=5))
                self.assertEqual(jp_hits[0].canonical_base_id, "pokemon|jp|SV10|001|クヌギダマ")
            finally:
                conn.close()

            manifest = json.loads((output_dir / "catalog_search_v1.manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["sha256"], sha256_file(output_dir / "catalog_search_v1.sqlite"))

    def test_no_duplicate_canonical_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalogue_root = Path(tmp) / "v1"
            output_dir = Path(tmp) / "search"
            _write_min_catalogue(catalogue_root)
            build_search_index(catalogue_root=catalogue_root, output_dir=output_dir, root=ROOT)
            conn = sqlite3.connect(output_dir / "catalog_search_v1.sqlite")
            duplicate_count = conn.execute(
                "SELECT COUNT(*) FROM (SELECT canonical_base_id FROM cards GROUP BY canonical_base_id HAVING COUNT(*) > 1)"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(duplicate_count, 0)

    def test_malformed_card_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalogue_root = Path(tmp) / "v1"
            _write_min_catalogue(catalogue_root)
            bad_path = catalogue_root / "catalog" / "pokemon" / "en" / "cards" / "bad.json"
            bad_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": "1.0.0",
                        "generatedAtUtc": "2026-01-01T00:00:00Z",
                        "setId": "bad",
                        "setName": "Bad",
                        "cards": [{"language": "en", "setId": "bad", "collectorNumber": "1", "name": "No Id"}],
                    }
                ),
                encoding="utf-8",
            )
            records = list(iter_catalogue_cards(catalogue_root))
            self.assertEqual(len(records), 4)


if __name__ == "__main__":
    unittest.main()
