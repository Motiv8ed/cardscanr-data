"""Default set-sweep fetch wiring: real EbayBrowserSoldCompsProvider, no fetch_html mock."""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.continuous_safety import ContinuousSafetyBudget
from cardscanr_market_engine.ebay_scale.set_sweep_worker_hook import (
    _default_fetch_set_sweep_html,
    record_set_sweep_plan_from_target,
    try_set_sweep_worker_cycle,
)
from cardscanr_market_engine.providers.linux_x11_ebay_nav import LinuxNavResult
from cardscanr_market_engine.providers.post_sold_capture_process import CaptureProcessResult
from tests.test_au_ebay_variant_resilience_20261120 import HEALTHY_BODY, HEALTHY_TITLE, SOLD_URL

FIXTURE = ROOT / "tests" / "fixtures" / "ebay_scale" / "sv3_set_sweep_fixture.html"


def _healthy_sold_nav() -> LinuxNavResult:
    return LinuxNavResult(
        ok=True,
        query="Obsidian Flames sv3 pokemon",
        search_success=True,
        sold_click_success=True,
        sold_state_verified=True,
        url=SOLD_URL,
        title=HEALTHY_TITLE,
        sorry=False,
        challenge=False,
        sold_date_lines=25,
        error=None,
        diagnostics={"sold": {"body": HEALTHY_BODY}},
    )


def _provider_low_level_patches(artifact_path: Path):
    """Patch only CDP/nav/capture — not hook fetch_html."""
    return (
        mock.patch(
            "cardscanr_market_engine.providers.ebay_browser_provider.EbayBrowserSoldCompsProvider._linux_x11_nav_mode_enabled",
            return_value=True,
        ),
        mock.patch(
            "cardscanr_market_engine.providers.ebay_browser_provider.EbayBrowserSoldCompsProvider._ensure_linux_chrome_cdp_ready",
            return_value=9444,
        ),
        mock.patch(
            "cardscanr_market_engine.providers.linux_x11_ebay_nav.navigate_query_to_sold",
            return_value=_healthy_sold_nav(),
        ),
        mock.patch(
            "cardscanr_market_engine.providers.post_sold_capture.resolve_authoritative_capture_html_path",
            return_value=artifact_path,
        ),
        mock.patch(
            "cardscanr_market_engine.providers.post_sold_capture_process.run_capture_worker_process",
            return_value=CaptureProcessResult(status="SUCCESS", payload={}),
        ),
    )


class FakeOwnedClient:
    def __init__(self, targets: list[dict]) -> None:
        self.targets = targets

    def list_owned_market_pricing_targets(self, **_kwargs) -> dict:
        return {"targets": list(self.targets)}


def _targets() -> list[dict]:
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
    ]


class SetSweepDefaultFetchTests(unittest.TestCase):
    def test_lazy_import_smoke(self) -> None:
        """Every symbol imported inside the hook must resolve (catches renamed providers)."""
        from cardscanr_market_engine.providers import ebay_browser_provider

        importlib.import_module("cardscanr_market_engine.ebay_scale.set_sweep")
        importlib.import_module("cardscanr_market_engine.continuous_safety")
        assert hasattr(ebay_browser_provider, "EbayBrowserSoldCompsProvider")
        _ = ebay_browser_provider.EbayBrowserSoldCompsProvider()

    def test_default_fetch_set_sweep_html_without_fetch_html_mock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "capture.html"
            artifact.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            bind_calls: list[bool] = []
            clear_calls: list[int] = []

            real_bind = (
                __import__(
                    "cardscanr_market_engine.providers.ebay_browser_provider",
                    fromlist=["EbayBrowserSoldCompsProvider"],
                ).EbayBrowserSoldCompsProvider.bind_live_submission_budget
            )
            real_clear = (
                __import__(
                    "cardscanr_market_engine.providers.ebay_browser_provider",
                    fromlist=["EbayBrowserSoldCompsProvider"],
                ).EbayBrowserSoldCompsProvider.clear_live_submission_budget
            )

            def _bind(self, b, *, first_slot_pre_reserved: bool) -> None:
                bind_calls.append(first_slot_pre_reserved)
                real_bind(self, b, first_slot_pre_reserved=first_slot_pre_reserved)

            def _clear(self) -> None:
                clear_calls.append(1)
                real_clear(self)

            with ExitStack() as stack:
                for patcher in _provider_low_level_patches(artifact):
                    stack.enter_context(patcher)
                stack.enter_context(
                    mock.patch(
                        "cardscanr_market_engine.providers.ebay_browser_provider.EbayBrowserSoldCompsProvider.bind_live_submission_budget",
                        _bind,
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "cardscanr_market_engine.providers.ebay_browser_provider.EbayBrowserSoldCompsProvider.clear_live_submission_budget",
                        _clear,
                    )
                )
                html = _default_fetch_set_sweep_html(
                    "Obsidian Flames sv3 pokemon",
                    "sv3",
                    "Obsidian Flames",
                    budget=budget,
                )
            self.assertIn("Charizard", html)
            self.assertEqual(bind_calls, [True])
            self.assertEqual(len(clear_calls), 1)

    def test_try_set_sweep_worker_cycle_default_fetch_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            plan_path = plan_dir / "plan.json"
            ledger_path = plan_dir / "ledger.json"
            artifact = plan_dir / "capture.html"
            artifact.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
            with ExitStack() as stack:
                stack.enter_context(
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
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.SET_SWEEP_PLAN_PATH",
                        plan_path,
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.SET_SWEEP_LAST_RUN_PATH",
                        plan_dir / "last.json",
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "cardscanr_market_engine.continuous_safety.BUDGET_LEDGER_PATH",
                        ledger_path,
                    )
                )
                for patcher in _provider_low_level_patches(artifact):
                    stack.enter_context(patcher)
                record_set_sweep_plan_from_target(
                    target={"set_id": "sv3", "set_name": "Obsidian Flames", "market_country": "AU"},
                    source="test",
                )
                budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
                result = try_set_sweep_worker_cycle(
                    client=FakeOwnedClient(_targets()),
                    budget=budget,
                    fetch_html=None,
                    attempt_paths={
                        "ledger": ledger_path,
                        "circuit": plan_dir / "circuit.json",
                        "attempts": plan_dir / "attempts.jsonl",
                    },
                )
            self.assertIsNotNone(result)
            assert result is not None
            self.assertTrue(result.ok, msg=result.reason)
            self.assertTrue(result.submission_consumed)
            budget.reload(ledger_path)
            self.assertEqual(budget.submissions_1h(), 1)

    def test_default_fetch_reuses_generic_error_page_retry(self) -> None:
        from tests.test_au_ebay_variant_resilience_20261120 import (
            ERROR_TITLE,
            REAL_GENERIC_ERROR_BODY,
        )

        error_nav = LinuxNavResult(
            ok=False,
            query="Obsidian Flames sv3 pokemon",
            search_success=True,
            sold_click_success=False,
            sold_state_verified=False,
            url=SOLD_URL,
            title=ERROR_TITLE,
            sorry=True,
            challenge=False,
            sold_date_lines=0,
            error="ebay_error_page",
            diagnostics={
                "sold": {
                    "title": ERROR_TITLE,
                    "sorry": True,
                    "error": "ebay_error_page",
                    "body": REAL_GENERIC_ERROR_BODY,
                }
            },
        )
        nav_calls: list[int] = []

        def _nav(*_a, **_k):
            nav_calls.append(1)
            return error_nav

        budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "capture.html"
            with (
                mock.patch(
                    "cardscanr_market_engine.providers.ebay_browser_provider.EbayBrowserSoldCompsProvider._linux_x11_nav_mode_enabled",
                    return_value=True,
                ),
                mock.patch(
                    "cardscanr_market_engine.providers.ebay_browser_provider.EbayBrowserSoldCompsProvider._ensure_linux_chrome_cdp_ready",
                    return_value=9444,
                ),
                mock.patch(
                    "cardscanr_market_engine.providers.linux_x11_ebay_nav.navigate_query_to_sold",
                    side_effect=_nav,
                ),
                mock.patch(
                    "cardscanr_market_engine.providers.au_ebay_variant_resilience.generic_error_page_retry_delay_seconds",
                    return_value=0.0,
                ),
                mock.patch(
                    "cardscanr_market_engine.providers.post_sold_capture.resolve_authoritative_capture_html_path",
                    return_value=artifact,
                ),
            ):
                with self.assertRaises(Exception):
                    _default_fetch_set_sweep_html(
                        "Obsidian Flames sv3 pokemon",
                        "sv3",
                        "Obsidian Flames",
                        budget=budget,
                    )
        self.assertEqual(len(nav_calls), 2)

    def test_ebay_scale_lazy_imports_resolve(self) -> None:
        """Grep guard: lazy imports in ebay_scale modules must exist."""
        mods = [
            "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook",
            "cardscanr_market_engine.ebay_scale.set_sweep_safety",
            "cardscanr_market_engine.ebay_scale.cross_user_cache",
        ]
        for name in mods:
            mod = importlib.import_module(name)
            self.assertIsNotNone(mod)


if __name__ == "__main__":
    unittest.main()