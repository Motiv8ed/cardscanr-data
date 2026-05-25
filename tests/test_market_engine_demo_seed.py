"""Unit tests for Phase 4A demo seed script.

Tests cover:
- demo card definitions are valid
- market list parsing
- expected domain/currency assertions
- dry-run does not enqueue/process
- report redaction
- repeated seed planning is stable
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.seed_market_price_demo_data import (
    ALL_CARDS,
    CLASSIC_CARDS,
    DEMO_REPORT_LATEST,
    DEMO_REPORT_RUNS,
    MARKET_CONFIG,
    SMOKE_CARDS,
    DemoSeedPlan,
    DemoSeedRunner,
    _CARD_DEFAULTS,
    build_demo_fingerprint,
    parse_card_filter,
    parse_markets,
)
from cardscanr_market_engine.smoke_utils import sanitize_for_report, REDACTED


# ---------------------------------------------------------------------------
# Demo card definition tests
# ---------------------------------------------------------------------------


class DemoCardDefinitionTests(unittest.TestCase):
    def test_smoke_cards_are_non_empty(self) -> None:
        self.assertTrue(len(SMOKE_CARDS) >= 1)

    def test_classic_cards_are_non_empty(self) -> None:
        self.assertTrue(len(CLASSIC_CARDS) >= 1)

    def test_all_cards_union_of_smoke_and_classic(self) -> None:
        self.assertEqual(len(ALL_CARDS), len(SMOKE_CARDS) + len(CLASSIC_CARDS))

    def test_each_card_has_required_fields(self) -> None:
        required = {"label", "card_name", "set_name", "set_code", "collector_number"}
        for card in ALL_CARDS:
            missing = required - set(card.keys())
            self.assertFalse(
                missing,
                f"Card {card.get('card_name', '?')} missing fields: {missing}",
            )

    def test_card_fields_are_non_empty_strings(self) -> None:
        for card in ALL_CARDS:
            for field in ("card_name", "set_name", "set_code", "collector_number"):
                self.assertIsInstance(card[field], str)
                self.assertTrue(
                    card[field].strip(),
                    f"Card field '{field}' is empty for {card.get('card_name', '?')}",
                )

    def test_classic_cards_use_demo_label_prefix(self) -> None:
        for card in CLASSIC_CARDS:
            self.assertTrue(
                card["card_name"].startswith("[DEMO]"),
                f"Classic card should start with [DEMO]: {card['card_name']}",
            )

    def test_smoke_card_label_is_smoke(self) -> None:
        for card in SMOKE_CARDS:
            self.assertEqual(card["label"], "smoke")

    def test_shared_defaults_contain_required_keys(self) -> None:
        for key in ("game", "language", "variant", "condition"):
            self.assertIn(key, _CARD_DEFAULTS)
            self.assertTrue(_CARD_DEFAULTS[key].strip())


# ---------------------------------------------------------------------------
# Market list parsing tests
# ---------------------------------------------------------------------------


class MarketParsingTests(unittest.TestCase):
    def test_parse_all_four_supported_markets(self) -> None:
        result = parse_markets("AU,US,GB,CA")
        self.assertEqual(result, ["AU", "US", "GB", "CA"])

    def test_parse_markets_lowercased_input(self) -> None:
        result = parse_markets("au,us")
        self.assertEqual(result, ["AU", "US"])

    def test_parse_markets_with_spaces(self) -> None:
        result = parse_markets(" AU , GB ")
        self.assertEqual(result, ["AU", "GB"])

    def test_parse_markets_single(self) -> None:
        result = parse_markets("CA")
        self.assertEqual(result, ["CA"])

    def test_parse_markets_raises_on_unknown_code(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_markets("AU,NZ,ZZ")
        self.assertIn("NZ", str(ctx.exception))
        self.assertIn("ZZ", str(ctx.exception))

    def test_parse_markets_raises_on_empty_string(self) -> None:
        with self.assertRaises(ValueError):
            parse_markets("  ")

    def test_parse_card_filter_smoke(self) -> None:
        result = parse_card_filter("smoke")
        self.assertEqual(result, SMOKE_CARDS)

    def test_parse_card_filter_classic(self) -> None:
        result = parse_card_filter("classic")
        self.assertEqual(result, CLASSIC_CARDS)

    def test_parse_card_filter_all(self) -> None:
        result = parse_card_filter("all")
        self.assertEqual(result, ALL_CARDS)

    def test_parse_card_filter_combined(self) -> None:
        result = parse_card_filter("smoke,classic")
        self.assertEqual(result, ALL_CARDS)

    def test_parse_card_filter_raises_on_unknown(self) -> None:
        with self.assertRaises(ValueError):
            parse_card_filter("unknown_set")


# ---------------------------------------------------------------------------
# Expected domain / currency assertion tests
# ---------------------------------------------------------------------------


class DomainCurrencyTests(unittest.TestCase):
    def test_market_config_has_all_four_markets(self) -> None:
        for code in ("AU", "US", "GB", "CA"):
            self.assertIn(code, MARKET_CONFIG)

    def test_au_market_config(self) -> None:
        currency, domain = MARKET_CONFIG["AU"]
        self.assertEqual(currency, "AUD")
        self.assertEqual(domain, "ebay.com.au")

    def test_us_market_config(self) -> None:
        currency, domain = MARKET_CONFIG["US"]
        self.assertEqual(currency, "USD")
        self.assertEqual(domain, "ebay.com")

    def test_gb_market_config(self) -> None:
        currency, domain = MARKET_CONFIG["GB"]
        self.assertEqual(currency, "GBP")
        self.assertEqual(domain, "ebay.co.uk")

    def test_ca_market_config(self) -> None:
        currency, domain = MARKET_CONFIG["CA"]
        self.assertEqual(currency, "CAD")
        self.assertEqual(domain, "ebay.ca")

    def test_fingerprints_differ_across_markets(self) -> None:
        card = SMOKE_CARDS[0]
        fps = [
            build_demo_fingerprint(card, market, MARKET_CONFIG[market][0])
            for market in ("AU", "US", "GB", "CA")
        ]
        self.assertEqual(len(fps), len(set(fps)), "Fingerprints must be unique per market")

    def test_fingerprints_differ_across_classic_cards_same_market(self) -> None:
        fps = [
            build_demo_fingerprint(card, "AU", "AUD")
            for card in CLASSIC_CARDS
        ]
        self.assertEqual(len(fps), len(set(fps)), "Fingerprints must be unique per card")

    def test_fingerprint_encodes_market_country_and_currency(self) -> None:
        card = SMOKE_CARDS[0]
        fp_au = build_demo_fingerprint(card, "AU", "AUD")
        fp_us = build_demo_fingerprint(card, "US", "USD")
        self.assertIn("au", fp_au)
        self.assertIn("aud", fp_au)
        self.assertIn("us", fp_us)
        self.assertIn("usd", fp_us)


# ---------------------------------------------------------------------------
# Seed planning stability tests
# ---------------------------------------------------------------------------


class SeedPlanStabilityTests(unittest.TestCase):
    def test_plan_item_count_matches_cards_times_markets(self) -> None:
        plan = DemoSeedPlan(cards=ALL_CARDS, markets=["AU", "US", "GB", "CA"])
        self.assertEqual(len(plan.items), len(ALL_CARDS) * 4)

    def test_plan_describes_all_items(self) -> None:
        plan = DemoSeedPlan(cards=SMOKE_CARDS, markets=["AU", "US"])
        described = plan.describe()
        self.assertEqual(len(described), len(SMOKE_CARDS) * 2)

    def test_plan_is_stable_on_repeated_calls(self) -> None:
        plan_a = DemoSeedPlan(cards=ALL_CARDS, markets=["AU", "US", "GB", "CA"])
        plan_b = DemoSeedPlan(cards=ALL_CARDS, markets=["AU", "US", "GB", "CA"])
        fps_a = [item["fingerprint"] for item in plan_a.items]
        fps_b = [item["fingerprint"] for item in plan_b.items]
        self.assertEqual(fps_a, fps_b, "Plan fingerprints must be deterministic")

    def test_plan_describe_has_required_keys(self) -> None:
        plan = DemoSeedPlan(cards=SMOKE_CARDS, markets=["AU"])
        for row in plan.describe():
            for key in ("card_name", "market_country", "currency", "expected_domain", "fingerprint"):
                self.assertIn(key, row)

    def test_plan_item_expected_domains_match_market_config(self) -> None:
        plan = DemoSeedPlan(cards=SMOKE_CARDS, markets=["AU", "US", "GB", "CA"])
        for item in plan.items:
            expected = MARKET_CONFIG[item["market_country"]][1]
            self.assertEqual(item["expected_domain"], expected)


# ---------------------------------------------------------------------------
# Dry-run tests
# ---------------------------------------------------------------------------


class _RecordingClient:
    """Fake client that records calls so dry-run tests can verify no calls happen."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_or_create_price_key(self, **kwargs: object) -> str:
        self.calls.append("get_or_create_price_key")
        return "fake-key-id"

    def get_active_jobs_for_keys(self, *, price_key_ids: list[str]) -> dict:
        self.calls.append("get_active_jobs_for_keys")
        return {}

    def enqueue_refresh_job(self, **kwargs: object) -> dict:
        self.calls.append("enqueue_refresh_job")
        return {"id": "fake-job-id", "status": "queued"}

    def get_market_price_bundle(self, **kwargs: object) -> dict | None:
        self.calls.append("get_market_price_bundle")
        return None


class _NoOpJobRunner:
    def run_once(self, *, max_jobs: int | None = None) -> list:
        return []


class DryRunTests(unittest.TestCase):
    def _make_runner(self, *, dry_run: bool) -> tuple[DemoSeedRunner, _RecordingClient]:
        client = _RecordingClient()
        plan = DemoSeedPlan(cards=SMOKE_CARDS, markets=["AU"])
        runner = DemoSeedRunner(
            client=client,  # type: ignore[arg-type]
            job_runner=_NoOpJobRunner(),  # type: ignore[arg-type]
            plan=plan,
            dry_run=dry_run,
            enqueue_only=False,
            process=False,
            max_jobs=10,
        )
        return runner, client

    def test_dry_run_returns_dry_run_status(self) -> None:
        runner, _ = self._make_runner(dry_run=True)
        result = runner.run()
        self.assertEqual(result["status"], "dry_run")
        self.assertTrue(result["dryRun"])

    def test_dry_run_does_not_call_client(self) -> None:
        runner, client = self._make_runner(dry_run=True)
        runner.run()
        self.assertEqual(client.calls, [], "Dry-run must not call the Supabase client")

    def test_dry_run_includes_plan(self) -> None:
        runner, _ = self._make_runner(dry_run=True)
        result = runner.run()
        self.assertIn("plan", result)
        self.assertEqual(result["planItemCount"], len(SMOKE_CARDS) * 1)

    def test_non_dry_run_does_call_client(self) -> None:
        runner, client = self._make_runner(dry_run=False)
        runner.run()
        self.assertIn("get_or_create_price_key", client.calls)
        self.assertIn("enqueue_refresh_job", client.calls)


# ---------------------------------------------------------------------------
# Report redaction tests
# ---------------------------------------------------------------------------


class ReportRedactionTests(unittest.TestCase):
    def test_sensitive_keys_are_redacted(self) -> None:
        payload = {
            "SUPABASE_SERVICE_ROLE_KEY": "supersecret",
            "apikey": "abc123",
            "nested": {"authorization": "Bearer xyz"},
            "safe": "visible",
        }
        sanitized = sanitize_for_report(payload)
        self.assertEqual(sanitized["SUPABASE_SERVICE_ROLE_KEY"], REDACTED)
        self.assertEqual(sanitized["apikey"], REDACTED)
        self.assertEqual(sanitized["nested"]["authorization"], REDACTED)
        self.assertEqual(sanitized["safe"], "visible")

    def test_redaction_is_recursive_in_lists(self) -> None:
        payload = {"steps": [{"apikey": "leak"}, {"ok": "value"}]}
        sanitized = sanitize_for_report(payload)
        self.assertEqual(sanitized["steps"][0]["apikey"], REDACTED)
        self.assertEqual(sanitized["steps"][1]["ok"], "value")

    def test_non_sensitive_report_fields_are_preserved(self) -> None:
        payload = {
            "status": "dry_run",
            "planItemCount": 4,
            "dryRun": True,
            "plan": [{"card_name": "Pikachu", "market_country": "AU"}],
        }
        sanitized = sanitize_for_report(payload)
        self.assertEqual(sanitized["status"], "dry_run")
        self.assertEqual(sanitized["planItemCount"], 4)
        self.assertTrue(sanitized["dryRun"])
        self.assertEqual(sanitized["plan"][0]["card_name"], "Pikachu")


# ---------------------------------------------------------------------------
# Optional integration test (skipped unless env is ready)
# ---------------------------------------------------------------------------


def _has_integration_env() -> bool:
    env = os.environ
    return (
        bool(env.get("SUPABASE_URL", "").strip())
        and bool(env.get("SUPABASE_SERVICE_ROLE_KEY", "").strip())
        and env.get("MARKET_LOOKUP_PROVIDER", "mock").strip().lower() == "mock"
    )


@unittest.skipUnless(_has_integration_env(), "Integration env vars not set (SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, MARKET_LOOKUP_PROVIDER=mock)")
class DemoSeedIntegrationTest(unittest.TestCase):
    """Runs one smoke card × one market through the full seed pipeline.

    Skipped unless SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY + MARKET_LOOKUP_PROVIDER=mock
    are all set in the environment.
    """

    def test_smoke_card_au_market_seed_and_verify(self) -> None:
        from cardscanr_market_engine.config import MarketEngineConfig
        from cardscanr_market_engine.job_runner import MarketPriceJobRunner
        from cardscanr_market_engine.providers import MockMarketCompsProvider
        from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient

        config = MarketEngineConfig.from_env(require_supabase=True)
        self.assertEqual(config.provider_name, "mock")

        client = SupabaseMarketEngineClient(
            supabase_url=config.supabase_url,
            service_role_key=config.supabase_service_role_key,
        )
        job_runner = MarketPriceJobRunner(
            client=client,
            provider=MockMarketCompsProvider(),
            config=config,
        )
        plan = DemoSeedPlan(cards=SMOKE_CARDS, markets=["AU"])
        runner = DemoSeedRunner(
            client=client,
            job_runner=job_runner,
            plan=plan,
            dry_run=False,
            enqueue_only=False,
            process=True,
            max_jobs=5,
        )
        result = runner.run()
        self.assertEqual(result["status"], "success")
        self.assertGreater(result.get("processed", 0) + result.get("skippedActive", 0) + result.get("enqueued", 0), 0)

        verify_results = result.get("verifyResults", [])
        self.assertTrue(len(verify_results) > 0)
        for vr in verify_results:
            self.assertIn(vr.get("status"), ("ok", "partial", "no_bundle"))


if __name__ == "__main__":
    unittest.main()
