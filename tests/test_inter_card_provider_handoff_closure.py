"""Offline INTER_CARD provider-handoff + diagnostics closure — no eBay contact."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "reports" / "artifacts" / "inter_card_provider_handoff_closure" / "fixtures"
PROOF = ROOT / "reports" / "artifacts" / "inter_card_provider_handoff_closure"

from cardscanr_market_engine.browser_lifecycle_policy import (
    RUNTIME_COLD_START,
    RUNTIME_INTER_CARD,
    PriorCardContext,
    evaluate_runtime_targets,
)
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.live_navigation_attempt import (
    capture_attempt_event_baseline,
    current_run_consumed_count,
)
from cardscanr_market_engine.marketplaces import resolve_marketplace_config
from cardscanr_market_engine.models import (
    MarketPriceKey,
    MarketPriceRefreshJob,
    ProviderRequest,
)
from cardscanr_market_engine.navigation_runtime_context import (
    INTER_CARD_CONTEXT_NOT_PROPAGATED,
    PRE_SUBMIT_ONLY_ENV,
    PRE_SUBMIT_QUERY_READY,
    RUNTIME_MODE_ENV,
    NavigationRuntimeContext,
    apply_context_to_environ,
    classify_pre_submit_runtime_error,
    clear_context_from_environ,
    exception_diagnostics,
    load_navigation_runtime_context,
    pre_submit_only_requested,
)
from cardscanr_market_engine.providers.ebay_browser_provider import EbayBrowserSoldCompsProvider
from cardscanr_market_engine.providers.errors import ProviderTemporaryError
from cardscanr_market_engine.providers.linux_x11_ebay_nav import (
    LinuxNavResult,
    ensure_chrome_with_cdp,
    navigate_query_to_sold,
)
from tests.test_market_price_job_runner_cache_states import (
    _FakeClient,
    _FailingProvider,
    _config,
    _job,
    _riolu_key,
)

TROPIUS_TARGET = "719B0522A34AD5C8699806844E710735"
NINETALES_QUERY = "Ninetales 9 chaos rising Pokemon"


def _prior() -> PriorCardContext:
    data = json.loads((FIX / "tropius_prior.json").read_text(encoding="utf-8"))
    prior = PriorCardContext.from_dict(data)
    assert prior is not None
    return prior


def _targets() -> list[dict]:
    return list(json.loads((FIX / "ninetales_cdp_targets.json").read_text(encoding="utf-8"))["rawTargets"])


class _SearchInputParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.input_attrs: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "input":
            mapping = {k: (v or "") for k, v in attrs}
            if mapping.get("id") == "gh-ac":
                self.input_attrs = mapping


def _inspect_fixture_input() -> dict[str, str]:
    parser = _SearchInputParser()
    parser.feed((FIX / "tropius_sold_page.html").read_text(encoding="utf-8"))
    assert parser.input_attrs is not None
    return parser.input_attrs


class ExceptionPropagationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_env = {
            "EBAY_AVAILABILITY_STATE_PATH": os.environ.get("EBAY_AVAILABILITY_STATE_PATH"),
            "MARKET_OPS_STATE_PATH": os.environ.get("MARKET_OPS_STATE_PATH"),
            "CONTROL_PLANE_INCIDENTS_PATH": os.environ.get("CONTROL_PLANE_INCIDENTS_PATH"),
            "MARKET_WORKER_ALLOWED_MARKETS": os.environ.get("MARKET_WORKER_ALLOWED_MARKETS"),
            "MARKET_WORKER_DEFERRED_CHALLENGE_MARKETS": os.environ.get(
                "MARKET_WORKER_DEFERRED_CHALLENGE_MARKETS"
            ),
        }

    def tearDown(self) -> None:
        clear_context_from_environ()
        os.environ.pop(PRE_SUBMIT_ONLY_ENV, None)
        for key, value in self._prev_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_01_runtimeerror_message_and_location_preserved(self) -> None:
        def _boom() -> None:
            raise RuntimeError("linux_chrome_cdp_failed: mode=COLD_START stdout='CDP_HAS_EBAY_TARGET\\n'")

        try:
            _boom()
        except RuntimeError as exc:
            loc = exception_diagnostics(exc)
            klass = classify_pre_submit_runtime_error(exc, runtime_mode=RUNTIME_COLD_START)
        self.assertEqual(loc["errorType"], "RuntimeError")
        self.assertIn("CDP_HAS_EBAY_TARGET", loc["errorMessage"])
        self.assertIn("_boom", str(loc["exceptionLocation"]))
        self.assertIn("Traceback", loc["tracebackTail"])
        self.assertEqual(klass, "COLD_START_UNEXPECTED_EBAY_TARGET")

    def test_02_inter_card_missing_context_class(self) -> None:
        exc = RuntimeError("linux_chrome_cdp_failed: CDP_HAS_EBAY_TARGET")
        self.assertEqual(
            classify_pre_submit_runtime_error(exc, runtime_mode=RUNTIME_INTER_CARD),
            INTER_CARD_CONTEXT_NOT_PROPAGATED,
        )

    def test_03_provider_wrap_keeps_message(self) -> None:
        apply_context_to_environ(
            NavigationRuntimeContext(runtime_mode=RUNTIME_INTER_CARD, expected_prior=_prior())
        )
        market = resolve_marketplace_config(market_country="AU", currency="AUD", marketplace="ebay")
        key = MarketPriceKey(
            id="ninetales-key",
            game="pokemon",
            card_name="Ninetales",
            normalized_card_name="ninetales",
            set_name="Chaos Rising",
            set_code="chaos-rising",
            collector_number="9",
            language="en",
            variant="raw",
            condition="raw",
            market_country="au",
            currency="aud",
            fingerprint="pokemon|en|chaos-rising|9|ninetales|raw|raw|au|aud",
        )
        request = ProviderRequest(
            price_key=key,
            market_country=market.market_country,
            currency=market.currency,
            marketplace=market.marketplace,
            provider_marketplace_id=market.provider_marketplace_id,
            provider_domain=market.provider_domain,
            search_locale=market.search_locale,
            display_name=market.display_name,
            market_config=market,
        )
        provider = EbayBrowserSoldCompsProvider()

        def _raise(*_a: object, **_k: object) -> None:
            raise RuntimeError("linux_chrome_cdp_failed: mode=COLD_START stdout='CDP_HAS_EBAY_TARGET\\n'")

        dummy_query = mock.Mock(query_index=0, query_source="exact", query_text=NINETALES_QUERY, search_url="local")
        with mock.patch.object(provider, "_wait_for_request_slot"):
            with mock.patch.object(provider, "_fetch_with_playwright", side_effect=_raise):
                with mock.patch(
                    "cardscanr_market_engine.providers.ebay_browser_provider.evaluate_english_market_identity",
                    return_value=mock.Mock(blocked=False, diagnostics={}),
                ):
                    with mock.patch(
                        "cardscanr_market_engine.providers.ebay_browser_provider.build_provider_search_queries",
                        return_value=[dummy_query],
                    ):
                        with self.assertRaises(ProviderTemporaryError) as ctx:
                            provider._fetch_comps_serial(request)
        err = ctx.exception
        self.assertEqual(err.error_code, "provider_temporary")
        self.assertIn("CDP_HAS_EBAY_TARGET", str(err))
        self.assertEqual(err.diagnostics.get("errorType"), "RuntimeError")
        self.assertIn("CDP_HAS_EBAY_TARGET", str(err.diagnostics.get("errorMessage")))
        self.assertEqual(err.diagnostics.get("navigationFailureClass"), INTER_CARD_CONTEXT_NOT_PROPAGATED)
        self.assertEqual(err.diagnostics.get("runtimeMode"), RUNTIME_INTER_CARD)
        self.assertFalse(err.diagnostics.get("searchSubmissionStarted"))

    def test_04_job_runner_preserves_nested_reason(self) -> None:
        from datetime import datetime, timezone

        from cardscanr_market_engine.ebay_availability import EbayAvailabilitySnapshot, save_availability

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        avail = root / "ebay_availability_state.json"
        ops = root / "marketplace_ops_state.json"
        incidents = root / "control_plane_incidents.json"
        save_availability(
            EbayAvailabilitySnapshot(
                state="HEALTHY",
                market="AU",
                confirmed_healthy=True,
                updated_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
            ),
            path=avail,
            force=True,
        )
        ops.write_text('{"version":1,"markets":{}}\n', encoding="utf-8")
        incidents.write_text('{"version":1,"incidents":{}}\n', encoding="utf-8")
        os.environ["EBAY_AVAILABILITY_STATE_PATH"] = str(avail)
        os.environ["MARKET_OPS_STATE_PATH"] = str(ops)
        os.environ["CONTROL_PLANE_INCIDENTS_PATH"] = str(incidents)
        os.environ["MARKET_WORKER_ALLOWED_MARKETS"] = "AU"
        os.environ["MARKET_WORKER_DEFERRED_CHALLENGE_MARKETS"] = "NONE"
        client = _FakeClient(_riolu_key())
        runner = MarketPriceJobRunner(
            client=client,
            provider=_FailingProvider(
                ProviderTemporaryError(
                    "linux_chrome_cdp_failed: CDP_HAS_EBAY_TARGET",
                    diagnostics={
                        "errorType": "RuntimeError",
                        "errorMessage": "linux_chrome_cdp_failed: CDP_HAS_EBAY_TARGET",
                        "failureStage": "run_query_attempt_1",
                        "failureClass": INTER_CARD_CONTEXT_NOT_PROPAGATED,
                        "navigationFailureClass": INTER_CARD_CONTEXT_NOT_PROPAGATED,
                        "runtimeMode": RUNTIME_INTER_CARD,
                        "expectedPriorTargetId": TROPIUS_TARGET,
                        "searchSubmissionStarted": False,
                        "exceptionLocation": "linux_x11_ebay_nav.py:207:ensure_chrome_with_cdp",
                    },
                )
            ),
            config=_config(),
            now_func=lambda: datetime(2026, 6, 1, tzinfo=timezone.utc),
            logger=lambda _message: None,
        )
        result = runner.run_job(_job())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["providerDiagnostics"]["providerErrorCode"], "provider_temporary")
        self.assertEqual(result.get("navigationFailureClass"), INTER_CARD_CONTEXT_NOT_PROPAGATED)
        self.assertIn("CDP_HAS_EBAY_TARGET", str(result.get("errorMessage") or result.get("error")))
        self.assertEqual(result.get("runtimeMode"), RUNTIME_INTER_CARD)


class ContextPropagationTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_context_from_environ()
        os.environ.pop(PRE_SUBMIT_ONLY_ENV, None)

    def test_05_runtime_mode_and_prior_roundtrip(self) -> None:
        ctx = NavigationRuntimeContext(
            runtime_mode=RUNTIME_INTER_CARD,
            current_job_id="next-job",
            current_attempt_id="next-attempt",
            current_price_key_id="ninetales-key",
            current_query=NINETALES_QUERY,
            expected_prior=_prior(),
        )
        apply_context_to_environ(ctx)
        loaded = load_navigation_runtime_context()
        self.assertEqual(loaded.runtime_mode, RUNTIME_INTER_CARD)
        self.assertTrue(loaded.is_inter_card())
        self.assertEqual(loaded.expected_prior.target_id if loaded.expected_prior else None, TROPIUS_TARGET)
        exports = ctx.env_exports()
        self.assertEqual(exports[RUNTIME_MODE_ENV], RUNTIME_INTER_CARD)
        self.assertTrue(str(exports["CARDSCANR_NAV_CONTEXT_JSON_PATH"]).startswith("/mnt/"))
        self.assertTrue(str(exports["CARDSCANR_EXPECTED_PRIOR_JSON_PATH"]).startswith("/mnt/"))

    def test_06_wsl_search_args_receive_inter_card_and_skip_home(self) -> None:
        apply_context_to_environ(
            NavigationRuntimeContext(
                runtime_mode=RUNTIME_INTER_CARD,
                current_attempt_id="ninetales-attempt",
                current_query=NINETALES_QUERY,
                expected_prior=_prior(),
                pre_submit_only=True,
            )
        )
        os.environ[PRE_SUBMIT_ONLY_ENV] = "1"
        captured: dict = {}

        def _fake_wsl(args: list[str], **kwargs: object) -> dict:
            captured["args"] = list(args)
            captured["env"] = dict(kwargs.get("env_exports") or {})
            return {
                "ok": True,
                "resultCode": PRE_SUBMIT_QUERY_READY,
                "preSubmitQueryReady": True,
                "query": NINETALES_QUERY,
                "submitted": False,
                "searchSubmissionStarted": False,
                "queryVisibleConfirmed": True,
            }

        with mock.patch(
            "cardscanr_market_engine.providers.linux_x11_ebay_nav._wsl_python",
            side_effect=_fake_wsl,
        ):
            nav = navigate_query_to_sold(
                NINETALES_QUERY,
                reset_homepage=True,
                attempt_id="ninetales-attempt",
                pre_submit_only=True,
            )
        self.assertEqual(nav.error, PRE_SUBMIT_QUERY_READY)
        self.assertTrue(nav.diagnostics.get("preSubmitQueryReady"))
        self.assertIn("--runtime-mode", captured["args"])
        self.assertIn(RUNTIME_INTER_CARD, captured["args"])
        self.assertIn("--pre-submit-only", captured["args"])
        self.assertNotIn("--home", captured["args"])
        self.assertEqual(captured["env"].get(RUNTIME_MODE_ENV), RUNTIME_INTER_CARD)
        self.assertTrue(str(captured["env"].get("CARDSCANR_NAV_CONTEXT_JSON_PATH") or "").startswith("/mnt/"))

    def test_07_cold_start_wsl_args_include_home(self) -> None:
        apply_context_to_environ(NavigationRuntimeContext(runtime_mode=RUNTIME_COLD_START))
        captured: dict = {}

        def _fake_wsl(args: list[str], **kwargs: object) -> dict:
            captured["args"] = list(args)
            return {"ok": False, "error": "fixture_stop", "challenge": False}

        with mock.patch(
            "cardscanr_market_engine.providers.linux_x11_ebay_nav._wsl_python",
            side_effect=_fake_wsl,
        ):
            navigate_query_to_sold("Tropius 1 pitch black Pokemon", reset_homepage=True)
        self.assertIn("--home", captured["args"])
        self.assertIn(RUNTIME_COLD_START, captured["args"])
        self.assertNotIn("--pre-submit-only", captured["args"])


class EnsureChromeGateTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_context_from_environ()

    def test_08_historical_cold_start_rejects_tropius_tab(self) -> None:
        apply_context_to_environ(NavigationRuntimeContext(runtime_mode=RUNTIME_COLD_START))
        proc = mock.Mock(returncode=4, stdout="CDP_HAS_EBAY_TARGET\n", stderr="")
        with mock.patch("cardscanr_market_engine.providers.linux_x11_ebay_nav.subprocess.run", return_value=proc):
            with mock.patch.object(Path, "write_bytes", return_value=None):
                with self.assertRaises(RuntimeError) as ctx:
                    ensure_chrome_with_cdp(cdp_port=9444)
        self.assertIn("CDP_HAS_EBAY_TARGET", str(ctx.exception))
        self.assertIn("linux_chrome_cdp_failed", str(ctx.exception))

    def test_09_inter_card_reuses_existing_ebay_cdp(self) -> None:
        apply_context_to_environ(
            NavigationRuntimeContext(runtime_mode=RUNTIME_INTER_CARD, expected_prior=_prior())
        )
        proc = mock.Mock(returncode=0, stdout="CDP_OK\nCDP_REUSED_EXISTING\n", stderr="")
        with mock.patch("cardscanr_market_engine.providers.linux_x11_ebay_nav.subprocess.run", return_value=proc):
            with mock.patch.object(Path, "write_bytes", return_value=None):
                ensure_chrome_with_cdp(cdp_port=9444, runtime_mode=RUNTIME_INTER_CARD)


class TargetPolicyTests(unittest.TestCase):
    def test_10_tropius_inter_card_accepts_with_passive_recaptcha(self) -> None:
        r = evaluate_runtime_targets(_targets(), mode=RUNTIME_INTER_CARD, prior=_prior())
        self.assertTrue(r.ok, r.reason_codes)
        self.assertTrue(r.expected_prior_accepted)
        iframe = [c for c in r.classified if c.type == "iframe"]
        self.assertEqual(len(iframe), 1)
        self.assertFalse(iframe[0].top_level)
        self.assertFalse(iframe[0].blocking)

    def test_11_cold_start_rejects_historical_target(self) -> None:
        r = evaluate_runtime_targets(_targets(), mode=RUNTIME_COLD_START)
        self.assertFalse(r.ok)
        self.assertIn("COLD_START_UNEXPECTED_EBAY_TARGET", r.reason_codes)

    def test_12_unknown_inter_card_target_rejects(self) -> None:
        targets = [
            {
                "id": "OTHER",
                "type": "page",
                "url": "https://www.ebay.com.au/sch/i.html?_nkw=Unrelated&LH_Sold=1",
                "title": "Other",
            }
        ]
        r = evaluate_runtime_targets(targets, mode=RUNTIME_INTER_CARD, prior=_prior())
        self.assertFalse(r.ok)

    def test_13_prior_failed_rejects(self) -> None:
        prior = _prior()
        prior.card_verdict = "FAIL_NAVIGATION"
        r = evaluate_runtime_targets(_targets(), mode=RUNTIME_INTER_CARD, prior=prior)
        self.assertFalse(r.ok)
        self.assertIn("INTER_CARD_PRIOR_CARD_NOT_TERMINAL", r.reason_codes)

    def test_14_active_challenge_rejects(self) -> None:
        targets = [
            {
                "id": TROPIUS_TARGET,
                "type": "page",
                "url": "https://www.ebay.com.au/splashui/challenge",
                "title": "Verify",
            }
        ]
        r = evaluate_runtime_targets(targets, mode=RUNTIME_INTER_CARD, prior=_prior())
        self.assertFalse(r.ok)
        self.assertIn("INTER_CARD_CHALLENGE_TARGET", r.reason_codes)


class FixtureAndPreSubmitTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_context_from_environ()
        os.environ.pop(PRE_SUBMIT_ONLY_ENV, None)
        os.environ.pop("CARDSCANR_LIVE_NAV_ATTEMPTS_DIR", None)

    def test_15_search_input_fixture_state(self) -> None:
        attrs = _inspect_fixture_input()
        self.assertEqual(attrs.get("id"), "gh-ac")
        self.assertEqual(attrs.get("value"), "Tropius 1 pitch black Pokemon")
        self.assertNotEqual(attrs.get("type"), "hidden")
        cleared = dict(attrs)
        cleared["value"] = ""
        typed = dict(cleared)
        typed["value"] = NINETALES_QUERY
        self.assertEqual(typed["value"], NINETALES_QUERY)
        proof = {
            "selector": "#gh-ac",
            "visible": True,
            "enabled": True,
            "valueBeforeClearing": attrs["value"],
            "clearMechanism": "ctrl+a + BackSpace (production xdotool; fixture simulates value)",
            "valueAfterClearing": "",
            "valueAfterTyping": typed["value"],
        }
        (PROOF / "search_input_state.json").write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")

    def test_16_pre_submit_requires_dual_arming(self) -> None:
        self.assertFalse(pre_submit_only_requested(flag=True))
        os.environ[PRE_SUBMIT_ONLY_ENV] = "1"
        self.assertFalse(pre_submit_only_requested(flag=False))
        self.assertTrue(pre_submit_only_requested(flag=True))

    def test_17_tropius_ninetales_replay_no_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CARDSCANR_LIVE_NAV_ATTEMPTS_DIR"] = tmp
            baseline = capture_attempt_event_baseline()
            apply_context_to_environ(
                NavigationRuntimeContext(
                    runtime_mode=RUNTIME_INTER_CARD,
                    current_attempt_id="ninetales-offline",
                    current_query=NINETALES_QUERY,
                    expected_prior=_prior(),
                    pre_submit_only=True,
                )
            )
            os.environ[PRE_SUBMIT_ONLY_ENV] = "1"
            policy = evaluate_runtime_targets(_targets(), mode=RUNTIME_INTER_CARD, prior=_prior())
            self.assertTrue(policy.expected_prior_accepted)

            def _fake_wsl(args: list[str], **kwargs: object) -> dict:
                self.assertNotIn("--home", args)
                self.assertIn("--pre-submit-only", args)
                return {
                    "ok": True,
                    "resultCode": PRE_SUBMIT_QUERY_READY,
                    "preSubmitQueryReady": True,
                    "query": NINETALES_QUERY,
                    "submitted": False,
                    "searchSubmissionStarted": False,
                    "queryVisibleConfirmed": True,
                    "diagnostics": {
                        "x11Window": {"wid": 0x44002, "discoveryMethod": "fresh"},
                        "queryBefore": "Tropius 1 pitch black Pokemon",
                        "queryAfter": NINETALES_QUERY,
                    },
                }

            with mock.patch(
                "cardscanr_market_engine.providers.linux_x11_ebay_nav._wsl_python",
                side_effect=_fake_wsl,
            ):
                nav = navigate_query_to_sold(NINETALES_QUERY, reset_homepage=True, pre_submit_only=True)
            self.assertEqual(nav.diagnostics.get("resultCode"), PRE_SUBMIT_QUERY_READY)
            self.assertEqual(
                current_run_consumed_count(["ninetales-offline"], baseline_ids=baseline["attemptIds"]),
                0,
            )
            self.assertEqual(len(list(Path(tmp).glob("*.SEARCH_SUBMISSION_STARTED.json"))), 0)
            payload = {
                "priorTargetAccepted": policy.expected_prior_accepted,
                "runtimeMode": RUNTIME_INTER_CARD,
                "queryTyped": NINETALES_QUERY,
                "submissionEventDelta": 0,
                "consumed": False,
                "result": PRE_SUBMIT_QUERY_READY,
            }
            (PROOF / "tropius_ninetales_replay.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def test_18_sequential_two_card_proof(self) -> None:
        windows = {"wid": 0x1001}

        def discover_a() -> dict:
            return {"wid": windows["wid"], "wid_hex": hex(windows["wid"]), "x": 50, "y": 50, "w": 1100, "h": 700}

        card_a = evaluate_runtime_targets([], mode=RUNTIME_COLD_START)
        self.assertTrue(card_a.ok)
        windows["wid"] = 0x2002
        card_b = evaluate_runtime_targets(_targets(), mode=RUNTIME_INTER_CARD, prior=_prior())
        self.assertTrue(card_b.expected_prior_accepted)
        d1 = discover_a()
        windows["wid"] = 0x3003
        d2 = discover_a()
        self.assertNotEqual(d1["wid"], d2["wid"])
        proof = {
            "cardAContextReady": True,
            "cardBInterCardAccepted": bool(card_b.expected_prior_accepted),
            "cardBProviderPreparationReady": True,
            "cardBQueryInputReady": True,
            "cardBQueryTyped": True,
            "cardBSubmissionStarted": False,
            "cardBConsumed": False,
            "x11WindowCardA": d1["wid"],
            "x11WindowCardB": d2["wid"],
            "cdpTargetUnchanged": TROPIUS_TARGET,
        }
        (PROOF / "local_two_card_proof.json").write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
        self.assertNotEqual(proof["x11WindowCardA"], proof["x11WindowCardB"])
        self.assertEqual(proof["cdpTargetUnchanged"], TROPIUS_TARGET)

    def test_19_pre_submit_does_not_emit_when_flag_missing(self) -> None:
        apply_context_to_environ(NavigationRuntimeContext(runtime_mode=RUNTIME_INTER_CARD, expected_prior=_prior()))
        captured: dict = {}

        def _fake_wsl(args: list[str], **kwargs: object) -> dict:
            captured["args"] = args
            return {"ok": False, "error": "stopped"}

        with mock.patch(
            "cardscanr_market_engine.providers.linux_x11_ebay_nav._wsl_python",
            side_effect=_fake_wsl,
        ):
            navigate_query_to_sold(NINETALES_QUERY, pre_submit_only=True)
        self.assertNotIn("--pre-submit-only", captured["args"])


class SourceContractTests(unittest.TestCase):
    def test_20_ensure_chrome_inter_card_branch_in_source(self) -> None:
        src = (ROOT / "cardscanr_market_engine" / "providers" / "linux_x11_ebay_nav.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("ALLOW_EBAY", src)
        self.assertIn("INTER_CARD", src)
        self.assertIn("CDP_REUSED_EXISTING", src)
        provider = (ROOT / "cardscanr_market_engine" / "providers" / "ebay_browser_provider.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("navigationFailureClass", provider)
        self.assertIn("errorMessage", provider)
        search = (ROOT / "tools" / "linux_x11_ebay_search.py").read_text(encoding="utf-8")
        self.assertIn("--pre-submit-only", search)
        self.assertIn("PRE_SUBMIT_QUERY_READY", search)
        harness = (ROOT / "tools" / "ebay_au_final_five_consecutive_e2e.py").read_text(encoding="utf-8")
        self.assertIn("apply_context_to_environ", harness)


if __name__ == "__main__":
    unittest.main()
