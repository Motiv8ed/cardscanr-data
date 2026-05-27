from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.import_manual_sold_listings import (
    SOURCE_ID,
    aggregate,
    build_report,
    filter_row,
    match_row,
    normalize_condition,
    normalize_currency,
    normalize_date,
    normalize_graded,
    normalize_language,
    normalize_market,
    normalize_marketplace,
    normalize_price,
    normalize_row,
    normalize_variant,
    run_import,
    update_market_price_status,
    write_market_price_files,
    _redact_suspicious,
)


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _row(
    title: str = "Charizard Base Set 4/102 Raw NM",
    sold_price: float = 250.0,
    shipping_price: float = 5.0,
    total_price: float | None = None,
    currency: str = "AUD",
    sold_date: str = "2026-05-20",
    listing_url: str = "https://www.ebay.com.au/itm/test-001",
    marketplace: str = "ebay_au",
    market: str = "AU",
    condition: str = "near_mint",
    graded: str = "false",
    card_name: str = "Charizard",
    set_name: str = "Base",
    set_id: str = "base1",
    collector_number: str = "4",
    language: str = "en",
    variant: str = "raw",
    canonical_id: str = "",
) -> dict:
    return {
        "title": title,
        "soldPrice": sold_price,
        "shippingPrice": shipping_price,
        "totalPrice": total_price,
        "currency": currency,
        "soldDate": sold_date,
        "listingUrl": listing_url,
        "marketplace": marketplace,
        "market": market,
        "condition": condition,
        "graded": graded,
        "cardName": card_name,
        "setName": set_name,
        "setId": set_id,
        "collectorNumber": collector_number,
        "language": language,
        "variant": variant,
        "canonicalId": canonical_id,
    }


def _norm(raw: dict) -> dict:
    return normalize_row(raw)


# ---------------------------------------------------------------------------
# Exclusion term tests
# ---------------------------------------------------------------------------

class ExclusionTermTests(unittest.TestCase):
    def _check(self, title: str, expected_reason: str) -> None:
        row = _norm(_row(title=title))
        ok, reason = filter_row(row)
        self.assertFalse(ok, f"Expected exclusion for: {title!r}")
        self.assertEqual(reason, expected_reason)

    def test_excludes_proxy(self) -> None:
        self._check("Charizard Base Set 4 proxy custom card", "proxy_or_custom")

    def test_excludes_digital(self) -> None:
        self._check("Charizard PTCGO digital code card", "digital")

    def test_excludes_lot(self) -> None:
        self._check("Charizard lot x3 pokemon cards", "lot_or_bundle")

    def test_excludes_bundle(self) -> None:
        self._check("Charizard bundle collection pokemon", "lot_or_bundle")

    def test_excludes_fake(self) -> None:
        self._check("Charizard fake pokemon card replica", "fake")

    def test_allows_lots_when_flag_set(self) -> None:
        row = _norm(_row(title="Charizard lot x3 pokemon cards"))
        ok, reason = filter_row(row, allow_lots=True)
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_excludes_damaged_by_default(self) -> None:
        row = _norm(_row(condition="damaged"))
        ok, reason = filter_row(row)
        self.assertFalse(ok)
        self.assertEqual(reason, "damaged_excluded")

    def test_allows_damaged_when_flag_set(self) -> None:
        row = _norm(_row(condition="damaged"))
        ok, reason = filter_row(row, allow_damaged=True)
        self.assertTrue(ok)


# ---------------------------------------------------------------------------
# Amount parsing tests
# ---------------------------------------------------------------------------

class AmountParsingTests(unittest.TestCase):
    def test_parses_plain_float(self) -> None:
        self.assertEqual(normalize_price("250.00"), 250.0)

    def test_parses_int_string(self) -> None:
        self.assertEqual(normalize_price("250"), 250.0)

    def test_strips_currency_symbol(self) -> None:
        self.assertEqual(normalize_price("$250.00"), 250.0)

    def test_strips_au_dollar(self) -> None:
        self.assertEqual(normalize_price("AUD250"), 250.0)

    def test_returns_none_for_empty(self) -> None:
        self.assertIsNone(normalize_price(""))

    def test_returns_none_for_none(self) -> None:
        self.assertIsNone(normalize_price(None))

    def test_returns_none_for_negative(self) -> None:
        self.assertIsNone(normalize_price(-5.0))

    def test_rejects_invalid_price(self) -> None:
        row = _norm(_row(sold_price=None, total_price=None))  # type: ignore[arg-type]
        ok, reason = filter_row(row)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_sold_price")


# ---------------------------------------------------------------------------
# Shipping inclusion tests
# ---------------------------------------------------------------------------

class ShippingInclusionTests(unittest.TestCase):
    def test_calculates_total_from_sold_plus_shipping(self) -> None:
        row = _norm(_row(sold_price=250.0, shipping_price=5.0, total_price=None))
        self.assertEqual(row["totalPrice"], 255.0)

    def test_uses_explicit_total_when_provided(self) -> None:
        row = _norm(_row(sold_price=250.0, shipping_price=5.0, total_price=260.0))
        self.assertEqual(row["totalPrice"], 260.0)

    def test_aggregate_marks_shipping_included(self) -> None:
        rows = [_norm(_row(sold_price=250.0, shipping_price=5.0, total_price=255.0))]
        matches = [match_row(r) for r in rows]
        aggs = aggregate(rows, matches)
        self.assertTrue(aggs[0]["shippingIncluded"])

    def test_aggregate_marks_shipping_not_included_when_zero(self) -> None:
        rows = [_norm(_row(sold_price=250.0, shipping_price=0.0, total_price=250.0))]
        matches = [match_row(r) for r in rows]
        aggs = aggregate(rows, matches)
        self.assertFalse(aggs[0]["shippingIncluded"])


# ---------------------------------------------------------------------------
# Date normalisation tests
# ---------------------------------------------------------------------------

class DateNormalisationTests(unittest.TestCase):
    def test_iso_date(self) -> None:
        self.assertEqual(normalize_date("2026-05-20"), "2026-05-20")

    def test_iso_datetime_z(self) -> None:
        self.assertEqual(normalize_date("2026-05-20T12:30:00Z"), "2026-05-20")

    def test_slash_dmy(self) -> None:
        self.assertEqual(normalize_date("20/05/2026"), "2026-05-20")

    def test_slash_ymd(self) -> None:
        self.assertEqual(normalize_date("2026/05/20"), "2026-05-20")

    def test_none_for_empty(self) -> None:
        self.assertIsNone(normalize_date(""))

    def test_none_for_none(self) -> None:
        self.assertIsNone(normalize_date(None))

    def test_filters_missing_date(self) -> None:
        row = _norm(_row(sold_date=""))
        ok, reason = filter_row(row)
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_sold_date")


# ---------------------------------------------------------------------------
# Matching tests
# ---------------------------------------------------------------------------

class MatchingTests(unittest.TestCase):
    def test_matching_with_canonical_id(self) -> None:
        row = _norm(_row(canonical_id="pokemon|en|base1|4|charizard"))
        result = match_row(row)
        self.assertEqual(result["status"], "matched_canonical")
        self.assertEqual(result["canonicalId"], "pokemon|en|base1|4|charizard")

    def test_matching_derived_by_name_set_collector(self) -> None:
        row = _norm(_row(card_name="Charizard", set_id="base1", collector_number="4", language="en"))
        result = match_row(row)
        self.assertEqual(result["status"], "matched_derived")
        self.assertIn("charizard", result["canonicalId"])
        self.assertIn("base1", result["canonicalId"])
        self.assertIn("4", result["canonicalId"])

    def test_unmatched_when_missing_card_name(self) -> None:
        row = _norm(_row(card_name=""))
        result = match_row(row)
        self.assertEqual(result["status"], "unmatched")
        self.assertEqual(result["reason"], "missing_card_name")

    def test_unmatched_when_missing_set_identity(self) -> None:
        row = _norm(_row(set_id="", set_name=""))
        result = match_row(row)
        self.assertEqual(result["status"], "unmatched")
        self.assertEqual(result["reason"], "missing_set_identity")


# ---------------------------------------------------------------------------
# Aggregation tests
# ---------------------------------------------------------------------------

class AggregationTests(unittest.TestCase):
    def _make_rows(self, prices: list[float]) -> tuple[list[dict], list[dict]]:
        rows = [
            _norm(_row(
                sold_price=p - 5.0,
                shipping_price=5.0,
                total_price=p,
                card_name="Charizard",
                set_id="base1",
                collector_number="4",
                language="en",
                condition="near_mint",
            ))
            for p in prices
        ]
        matches = [match_row(r) for r in rows]
        return rows, matches

    def test_median(self) -> None:
        rows, matches = self._make_rows([10.0, 20.0, 30.0])
        aggs = aggregate(rows, matches)
        self.assertEqual(len(aggs), 1)
        self.assertEqual(aggs[0]["medianPrice"], 20.0)

    def test_average(self) -> None:
        rows, matches = self._make_rows([10.0, 20.0, 30.0])
        aggs = aggregate(rows, matches)
        self.assertAlmostEqual(aggs[0]["averagePrice"], 20.0)

    def test_low_price(self) -> None:
        rows, matches = self._make_rows([10.0, 20.0, 30.0])
        aggs = aggregate(rows, matches)
        self.assertEqual(aggs[0]["lowPrice"], 10.0)

    def test_high_price(self) -> None:
        rows, matches = self._make_rows([10.0, 20.0, 30.0])
        aggs = aggregate(rows, matches)
        self.assertEqual(aggs[0]["highPrice"], 30.0)

    def test_sample_count(self) -> None:
        rows, matches = self._make_rows([10.0, 20.0, 30.0])
        aggs = aggregate(rows, matches)
        self.assertEqual(aggs[0]["sampleCount"], 3)

    def test_groups_by_condition(self) -> None:
        nm_rows = [
            _norm(_row(total_price=250.0, condition="near_mint", set_id="base1", collector_number="4", card_name="Charizard")),
        ]
        hp_rows = [
            _norm(_row(total_price=80.0, condition="heavily_played", set_id="base1", collector_number="4", card_name="Charizard")),
        ]
        all_rows = nm_rows + hp_rows
        all_matches = [match_row(r) for r in all_rows]
        aggs = aggregate(all_rows, all_matches)
        conditions = {a["condition"] for a in aggs}
        self.assertIn("near_mint", conditions)
        self.assertIn("heavily_played", conditions)


# ---------------------------------------------------------------------------
# Confidence label tests
# ---------------------------------------------------------------------------

class ConfidenceLabelTests(unittest.TestCase):
    def _agg_with_n_rows(self, n: int) -> dict:
        rows = [
            _norm(_row(
                total_price=float(100 + i),
                card_name="Charizard",
                set_id="base1",
                collector_number="4",
                language="en",
            ))
            for i in range(n)
        ]
        matches = [match_row(r) for r in rows]
        aggs = aggregate(rows, matches)
        self.assertGreater(len(aggs), 0, "Expected at least one aggregate")
        return aggs[0]

    def test_low_confidence_1_sample(self) -> None:
        agg = self._agg_with_n_rows(1)
        self.assertEqual(agg["confidenceLabel"], "low")

    def test_medium_confidence_3_samples(self) -> None:
        agg = self._agg_with_n_rows(3)
        self.assertEqual(agg["confidenceLabel"], "medium")

    def test_high_confidence_8_samples(self) -> None:
        agg = self._agg_with_n_rows(8)
        self.assertEqual(agg["confidenceLabel"], "high")


# ---------------------------------------------------------------------------
# Dry-run / write mode tests
# ---------------------------------------------------------------------------

class DryRunAndWriteModeTests(unittest.TestCase):
    def _sample_csv(self, tmp_dir: Path) -> Path:
        csv_path = tmp_dir / "test_input.csv"
        csv_path.write_text(
            "title,soldPrice,shippingPrice,totalPrice,currency,soldDate,listingUrl,"
            "marketplace,market,condition,graded,cardName,setName,setId,collectorNumber,"
            "language,variant,canonicalId\n"
            "Charizard Base Set 4 Raw NM,250.00,5.00,255.00,AUD,2026-05-20,"
            "https://www.ebay.com.au/itm/t1,ebay_au,AU,near_mint,false,Charizard,Base,"
            "base1,4,en,raw,\n",
            encoding="utf-8",
        )
        return csv_path

    def test_dry_run_does_not_write_public_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp_dir = Path(tmp_str)
            csv_path = self._sample_csv(tmp_dir)
            reports_dir = tmp_dir / "reports"
            public_prices_dir = tmp_dir / "public" / "v1" / "markets" / "prices"

            # Patch MARKET_PRICES_ROOT and MARKET_STATUS_PATH temporarily
            import tools.import_manual_sold_listings as mod
            orig_prices_root = mod.MARKET_PRICES_ROOT
            orig_status_path = mod.MARKET_STATUS_PATH
            mod.MARKET_PRICES_ROOT = public_prices_dir
            mod.MARKET_STATUS_PATH = tmp_dir / "market-price-status.json"
            try:
                report = run_import(
                    input_path=csv_path,
                    market="AU",
                    language="en",
                    dry_run=True,
                    allow_lots=False,
                    allow_damaged=False,
                    max_rows=None,
                    reports_dir=reports_dir,
                    commit_safe_report=False,
                )
            finally:
                mod.MARKET_PRICES_ROOT = orig_prices_root
                mod.MARKET_STATUS_PATH = orig_status_path

            # No market price files should be written
            self.assertFalse(public_prices_dir.exists(), "public prices dir should not be created in dry-run")
            self.assertEqual(report["mode"], "dry_run")
            self.assertGreater(len(report["writeTargets"]), 0)

    def test_write_mode_writes_valid_market_price_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp_dir = Path(tmp_str)
            csv_path = self._sample_csv(tmp_dir)
            reports_dir = tmp_dir / "reports"
            public_prices_dir = tmp_dir / "public" / "v1" / "markets" / "prices"

            import tools.import_manual_sold_listings as mod
            orig_prices_root = mod.MARKET_PRICES_ROOT
            orig_status_path = mod.MARKET_STATUS_PATH
            mod.MARKET_PRICES_ROOT = public_prices_dir
            mod.MARKET_STATUS_PATH = tmp_dir / "market-price-status.json"
            try:
                report = run_import(
                    input_path=csv_path,
                    market="AU",
                    language="en",
                    dry_run=False,
                    allow_lots=False,
                    allow_damaged=False,
                    max_rows=None,
                    reports_dir=reports_dir,
                    commit_safe_report=False,
                )
            finally:
                mod.MARKET_PRICES_ROOT = orig_prices_root
                mod.MARKET_STATUS_PATH = orig_status_path

            self.assertEqual(report["mode"], "write")
            self.assertGreater(len(report["writeTargets"]), 0)

            # Check file exists and is valid JSON with required fields
            written_path = public_prices_dir / "au" / "pokemon" / "en" / "base1.json"
            self.assertTrue(written_path.exists(), f"Expected written file: {written_path}")
            payload = json.loads(written_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schemaVersion"], "1.0.0")
            self.assertEqual(payload["market"], "AU")
            self.assertEqual(payload["game"], "pokemon")
            self.assertEqual(payload["language"], "en")
            self.assertEqual(payload["setId"], "base1")
            self.assertIn("prices", payload)
            prices = payload["prices"]
            self.assertGreater(len(prices), 0)
            price = prices[0]
            self.assertEqual(price["source"], SOURCE_ID)
            self.assertIn("confidenceLabel", price)
            self.assertIn("medianPrice", price)
            self.assertIn("sampleCount", price)


# ---------------------------------------------------------------------------
# Secret redaction tests
# ---------------------------------------------------------------------------

class SecretRedactionTests(unittest.TestCase):
    def test_redacts_api_key_in_value(self) -> None:
        self.assertEqual(_redact_suspicious("api_key=abc123"), "[REDACTED]")

    def test_redacts_token_in_value(self) -> None:
        self.assertEqual(_redact_suspicious("bearer token xyz"), "[REDACTED]")

    def test_passes_normal_value(self) -> None:
        self.assertEqual(_redact_suspicious("Charizard Base Set 4"), "Charizard Base Set 4")

    def test_no_secrets_in_sample_csv(self) -> None:
        sample_path = ROOT / "data" / "manual_market_prices" / "examples" / "sample_sold_listings.csv"
        if not sample_path.exists():
            self.skipTest("Sample CSV not found")
        content = sample_path.read_text(encoding="utf-8")
        for line in content.splitlines():
            for field in line.split(","):
                self.assertNotEqual(
                    _redact_suspicious(field),
                    "[REDACTED]",
                    f"Suspicious field found in sample CSV: {field!r}",
                )


# ---------------------------------------------------------------------------
# Normalisation sanity checks
# ---------------------------------------------------------------------------

class NormalisationTests(unittest.TestCase):
    def test_currency_uppercase(self) -> None:
        self.assertEqual(normalize_currency("aud"), "AUD")
        self.assertEqual(normalize_currency("USD"), "USD")

    def test_language_normalised(self) -> None:
        self.assertEqual(normalize_language("japanese"), "jp")
        self.assertEqual(normalize_language("english"), "en")

    def test_market_normalised(self) -> None:
        self.assertEqual(normalize_market("australia"), "AU")
        self.assertEqual(normalize_market("UK"), "GB")

    def test_condition_normalised(self) -> None:
        self.assertEqual(normalize_condition("nm"), "near_mint")
        self.assertEqual(normalize_condition("HP"), "heavily_played")
        self.assertEqual(normalize_condition("near mint"), "near_mint")

    def test_graded_normalised(self) -> None:
        self.assertEqual(normalize_graded("true"), "graded")
        self.assertEqual(normalize_graded("false"), "ungraded")
        self.assertEqual(normalize_graded("psa"), "graded")

    def test_variant_normalised(self) -> None:
        self.assertEqual(normalize_variant("raw"), "raw")
        self.assertEqual(normalize_variant(""), "raw")
        self.assertEqual(normalize_variant("graded"), "graded")


if __name__ == "__main__":
    unittest.main()
