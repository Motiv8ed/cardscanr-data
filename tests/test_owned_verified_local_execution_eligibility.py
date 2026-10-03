"""Source-aware owned verified-local execution eligibility + job_runner alignment."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.models import MarketPriceKey, MarketPriceRefreshJob
from cardscanr_market_engine.owned_daily_source_policy import classify_owned_daily_band
from cardscanr_market_engine.owned_verified_local_execution import (
    evaluate_owned_verified_local_execution,
    job_requests_owned_verified_local,
    scheduler_jobrunner_agreement,
)
from tests.test_market_price_job_runner_cache_states import (
    _FakeClient,
    _StaticProvider,
    _config,
    _riolu_key,
    _sold_comp,
)

NOW = datetime(2026, 10, 2, 11, 21, tzinfo=timezone.utc)


def _ref_row(**overrides):
    row = {
        "current_market_price": 0.22,
        "display_price_source": "reference",
        "provider": "tcgdex_tcgplayer",
        "last_updated_at": "2026-10-02T10:58:20.761596+00:00",
        "next_refresh_due_at": "2026-10-02T22:58:20.761596+00:00",
        "stale_after": "2026-10-02T22:58:20.761596+00:00",
        "refresh_status": "completed",
    }
    row.update(overrides)
    return row


class ExecutionEligibilityUnitTests(unittest.TestCase):
    def test_recent_reference_only_never_verified_executes(self) -> None:
        d = evaluate_owned_verified_local_execution(_ref_row(), now=NOW)
        self.assertTrue(d.should_execute)
        self.assertFalse(d.would_skip_fresh)
        self.assertEqual(d.scheduler_band, "P0_NEEDS_VERIFIED_LOCAL")
        self.assertEqual(d.source_class, "reference_only")

    def test_old_reference_only_executes(self) -> None:
        d = evaluate_owned_verified_local_execution(
            _ref_row(last_updated_at="2026-09-01T00:00:00Z", next_refresh_due_at="2026-09-01T12:00:00Z"),
            now=NOW,
        )
        self.assertTrue(d.should_execute)

    def test_no_price_executes(self) -> None:
        d = evaluate_owned_verified_local_execution(
            {"current_market_price": None, "display_price_source": None, "provider": None},
            now=NOW,
        )
        self.assertTrue(d.should_execute)
        self.assertEqual(d.scheduler_band, "P0_NEVER_PRICED")

    def test_recent_verified_local_skips(self) -> None:
        d = evaluate_owned_verified_local_execution(
            {
                "current_market_price": 1.5,
                "display_price_source": "verified_local",
                "provider": "ebay_browser",
                "last_updated_at": (NOW - timedelta(hours=2)).isoformat(),
                "refresh_status": "completed",
            },
            now=NOW,
        )
        self.assertFalse(d.should_execute)
        self.assertTrue(d.would_skip_fresh)
        self.assertEqual(d.reason_code, "SKIP_ALREADY_FRESH_VERIFIED_LOCAL")

    def test_stale_verified_local_executes(self) -> None:
        d = evaluate_owned_verified_local_execution(
            {
                "current_market_price": 1.5,
                "display_price_source": "verified_local",
                "provider": "ebay_browser",
                "last_updated_at": (NOW - timedelta(hours=30)).isoformat(),
                "refresh_status": "completed",
            },
            now=NOW,
        )
        self.assertTrue(d.should_execute)
        self.assertEqual(d.scheduler_band, "P1_STALE_GT_24H")

    def test_reference_timestamp_cannot_refresh_verified_freshness(self) -> None:
        d = evaluate_owned_verified_local_execution(_ref_row(), now=NOW)
        self.assertIsNone(d.successful_verified_at)
        self.assertTrue(d.should_execute)

    def test_local_infra_failure_reference_still_executes(self) -> None:
        d = evaluate_owned_verified_local_execution(
            _ref_row(
                refresh_status="failed",
                last_error_message="POST_SOLD_CAPTURE_FAILURE: CDP_CONNECT_FAILURE",
            ),
            now=NOW,
        )
        self.assertTrue(d.should_execute)
        self.assertEqual(d.scheduler_band, "P0_NEEDS_VERIFIED_LOCAL")

    def test_scheduler_due_and_execution_agree(self) -> None:
        row = _ref_row()
        band, due, *_ = classify_owned_daily_band(row, now=NOW)
        d = evaluate_owned_verified_local_execution(row, now=NOW)
        agr = scheduler_jobrunner_agreement(scheduler_due=due, execution=d)
        self.assertEqual(band, "P0_NEEDS_VERIFIED_LOCAL")
        self.assertTrue(agr["agrees"])
        self.assertTrue(agr["executionEligible"])

    def test_state_change_to_verified_may_skip(self) -> None:
        # Scheduler saw reference-only due; runner sees newly verified-local.
        d = evaluate_owned_verified_local_execution(
            {
                "current_market_price": 2.0,
                "display_price_source": "verified_local",
                "provider": "ebay_browser",
                "last_updated_at": (NOW - timedelta(minutes=5)).isoformat(),
                "refresh_status": "completed",
            },
            now=NOW,
        )
        agr = scheduler_jobrunner_agreement(scheduler_due=True, execution=d)
        self.assertFalse(agr["agrees"])
        self.assertFalse(d.should_execute)

    def test_intent_detection(self) -> None:
        self.assertTrue(job_requests_owned_verified_local(reason="owned_daily:p0_needs_verified_local"))
        self.assertTrue(
            job_requests_owned_verified_local(
                reason="reliability_five_consecutive_post_unicode:owned_daily:p0_needs_verified_local"
            )
        )
        self.assertFalse(job_requests_owned_verified_local(reason="unit_test"))
        self.assertFalse(job_requests_owned_verified_local(reason="manual_refresh"))


class JobRunnerSourceAwareFreshnessTests(unittest.TestCase):
    def test_ceruledge_reference_not_skipped_already_fresh(self) -> None:
        key = MarketPriceKey(
            id="686b5181-145f-4a87-a770-d4176e520ec1",
            game="pokemon",
            card_name="Ceruledge",
            normalized_card_name="ceruledge",
            set_name="Phantasmal Flames",
            set_code="me2",
            collector_number="20",
            language="en",
            variant="raw",
            condition="raw",
            market_country="au",
            currency="aud",
            fingerprint="pokemon|en|me2|20|ceruledge|raw|raw|au|aud",
        )
        client = _FakeClient(key)
        client._cache_row = _ref_row()
        provider = _StaticProvider([_sold_comp(title="Ceruledge 20 Phantasmal Flames Pokemon")])
        runner = MarketPriceJobRunner(
            client=client,
            provider=provider,
            config=_config(),
            now_func=lambda: NOW,
            logger=lambda _m: None,
        )
        # Marketplace gate may block — patch allow. Stop at provider; run_job
        # catches provider exceptions into status=failed (not skip).
        provider_reached = {"called": False}

        def _stop_before_provider(**_kwargs):
            provider_reached["called"] = True
            raise RuntimeError("STOP_BEFORE_PROVIDER_FOR_TEST")

        with mock.patch.object(runner, "_assert_market_allowed_for_worker", return_value=None):
            with mock.patch.object(
                runner,
                "fetch_fallback_result",
                side_effect=_stop_before_provider,
            ):
                job = MarketPriceRefreshJob(
                    id="job-ceruledge",
                    price_key_id=key.id,
                    reason="reliability_five_consecutive_post_unicode:owned_daily:p0_needs_verified_local",
                    priority=1,
                    status="running",
                    attempt_count=1,
                )
                result = runner.run_job(job)
        self.assertTrue(provider_reached["called"])
        self.assertNotEqual(result.get("status"), "skipped_already_fresh")
        self.assertEqual(len(client.cancelled_jobs), 0)
        self.assertIn("STOP_BEFORE_PROVIDER_FOR_TEST", str(result.get("error") or ""))

    def test_non_owned_manual_still_skips_on_due(self) -> None:
        client = _FakeClient(_riolu_key())
        client._cache_row = {
            "current_market_price": 4.25,
            "next_refresh_due_at": "2099-01-01T00:00:00+00:00",
            "display_price_source": "verified_local",
            "provider": "ebay_browser",
            "last_updated_at": "2026-06-01T00:00:00+00:00",
        }
        runner = MarketPriceJobRunner(
            client=client,
            provider=_StaticProvider([_sold_comp()]),
            config=_config(),
            now_func=lambda: datetime(2026, 6, 1, tzinfo=timezone.utc),
            logger=lambda _message: None,
        )
        result = runner.run_job(
            MarketPriceRefreshJob(
                id="job-1",
                price_key_id="key-riolu-au",
                reason="unit_test",
                priority=10,
                status="running",
                attempt_count=1,
            )
        )
        self.assertEqual(result["status"], "skipped_already_fresh")
        self.assertEqual(result.get("outcomeClass"), "already_fresh_noop")


if __name__ == "__main__":
    unittest.main()
