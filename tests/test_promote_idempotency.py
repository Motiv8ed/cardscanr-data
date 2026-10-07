#!/usr/bin/env python3
"""Regression: promote of the same EN set twice must be idempotent."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import promote_provider_catalog_to_app_catalog as promote  # noqa: E402


class PromoteIdempotencyTests(unittest.TestCase):
    def test_collector_identity_collapses_slash_and_plain(self) -> None:
        self.assertEqual(promote.collector_identity_key("024/086"), "24")
        self.assertEqual(promote.collector_identity_key("24"), "24")
        self.assertEqual(promote.collector_identity_key("SV1"), "sv1")
        self.assertEqual(promote.collector_identity_key("SV1/SV94"), "sv1")
        self.assertEqual(promote.collector_identity_key("SV001"), "sv1")
        self.assertEqual(promote.collector_identity_key("SV001/SV122"), "sv1")
        self.assertEqual(promote.collector_identity_key("TG01/TG30"), "tg1")
        self.assertEqual(promote.collector_identity_key("TG01"), "tg1")
        self.assertNotEqual(promote.collector_identity_key("TG01/TG30"), "1")
        self.assertNotEqual(promote.collector_identity_key("98a"), "98")

    def test_position_gate_requires_compatible_names(self) -> None:
        position_names = {
            promote.make_position_key("en", "cel25c", "15"): ["Claydol"],
        }
        self.assertTrue(
            promote.position_already_represented(
                position_names,
                language="en",
                set_id="cel25c",
                collector_number="15/25",
                display_name="Claydol",
            )
        )
        self.assertFalse(
            promote.position_already_represented(
                position_names,
                language="en",
                set_id="cel25c",
                collector_number="15",
                display_name="Venusaur",
            )
        )

    def test_second_promote_pass_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_root = root / "catalog" / "pokemon"
            provider_root = root / "provider-catalog" / "pokewallet" / "cards"
            (app_root / "en" / "cards").mkdir(parents=True)
            (provider_root / "en").mkdir(parents=True)
            (root / "reports").mkdir()

            (app_root / "en" / "sets.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": "1.0.0",
                        "language": "en",
                        "sets": [
                            {
                                "id": "me4",
                                "name": "Chaos Rising",
                                "ptcgoCode": "CRI",
                                "printedTotal": 86,
                                "total": 122,
                            }
                        ],
                        "setCount": 1,
                        "cardCount": 1,
                    }
                ),
                encoding="utf-8",
            )
            (app_root / "en" / "cards" / "me4.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": "1.0.0",
                        "language": "en",
                        "setId": "me4",
                        "setName": "Chaos Rising",
                        "cardCount": 1,
                        "cards": [
                            {
                                "canonicalBaseId": "pokemon|en|me4|24|avalugg",
                                "name": "Avalugg",
                                "normalizedName": "avalugg",
                                "collectorNumber": "24",
                                "imageSource": "pokemon_tcg_api",
                                "imageSmall": "https://images.scrydex.com/pokemon/me4-24/small",
                                "imageLarge": "https://images.scrydex.com/pokemon/me4-24/large",
                                "providerIds": {},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (provider_root / "en" / "me4.json").write_text(
                json.dumps(
                    {
                        "providerSetId": "me4",
                        "providerSetCode": "CRI",
                        "providerSetName": "Chaos Rising",
                        "cards": [
                            {
                                "providerCardId": "pw-avalugg-24",
                                "cardScanRLanguage": "en",
                                "providerSetId": "me4",
                                "providerSetCode": "CRI",
                                "providerSetName": "Chaos Rising",
                                "cardNumber": "024/086",
                                "cleanName": "Avalugg",
                                "name": "Avalugg",
                                "imageUrlSmall": "https://api.pokewallet.io/images/x?size=low",
                                "imageUrlLarge": "https://api.pokewallet.io/images/x?size=high",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            old_app, old_provider, old_reports = (
                promote.APP_ROOT,
                promote.PROVIDER_ROOT,
                promote.REPORTS_DIR,
            )
            try:
                promote.APP_ROOT = app_root
                promote.PROVIDER_ROOT = provider_root
                promote.REPORTS_DIR = root / "reports"
                first = promote.promote(
                    ["en"], include_zh=False, dry_run=False, write_reports=False
                )
                second = promote.promote(
                    ["en"], include_zh=False, dry_run=False, write_reports=False
                )
            finally:
                promote.APP_ROOT = old_app
                promote.PROVIDER_ROOT = old_provider
                promote.REPORTS_DIR = old_reports

            cards = json.loads(
                (app_root / "en" / "cards" / "me4.json").read_text(encoding="utf-8")
            )["cards"]
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0]["collectorNumber"], "24")
            self.assertGreaterEqual(
                int(first.get("providerCardsAlreadyRepresentedByLanguage", {}).get("en", 0)),
                1,
            )
            self.assertGreaterEqual(
                int(second.get("providerCardsAlreadyRepresentedByLanguage", {}).get("en", 0)),
                1,
            )

    def test_three_identical_promotes_remain_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_root = root / "catalog" / "pokemon"
            provider_root = root / "provider-catalog" / "pokewallet" / "cards"
            (app_root / "en" / "cards").mkdir(parents=True)
            (provider_root / "en").mkdir(parents=True)
            (root / "reports").mkdir()
            (app_root / "en" / "sets.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": "1.0.0",
                        "language": "en",
                        "sets": [
                            {
                                "id": "sma",
                                "name": "Hidden Fates Shiny Vault",
                                "printedTotal": 94,
                                "total": 94,
                            }
                        ],
                        "setCount": 1,
                        "cardCount": 1,
                    }
                ),
                encoding="utf-8",
            )
            (app_root / "en" / "cards" / "sma.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": "1.0.0",
                        "language": "en",
                        "setId": "sma",
                        "setName": "Hidden Fates Shiny Vault",
                        "cardCount": 1,
                        "cards": [
                            {
                                "canonicalBaseId": "pokemon|en|sma|SV1|scyther",
                                "name": "Scyther",
                                "normalizedName": "scyther",
                                "collectorNumber": "SV1",
                                "imageSource": "pokemon_tcg_api",
                                "imageSmall": "https://images.pokemontcg.io/sma/SV1.png",
                                "providerIds": {},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            provider_payload = {
                "providerSetId": "sma",
                "providerSetCode": "HIF",
                "providerSetName": "Hidden Fates Shiny Vault",
                "cards": [
                    {
                        "providerCardId": "pw-scyther-sv1",
                        "cardScanRLanguage": "en",
                        "providerSetId": "sma",
                        "cardNumber": "SV1/SV94",
                        "cleanName": "Scyther",
                        "name": "Scyther",
                        "imageUrlSmall": "https://api.pokewallet.io/images/x?size=low",
                    }
                ],
            }
            (provider_root / "en" / "sma.json").write_text(
                json.dumps(provider_payload), encoding="utf-8"
            )

            old_app, old_provider, old_reports = (
                promote.APP_ROOT,
                promote.PROVIDER_ROOT,
                promote.REPORTS_DIR,
            )
            counts = []
            try:
                promote.APP_ROOT = app_root
                promote.PROVIDER_ROOT = provider_root
                promote.REPORTS_DIR = root / "reports"
                for _ in range(3):
                    promote.promote(
                        ["en"], include_zh=False, dry_run=False, write_reports=False
                    )
                    cards = json.loads(
                        (app_root / "en" / "cards" / "sma.json").read_text(encoding="utf-8")
                    )["cards"]
                    counts.append(len(cards))
                # Harmless metadata change should not insert a new card.
                provider_payload["cards"][0]["cleanName"] = "Scythér"
                provider_payload["cards"][0]["imageUrlSmall"] = (
                    "https://api.pokewallet.io/images/y?size=low"
                )
                (provider_root / "en" / "sma.json").write_text(
                    json.dumps(provider_payload), encoding="utf-8"
                )
                promote.promote(
                    ["en"], include_zh=False, dry_run=False, write_reports=False
                )
                cards_after = json.loads(
                    (app_root / "en" / "cards" / "sma.json").read_text(encoding="utf-8")
                )["cards"]
            finally:
                promote.APP_ROOT = old_app
                promote.PROVIDER_ROOT = old_provider
                promote.REPORTS_DIR = old_reports

            self.assertEqual(counts, [1, 1, 1])
            self.assertEqual(len(cards_after), 1)
            self.assertEqual(cards_after[0]["collectorNumber"], "SV1")


if __name__ == "__main__":
    unittest.main()
