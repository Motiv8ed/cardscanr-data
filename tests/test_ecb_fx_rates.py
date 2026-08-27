"""Fixture-based tests for official ECB FX parsing and freshness."""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from cardscanr_market_engine.international.ecb_client import (
    parse_ecb_daily_xml,
    fetch_ecb_daily_xml,
)
from cardscanr_market_engine.international.fx_cache import (
    evaluate_ecb_fx_freshness,
    refresh_ecb_fx_cache,
    save_fx_cache,
)
from cardscanr_market_engine.international.fx_freshness import evaluate_fx_freshness

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ecb_eurofxref_daily_sample.xml"


class EcbParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.xml = FIXTURE.read_text(encoding="utf-8")
        self.fetched_at = datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc)
        self.snapshot = parse_ecb_daily_xml(self.xml, fetched_at=self.fetched_at)

    def test_parses_provider_rate_date(self) -> None:
        self.assertEqual(self.snapshot.provider_rate_date, date(2026, 8, 22))
        self.assertEqual(self.snapshot.source, "ECB")
        self.assertEqual(self.snapshot.fetched_at, self.fetched_at)

    def test_eur_to_aud(self) -> None:
        self.assertEqual(self.snapshot.pair_rate("EUR", "AUD"), Decimal("1.6196"))

    def test_usd_to_aud(self) -> None:
        # AUD_per_EUR / USD_per_EUR
        expected = Decimal("1.6196") / Decimal("1.1645")
        self.assertEqual(self.snapshot.pair_rate("USD", "AUD"), expected)

    def test_gbp_to_aud(self) -> None:
        expected = Decimal("1.6196") / Decimal("0.85740")
        self.assertEqual(self.snapshot.pair_rate("GBP", "AUD"), expected)

    def test_cad_to_aud(self) -> None:
        expected = Decimal("1.6196") / Decimal("1.5573")
        self.assertEqual(self.snapshot.pair_rate("CAD", "AUD"), expected)

    def test_jpy_to_aud(self) -> None:
        expected = Decimal("1.6196") / Decimal("185.61")
        self.assertEqual(self.snapshot.pair_rate("JPY", "AUD"), expected)

    def test_aud_to_usd(self) -> None:
        expected = Decimal("1.1645") / Decimal("1.6196")
        self.assertEqual(self.snapshot.pair_rate("AUD", "USD"), expected)

    def test_same_currency(self) -> None:
        self.assertEqual(self.snapshot.pair_rate("AUD", "AUD"), Decimal("1"))

    def test_unsupported_currency(self) -> None:
        with self.assertRaises(ValueError):
            self.snapshot.pair_rate("XXX", "AUD")

    def test_malformed_response(self) -> None:
        with self.assertRaises(ValueError):
            parse_ecb_daily_xml("<not-ecb/>", fetched_at=self.fetched_at)

    def test_missing_required_currency(self) -> None:
        bad = self.xml.replace("<Cube currency='NZD' rate='1.7835'/>", "")
        with self.assertRaises(ValueError):
            parse_ecb_daily_xml(bad, fetched_at=self.fetched_at)

    def test_zero_rate_rejected(self) -> None:
        bad = self.xml.replace("rate='1.6196'", "rate='0'")
        with self.assertRaises(ValueError):
            parse_ecb_daily_xml(bad, fetched_at=self.fetched_at)

    def test_network_failure(self) -> None:
        with patch(
            "cardscanr_market_engine.international.ecb_client.urllib.request.urlopen",
            side_effect=OSError("boom"),
        ):
            with self.assertRaises(ValueError):
                fetch_ecb_daily_xml()


class EcbFreshnessTests(unittest.TestCase):
    def _cache(self, *, provider_date: str, fetched_at: datetime) -> dict:
        snapshot = parse_ecb_daily_xml(
            FIXTURE.read_text(encoding="utf-8"),
            fetched_at=fetched_at,
        )
        # Force provider date for weekend scenarios.
        payload = snapshot.to_cache_payload()
        payload["providerRateDate"] = provider_date
        payload["status"] = "success"
        return payload

    def test_weekend_friday_rate_saturday_check_is_healthy(self) -> None:
        # ECB published Friday 2026-08-22; CardScanR checks Saturday.
        saturday = datetime(2026, 8, 23, 9, 0, tzinfo=timezone.utc)
        cache = self._cache(provider_date="2026-08-22", fetched_at=saturday)
        fx = evaluate_ecb_fx_freshness(cache=cache, now=saturday)
        self.assertEqual(fx.health, "HEALTHY")
        self.assertTrue(fx.allows_conversion)
        self.assertFalse(fx.stale)

    def test_prolonged_outage_is_stale(self) -> None:
        fetched = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
        now = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)  # 7 days later
        cache = self._cache(provider_date="2026-08-20", fetched_at=fetched)
        fx = evaluate_ecb_fx_freshness(cache=cache, now=now)
        self.assertEqual(fx.health, "STALE")
        self.assertFalse(fx.allows_conversion)
        self.assertEqual(fx.block_reason, "FX_RATE_STALE_NO_SAFE_CONVERSION")

    def test_static_rates_never_authorize_conversion(self) -> None:
        now = datetime(2026, 8, 27, tzinfo=timezone.utc)
        freshness = evaluate_fx_freshness(
            rate_source="configured_static_rates",
            rate_timestamp=None,
            now=now,
            cache={"source": "configured_static_rates", "fetchedAt": now.isoformat()},
        )
        self.assertTrue(freshness.stale)
        self.assertFalse(freshness.allows_conversion)
        self.assertEqual(freshness.health, "STALE")

    def test_refresh_writes_shared_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ecb_fx_rates.json"
            snapshot = parse_ecb_daily_xml(
                FIXTURE.read_text(encoding="utf-8"),
                fetched_at=datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc),
            )

            def _fetch(*, now):
                return snapshot

            payload = refresh_ecb_fx_cache(now=datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc), path=path, fetch_fn=_fetch)
            self.assertEqual(payload["source"], "ECB")
            self.assertTrue(path.is_file())
            self.assertIn("USD:AUD", payload["pairRates"])


if __name__ == "__main__":
    unittest.main()
