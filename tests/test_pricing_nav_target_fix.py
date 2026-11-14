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
    REASON_NOT_CORRELATED,
    REASON_PRIOR_NOT_TERMINAL,
    REASON_UNKNOWN_EBAY,
    RUNTIME_INTER_CARD,
    PriorCardContext,
    ebay_au_sch_sold_query_contract_ok,
    evaluate_runtime_targets,
    url_matches_job_query,
)
from cardscanr_market_engine.providers.sold_page_health import evaluate_sold_verification
from cardscanr_market_engine.continuous_safety import ContinuousSafetyBudget
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.models import MarketPriceKey, MarketPriceRefreshJob
from cardscanr_market_engine.navigation_runtime_context import (
    NAV_CONTEXT_ENV,
    NAV_CONTEXT_PATH_ENV,
    NAV_HANDOFF_WRITER_RUN_ENV,
    clear_context_from_environ,
    load_navigation_runtime_context,
    mint_nav_handoff_writer_run_id,
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

    def test_fresh_healthy_prior_matching_tab_without_stale_handoff_rejected(self) -> None:
        prior = _stale_charizard_prior()
        targets = json.loads((FIX / "drifloon_continuation_targets.json").read_text(encoding="utf-8"))[
            "rawTargets"
        ]
        r = evaluate_runtime_targets(
            targets,
            mode=RUNTIME_INTER_CARD,
            prior=prior,
            current_query=DRIFLOON_QUERY,
            prior_handoff_stale=False,
        )
        self.assertFalse(r.ok)
        self.assertIn(REASON_UNKNOWN_EBAY, r.reason_codes)
        self.assertIn(REASON_NOT_CORRELATED, r.reason_codes)

    def test_fresh_unhealthy_prior_with_sold_tab_rejected(self) -> None:
        prior = PriorCardContext(
            job_id="j1",
            target_id="OLD",
            final_url="https://www.ebay.com.au/sch/i.html?_nkw=Old+Card&LH_Sold=1",
            query="Old Card",
            x11_sold_state_verified=False,
            card_verdict="FAIL",
        )
        url = "https://www.ebay.com.au/sch/i.html?_nkw=Drifloon+89+scarlet+violet+Pokemon&LH_Sold=1"
        targets = [{"id": "NEW_TAB", "type": "page", "url": url, "title": "Drifloon"}]
        r = evaluate_runtime_targets(
            targets,
            mode=RUNTIME_INTER_CARD,
            prior=prior,
            current_query=DRIFLOON_QUERY,
            prior_handoff_stale=False,
        )
        self.assertFalse(r.ok)
        self.assertIn(REASON_PRIOR_NOT_TERMINAL, r.reason_codes)


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

    def test_active_listings_tab_without_lh_sold_rejected(self) -> None:
        url = "https://www.ebay.com.au/sch/i.html?_nkw=Drifloon+89+scarlet+violet+Pokemon"
        self.assertFalse(ebay_au_sch_sold_query_contract_ok(url))
        self.assertFalse(url_matches_job_query(DRIFLOON_QUERY, url))

    def test_lh_sold_zero_and_duplicate_rejected(self) -> None:
        zero = "https://www.ebay.com.au/sch/i.html?_nkw=Drifloon+89+scarlet+violet+Pokemon&LH_Sold=0"
        dup = "https://www.ebay.com.au/sch/i.html?_nkw=Drifloon+89+scarlet+violet+Pokemon&LH_Sold=1&LH_Sold=0"
        self.assertFalse(ebay_au_sch_sold_query_contract_ok(zero))
        self.assertFalse(ebay_au_sch_sold_query_contract_ok(dup))

    def test_hostile_url_variants_rejected(self) -> None:
        evil = "https://www.ebay.com.au.evil.com/sch/i.html?_nkw=Drifloon+89+scarlet+violet+Pokemon&LH_Sold=1"
        userinfo = (
            "https://attacker:secret@www.ebay.com.au/sch/i.html"
            "?_nkw=Drifloon+89+scarlet+violet+Pokemon&LH_Sold=1"
        )
        http_scheme = "http://www.ebay.com.au/sch/i.html?_nkw=Drifloon+89+scarlet+violet+Pokemon&LH_Sold=1"
        extra_nkw = "https://www.ebay.com.au/sch/i.html?_nkw=a&_nkw=b&LH_Sold=1"
        self.assertFalse(url_matches_job_query(DRIFLOON_QUERY, evil))
        self.assertFalse(url_matches_job_query(DRIFLOON_QUERY, userinfo))
        self.assertFalse(url_matches_job_query(DRIFLOON_QUERY, http_scheme))
        self.assertFalse(url_matches_job_query(DRIFLOON_QUERY, extra_nkw))

    def test_empty_query_and_empty_nkw_rejected(self) -> None:
        self.assertFalse(
            url_matches_job_query(None, "https://www.ebay.com.au/sch/i.html?_nkw=+&LH_Sold=1")
        )
        self.assertFalse(url_matches_job_query("", "https://www.ebay.com.au/sch/i.html?LH_Sold=1"))
        self.assertFalse(url_matches_job_query(DRIFLOON_QUERY, "https://www.ebay.com.au/sch/i.html?LH_Sold=1"))

    def test_continuation_tab_without_sold_verification_blocks_capture(self) -> None:
        active = "https://www.ebay.com.au/sch/i.html?_nkw=Drifloon+89+scarlet+violet+Pokemon"
        ev = evaluate_sold_verification(
            url=active,
            title="Drifloon 89 scarlet violet Pokemon for sale | eBay",
            body="",
        )
        self.assertFalse(ev.get("captureReady"))
        self.assertFalse(ev.get("x11SoldStateVerified"))

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
        os.environ[NAV_HANDOFF_WRITER_RUN_ENV] = run_id
        self.assertFalse(nav_handoff_payload_is_stale(payload, now=now))
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

    def test_recorded_at_without_writer_run_id_is_stale(self) -> None:
        payload = {
            "recordedAtUtc": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        self.assertTrue(nav_handoff_payload_is_stale(payload, now=NOW, require_env_writer_run=False))

    def test_future_dated_recorded_at_is_stale(self) -> None:
        future = (NOW + timedelta(seconds=300)).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "recordedAtUtc": future,
            "writerRunId": "run-future",
        }
        self.assertTrue(nav_handoff_payload_is_stale(payload, now=NOW, require_env_writer_run=False))

    def test_malformed_recorded_at_is_stale(self) -> None:
        payload = {"recordedAtUtc": "not-a-timestamp", "writerRunId": "run-bad-ts"}
        self.assertTrue(nav_handoff_payload_is_stale(payload, require_env_writer_run=False))

    def test_worker_restart_mints_new_run_id_and_file_handoff_is_stale(self) -> None:
        import tempfile

        now = datetime.now(timezone.utc)
        recorded = (now - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        old_run = "prior-worker-aaa"
        payload = {
            "runtimeMode": "INTER_CARD",
            "recordedAtUtc": recorded,
            "writerRunId": old_run,
            "expectedPrior": json.loads((FIX / "stale_prior_charizard.json").read_text(encoding="utf-8")),
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nav_runtime_context.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            os.environ[NAV_CONTEXT_PATH_ENV] = str(path)
            mint_nav_handoff_writer_run_id()
            ctx = load_navigation_runtime_context()
        self.assertTrue(ctx.prior_handoff_stale)
        self.assertNotEqual(os.environ.get(NAV_HANDOFF_WRITER_RUN_ENV), old_run)

    def test_subprocess_without_env_treats_handoff_as_stale(self) -> None:
        import tempfile

        now = datetime.now(timezone.utc)
        recorded = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        run_id = "worker-env-missing"
        payload = {
            "runtimeMode": "INTER_CARD",
            "recordedAtUtc": recorded,
            "writerRunId": run_id,
            "expectedPrior": json.loads((FIX / "stale_prior_charizard.json").read_text(encoding="utf-8")),
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nav_runtime_context.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            os.environ[NAV_CONTEXT_PATH_ENV] = str(path)
            ctx = load_navigation_runtime_context()
        self.assertTrue(ctx.prior_handoff_stale)

    def test_subprocess_inherits_env_writer_run_handoff_fresh(self) -> None:
        import tempfile

        now = datetime.now(timezone.utc)
        recorded = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_id = "worker-child-inherit"
        payload = {
            "runtimeMode": "INTER_CARD",
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

    def test_fresh_env_handoff_overrides_stale_disk_metadata(self) -> None:
        import tempfile

        now = datetime.now(timezone.utc)
        stale_recorded = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fresh_recorded = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        run_id = "env-handoff-run"
        prior = json.loads((FIX / "stale_prior_charizard.json").read_text(encoding="utf-8"))
        file_payload = {
            "runtimeMode": "INTER_CARD",
            "recordedAtUtc": stale_recorded,
            "writerRunId": "stale-disk",
            "expectedPrior": prior,
        }
        env_ctx = {
            "runtimeMode": "INTER_CARD",
            "recordedAtUtc": fresh_recorded,
            "writerRunId": run_id,
            "expectedPrior": prior,
        }
        os.environ[NAV_HANDOFF_WRITER_RUN_ENV] = run_id
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nav_runtime_context.json"
            path.write_text(json.dumps(file_payload), encoding="utf-8")
            os.environ[NAV_CONTEXT_PATH_ENV] = str(path)
            os.environ[NAV_CONTEXT_ENV] = json.dumps(env_ctx, separators=(",", ":"))
            ctx = load_navigation_runtime_context()
        self.assertEqual(ctx.handoff_source, "env")
        self.assertFalse(ctx.prior_handoff_stale)

    def test_stale_env_handoff_not_refreshed_by_fresh_disk_file(self) -> None:
        import tempfile

        now = datetime.now(timezone.utc)
        fresh_recorded = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        stale_recorded = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_id = "env-stale-run"
        prior = json.loads((FIX / "stale_prior_charizard.json").read_text(encoding="utf-8"))
        file_payload = {
            "runtimeMode": "INTER_CARD",
            "recordedAtUtc": fresh_recorded,
            "writerRunId": "fresh-disk",
            "expectedPrior": prior,
        }
        env_ctx = {
            "runtimeMode": "INTER_CARD",
            "recordedAtUtc": stale_recorded,
            "writerRunId": run_id,
            "expectedPrior": prior,
        }
        os.environ[NAV_HANDOFF_WRITER_RUN_ENV] = run_id
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nav_runtime_context.json"
            path.write_text(json.dumps(file_payload), encoding="utf-8")
            os.environ[NAV_CONTEXT_PATH_ENV] = str(path)
            os.environ[NAV_CONTEXT_ENV] = json.dumps(env_ctx, separators=(",", ":"))
            ctx = load_navigation_runtime_context()
        self.assertEqual(ctx.handoff_source, "env")
        self.assertTrue(ctx.prior_handoff_stale)


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

class WriterRunPropagationTests(unittest.TestCase):
    """Writer run id must cross the explicit-export (WSL) boundary for same-run handoffs."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._saved_run = os.environ.get(NAV_HANDOFF_WRITER_RUN_ENV)

    def tearDown(self) -> None:
        clear_context_from_environ()
        if self._saved_run is None:
            os.environ.pop(NAV_HANDOFF_WRITER_RUN_ENV, None)
        else:
            os.environ[NAV_HANDOFF_WRITER_RUN_ENV] = self._saved_run
        self._tmp.cleanup()

    def _ctx(self):
        from cardscanr_market_engine.navigation_runtime_context import NavigationRuntimeContext

        return NavigationRuntimeContext(
            runtime_mode=RUNTIME_INTER_CARD,
            current_job_id="next-job",
            current_query=DRIFLOON_QUERY,
            expected_prior=_stale_charizard_prior(),
        )

    def _run_child(self, exports: dict, *, forward_run_id: bool) -> dict:
        import subprocess

        from cardscanr_market_engine.navigation_runtime_context import EXPECTED_PRIOR_PATH_ENV

        env = {k: v for k, v in os.environ.items() if not k.startswith("CARDSCANR_")}
        for key, value in exports.items():
            if key == NAV_HANDOFF_WRITER_RUN_ENV and not forward_run_id:
                continue
            env[key] = value
        # WSL paths are remapped to the Windows temp files for this offline child.
        env[NAV_CONTEXT_PATH_ENV] = str(self.tmp / "nav.json")
        env[EXPECTED_PRIOR_PATH_ENV] = str(self.tmp / "nav.prior.json")
        code = (
            "import json,sys; sys.path.insert(0, sys.argv[1]);"
            "from cardscanr_market_engine.navigation_runtime_context import load_navigation_runtime_context as L;"
            "c=L(); p=c.expected_prior;"
            "print(json.dumps({'stale': c.prior_handoff_stale, 'target': p.target_id if p else None}))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code, str(ROOT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_env_exports_forward_writer_run_id(self) -> None:
        run_id = mint_nav_handoff_writer_run_id()
        exports = self._ctx().env_exports(path=self.tmp / "nav.json")
        self.assertEqual(exports.get(NAV_HANDOFF_WRITER_RUN_ENV), run_id)
        written = json.loads((self.tmp / "nav.json").read_text(encoding="utf-8"))
        self.assertEqual(written.get("writerRunId"), run_id)

    def test_writer_without_run_id_mints_own_and_exports_it(self) -> None:
        os.environ.pop(NAV_HANDOFF_WRITER_RUN_ENV, None)
        exports = self._ctx().env_exports(path=self.tmp / "nav.json")
        run_id = exports.get(NAV_HANDOFF_WRITER_RUN_ENV)
        self.assertTrue(run_id)
        written = json.loads((self.tmp / "nav.json").read_text(encoding="utf-8"))
        self.assertEqual(written.get("writerRunId"), run_id)

    def test_cross_process_same_run_handoff_is_fresh(self) -> None:
        mint_nav_handoff_writer_run_id()
        exports = self._ctx().env_exports(path=self.tmp / "nav.json")
        result = self._run_child(exports, forward_run_id=True)
        self.assertFalse(result["stale"])
        self.assertEqual(result["target"], _stale_charizard_prior().target_id)

    def test_cross_process_without_run_id_is_stale(self) -> None:
        mint_nav_handoff_writer_run_id()
        exports = self._ctx().env_exports(path=self.tmp / "nav.json")
        result = self._run_child(exports, forward_run_id=False)
        self.assertTrue(result["stale"])
        self.assertIsNone(result["target"])

    def test_cross_process_other_run_id_is_stale(self) -> None:
        mint_nav_handoff_writer_run_id()
        exports = dict(self._ctx().env_exports(path=self.tmp / "nav.json"))
        exports[NAV_HANDOFF_WRITER_RUN_ENV] = "other-run-000000000000"
        result = self._run_child(exports, forward_run_id=True)
        self.assertTrue(result["stale"])
        self.assertIsNone(result["target"])

    def test_wsl_search_call_receives_writer_run_id(self) -> None:
        from cardscanr_market_engine.navigation_runtime_context import (
            PRE_SUBMIT_ONLY_ENV,
            PRE_SUBMIT_QUERY_READY,
            apply_context_to_environ,
        )
        from cardscanr_market_engine.providers.linux_x11_ebay_nav import navigate_query_to_sold

        run_id = mint_nav_handoff_writer_run_id()
        apply_context_to_environ(self._ctx())
        os.environ[PRE_SUBMIT_ONLY_ENV] = "1"
        captured: dict = {}

        def _fake_wsl(args, **kwargs):
            captured["env"] = dict(kwargs.get("env_exports") or {})
            return {
                "ok": True,
                "resultCode": PRE_SUBMIT_QUERY_READY,
                "preSubmitQueryReady": True,
                "query": DRIFLOON_QUERY,
                "submitted": False,
                "searchSubmissionStarted": False,
                "queryVisibleConfirmed": True,
            }

        try:
            with mock.patch(
                "cardscanr_market_engine.providers.linux_x11_ebay_nav._wsl_python",
                side_effect=_fake_wsl,
            ):
                navigate_query_to_sold(DRIFLOON_QUERY, reset_homepage=True, attempt_id="a1", pre_submit_only=True)
        finally:
            os.environ.pop(PRE_SUBMIT_ONLY_ENV, None)
        self.assertEqual(captured["env"].get(NAV_HANDOFF_WRITER_RUN_ENV), run_id)

if __name__ == "__main__":
    unittest.main()