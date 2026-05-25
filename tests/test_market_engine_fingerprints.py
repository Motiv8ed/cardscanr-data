from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.fingerprints import build_market_price_fingerprint


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_normalizes_and_uses_set_name_fallback(self) -> None:
        fingerprint = build_market_price_fingerprint(
            game=" Pokemon ",
            language=" EN ",
            set_code="",
            set_name="Base Set",
            collector_number=" 4/102 ",
            card_name=" Charizard ",
            variant=" RAW ",
            condition=" Near Mint ",
            market_country=" US ",
            currency=" usd ",
        )
        self.assertEqual(
            fingerprint,
            "pokemon|en|base_set|4/102|charizard|raw|near mint|us|usd",
        )

    def test_fingerprint_is_deterministic(self) -> None:
        left = build_market_price_fingerprint(
            game="pokemon",
            language="en",
            set_code="base1",
            set_name="Base Set",
            collector_number="4",
            card_name="Charizard",
            variant="raw",
            condition="near_mint",
            market_country="us",
            currency="usd",
        )
        right = build_market_price_fingerprint(
            game="pokemon",
            language="en",
            set_code="base1",
            set_name="Base Set",
            collector_number="4",
            card_name="Charizard",
            variant="raw",
            condition="near_mint",
            market_country="us",
            currency="usd",
        )
        self.assertEqual(left, right)


if __name__ == "__main__":
    unittest.main()
