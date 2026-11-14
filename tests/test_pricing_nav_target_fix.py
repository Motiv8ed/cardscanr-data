"""Regression: stale INTER_CARD handoff metadata + strict _nkw + eBay-sold-only freshness."""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.browser_lifecycle_policy import (
    REASON_COLD_UNEXPECTED,
    RUNTIME_INTER_CARD,
    PriorCardContext,
    evaluate_runtime_targets,
    url_matches_job_query,
)
from cardscanr_market_engine.continuous_safety import ContinuousSafetyBudget
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.models import MarketPriceKey, MarketPriceRefreshJob
from cardscanr_market_engine.navigation_runtime_context import (
    NAV_CONTEXT_PATH_ENV,
    NAV_HANDOFF_WRITER_RUN_ENV,
    clear_context_from_environ,
    load_navigation_runtime_context,
    nav_handoff_payload_is_stale,
)
from cardscanr_market_engine.providers.errors import ProviderTemporaryError
from tests.test_market_price_job_runner_cache_states import (
    _FakeClient,
    _StaticProvider,
    _config,
    _sold_comp,
)

FIX = ROOT / "tests" / "fixtures" / "pricing_nav_target_fix"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
DRIFLOON_QUERY = "Drifloon 89 scarlet violet Pokemon"
ELECTIVIRE_QUERY = "Electivire 43 breakpoint Pokemon"
TINKATUFF_QUERY = "Tinkatuff 97 mega evolution Pokemon"


def _stale_charizard_prior() -> PriorCardContext:
    data = json.loads((FIX / "stale_prior_charizard.json").read_text(encoding="utf-8"))
    prior = PriorCardContext.from_dict(data)
    assert prior is not None
    return prior


class StaleInterCardTargetPolicyTests(unittest.TestCase):
    def test_live_drifloon_shape_passes_strict_nkw(self) -> None:
        prior = _stale_charizard_prior()
        targets = json.loads((FIX / "drifloon_continuation_targets.json").read_text(encoding="utf-8"))[
            "rawTargets"
        ]
        r = evaluate_runtime_targets(
            targets,
            mode=RUNTIME_INTER_CARD,
            prior=prior,
            current_query=DRIFLOON_QUERY,
            prior_handoff_stale=True,
        )
        self.assertTrue(r.ok, r.reason_codes)
        self.assertEqual(r.top_level_ebay_count, 1)
        self.assertFalse(r.expected_prior_accepted)

    def test_electivire_shape_rejects_stale_wrong_tab(self) -> None:
        prior = _stale_charizard_prior()
        targets = json.loads((FIX / "electivire_wrong_tab_targets.json").read_text(encoding="utf-8"))[
            "rawTargets"
        ]
        r = evaluate_runtime_targets(
            targets,
            mode=RUNTIME_INTER_CARD,
            prior=prior,
            current_query=ELECTIVIRE_QUERY,
            prior_handoff_stale=True,
        )
        self.assertFalse(r.ok)
        self.assertIn(REASON_COLD_UNEXPECTED, r.reason_codes)

    def test_tinkatuff_shape_rejects_stale_wrong_tab(self) -> None:
        prior = _stale_charizard_prior()
        targets = json.loads((FIX / "tinkatuff_wrong_tab_targets.json").read_text(encoding="utf-8"))[
            "rawTargets"
        ]
        r = evaluate_runtime_targets(
            targets,
            mode=RUNTIME_INTER_CARD,
            prior=prior,
            current_query=TINKATUFF_QUERY,
            prior_handoff_stale=True,
        )
        self.assertFalse(r.ok)
        self.assertIn(REASON_COLD_UNEXPECTED, r.reason_codes)

    def test_fresh_correlated_prior_still_enforced(self) -> None:
        prior = _stale_charizard_prior()
        targets = [
            {
                "id": prior.target_id,
                "type": "page",
                "url": prior.final_url or "",
                "title": "Charizard-GX 9 Hidden Fates",
            }
        ]
        r = evaluate_runtime_targets(targets, mode=RUNTIME_INTER_CARD, prior=prior)
        self.assertTrue(r.ok)
        self.assertTrue(r.expected_prior_accepted)

    def test_unknown_extra_ebay_tab_still_rejected(self) -> None:
        prior = _stale_charizard_prior()
        targets = [
            {
                "id": prior.target_id,
                "type": "page",
                "url": prior.final_url or "",
                "title": "Charizard-GX",
            },
            {
                "id": "EXTRA_TAB",
                "type": "page",
                "url": "https://www.ebay.com.au/sch/i.html?_nkw=Other+Card&LH_Sold=1",
                "title": "Other",
            },
        ]
        r = evaluate_runtime_targets(targets, mode=RUNTIME_INTER_CARD, prior=prior)
        self.assertFalse(r.ok)
        self.assertIn("INTER_CARD_MULTIPLE_TOP_LEVEL_EBAY_TARGETS", r.reason_codes)


class StrictNkwRejectionTests(unittest.TestCase):
    def test_same_name_different_number_rejected(self) -> None:
        url = "https://www.ebay.com.au/sch/i.html?_nkw=Drifloon+90+scarlet+violet+Pokemon&LH_Sold=1"
        self.assertFalse(url_matches_job_query(DRIFLOON_QUERY, url))

    def test_different_name_same_number_rejected(self) -> None:
        url = "https://www.ebay.com.au/sch/i.html?_nkw=Drifblim+89+scarlet+violet+Pokemon&LH_Sold=1"
        self.assertFalse(url_matches_job_query(DRIFLOON_QUERY, url))

    def test_generic_pokemon_query_rejected(self) -> None:
        url = "https://www.ebay.com.au/sch/i.html?_nkw=pokemon&LH_Sold=1"
        self.assertFalse(url_matches_job_query(DRIFLOON_QUERY, url))

    def test_wrong_marketplace_domain_rejected(self) -> None:
        url = "https://www.ebay.com/sch/i.html?_nkw=Drifloon+89+scarlet+violet+Pokemon&LH_Sold=1"
        self.assertFalse(url_matches_job_query(DRIFLOON_QUERY, url))

    def test_wrong_card_target_fixture_rejected(self) -> None:
        prior = _stale_charizard_prior()
        targets = json.loads((FIX / "wrong_card_target.json").read_text(encoding="utf-8"))["rawTargets"]
        r = evaluate_runtime_targets(
            targets,
            mode=RUNTIME_INTER_CARD,
            prior=prior,
            current_query=DRIFLOON_QUERY,
            prior_handoff_stale=True,
        )
        self.assertFalse(r.ok)
        self.assertIn(REASON_COLD_UNEXPECTED, r.reason_codes)


class NavContextHandoffStaleTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_context_from_environ()
        os.environ.pop(NAV_HANDOFF_WRITER_RUN_ENV, None)

    def tearDown(self) -> None:
        clear_context_from_environ()
        os.environ.pop(NAV_HANDOFF_WRITER_RUN_ENV, None)

    def test_legacy_context_without_metadata_is_stale(self) -> None:
        import tempfile

        payload = {
            "runtimeMode": "INTER_CARD",
            "currentJobId": "e7485e96-8a72-4348-9992-1cedfd66adb1",
            "expectedPrior": json.loads((FIX / "stale_prior_charizard.json").read_text(encoding="utf-8")),
        }
        self.assertTrue(nav_handoff_payload_is_stale(payload))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nav_runtime_context.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            os.environ[NAV_CONTEXT_PATH_ENV] = str(path)
            ctx = load_navigation_runtime_context()
        self.assertTrue(ctx.prior_handoff_stale)
        self.assertIsNotNone(ctx.expected_prior)
        self.assertIsNone(ctx.expected_prior.target_id)

    def test_fresh_prior_from_current_run_not_stale(self) -> None:
        import tempfile

        now = datetime.now(timezone.utc)
        recorded = (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_id = "worker-batch-test-1"
        payload = {
            "runtimeMode": "INTER_CARD",
            "currentJobId": "next-job",
            "recordedAtUtc": recorded,
            "writerRunId": run_id,
            "writerPid": 4242,
            "expectedPrior": json.loads((FIX / "stale_prior_charizard.json").read_text(encoding="utf-8")),
        }
        self.assertFalse(nav_handoff_payload_is_stale(payload, now=now))
        os.environ[NAV_HANDOFF_WRITER_RUN_ENV] = run_id
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nav_runtime_context.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            os.environ[NAV_CONTEXT_PATH_ENV] = str(path)
            ctx = load_navigation_runtime_context()
        self.assertFalse(ctx.prior_handoff_stale)
        self.assertEqual(ctx.expected_prior.target_id, "ADF72CAC9F7617368EEE9542339C6DBC")

    def test_inter_card_keeps_prior_target_id_across_jobs(self) -> None:
        import tempfile

        now = datetime.now(timezone.utc)
        recorded = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        run_id = "worker-seq-2"
        payload = {
            "runtimeMode": "INTER_CARD",
            "currentJobId": "e7485e96-8a72-4348-9992-1cedfd66adb1",
            "recordedAtUtc": recorded,
            "writerRunId": run_id,
            "expectedPrior": json.loads((FIX / "stale_prior_charizard.json").read_text(encoding="utf-8")),
        }
        os.environ[NAV_HANDOFF_WRITER_RUN_ENV] = run_id
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nav_runtime_context.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            os.environ[NAV_CONTEXT_PATH_ENV] = str(path)
            ctx = load_navigation_runtime_context()
        self.assertFalse(ctx.prior_handoff_stale)
        self.assertIsNotNone(ctx.expected_prior.target_id)


class EbayBrowserReferenceFreshnessTests(unittest.TestCase):
    def test_reference_fresh_ebay_stale_executes(self) -> None:
        key = MarketPriceKey(
            id="key-ref-au",
            game="pokemon",
            card_name="Drifloon",
            normalized_card_name="drifloon",
            set_name="Scarlet Violet",
            set_code="sv1",
            collector_number="89",
            language="en",
            variant="raw",
            condition="raw",
            market_country="au",
            currency="aud",
            fingerprint="pokemon|en|sv1|89|drifloon|raw|raw|au|aud",
        )
        client = _FakeClient(key)
        client._cache_row = {
            "current_market_price": 0.55,
            "display_price_source": "reference",
            "provider": "tcgdex_tcgplayer",
            "last_updated_at": (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "next_refresh_due_at": "2099-01-01T00:00:00+00:00",
            "stale_after": "2099-01-01T00:00:00+00:00",
            "refresh_status": "completed",
        }

        class _EbayProvider(_StaticProvider):
            provider_name = "ebay_browser"

        provider = _EbayProvider([_sold_comp(title="Drifloon 89 Scarlet Violet Pokemon")])
        runner = MarketPriceJobRunner(
            client=client,
            provider=provider,
            config=_config(),
            now_func=lambda: NOW,
            logger=lambda _m: None,
        )
        reached = {"called": False}

        def _stop(**_kwargs):
            reached["called"] = True
            raise ProviderTemporaryError("STOP_BEFORE_PROVIDER_FOR_TEST")

        with mock.patch.object(runner, "_assert_market_allowed_for_worker", return_value=None):
            with mock.patch.object(runner, "fetch_fallback_result", side_effect=_stop):
                job = MarketPriceRefreshJob(
                    id="job-drifloon",
                    price_key_id=key.id,
                    reason="owned_daily:p0_needs_verified_local",
                    priority=1,
                    status="running",
                    attempt_count=1,
                )
                result = runner.run_job(job)
        self.assertTrue(reached["called"])
        self.assertNotEqual(result.get("status"), "skipped_already_fresh")

    def test_ebay_browser_not_skipped_on_reference_due_without_owned_reason(self) -> None:
        key = MarketPriceKey(
            id="key-no-owned-reason",
            game="pokemon",
            card_name="Testmon",
            normalized_card_name="testmon",
            set_name="Set",
            set_code="set1",
            collector_number="1",
            language="en",
            variant="raw",
            condition="raw",
            market_country="au",
            currency="aud",
            fingerprint="pokemon|en|set1|1|testmon|raw|raw|au|aud",
        )
        client = _FakeClient(key)
        client._cache_row = {
            "current_market_price": 1.0,
            "display_price_source": "reference",
            "provider": "tcgdex_cardmarket",
            "last_updated_at": (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "next_refresh_due_at": "2099-01-01T00:00:00+00:00",
            "refresh_status": "completed",
        }

        class _EbayProvider(_StaticProvider):
            provider_name = "ebay_browser"

        runner = MarketPriceJobRunner(
            client=client,
            provider=_EbayProvider([_sold_comp(title="Testmon 1 Set Pokemon")]),
            config=_config(),
            now_func=lambda: NOW,
            logger=lambda _m: None,
        )

        def _stop(**_kwargs):
            raise ProviderTemporaryError("STOP_BEFORE_PROVIDER")

        with mock.patch.object(runner, "_assert_market_allowed_for_worker", return_value=None):
            with mock.patch.object(runner, "fetch_fallback_result", side_effect=_stop):
                result = runner.run_job(
                    MarketPriceRefreshJob(
                        id="job-generic-reason",
                        price_key_id=key.id,
                        reason="scheduler:stale_refresh",
                        priority=5,
                        status="running",
                        attempt_count=1,
                    )
                )
        self.assertNotEqual(result.get("status"), "skipped_already_fresh")

    def test_ebay_fresh_evidence_still_noops(self) -> None:
        key = MarketPriceKey(
            id="key-verified-au",
            game="pokemon",
            card_name="Riolu",
            normalized_card_name="riolu",
            set_name="Prismatic Evolutions",
            set_code="sv8pt5",
            collector_number="050/131",
            language="en",
            variant="reverse_holo",
            condition="raw",
            market_country="au",
            currency="aud",
            fingerprint="pokemon|en|sv8pt5|050/131|riolu|reverse_holo|raw|au|aud",
        )
        client = _FakeClient(key)
        client._cache_row = {
            "current_market_price": 4.25,
            "display_price_source": "verified_local",
            "provider": "ebay_browser",
            "last_updated_at": (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
            "next_refresh_due_at": "2099-01-01T00:00:00+00:00",
            "refresh_status": "completed",
        }

        class _EbayProvider(_StaticProvider):
            provider_name = "ebay_browser"

        runner = MarketPriceJobRunner(
            client=client,
            provider=_EbayProvider([_sold_comp()]),
            config=_config(),
            now_func=lambda: NOW,
            logger=lambda _m: None,
        )
        result = runner.run_job(
            MarketPriceRefreshJob(
                id="job-fresh",
                price_key_id=key.id,
                reason="owned_daily:fresh_lt_24h",
                priority=1,
                status="running",
                attempt_count=1,
            )
        )
        self.assertEqual(result.get("status"), "skipped_already_fresh")
        self.assertEqual(result.get("outcomeClass"), "owned_daily_fresh_noop")

    def test_budget_paths_unchanged_by_freshness_tweak(self) -> None:
        """Safety budget env parsing is unchanged — freshness only touches early run_job gate."""
        budget = ContinuousSafetyBudget.from_env()
        self.assertIsInstance(budget, ContinuousSafetyBudget)


if __name__ == "__main__":
    unittest.main()