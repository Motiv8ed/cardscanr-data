#!/usr/bin/env python3
"""Regressions for EN/JP production publication validator closeout blockers."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Zsv10pt5PriceIdentityTests(unittest.TestCase):
    def test_antique_cover_fossil_collector_matches_canonical(self) -> None:
        path = ROOT / "public/v1/prices/current/pokemon/en/zsv10pt5.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        prices = payload.get("prices") or []
        targets = [
            entry
            for entry in prices
            if isinstance(entry, dict)
            and "antique_cover_fossil" in str(entry.get("canonicalCardId") or "")
        ]
        self.assertGreaterEqual(len(targets), 1)
        for entry in targets:
            cid = str(entry.get("canonicalCardId") or "")
            collector = str(entry.get("collectorNumber") or "")
            parts = cid.split("|")
            self.assertGreaterEqual(len(parts), 5)
            self.assertEqual(parts[3], collector)
            expected = (
                f"pokemon|en|{entry.get('setId')}|{collector}|{entry.get('normalizedName')}"
            )
            self.assertEqual(cid, expected)


class SveCardShapeTests(unittest.TestCase):
    def test_sve_energies_have_full_en_shape(self) -> None:
        path = ROOT / "public/v1/catalog/pokemon/en/cards/sve.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        cards = payload.get("cards") or []
        incomplete = [
            c
            for c in cards
            if isinstance(c, dict)
            and (
                c.get("imageSource") == "IMAGE_UNAVAILABLE"
                or not c.get("canonicalBaseId")
                or not c.get("imageSmall")
                or not c.get("normalizedName")
            )
        ]
        self.assertEqual(incomplete, [])
        numbered = {
            str(c.get("collectorNumber")): c
            for c in cards
            if isinstance(c, dict) and str(c.get("collectorNumber")) in {f"{n:03d}" for n in range(17, 25)}
        }
        self.assertEqual(len(numbered), 8)
        for num, card in numbered.items():
            self.assertEqual(card.get("imageSource"), "pokemon_tcg_api")
            self.assertIn(f"sve-{int(num)}", str(card.get("imageSmall") or ""))
            self.assertTrue(str(card.get("canonicalBaseId") or "").startswith(f"pokemon|en|sve|{num}|"))


class SwshpCardShapeTests(unittest.TestCase):
    def test_swshp_late_promos_use_allowed_image_source(self) -> None:
        path = ROOT / "public/v1/catalog/pokemon/en/cards/swshp.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        cards = {
            str(c.get("collectorNumber")): c
            for c in (payload.get("cards") or [])
            if isinstance(c, dict)
            and str(c.get("collectorNumber")) in {"SWSH299", "SWSH300", "SWSH301"}
        }
        self.assertEqual(set(cards), {"SWSH299", "SWSH300", "SWSH301"})
        for num, card in cards.items():
            self.assertEqual(card.get("imageSource"), "pokemon_tcg_api")
            self.assertNotEqual(card.get("imageSource"), "pokemon_com_cdn")
            self.assertTrue(card.get("canonicalBaseId"))
            self.assertTrue(card.get("normalizedName"))
            self.assertTrue(card.get("imageSmall"))
            self.assertIn(f"swshp-{num}", str(card.get("imageSmall") or ""))


class ProviderDuplicateCollapseTests(unittest.TestCase):
    def test_shiny_vault_style_sets_have_no_pokewallet_true_duplicates(self) -> None:
        from cardscanr_catalogue_identity import collector_position_key, names_compatible, parse_collector_number
        import re

        digit_group = re.compile(r"\d+")

        def dedupe_key(num: object) -> str:
            parsed = parse_collector_number(num)
            if parsed.parse_ok:
                return parsed.position_key
            left = str(num or "").strip().split("/", 1)[0]
            left = digit_group.sub(lambda m: str(int(m.group(0))), left)
            return left.casefold()

        for sid in ("swsh45sv", "sma", "swsh12pt5gg", "ecard2", "ecard3", "ex10"):
            path = ROOT / f"public/v1/catalog/pokemon/en/cards/{sid}.json"
            cards = json.loads(path.read_text(encoding="utf-8")).get("cards") or []
            tcg_by_key: dict[str, list[dict]] = {}
            for card in cards:
                if card.get("imageSource") == "pokemon_tcg_api":
                    tcg_by_key.setdefault(dedupe_key(card.get("collectorNumber")), []).append(card)
            leftovers = []
            for card in cards:
                if card.get("imageSource") != "pokewallet":
                    continue
                peers = tcg_by_key.get(dedupe_key(card.get("collectorNumber"))) or []
                if any(names_compatible(card.get("name"), peer.get("name")) for peer in peers):
                    leftovers.append(card.get("collectorNumber"))
            self.assertEqual(leftovers, [], msg=f"{sid} still has pokewallet true duplicates")


if __name__ == "__main__":
    unittest.main()
