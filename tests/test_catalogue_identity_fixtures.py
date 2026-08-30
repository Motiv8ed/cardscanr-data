#!/usr/bin/env python3
"""Positive and negative fixtures for variant-safe catalogue identity/dedup."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cardscanr_catalogue_identity import (  # noqa: E402
    canonical_identity_key,
    classify_pair,
    collector_position_key,
    names_compatible,
    parse_collector_number,
)


class CollectorParserTests(unittest.TestCase):
    def test_numeric_fraction_collapses(self) -> None:
        self.assertEqual(collector_position_key("024/086"), "24")
        self.assertEqual(collector_position_key("24"), "24")
        self.assertEqual(collector_position_key("24/86"), "24")

    def test_prefixed_fraction_collapses(self) -> None:
        self.assertEqual(collector_position_key("SV1"), "sv1")
        self.assertEqual(collector_position_key("SV1/SV94"), "sv1")
        self.assertEqual(collector_position_key("SV001"), "sv1")
        self.assertEqual(collector_position_key("SV001/SV122"), "sv1")
        self.assertEqual(collector_position_key("TG01"), "tg1")
        self.assertEqual(collector_position_key("TG01/TG30"), "tg1")
        self.assertEqual(collector_position_key("GG01"), "gg1")
        self.assertEqual(collector_position_key("GG01/GG70"), "gg1")
        self.assertEqual(collector_position_key("RC1/RC32"), "rc1")
        self.assertEqual(collector_position_key("SWSH001"), "swsh1")
        self.assertEqual(collector_position_key("SM001"), "sm1")
        self.assertEqual(collector_position_key("XY001"), "xy1")

    def test_prefix_never_collapses_onto_bare_number(self) -> None:
        self.assertNotEqual(collector_position_key("TG01/TG30"), "1")
        self.assertNotEqual(collector_position_key("SV1"), "1")
        self.assertNotEqual(collector_position_key("GG01"), "1")

    def test_letter_suffix_stays_distinct(self) -> None:
        self.assertEqual(collector_position_key("98a"), "98a")
        self.assertNotEqual(collector_position_key("98a"), "98")
        self.assertEqual(collector_position_key("001M"), "1m")
        self.assertNotEqual(collector_position_key("001M"), "1")

    def test_suffix_fraction_parses_losslessly(self) -> None:
        cases = [
            ("002a/131", 2, "a", 131, "2a"),
            ("039a/147", 39, "a", 147, "39a"),
            ("098a/122", 98, "a", 122, "98a"),
            ("098b/122", 98, "b", 122, "98b"),
            ("146a/162", 146, "a", 162, "146a"),
            ("148a/168", 148, "a", 168, "148a"),
            ("152a/181", 152, "a", 181, "152a"),
            ("152b/181", 152, "b", 181, "152b"),
            ("182a/214", 182, "a", 214, "182a"),
            ("182b/214", 182, "b", 214, "182b"),
            ("195a/214", 195, "a", 214, "195a"),
            ("206a/236", 206, "a", 236, "206a"),
        ]
        for raw, num, suf, den, pos in cases:
            with self.subTest(raw=raw):
                parsed = parse_collector_number(raw)
                self.assertTrue(parsed.parse_ok, parsed.reason)
                self.assertEqual(parsed.numerator, num)
                self.assertEqual(parsed.suffix, suf)
                self.assertEqual(parsed.denominator, den)
                self.assertEqual(parsed.position_key, pos)
                self.assertEqual(collector_position_key(raw), pos)
        # Exact A/B variants remain distinct; bare numerator stays distinct.
        self.assertNotEqual(collector_position_key("098a/122"), collector_position_key("098b/122"))
        self.assertNotEqual(collector_position_key("148a/168"), collector_position_key("148"))
        self.assertNotEqual(collector_position_key("182b/214"), collector_position_key("182"))
        # Leading zeroes normalize safely.
        self.assertEqual(collector_position_key("002a/131"), collector_position_key("2a"))
        self.assertEqual(collector_position_key("002a/131"), collector_position_key("2a/131"))

    def test_provider_appended_collector_stripped_from_name(self) -> None:
        from cardscanr_catalogue_identity import name_fingerprint

        self.assertEqual(
            name_fingerprint("Tate and Liza 148a 168"),
            name_fingerprint("Tate & Liza"),
        )
        # After collector-fragment strip, trailing bare numbers (including
        # Pokégear "30" from 3.0) normalize away so both forms fingerprint equal.
        self.assertEqual(
            name_fingerprint("Pokegear 30 182b 214"),
            name_fingerprint("Pokegear 30"),
        )
        self.assertEqual(
            name_fingerprint("Professors Research 189 198"),
            name_fingerprint("Professor's Research"),
        )
        self.assertEqual(
            name_fingerprint("Unit Energy GRW Secret Rare"),
            name_fingerprint("Unit Energy GrassFireWater"),
        )
        self.assertEqual(
            name_fingerprint("Garbodor 51a 145 Cosmos Holo"),
            name_fingerprint("Garbodor Cosmos Holo"),
        )
    def test_conflicting_prefix_fraction_fails_closed(self) -> None:
        parsed = parse_collector_number("SV1/TG30")
        self.assertFalse(parsed.parse_ok)
        self.assertEqual(parsed.reason, "conflicting_prefix_fraction")


class NameCompatibilityTests(unittest.TestCase):
    def test_accent_and_punctuation_variants_compatible(self) -> None:
        self.assertTrue(names_compatible("Pokémon", "Pokemon"))
        self.assertTrue(names_compatible("Mr. Mime", "Mr Mime"))
        self.assertTrue(names_compatible("Type: Null", "Type Null"))
        self.assertTrue(names_compatible("Charizard-GX", "Charizard GX"))
        self.assertTrue(names_compatible("Electivire LVX", "Electivire LV.X"))
        self.assertTrue(names_compatible("Anthea and Concordia", "Anthea & Concordia"))
        self.assertTrue(names_compatible("EXPALL", "EXP.ALL"))
        self.assertTrue(
            names_compatible("Here Comes Team Rocket", "Here Comes Team Rocket!")
        )
        self.assertTrue(names_compatible("Drowsee", "Drowzee"))
        self.assertTrue(names_compatible("Dark Exeggcutor", "Dark Exeggutor"))

    def test_incompatible_distinct_names(self) -> None:
        self.assertFalse(names_compatible("Claydol", "Venusaur"))
        self.assertFalse(names_compatible("Here Comes Team Rocket!", "Rocket's Zapdos"))


class DedupClassificationFixtures(unittest.TestCase):
    def test_true_duplicates_should_collapse(self) -> None:
        cases = [
            (
                {"name": "Avalugg", "collectorNumber": "24", "imageSource": "pokemon_tcg_api"},
                {
                    "name": "Avalugg",
                    "collectorNumber": "024/086",
                    "imageSource": "pokewallet",
                },
            ),
            (
                {"name": "Scyther", "collectorNumber": "SV1", "imageSource": "pokemon_tcg_api"},
                {
                    "name": "Scyther",
                    "collectorNumber": "SV1/SV94",
                    "imageSource": "pokewallet",
                },
            ),
            (
                {
                    "name": "Pokémon Fan Club",
                    "collectorNumber": "9",
                    "imageSource": "pokemon_tcg_api",
                },
                {
                    "name": "Pokemon Fan Club",
                    "collectorNumber": "009/017",
                    "imageSource": "pokewallet",
                },
            ),
        ]
        for kept, dropped in cases:
            with self.subTest(kept=kept["collectorNumber"], dropped=dropped["collectorNumber"]):
                self.assertEqual(
                    classify_pair(kept, dropped, language="en", set_id="x"),
                    "TRUE_DUPLICATE",
                )
                self.assertEqual(
                    canonical_identity_key(
                        language="en",
                        set_id="me4",
                        collector_number=kept["collectorNumber"],
                        name=kept["name"],
                    ),
                    canonical_identity_key(
                        language="en",
                        set_id="me4",
                        collector_number=dropped["collectorNumber"],
                        name=dropped["name"],
                    ),
                )

    def test_legitimate_distinct_must_not_collapse(self) -> None:
        # cel25c shared local #15
        claydol = {"name": "Claydol", "collectorNumber": "15"}
        venusaur = {"name": "Venusaur", "collectorNumber": "15"}
        self.assertEqual(
            classify_pair(claydol, venusaur, language="en", set_id="cel25c"),
            "FALSE_MERGE",
        )
        self.assertNotEqual(
            canonical_identity_key(
                language="en", set_id="cel25c", collector_number="15", name="Claydol"
            ),
            canonical_identity_key(
                language="en", set_id="cel25c", collector_number="15", name="Venusaur"
            ),
        )
        # same numerator across different sets
        self.assertNotEqual(
            canonical_identity_key(
                language="en", set_id="me4", collector_number="24", name="Avalugg"
            ),
            canonical_identity_key(
                language="en", set_id="base2", collector_number="24", name="Avalugg"
            ),
        )
        # different languages
        self.assertNotEqual(
            canonical_identity_key(
                language="en", set_id="me4", collector_number="24", name="Avalugg"
            ),
            canonical_identity_key(
                language="jp", set_id="me4", collector_number="24", name="Avalugg"
            ),
        )
        # lettered deck vs plain number
        self.assertNotEqual(
            collector_position_key("001M"),
            collector_position_key("1"),
        )


if __name__ == "__main__":
    unittest.main()
