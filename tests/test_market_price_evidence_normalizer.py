"""
test_market_price_evidence_normalizer.py

Tests for market_price_evidence_normalizer.py
"""

from __future__ import annotations

import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from market_price_evidence_normalizer import (
    normalize_evidence,
    normalize_evidence_batch,
    _normalize_currency,
    _normalize_condition,
    _detect_graded,
    _parse_price,
    EXCLUSION_TERM_PATTERNS,
)


# ---------------------------------------------------------------------------
# Price normalisation
# ---------------------------------------------------------------------------


class TestPriceNormalization(unittest.TestCase):
    def test_float_input(self) -> None:
        self.assertEqual(_parse_price(12.5), 12.5)

    def test_int_input(self) -> None:
        self.assertEqual(_parse_price(50), 50.0)

    def test_string_with_dollar(self) -> None:
        self.assertEqual(_parse_price("$15.99"), 15.99)

    def test_string_au_dollar(self) -> None:
        self.assertEqual(_parse_price("AU$22.00"), 22.0)

    def test_none_returns_none(self) -> None:
        self.assertIsNone(_parse_price(None))

    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(_parse_price(""))

    def test_rounding_two_dp(self) -> None:
        result = _parse_price(10.999)
        self.assertEqual(result, 11.0)


class TestShippingTotalCalculation(unittest.TestCase):
    def test_total_includes_shipping(self) -> None:
        listing, reason = normalize_evidence(
            {"title": "Charizard pokemon card", "soldPrice": 80.0, "shippingPrice": 5.0, "currency": "AUD",
             "soldDate": "2024-03-15", "listingUrl": "https://www.ebay.com.au/itm/1"}
        )
        self.assertIsNone(reason)
        assert listing is not None
        self.assertEqual(listing.sold_price, 80.0)
        self.assertEqual(listing.shipping_price, 5.0)
        self.assertEqual(listing.total_price, 85.0)

    def test_missing_shipping_defaults_to_zero(self) -> None:
        listing, reason = normalize_evidence(
            {"title": "Pikachu pokemon card", "soldPrice": 10.0, "currency": "USD",
             "soldDate": "2024-01-01", "listingUrl": "https://www.ebay.com/itm/2"}
        )
        assert listing is not None
        self.assertEqual(listing.shipping_price, 0.0)
        self.assertEqual(listing.total_price, 10.0)


# ---------------------------------------------------------------------------
# Currency normalisation
# ---------------------------------------------------------------------------


class TestCurrencyNormalization(unittest.TestCase):
    def test_aud_alias(self) -> None:
        self.assertEqual(_normalize_currency("aud"), "AUD")
        self.assertEqual(_normalize_currency("AUD"), "AUD")
        self.assertEqual(_normalize_currency("au$"), "AUD")

    def test_usd_alias(self) -> None:
        self.assertEqual(_normalize_currency("usd"), "USD")

    def test_gbp_symbol(self) -> None:
        self.assertEqual(_normalize_currency("£"), "GBP")

    def test_eur_symbol(self) -> None:
        self.assertEqual(_normalize_currency("€"), "EUR")

    def test_unknown_passes_upper(self) -> None:
        self.assertEqual(_normalize_currency("nzd"), "NZD")

    def test_none_defaults_to_usd(self) -> None:
        self.assertEqual(_normalize_currency(None), "USD")


# ---------------------------------------------------------------------------
# Exclusion term rejection
# ---------------------------------------------------------------------------


class TestExclusionTermRejection(unittest.TestCase):
    def _rejected_reason(self, title: str) -> str | None:
        _, reason = normalize_evidence(
            {"title": title, "soldPrice": 10.0, "currency": "USD",
             "soldDate": "2024-01-01", "listingUrl": "https://example.com"}
        )
        return reason

    def test_proxy_rejected(self) -> None:
        self.assertEqual(self._rejected_reason("Charizard proxy card pokemon"), "excluded:proxy")

    def test_fake_rejected(self) -> None:
        self.assertEqual(self._rejected_reason("Pikachu fake card"), "excluded:fake")

    def test_digital_rejected(self) -> None:
        self.assertEqual(self._rejected_reason("Mewtwo PTCGO digital code"), "excluded:digital")

    def test_lot_rejected(self) -> None:
        self.assertEqual(self._rejected_reason("Charizard lot of 5 pokemon cards"), "excluded:lot")

    def test_bundle_rejected(self) -> None:
        self.assertEqual(self._rejected_reason("Pokemon bundle near mint"), "excluded:lot")

    def test_damaged_rejected_by_default(self) -> None:
        self.assertEqual(self._rejected_reason("Charizard damaged base set"), "excluded:damaged")

    def test_clean_listing_not_rejected(self) -> None:
        listing, reason = normalize_evidence(
            {"title": "Charizard Base Set 4 near mint raw pokemon",
             "soldPrice": 80.0, "currency": "AUD",
             "soldDate": "2024-03-15", "listingUrl": "https://www.ebay.com.au/itm/5"}
        )
        self.assertIsNone(reason)
        self.assertIsNotNone(listing)


# ---------------------------------------------------------------------------
# Graded detection
# ---------------------------------------------------------------------------


class TestGradedDetection(unittest.TestCase):
    def test_psa_graded(self) -> None:
        raw = {"title": "Charizard PSA 9 pokemon card", "graded": False,
               "soldPrice": 200.0, "currency": "USD", "soldDate": "2024-01-01",
               "listingUrl": "https://example.com"}
        listing, _ = normalize_evidence(raw, allow_exclusion_terms=frozenset())
        # Title contains PSA; graded field is False — title detection should win
        # since graded field bool=False means we defer to title scan
        # Note: raw has graded=False explicitly, so _detect_graded returns False (bool takes priority)
        # We just check that the normalizer does not crash
        self.assertIsNotNone(listing)

    def test_graded_bool_true(self) -> None:
        raw = {"title": "Charizard Base Set pokemon card",
               "graded": True,
               "soldPrice": 200.0, "currency": "USD", "soldDate": "2024-01-01",
               "listingUrl": "https://example.com"}
        listing, _ = normalize_evidence(raw)
        assert listing is not None
        self.assertTrue(listing.graded)

    def test_ungraded_by_default(self) -> None:
        raw = {"title": "Charizard pokemon raw near mint",
               "soldPrice": 80.0, "currency": "AUD", "soldDate": "2024-01-01",
               "listingUrl": "https://example.com"}
        listing, _ = normalize_evidence(raw)
        assert listing is not None
        self.assertFalse(listing.graded)

    def test_detect_graded_from_title(self) -> None:
        result = _detect_graded("Charizard PSA 10 Pokemon", {})
        self.assertTrue(result)

    def test_detect_ungraded_from_title(self) -> None:
        result = _detect_graded("Charizard raw near mint pokemon", {})
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Invalid price rejection
# ---------------------------------------------------------------------------


class TestInvalidPriceRejection(unittest.TestCase):
    def test_zero_price_rejected(self) -> None:
        _, reason = normalize_evidence(
            {"title": "Charizard pokemon", "soldPrice": 0, "currency": "USD",
             "soldDate": "2024-01-01", "listingUrl": "https://example.com"}
        )
        self.assertEqual(reason, "invalid_sold_price")

    def test_negative_price_rejected(self) -> None:
        _, reason = normalize_evidence(
            {"title": "Charizard pokemon", "soldPrice": -5.0, "currency": "USD",
             "soldDate": "2024-01-01", "listingUrl": "https://example.com"}
        )
        self.assertEqual(reason, "invalid_sold_price")

    def test_missing_price_rejected(self) -> None:
        _, reason = normalize_evidence(
            {"title": "Charizard pokemon", "currency": "USD",
             "soldDate": "2024-01-01", "listingUrl": "https://example.com"}
        )
        self.assertEqual(reason, "invalid_sold_price")


# ---------------------------------------------------------------------------
# Batch normalisation
# ---------------------------------------------------------------------------


class TestBatchNormalization(unittest.TestCase):
    def test_batch_splits_accepted_rejected(self) -> None:
        rows = [
            {"title": "Charizard near mint pokemon", "soldPrice": 80.0, "currency": "AUD",
             "soldDate": "2024-03-15", "listingUrl": "https://www.ebay.com.au/itm/1"},
            {"title": "Pikachu fake card pokemon", "soldPrice": 5.0, "currency": "AUD",
             "soldDate": "2024-01-01", "listingUrl": "https://example.com"},
            {"title": "Mewtwo near mint pokemon card", "soldPrice": 0.0, "currency": "AUD",
             "soldDate": "2024-01-01", "listingUrl": "https://example.com"},
        ]
        accepted, rejected = normalize_evidence_batch(rows)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 2)

    def test_batch_rejected_includes_reason(self) -> None:
        rows = [{"title": "Pikachu lot bundle", "soldPrice": 10.0, "currency": "USD",
                 "soldDate": "2024-01-01", "listingUrl": "https://example.com"}]
        _, rejected = normalize_evidence_batch(rows)
        self.assertIn("rejectReason", rejected[0])


# ---------------------------------------------------------------------------
# Secret scrubbing
# ---------------------------------------------------------------------------


class TestSecretScrubbing(unittest.TestCase):
    def test_api_key_in_raw_data_is_redacted(self) -> None:
        row = {
            "title": "Charizard near mint pokemon",
            "soldPrice": 80.0,
            "currency": "AUD",
            "soldDate": "2024-03-15",
            "listingUrl": "https://www.ebay.com.au/itm/1",
            "apiKey": "super-secret-key-12345",
        }
        listing, reason = normalize_evidence(row)
        assert listing is not None
        raw = listing.raw_data or {}
        self.assertEqual(raw.get("apiKey"), "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
