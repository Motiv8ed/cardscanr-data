"""Set-sweep worker/sched integration and browser-recycle nav guard."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.browser_lifecycle_policy import RUNTIME_INTER_CARD, evaluate_runtime_targets
from cardscanr_market_engine import continuous_safety
from cardscanr_market_engine.continuous_safety import ContinuousSafetyBudget, SubmissionEvent
from cardscanr_market_engine.ebay_scale.audit_report import summarise_sweeps
from cardscanr_market_engine.ebay_scale.set_sweep_matcher import SetSweepCatalogueCard, match_sold_row_to_catalogue
from cardscanr_market_engine.ebay_scale.set_sweep_worker_hook import (
    build_set_level_query_text,
    maybe_apply_set_sweep_live_skip,
    record_set_sweep_plan_from_target,
    should_attempt_set_sweep,
    try_set_sweep_worker_cycle,
)
from cardscanr_market_engine.ebay_scale.sold_row_html import SoldResultsRow
from cardscanr_market_engine.ebay_scale.shadow_audit import build_set_sweep_audit_rows
from cardscanr_market_engine.models import MarketPriceKey
from cardscanr_market_engine.navigation_runtime_context import (
    NAV_BROWSER_RECYCLE_FIELD,
    PriorCardContext,
    apply_context_to_environ,
    load_navigation_runtime_context,
    reconcile_navigation_context_for_browser_recycle,
)
from cardscanr_market_engine.providers import ebay_browser_provider as ebp
from tests.test_pricing_nav_target_fix import DRIFLOON_QUERY, _stale_charizard_prior

FIXTURE = ROOT / "tests" / "fixtures" / "ebay_scale" / "sv3_set_sweep_fixture.html"


class FakeOwnedClient:
    def __init__(self, targets: list[dict]) -> None:
        self.targets = targets

    def list_owned_market_pricing_targets(self, **_kwargs) -> dict:
        return {"targets": list(self.targets)}


class SetSweepWorkerHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        for key in list(os.environ.keys()):
            if key.startswith("EBAY_SCALE") or key.startswith("MAX_LIVE_SUBMISSIONS"):
                os.environ.pop(key, None)

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._tmp.cleanup()

    def _targets(self) -> list[dict]:
        return [
            {
                "fingerprint": "fp-charizard",
                "catalogue_card_id": "charizard-ex",
                "card_name": "Charizard ex",
                "set_name": "Obsidian Flames",
                "set_id": "sv3",
                "collector_number": "223/197",
                "market_country": "AU",
                "current_market_price": 120.0,
            },
            {
                "fingerprint": "fp-pidgey",
                "catalogue_card_id": "pidgey",
                "card_name": "Pidgey",
                "set_name": "Obsidian Flames",
                "set_id": "sv3",
                "collector_number": "162/197",
                "market_country": "AU",
                "current_market_price": 0.5,
            },
        ]

    def _catalogue(self) -> list[SetSweepCatalogueCard]:
        return [
            SetSweepCatalogueCard(
                catalogue_card_id="charizard-ex",
                card_name="Charizard ex",
                normalized_card_name="charizard_ex",
                set_name="Obsidian Flames",
                set_code="sv3",
                collector_number="223/197",
            ),
            SetSweepCatalogueCard(
                catalogue_card_id="pidgey",
                card_name="Pidgey",
                normalized_card_name="pidgey",
                set_name="Obsidian Flames",
                set_code="sv3",
                collector_number="162/197",
            ),
        ]

    def test_shadow_mode_no_live_prices_on_rows(self) -> None:
        html = FIXTURE.read_text(encoding="utf-8")
        catalogue = self._catalogue()
        with mock.patch.dict(
            os.environ,
            {
                "EBAY_SCALE_ENABLED": "true",
                "EBAY_SCALE_SET_SWEEP": "true",
                "EBAY_SCALE_SHADOW_AUDIT": "true",
                "EBAY_SCALE_SET_SWEEP_LIVE": "false",
            },
            clear=False,
        ):
            rows, _ = build_set_sweep_audit_rows(
                sweep_id="hook-shadow",
                set_code="sv3",
                set_name="Obsidian Flames",
                html=html,
                catalogue=catalogue,
                per_card_prices={"charizard-ex": 120.0, "pidgey": 0.5},
                validation_card_ids={"charizard-ex"},
            )
        self.assertTrue(rows)
        for row in rows:
            self.assertFalse(row.live_price_written)

    def test_sweep_counts_against_submission_cap(self) -> None:
        budget = ContinuousSafetyBudget(max_submissions_per_hour=2, max_submissions_per_day=60)
        stamp = datetime.now(timezone.utc)
        budget.submission_events = [
            SubmissionEvent(id=f"cap-{i}", at=stamp, market="AU") for i in range(2)
        ]
        budget._sync_submissions_from_events()
        plan = {"setCode": "sv3", "setName": "Obsidian Flames", "marketCountry": "AU", "consumed": False}
        with mock.patch.dict(
            os.environ,
            {
                "EBAY_SCALE_ENABLED": "true",
                "EBAY_SCALE_SET_SWEEP": "true",
                "EBAY_SCALE_SHADOW_AUDIT": "true",
            },
            clear=False,
        ):
            ok, reason = should_attempt_set_sweep(plan=plan, budget=budget)
        self.assertFalse(ok)
        self.assertEqual(reason, "budget_exhausted")

    def test_bundle_title_rejected(self) -> None:
        row = SoldResultsRow(
            title="Pokemon Obsidian Flames 10 Card Lot Charizard Pidgey Bundle",
            sold_price=40.0,
            postage=0.0,
            sold_date="Sold 1 Nov 2026",
            listing_url="https://www.ebay.com.au/itm/1",
            best_offer=False,
            raw_price_text="A$40.00",
            raw_shipping_text="",
        )
        match = match_sold_row_to_catalogue(row, catalogue=self._catalogue(), index=0)
        self.assertIsNone(match)

    def test_worker_hook_records_audit_with_mock_fetch(self) -> None:
        plan_dir = Path(self._tmp.name)
        plan_path = plan_dir / "plan.json"
        ledger_path = plan_dir / "ledger.json"
        with (
            mock.patch.dict(
                os.environ,
                {
                    "EBAY_SCALE_ENABLED": "true",
                    "EBAY_SCALE_SET_SWEEP": "true",
                    "EBAY_SCALE_SHADOW_AUDIT": "true",
                    "EBAY_SCALE_SET_SWEEP_LIVE": "false",
                    "EBAY_SCALE_AUDIT_DIR": str(plan_dir / "audit"),
                },
                clear=False,
            ),
            mock.patch(
                "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.SET_SWEEP_PLAN_PATH",
                plan_path,
            ),
            mock.patch(
                "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.SET_SWEEP_LAST_RUN_PATH",
                plan_dir / "last.json",
            ),
            mock.patch(
                "cardscanr_market_engine.continuous_safety.BUDGET_LEDGER_PATH",
                ledger_path,
            ),
        ):
            record_set_sweep_plan_from_target(
                target={"set_id": "sv3", "set_name": "Obsidian Flames", "market_country": "AU"},
                source="test",
            )
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            html = FIXTURE.read_text(encoding="utf-8")
            result = try_set_sweep_worker_cycle(
                client=FakeOwnedClient(self._targets()),
                budget=budget,
                fetch_html=lambda _q, _c, _n: html,
                attempt_paths={
                    "ledger": ledger_path,
                    "circuit": plan_dir / "circuit.json",
                    "attempts": plan_dir / "attempts.jsonl",
                },
            )
            budget.reload(ledger_path)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertTrue(result.ok)
            self.assertTrue(result.submission_consumed)
            self.assertEqual(budget.submissions_1h(), 1)
            audit_dir = Path(os.environ["EBAY_SCALE_AUDIT_DIR"])
            self.assertTrue((audit_dir / "set_sweep_audit.jsonl").exists())

    def test_live_skip_only_when_flag_on(self) -> None:
        key = MarketPriceKey(
            id="k1",
            game="pokemon",
            card_name="Pidgey",
            normalized_card_name="pidgey",
            set_name="Obsidian Flames",
            set_code="sv3",
            collector_number="162/197",
            language="en",
            variant="raw",
            condition="raw",
            market_country="AU",
            currency="AUD",
            fingerprint="fp-pidgey",
            raw={"catalogue_card_id": "pidgey"},
        )
        plan_dir = Path(self._tmp.name)
        plan_path = plan_dir / "plan.json"
        plan_path.write_text(
            json.dumps(
                {
                    "setCode": "sv3",
                    "mediansByCatalogueCardId": {"pidgey": 0.75, "fp-pidgey": 0.75},
                    "consumed": True,
                }
            ),
            encoding="utf-8",
        )
        with (
            mock.patch(
                "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.SET_SWEEP_PLAN_PATH",
                plan_path,
            ),
            mock.patch.dict(os.environ, {"EBAY_SCALE_SET_SWEEP_LIVE": "false"}, clear=False),
        ):
            self.assertIsNone(maybe_apply_set_sweep_live_skip(price_key=key, prior_cache={}, job_id="j1"))
        with (
            mock.patch(
                "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.SET_SWEEP_PLAN_PATH",
                plan_path,
            ),
            mock.patch.dict(
                os.environ,
                {
                    "EBAY_SCALE_ENABLED": "true",
                    "EBAY_SCALE_SET_SWEEP": "true",
                    "EBAY_SCALE_SET_SWEEP_LIVE": "true",
                },
                clear=False,
            ),
        ):
            row = maybe_apply_set_sweep_live_skip(price_key=key, prior_cache={}, job_id="j1")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.get("setSweepMedian"), 0.75)

    def test_audit_summary_mispricing_rate(self) -> None:
        rows = [
            {"sweepId": "s1", "setCode": "sv3", "setSweepMedian": 10.0, "perCardPrice": 12.0, "pctDiff": 16.7},
            {"sweepId": "s1", "setCode": "sv3", "setSweepMedian": 50.0, "perCardPrice": 10.0, "pctDiff": 400.0},
        ]
        summary = summarise_sweeps(rows)
        block = summary["perSweep"][0]
        self.assertEqual(block.get("mispricingOver20PctCount"), 1)
        self.assertAlmostEqual(block.get("mispricingRate20Pct"), 0.5)

    def test_build_set_query_text(self) -> None:
        self.assertIn("Obsidian Flames", build_set_level_query_text(set_name="Obsidian Flames", set_code="sv3"))


class BrowserRecycleNavGuardTests(unittest.TestCase):
    def test_recycle_stale_prior_allows_query_correlated_continuation(self) -> None:
        targets = json.loads(
            (ROOT / "tests" / "fixtures" / "pricing_nav_target_fix" / "drifloon_continuation_targets.json").read_text(
                encoding="utf-8"
            )
        )["rawTargets"]
        prior = _stale_charizard_prior()
        file_payload = {NAV_BROWSER_RECYCLE_FIELD: 0}
        ctx = load_navigation_runtime_context()
        ctx.runtime_mode = RUNTIME_INTER_CARD
        ctx.expected_prior = prior
        ctx = reconcile_navigation_context_for_browser_recycle(ctx, file_payload=file_payload)
        with mock.patch(
            "cardscanr_market_engine.navigation_runtime_context._browser_session_recycle_events",
            return_value=2,
        ):
            ctx = reconcile_navigation_context_for_browser_recycle(ctx, file_payload=file_payload)
        self.assertTrue(ctx.prior_handoff_stale)
        self.assertIsNotNone(ctx.expected_prior)
        assert ctx.expected_prior is not None
        self.assertIsNone(ctx.expected_prior.target_id)
        result = evaluate_runtime_targets(
            targets,
            mode=RUNTIME_INTER_CARD,
            prior=ctx.expected_prior,
            current_query=DRIFLOON_QUERY,
            prior_handoff_stale=ctx.prior_handoff_stale,
        )
        self.assertTrue(result.ok, result.reason_codes)

    def test_invalidate_after_note_browser_recycle(self) -> None:
        ebp.reset_timeout_instrumentation_counters()
        prior = PriorCardContext(
            job_id="j1",
            target_id="CDP-OLD",
            final_url="https://www.ebay.com.au/sch/i.html?_nkw=Gyarados",
            query="Gyarados",
            x11_sold_state_verified=True,
            card_verdict="PASS",
        )
        ctx = load_navigation_runtime_context()
        ctx.runtime_mode = RUNTIME_INTER_CARD
        ctx.expected_prior = prior
        with tempfile.TemporaryDirectory() as tmp:
            nav_path = Path(tmp) / "nav.json"
            os.environ["CARDSCANR_NAV_CONTEXT_JSON_PATH"] = str(nav_path)
            apply_context_to_environ(ctx)
            ebp.note_browser_session_recycle()
            reloaded = load_navigation_runtime_context()
            self.assertTrue(reloaded.prior_handoff_stale or (reloaded.expected_prior and not reloaded.expected_prior.target_id))


if __name__ == "__main__":
    unittest.main()