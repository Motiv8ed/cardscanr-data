"""No duplicate bulk-reference snapshots for unchanged same-day checks."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from cardscanr_market_engine.bulk.price_semantics import ReferencePriceObservation
from cardscanr_market_engine.bulk.reference_refresh import (
    BulkReferenceRefreshRunner,
    BulkRefreshConfig,
    BulkRefreshCounters,
)
from tests.test_bulk_reference_pricing import FakeBulkClient, _sample_key, sample_engine_config


START = datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc)


class MergeCacheClient(FakeBulkClient):
    """Simulate PostgREST merge-on-upsert, preserving selected eBay evidence."""

    def upsert_cache(self, payload):
        prior = self.cache.get(payload["price_key_id"], {})
        self.cache[payload["price_key_id"]] = {**prior, **payload}
        return self.cache[payload["price_key_id"]]


class ProjectingCacheClient(MergeCacheClient):
    """Emulate PostgREST column projection so load_cache_map omissions surface."""

    def _table_get(self, table, params=None):
        rows = super()._table_get(table, params)
        select = str((params or {}).get("select") or "")
        if table != "market_price_cache" or not select or select == "*":
            return rows
        columns = [column.strip() for column in select.split(",") if column.strip()]
        return [{column: row.get(column) for column in columns} for row in rows]


def _reference(*, price=5.84, provider="static_reference"):
    return ReferencePriceObservation(
        provider=provider,
        source_market="US",
        source_currency="AUD",
        market_price=price,
        low_price=4.0,
        high_price=8.0,
        confidence="medium",
        mapping_status="exact",
    )


def _verified_cache(key_id, *, prior_checked=None, ref_price=5.84):
    return {
        "price_key_id": key_id,
        "provider": "ebay_browser",
        "marketplace": "EBAY_AU",
        "market_country": "AU",
        "currency": "AUD",
        "current_market_price": 65.14,
        "display_price_source": "verified_au",
        "confidence": "medium",
        "verification_required": False,
        "verification_reason": None,
        "reference_provider": "static_reference",
        "reference_price": ref_price,
        "reference_updated_at": prior_checked,
        "last_updated_at": "2026-11-05T00:00:00Z",
        "latest_snapshot_id": "ebay-original-snapshot",
    }


def _runner(client, clock, observation):
    runner = BulkReferenceRefreshRunner(
        client=client,  # type: ignore[arg-type]
        engine_config=sample_engine_config(),
        refresh_config=BulkRefreshConfig(
            dry_run=False,
            max_keys=100,
            enable_live_tcgdex=False,
            verification_budget_per_run=0,
            high_value_threshold=50.0,
            reference_fresh_hours=24,
        ),
        now_func=lambda: clock[0],
    )
    runner._lookup_reference = lambda _key: observation[0]  # type: ignore[method-assign]
    runner._convert_price = lambda obs, _currency, _now: obs.market_price  # type: ignore[method-assign]
    return runner


class ReferenceSnapshotDedupeTests(unittest.TestCase):
    def setUp(self):
        self.client = MergeCacheClient()
        self.key = _sample_key(id="snapshot-key")
        self.clock = [START]
        self.observation = [_reference()]
        self.runner = _runner(self.client, self.clock, self.observation)
        self.counters = BulkRefreshCounters()

    def process(self):
        return self.runner.process_key(
            key=self.key,
            prior_cache=dict(self.client.cache.get(self.key.id, {})) or None,
            counters=self.counters,
            verification_budget=[0],
        )

    def test_hourly_verified_rechecks_keep_one_daily_checkpoint(self):
        self.client.cache[self.key.id] = _verified_cache(
            self.key.id, prior_checked="2026-11-05T23:00:00Z",
        )
        for hour in range(24):
            self.clock[0] = START + timedelta(hours=hour)
            self.assertEqual(self.process()["status"], "preserve_verified")

        self.assertEqual(len(self.client.snapshots), 1)
        self.assertEqual(self.counters.snapshots_skipped_unchanged, 23)
        cached = self.client.cache[self.key.id]
        self.assertEqual(cached["reference_updated_at"], "2026-11-06T23:00:00Z")
        self.assertEqual(cached["latest_snapshot_id"], "ebay-original-snapshot")
        self.assertEqual(cached["current_market_price"], 65.14)
        self.assertEqual(cached["provider"], "ebay_browser")
        self.assertEqual(cached["last_updated_at"], "2026-11-05T00:00:00Z")

        self.clock[0] = START + timedelta(days=1)
        self.process()
        self.assertEqual(len(self.client.snapshots), 2)

    def test_first_reference_check_or_missing_timestamp_still_records_snapshot(self):
        self.client.cache[self.key.id] = _verified_cache(self.key.id, prior_checked=None)
        self.process()
        self.assertEqual(len(self.client.snapshots), 1)
        self.assertEqual(self.counters.snapshots_skipped_unchanged, 0)

    def test_changed_reference_price_records_snapshot_same_day(self):
        self.client.cache[self.key.id] = _verified_cache(
            self.key.id, prior_checked="2026-11-06T00:00:00Z",
        )
        self.observation[0] = _reference(price=6.10)
        self.process()
        self.assertEqual(len(self.client.snapshots), 1)
        self.assertEqual(self.client.cache[self.key.id]["reference_price"], 6.10)

    def test_changed_reference_provider_records_snapshot_same_day(self):
        self.client.cache[self.key.id] = _verified_cache(
            self.key.id, prior_checked="2026-11-06T00:00:00Z",
        )
        self.observation[0] = _reference(provider="tcgdex_tcgplayer")
        self.process()
        self.assertEqual(len(self.client.snapshots), 1)

    def test_pending_verification_rechecks_do_not_flood_snapshots(self):
        from cardscanr_market_engine.bulk.display_price_policy import decide_display_price

        prior = {
            **_verified_cache(self.key.id, prior_checked="2026-11-06T00:00:00Z"),
            "provider": "static_reference",
            "marketplace": "REFERENCE",
            "display_price_source": "reference",
            "current_market_price": 80.0,
            "latest_snapshot_id": "reference-original",
            "verification_required": True,
        }
        decision = decide_display_price(
            prior_cache=prior,
            observation=self.observation[0],
            converted_price=5.84,
            target_currency="AUD",
            now=START,
        )
        self.assertEqual(decision.action, "pending_verification")
        prior["verification_reason"] = decision.verification_reason
        self.client.cache[self.key.id] = prior
        self.process()
        self.assertEqual(self.counters.snapshots_skipped_unchanged, 1)
        self.assertEqual(self.client.snapshots, [])
        self.assertEqual(self.client.cache[self.key.id]["latest_snapshot_id"], "reference-original")
        self.assertTrue(self.client.cache[self.key.id]["verification_required"])

    def test_first_selected_reference_write_still_records_snapshot(self):
        self.client.cache.pop(self.key.id, None)
        self.observation[0] = _reference(price=0.31)
        self.process()
        self.assertEqual(len(self.client.snapshots), 1)
        self.assertEqual(self.counters.snapshots_skipped_unchanged, 0)
        self.assertIn("latest_snapshot_id", self.client.cache[self.key.id])

    def test_invalid_prior_timestamp_records_snapshot(self):
        self.client.cache[self.key.id] = _verified_cache(
            self.key.id, prior_checked="not-a-timestamp",
        )
        self.process()
        self.assertEqual(len(self.client.snapshots), 1)
        self.assertEqual(self.counters.snapshots_skipped_unchanged, 0)

    def test_offset_timezone_same_utc_day_skips(self):
        # 2026-11-06T10:30:00+10:00 == 2026-11-06T00:30:00Z: same UTC day.
        self.client.cache[self.key.id] = _verified_cache(
            self.key.id, prior_checked="2026-11-06T10:30:00+10:00",
        )
        self.clock[0] = START + timedelta(hours=5)
        self.process()
        self.assertEqual(self.client.snapshots, [])
        self.assertEqual(self.counters.snapshots_skipped_unchanged, 1)

    def test_offset_timezone_previous_utc_day_records(self):
        # 2026-11-06T09:30:00+11:00 == 2026-11-05T22:30:00Z: prior UTC day.
        self.client.cache[self.key.id] = _verified_cache(
            self.key.id, prior_checked="2026-11-06T09:30:00+11:00",
        )
        self.process()
        self.assertEqual(len(self.client.snapshots), 1)
        self.assertEqual(self.counters.snapshots_skipped_unchanged, 0)

    def test_string_valued_postgrest_numerics_skip(self):
        row = _verified_cache(self.key.id, prior_checked="2026-11-06T00:00:00Z")
        row["reference_price"] = "5.84"
        row["current_market_price"] = "65.14"
        self.client.cache[self.key.id] = row
        self.process()
        self.assertEqual(self.client.snapshots, [])
        self.assertEqual(self.counters.snapshots_skipped_unchanged, 1)

    def test_reject_reference_rechecks_do_not_flood_snapshots(self):
        from cardscanr_market_engine.bulk.display_price_policy import decide_display_price

        self.observation[0] = ReferencePriceObservation(
            provider="static_reference",
            source_market="US",
            source_currency="AUD",
            market_price=1.10,
            low_price=None,
            high_price=None,
            confidence="low",
            mapping_status="exact",
        )
        prior = {
            **_verified_cache(self.key.id, prior_checked="2026-11-06T00:00:00Z", ref_price=1.10),
            "provider": "static_reference",
            "marketplace": "REFERENCE",
            "display_price_source": "reference",
            "current_market_price": 80.0,
            "sample_size": 5,
            "latest_snapshot_id": "reference-original",
        }
        decision = decide_display_price(
            prior_cache=prior,
            observation=self.observation[0],
            converted_price=1.10,
            target_currency="AUD",
            now=START,
        )
        self.assertEqual(decision.action, "reject_reference")
        prior["verification_required"] = decision.verification_required
        prior["verification_reason"] = decision.verification_reason
        self.client.cache[self.key.id] = prior
        self.process()
        self.assertEqual(self.counters.snapshots_skipped_unchanged, 1)
        self.assertEqual(self.client.snapshots, [])
        self.assertEqual(self.client.cache[self.key.id]["latest_snapshot_id"], "reference-original")
        self.assertEqual(self.client.cache[self.key.id]["current_market_price"], 80.0)

    def test_verification_state_change_records_snapshot_same_day(self):
        self.client.cache[self.key.id] = {
            **_verified_cache(self.key.id, prior_checked="2026-11-06T00:00:00Z"),
            "verification_required": True,
            "verification_reason": "extreme_move_needs_verification",
        }
        outcome = self.process()
        self.assertEqual(outcome["status"], "preserve_verified")
        self.assertEqual(len(self.client.snapshots), 1)
        self.assertEqual(self.counters.snapshots_skipped_unchanged, 0)
        cached = self.client.cache[self.key.id]
        self.assertFalse(cached["verification_required"])
        self.assertEqual(cached["latest_snapshot_id"], "ebay-original-snapshot")
        self.assertEqual(cached["current_market_price"], 65.14)


class VerifiedSourcePreservationTests(unittest.TestCase):
    """End-to-end: an unchanged reference recheck must not downgrade verified provenance."""

    def test_verified_au_source_survives_projection_limited_cache_load(self):
        client = ProjectingCacheClient()
        key = _sample_key(id="verified-au-key")
        client.keys = [key]
        client.cache[key.id] = _verified_cache(key.id, prior_checked="2026-11-05T23:00:00Z")
        clock = [START]
        runner = _runner(client, clock, [_reference()])

        report = runner.run()
        self.assertEqual(report["errors"], 0)
        cached = client.cache[key.id]
        self.assertEqual(cached["display_price_source"], "verified_au")
        self.assertEqual(cached["provider"], "ebay_browser")
        self.assertEqual(cached["marketplace"], "EBAY_AU")
        self.assertEqual(cached["current_market_price"], 65.14)
        self.assertEqual(cached["last_updated_at"], "2026-11-05T00:00:00Z")
        self.assertEqual(cached["latest_snapshot_id"], "ebay-original-snapshot")
        self.assertEqual(len(client.snapshots), 1)  # daily checkpoint only

        clock[0] = START + timedelta(hours=1)
        followup = runner.run()
        self.assertEqual(followup["errors"], 0)
        self.assertEqual(followup["snapshotsSkippedUnchanged"], 1)
        self.assertEqual(len(client.snapshots), 1)
        self.assertEqual(client.cache[key.id]["display_price_source"], "verified_au")
        self.assertEqual(client.cache[key.id]["latest_snapshot_id"], "ebay-original-snapshot")


if __name__ == "__main__":
    unittest.main()
