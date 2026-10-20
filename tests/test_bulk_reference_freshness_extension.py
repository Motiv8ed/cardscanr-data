"""Freshness extension for unchanged reference prices (CT-20261018-002)."""
from __future__ import annotations

import unittest
import uuid
from datetime import timedelta

from cardscanr_market_engine.bulk.price_semantics import ReferencePriceObservation
from cardscanr_market_engine.bulk.reference_refresh import (
    BulkReferenceRefreshRunner,
    BulkRefreshConfig,
    freshness_extension_guard_params,
)
from cardscanr_market_engine.refresh_policy import RefreshCooldownConfig
from cardscanr_market_engine.supabase_client import (
    MAX_GET_URL_LENGTH,
    MAX_IN_FILTER_CHUNK,
    SupabaseMarketEngineClient,
    chunk_ids_for_in_filter,
    content_range_total,
    prepared_get_url,
)
from tests.test_bulk_reference_pricing import NOW, FakeBulkClient, _sample_key, sample_engine_config

LOW_PRICE = 0.31
HIGH_PRICE = 150.0
PRICE_LAST_CHANGED = "2026-08-26T00:00:00Z"


def _iso_offset(hours: int) -> str:
    value = NOW + timedelta(hours=hours)
    return value.astimezone(NOW.tzinfo).isoformat().replace("+00:00", "Z")


def _observation(price: float) -> ReferencePriceObservation:
    return ReferencePriceObservation(
        provider="static_reference",
        source_market="us",
        source_currency="USD",
        market_price=price,
        low_price=None,
        high_price=None,
        confidence="medium",
        mapping_status="exact",
    )


def _ids_from_in_filter(value: str) -> list[str]:
    prefix = "in.("
    if not (value.startswith(prefix) and value.endswith(")")):
        raise AssertionError(f"expected in.(...) filter, got {value!r}")
    body = value[len(prefix) : -1]
    return [] if not body else body.split(",")


def _legacy_reference_source(value: object) -> bool:
    if value is None:
        return True
    text = str(value).strip().lower()
    return text in {"", "reference"}


def _refresh_status_allows_extension(value: object) -> bool:
    if value is None:
        return True
    return str(value).strip().lower() != "failed"


def _matches_freshness_guard(row: dict, params: dict) -> bool:
    and_filter = str(params.get("and") or "")
    if "display_price_source.eq.reference" not in and_filter:
        return False
    if "refresh_status.neq.failed" not in and_filter:
        return False
    if not _legacy_reference_source(row.get("display_price_source")):
        return False
    if not _refresh_status_allows_extension(row.get("refresh_status")):
        return False
    if str(params.get("verification_required") or "") == "is.false" and bool(
        row.get("verification_required")
    ):
        return False
    last_updated_filter = str(params.get("last_updated_at") or "")
    row_last_updated = row.get("last_updated_at")
    if last_updated_filter == "is.null":
        if row_last_updated is not None:
            return False
    elif last_updated_filter.startswith("eq."):
        if str(row_last_updated) != last_updated_filter[3:]:
            return False
    return True


class FreshnessClient(FakeBulkClient):
    extend_reference_cache_freshness = SupabaseMarketEngineClient.extend_reference_cache_freshness

    def __init__(self) -> None:
        super().__init__()
        self.patches: list[dict] = []
        self.upserts: list[dict] = []
        self.applied_freshness_ids: list[str] = []
        # Concurrent writers can mutate cache rows between snapshot and PATCH.
        self.mutate_before_patch: dict[str, dict] | None = None

    def _table_patch_affected_count(
        self,
        table,
        payload,
        *,
        params,
        prefer="return=minimal,count=exact",
    ):
        if self.mutate_before_patch:
            for key_id, overrides in self.mutate_before_patch.items():
                if key_id in self.cache:
                    self.cache[key_id].update(overrides)
        self.patches.append(
            {
                "table": table,
                "payload": dict(payload),
                "params": dict(params),
                "prefer": prefer,
            }
        )
        ids = _ids_from_in_filter(str(params.get("price_key_id") or "in.()"))
        applied = 0
        for key_id in ids:
            row = self.cache.get(key_id) or {}
            if not _matches_freshness_guard(row, params):
                continue
            self.cache[key_id].update(payload)
            self.applied_freshness_ids.append(key_id)
            applied += 1
        return applied

    def upsert_cache(self, payload):
        self.upserts.append(dict(payload))
        return super().upsert_cache(payload)


def _runner(client: FreshnessClient, *, dry_run: bool) -> BulkReferenceRefreshRunner:
    runner = BulkReferenceRefreshRunner(
        client=client,  # type: ignore[arg-type]
        engine_config=sample_engine_config(),
        refresh_config=BulkRefreshConfig(
            dry_run=dry_run,
            max_keys=5000,
            enable_live_tcgdex=False,
            verification_budget_per_run=0,
            high_value_threshold=50.0,
            # Flat fallback is 24h. Row TTL must come from the cooldown policy.
            reference_fresh_hours=24,
        ),
        now_func=lambda: NOW,
    )
    runner.cooldown_config = RefreshCooldownConfig(
        default_cooldown_hours=6,
        high_value_cooldown_hours=4,
        popular_cooldown_hours=4,
        hot_card_cooldown_hours=2,
        low_value_cooldown_hours=12,
    )
    return runner


def _reference_prior(key_id: str, price: float, **overrides) -> dict:
    row = {
        "price_key_id": key_id,
        "current_market_price": price,
        "recommended_price": price,
        "provider": "static_reference",
        "marketplace": "REFERENCE",
        "reference_price": price,
        "reference_provider": "static_reference",
        "display_price_source": "reference",
        "verification_required": False,
        "verification_reason": None,
        "confidence": "medium",
        "currency": "AUD",
        "last_updated_at": PRICE_LAST_CHANGED,
    }
    row.update(overrides)
    return row


def _install_prices(runner: BulkReferenceRefreshRunner, prices: dict[str, float]) -> None:
    def lookup(key):
        return _observation(prices[key.id])

    def convert(observation, _target_currency, _now):
        return float(observation.market_price)

    runner._lookup_reference = lookup  # type: ignore[method-assign]
    runner._convert_price = convert  # type: ignore[method-assign]


class NoChangeFreshnessTests(unittest.TestCase):
    def test_no_change_extends_stale_after_without_price_semantics(self) -> None:
        cases = (
            ("low_value", LOW_PRICE, 12),
            ("high_value", HIGH_PRICE, 4),
        )
        for name, price, ttl_hours in cases:
            with self.subTest(name=name):
                client = FreshnessClient()
                key = _sample_key(id=str(uuid.uuid4()))
                client.keys = [key]
                client.cache[key.id] = _reference_prior(key.id, price)
                runner = _runner(client, dry_run=False)
                _install_prices(runner, {key.id: price})

                report = runner.run()

                self.assertEqual(report["keysUnchanged"], 1)
                self.assertEqual(report["keysUpdated"], 0)
                self.assertEqual(report["keysFreshnessExtended"], 1)
                self.assertEqual(report["errors"], 0)
                self.assertEqual(client.snapshots, [])
                self.assertEqual(client.upserts, [])
                self.assertEqual(len(client.patches), 1)
                patch = client.patches[0]
                expected_until = _iso_offset(ttl_hours)
                self.assertEqual(patch["table"], "market_price_cache")
                self.assertEqual(patch["prefer"], "return=minimal,count=exact")
                self.assertEqual(
                    patch["payload"],
                    {
                        "stale_after": expected_until,
                        "next_refresh_due_at": expected_until,
                        "updated_at": "2026-08-27T00:00:00Z",
                    },
                )
                self.assertEqual(_ids_from_in_filter(patch["params"]["price_key_id"]), [key.id])
                self.assertNotIn("limit", patch["params"])
                self.assertEqual(
                    patch["params"],
                    {
                        "price_key_id": f"in.({key.id})",
                        **freshness_extension_guard_params(
                            expected_last_updated_at=PRICE_LAST_CHANGED,
                        ),
                    },
                )
                self.assertEqual(client.applied_freshness_ids, [key.id])
                stored = client.cache[key.id]
                self.assertEqual(stored["current_market_price"], price)
                self.assertEqual(stored["currency"], "AUD")
                self.assertEqual(stored["provider"], "static_reference")
                self.assertEqual(stored["last_updated_at"], PRICE_LAST_CHANGED)
                self.assertEqual(stored["stale_after"], expected_until)
                self.assertNotIn("last_updated_at", patch["payload"])
                self.assertNotIn("current_market_price", patch["payload"])
                self.assertNotIn("currency", patch["payload"])
                self.assertNotIn("provider", patch["payload"])

    def test_freshness_helper_rejects_price_columns(self) -> None:
        client = FreshnessClient()
        with self.assertRaises(ValueError):
            client.extend_reference_cache_freshness(
                price_key_ids=["abc"],
                payload={"stale_after": "2026-08-27T12:00:00Z", "current_market_price": 1},
            )
        self.assertEqual(client.patches, [])


class ProtectedRowTests(unittest.TestCase):
    def _run_one(self, prior: dict) -> tuple[dict, FreshnessClient]:
        client = FreshnessClient()
        key = _sample_key(id=prior["price_key_id"])
        client.keys = [key]
        client.cache[key.id] = dict(prior)
        runner = _runner(client, dry_run=False)
        _install_prices(runner, {key.id: LOW_PRICE})
        report = runner.run()
        return report, client

    def test_preserve_verified_and_user_verified_keep_current_write(self) -> None:
        for source in ("verified_au", "verified_local", "local_verified", "market"):
            with self.subTest(source=source):
                key_id = str(uuid.uuid4())
                prior = {
                    "price_key_id": key_id,
                    "current_market_price": 65.14,
                    "provider": "ebay_browser",
                    "display_price_source": source,
                    "marketplace": "EBAY_AU",
                    "market_country": "AU",
                    "confidence": "medium",
                    "currency": "AUD",
                    "last_updated_at": PRICE_LAST_CHANGED,
                    "verification_required": False,
                }
                report, client = self._run_one(prior)
                self.assertEqual(report["status"], "success")
                self.assertEqual(report["keysFreshnessExtended"], 0)
                self.assertEqual(report["keysUnchanged"], 0)
                self.assertEqual(report["keysUpdated"], 1)
                self.assertEqual(client.patches, [])
                self.assertEqual(len(client.snapshots), 1)
                self.assertEqual(len(client.upserts), 1)
                write = client.upserts[0]
                self.assertEqual(write["current_market_price"], 65.14)
                self.assertEqual(write["provider"], "ebay_browser")
                # Fresh eBay on an AU key is rewritten to verified_au by the
                # existing source-precedence keep path. That rewrite is unchanged.
                self.assertEqual(write["display_price_source"], "verified_au")
                self.assertEqual(write["marketplace"], "EBAY_AU")
                self.assertNotIn("stale_after", write)
                self.assertNotIn("next_refresh_due_at", write)
                self.assertNotIn("last_updated_at", write)
                self.assertNotIn("updated_at", write)
                self.assertNotIn("currency", write)
                self.assertEqual(
                    set(write),
                    {
                        "price_key_id",
                        "reference_price",
                        "reference_provider",
                        "reference_updated_at",
                        "verification_required",
                        "verification_reason",
                        "current_market_price",
                        "provider",
                        "marketplace",
                        "display_price_source",
                        "confidence",
                        "last_error_message",
                    },
                )

    def test_pending_verification_keeps_current_write(self) -> None:
        key_id = str(uuid.uuid4())
        prior = _reference_prior(key_id, 80.0, confidence="medium")
        report, client = self._run_one(prior)
        self.assertEqual(report["keysQuarantined"], 1)
        self.assertEqual(report["keysUpdated"], 0)
        self.assertEqual(report["keysFreshnessExtended"], 0)
        self.assertEqual(report["keysUnchanged"], 0)
        self.assertEqual(client.patches, [])
        self.assertEqual(len(client.snapshots), 1)
        self.assertEqual(len(client.upserts), 1)
        write = client.upserts[0]
        self.assertEqual(write["current_market_price"], 80.0)
        self.assertEqual(write["provider"], "static_reference")
        self.assertTrue(write["verification_required"])
        self.assertEqual(write["display_price_source"], "reference")
        self.assertNotIn("stale_after", write)
        self.assertNotIn("last_updated_at", write)
        self.assertNotIn("updated_at", write)

    def test_pending_display_source_no_change_is_not_extended(self) -> None:
        key_id = str(uuid.uuid4())
        prior = _reference_prior(
            key_id,
            LOW_PRICE,
            display_price_source="pending_verification",
            verification_required=True,
            verification_reason="extreme_move_needs_verification",
        )
        report, client = self._run_one(prior)
        self.assertEqual(report["keysUnchanged"], 1)
        self.assertEqual(report["keysFreshnessExtended"], 0)
        self.assertEqual(report["keysUpdated"], 0)
        self.assertEqual(client.patches, [])
        self.assertEqual(client.upserts, [])
        self.assertEqual(client.snapshots, [])
        self.assertEqual(client.cache[key_id]["last_updated_at"], PRICE_LAST_CHANGED)
        self.assertEqual(client.cache[key_id]["current_market_price"], LOW_PRICE)


class BatchedFreshnessTests(unittest.TestCase):
    def test_1518_unchanged_keys_are_batched_under_8kb(self) -> None:
        client = FreshnessClient()
        prices: dict[str, float] = {}
        keys = []
        for index in range(1518):
            key_id = str(uuid.uuid4())
            price = HIGH_PRICE if index >= 1318 else LOW_PRICE
            key = _sample_key(
                id=key_id,
                collector_number=str((index % 200) + 1),
                normalized_card_name=f"card_{index}",
                card_name=f"Card {index}",
            )
            keys.append(key)
            prices[key_id] = price
            client.cache[key_id] = _reference_prior(key_id, price)
        client.keys = keys
        runner = _runner(client, dry_run=False)
        _install_prices(runner, prices)

        report = runner.run()

        self.assertEqual(report["keysScanned"], 1518)
        self.assertEqual(report["keysUnchanged"], 1518)
        self.assertEqual(report["keysFreshnessExtended"], 1518)
        self.assertEqual(report["keysUpdated"], 0)
        self.assertEqual(report["errors"], 0)
        self.assertEqual(client.upserts, [])
        self.assertEqual(client.snapshots, [])
        self.assertLess(len(client.patches), 20)
        self.assertGreaterEqual(len(client.patches), 2)
        self.assertLess(len(client.patches), 1518)

        grouped: dict[str, list[str]] = {}
        for call in client.patches:
            self.assertEqual(call["table"], "market_price_cache")
            self.assertEqual(call["prefer"], "return=minimal,count=exact")
            self.assertNotIn("limit", call["params"])
            self.assertEqual(
                {
                    key: call["params"][key]
                    for key in ("and", "verification_required", "last_updated_at")
                },
                freshness_extension_guard_params(expected_last_updated_at=PRICE_LAST_CHANGED),
            )
            url = prepared_get_url(client.supabase_url, call["table"], call["params"])
            self.assertLess(len(url), 8000)
            self.assertLess(len(url), MAX_GET_URL_LENGTH)
            ids = _ids_from_in_filter(str(call["params"]["price_key_id"]))
            self.assertGreater(len(ids), 1)
            self.assertLessEqual(len(ids), MAX_IN_FILTER_CHUNK)
            self.assertEqual(set(call["payload"]), {"stale_after", "next_refresh_due_at", "updated_at"})
            grouped.setdefault(call["payload"]["stale_after"], []).extend(ids)

        self.assertEqual(set(grouped), {_iso_offset(12), _iso_offset(4)})
        low_ids = [key.id for key in keys if prices[key.id] == LOW_PRICE]
        high_ids = [key.id for key in keys if prices[key.id] == HIGH_PRICE]
        self.assertEqual(grouped[_iso_offset(12)], low_ids)
        self.assertEqual(grouped[_iso_offset(4)], high_ids)
        guard_params = freshness_extension_guard_params(
            expected_last_updated_at=PRICE_LAST_CHANGED,
        )
        for stale_iso, ids in ((_iso_offset(12), low_ids), (_iso_offset(4), high_ids)):
            expected = chunk_ids_for_in_filter(
                ids,
                table="market_price_cache",
                base_params=guard_params,
                column="price_key_id",
                base_url=client.supabase_url,
            )
            actual = [call for call in client.patches if call["payload"]["stale_after"] == stale_iso]
            self.assertEqual(len(actual), len(expected))
            self.assertEqual(
                [_ids_from_in_filter(str(call["params"]["price_key_id"])) for call in actual],
                expected,
            )
        self.assertEqual(client.cache[keys[0].id]["last_updated_at"], PRICE_LAST_CHANGED)
        self.assertEqual(client.cache[keys[0].id]["current_market_price"], LOW_PRICE)

    def test_dry_run_reports_keys_whose_freshness_would_be_extended(self) -> None:
        client = FreshnessClient()
        prices: dict[str, float] = {}
        keys = []
        for index in range(4):
            key_id = str(uuid.uuid4())
            key = _sample_key(id=key_id, collector_number=str(index + 1))
            keys.append(key)
            prices[key_id] = LOW_PRICE
            client.cache[key_id] = _reference_prior(key_id, LOW_PRICE)
        client.keys = keys
        runner = _runner(client, dry_run=True)
        _install_prices(runner, prices)

        report = runner.run()

        self.assertTrue(report["dryRun"])
        self.assertEqual(report["keysUnchanged"], 4)
        self.assertEqual(report["keysFreshnessExtended"], 4)
        self.assertEqual(report["keysUpdated"], 0)
        self.assertEqual(client.patches, [])
        self.assertEqual(client.upserts, [])
        self.assertEqual(client.snapshots, [])
        self.assertIn("keysFreshnessExtended", report)


class ConcurrentFreshnessGuardTests(unittest.TestCase):
    def test_verified_row_changed_after_snapshot_is_not_overwritten(self) -> None:
        client = FreshnessClient()
        key = _sample_key(id=str(uuid.uuid4()))
        client.keys = [key]
        client.cache[key.id] = _reference_prior(key.id, LOW_PRICE)
        client.mutate_before_patch = {
            key.id: {
                "display_price_source": "verified_au",
                "provider": "ebay_browser",
                "stale_after": "2026-08-27T01:00:00Z",
                "next_refresh_due_at": "2026-08-27T01:00:00Z",
            }
        }
        runner = _runner(client, dry_run=False)
        _install_prices(runner, {key.id: LOW_PRICE})

        report = runner.run()

        self.assertEqual(report["keysUnchanged"], 1)
        self.assertEqual(report["keysFreshnessExtended"], 0)
        self.assertEqual(report["errors"], 0)
        self.assertEqual(len(client.patches), 1)
        self.assertNotIn("limit", client.patches[0]["params"])
        self.assertEqual(client.applied_freshness_ids, [])
        stored = client.cache[key.id]
        self.assertEqual(stored["display_price_source"], "verified_au")
        self.assertEqual(stored["stale_after"], "2026-08-27T01:00:00Z")
        self.assertEqual(stored["next_refresh_due_at"], "2026-08-27T01:00:00Z")
        self.assertEqual(stored["last_updated_at"], PRICE_LAST_CHANGED)

    def test_pending_or_failed_row_changed_after_snapshot_is_not_overwritten(self) -> None:
        cases = (
            {
                "display_price_source": "pending_verification",
                "verification_required": True,
                "next_refresh_due_at": "2026-08-27T00:30:00Z",
            },
            {
                "display_price_source": "reference",
                "verification_required": True,
                "next_refresh_due_at": "2026-08-27T00:45:00Z",
                "refresh_status": "failed",
            },
            {
                # Failed-only: display_price_source and verification_required stay
                # eligible; the refresh_status guard must reject the PATCH alone.
                "refresh_status": "failed",
                "next_refresh_due_at": "2026-08-27T00:15:00Z",
            },
            {
                # Concurrent full reference upsert rewrote last_updated_at.
                "last_updated_at": "2026-08-27T00:05:00Z",
                "stale_after": "2026-08-27T12:05:00Z",
                "next_refresh_due_at": "2026-08-27T12:05:00Z",
            },
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                client = FreshnessClient()
                key = _sample_key(id=str(uuid.uuid4()))
                client.keys = [key]
                client.cache[key.id] = _reference_prior(key.id, LOW_PRICE)
                client.mutate_before_patch = {key.id: dict(overrides)}
                runner = _runner(client, dry_run=False)
                _install_prices(runner, {key.id: LOW_PRICE})

                report = runner.run()

                self.assertEqual(report["keysUnchanged"], 1)
                self.assertEqual(report["keysFreshnessExtended"], 0)
                self.assertEqual(report["errors"], 0)
                self.assertEqual(len(client.patches), 1)
                self.assertNotIn("limit", client.patches[0]["params"])
                self.assertEqual(client.applied_freshness_ids, [])
                stored = client.cache[key.id]
                for field, value in overrides.items():
                    self.assertEqual(stored[field], value)
                if "stale_after" not in overrides:
                    self.assertNotIn("stale_after", stored)

    def test_failed_row_at_snapshot_is_not_queued(self) -> None:
        client = FreshnessClient()
        key = _sample_key(id=str(uuid.uuid4()))
        client.keys = [key]
        client.cache[key.id] = _reference_prior(
            key.id,
            LOW_PRICE,
            refresh_status="failed",
            next_refresh_due_at="2026-08-27T00:15:00Z",
        )
        runner = _runner(client, dry_run=False)
        _install_prices(runner, {key.id: LOW_PRICE})

        report = runner.run()

        self.assertEqual(report["keysUnchanged"], 1)
        self.assertEqual(report["keysFreshnessExtended"], 0)
        self.assertEqual(client.patches, [])
        self.assertEqual(client.cache[key.id]["next_refresh_due_at"], "2026-08-27T00:15:00Z")

    def test_legacy_null_display_price_source_is_extended(self) -> None:
        client = FreshnessClient()
        key = _sample_key(id=str(uuid.uuid4()))
        client.keys = [key]
        client.cache[key.id] = _reference_prior(key.id, LOW_PRICE, display_price_source=None)
        runner = _runner(client, dry_run=False)
        _install_prices(runner, {key.id: LOW_PRICE})

        report = runner.run()

        self.assertEqual(report["keysFreshnessExtended"], 1)
        self.assertEqual(client.applied_freshness_ids, [key.id])
        self.assertNotIn("limit", client.patches[0]["params"])

    def test_content_range_total_parses_exact_count(self) -> None:
        self.assertEqual(content_range_total("*/0"), 0)
        self.assertEqual(content_range_total("*/12"), 12)
        self.assertEqual(content_range_total("0-2/3"), 3)
        self.assertIsNone(content_range_total("*/**"))
        self.assertIsNone(content_range_total(None))


class ApplyReferenceRegressionTests(unittest.TestCase):
    def test_price_change_still_writes_last_updated_at(self) -> None:
        client = FreshnessClient()
        key = _sample_key(id=str(uuid.uuid4()))
        client.keys = [key]
        client.cache[key.id] = _reference_prior(key.id, 1.0)
        runner = _runner(client, dry_run=False)
        _install_prices(runner, {key.id: LOW_PRICE})

        report = runner.run()

        self.assertEqual(report["keysUpdated"], 1)
        self.assertEqual(report["keysUnchanged"], 0)
        self.assertEqual(report["keysFreshnessExtended"], 0)
        self.assertEqual(client.patches, [])
        self.assertEqual(len(client.upserts), 1)
        write = client.upserts[0]
        self.assertEqual(write["current_market_price"], LOW_PRICE)
        self.assertEqual(write["last_updated_at"], "2026-08-27T00:00:00Z")
        self.assertEqual(write["currency"], "AUD")
        self.assertEqual(write["provider"], "static_reference")
        self.assertIn("stale_after", write)


if __name__ == "__main__":
    unittest.main()
