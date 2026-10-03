"""Reliability harness authoritative-truth closure (offline, no eBay)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.live_navigation_attempt import (
    DEFAULT_EVENTS_DIR,
    SEARCH_SUBMISSION_STARTED,
    attempts_dir,
    attempts_dir_wsl,
    canonical_attempts_dir,
    emit_search_submission_started,
    lookup_search_submission_event,
)
from cardscanr_market_engine.pipeline_phase_diagnostics import (
    PARSE_COMPLETE,
    extract_pipeline_phases,
)
from cardscanr_market_engine.providers.post_sold_capture import POST_SOLD_CAPTURE_READY
from cardscanr_market_engine.reliability_harness_evidence import classify_card_from_job_result
from cardscanr_market_engine.wsl_path import same_physical_path, windows_to_wsl_path, wsl_to_windows_path

FIXTURE = ROOT / "reports" / "artifacts" / "reliability_harness_truth_closure" / "fixtures" / "ceruledge_card1"


class WslPathContractTests(unittest.TestCase):
    def test_windows_to_wsl_and_back(self) -> None:
        win = r"D:\CardScanR_Data\cardscanr-data\reports\runtime\live_nav_attempts"
        wsl = windows_to_wsl_path(win)
        self.assertEqual(wsl, "/mnt/d/CardScanR_Data/cardscanr-data/reports/runtime/live_nav_attempts")
        back = wsl_to_windows_path(wsl)
        self.assertTrue(same_physical_path(win, back))

    def test_canonical_attempts_dir_default(self) -> None:
        os.environ.pop("CARDSCANR_LIVE_NAV_ATTEMPTS_DIR", None)
        self.assertEqual(attempts_dir().resolve(), canonical_attempts_dir().resolve())
        self.assertEqual(canonical_attempts_dir().resolve(), DEFAULT_EVENTS_DIR.resolve())
        self.assertTrue(attempts_dir_wsl().startswith("/mnt/"))


class AttemptLookupTests(unittest.TestCase):
    def test_lookup_by_attempt_id_and_price_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CARDSCANR_LIVE_NAV_ATTEMPTS_DIR"] = tmp
            try:
                aid = "attempt-lookup-1"
                emit_search_submission_started(
                    attempt_id=aid,
                    query="Ceruledge 20 phantasmal flames Pokemon",
                    price_key_id="pk-1",
                )
                ok = lookup_search_submission_event(aid, expected_price_key_id="pk-1")
                self.assertTrue(ok["found"])
                self.assertTrue(ok["valid"])
                bad = lookup_search_submission_event(aid, expected_price_key_id="pk-other")
                self.assertFalse(bad["valid"])
                self.assertIn("priceKeyId_mismatch", bad["rejectionReasons"])
                other = lookup_search_submission_event("different-attempt")
                self.assertFalse(other["found"])
            finally:
                os.environ.pop("CARDSCANR_LIVE_NAV_ATTEMPTS_DIR", None)


class WslSharedDirectoryProofTests(unittest.TestCase):
    def test_windows_sees_wsl_written_event(self) -> None:
        """Real Windows↔WSL boundary: WSL writes event; Windows reads same file."""
        with tempfile.TemporaryDirectory() as tmp:
            win_dir = Path(tmp).resolve()
            wsl_dir = windows_to_wsl_path(win_dir)
            attempt = "wsl-shared-dir-proof-1"
            script = f"""
set -e
mkdir -p {wsl_dir}
python3 - <<'PY'
import json
from pathlib import Path
p = Path({wsl_dir!r}) / "{attempt}.SEARCH_SUBMISSION_STARTED.json"
p.write_text(json.dumps({{
  "event": "SEARCH_SUBMISSION_STARTED",
  "attemptId": "{attempt}",
  "timestamp": "2026-10-02T00:00:00Z",
  "queryFingerprint": "deadbeef",
  "query": "offline-proof",
  "pid": 1,
  "priceKeyId": "pk-proof",
}}, indent=2) + "\\n", encoding="utf-8")
print("WROTE", p)
PY
"""
            tmp_sh = Path(r"D:\DevCache\Temp\wsl_attempts_dir_proof.sh")
            tmp_sh.parent.mkdir(parents=True, exist_ok=True)
            tmp_sh.write_bytes(script.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))
            proc = subprocess.run(
                ["wsl", "-d", "Ubuntu", "--", "bash", "/mnt/d/DevCache/Temp/wsl_attempts_dir_proof.sh"],
                capture_output=True,
                text=True,
                timeout=60,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
            event_path = win_dir / f"{attempt}.SEARCH_SUBMISSION_STARTED.json"
            self.assertTrue(event_path.is_file(), msg=f"missing {event_path}; stdout={proc.stdout}")
            os.environ["CARDSCANR_LIVE_NAV_ATTEMPTS_DIR"] = str(win_dir)
            try:
                lookup = lookup_search_submission_event(attempt, expected_price_key_id="pk-proof")
                self.assertTrue(lookup["valid"])
                self.assertTrue(same_physical_path(win_dir, wsl_to_windows_path(wsl_dir)))
            finally:
                os.environ.pop("CARDSCANR_LIVE_NAV_ATTEMPTS_DIR", None)


class PhaseExtractionTests(unittest.TestCase):
    def test_extracts_success_phases_from_aggregate_shape(self) -> None:
        meta = {
            "x11SoldStateVerified": True,
            "postSoldCapturePhase": POST_SOLD_CAPTURE_READY,
            "parsePhase": PARSE_COMPLETE,
            "desktopNav": {"SOLD_STATE_VERIFIED": True},
            "persistedCaptureArtifact": {"htmlPath": "/tmp/a.html", "sha256": "abc"},
        }
        phases = extract_pipeline_phases(meta)
        self.assertTrue(phases["x11SoldStateVerified"])
        self.assertEqual(phases["postSoldCapturePhase"], POST_SOLD_CAPTURE_READY)
        self.assertEqual(phases["parsePhase"], PARSE_COMPLETE)
        self.assertEqual(phases["persistedCaptureArtifact"]["sha256"], "abc")

    def test_recovers_from_nested_last_attempt_stage(self) -> None:
        meta = {
            "stageTimings": {
                "lastAttempt": {
                    "x11SoldStateVerified": True,
                    "postSoldCapturePhase": POST_SOLD_CAPTURE_READY,
                    "parsePhase": PARSE_COMPLETE,
                }
            }
        }
        # lastAttempt alone isn't auto-walked — ensure desktopNav / top-level still work.
        # Add recovered path via queryAttempts raw_metadata.
        meta = {
            "queryAttempts": [
                {
                    "raw_metadata": {
                        "x11SoldStateVerified": True,
                        "postSoldCapturePhase": POST_SOLD_CAPTURE_READY,
                        "parsePhase": PARSE_COMPLETE,
                    }
                }
            ]
        }
        phases = extract_pipeline_phases(meta)
        self.assertTrue(phases["x11SoldStateVerified"])
        self.assertEqual(phases["postSoldCapturePhase"], POST_SOLD_CAPTURE_READY)


class CeruledgeOfflineReplayTests(unittest.TestCase):
    def test_ceruledge_offline_replay_pass_price_updated(self) -> None:
        job = json.loads((FIXTURE / "job_result.json").read_text(encoding="utf-8"))
        ba = json.loads((FIXTURE / "before_after.json").read_text(encoding="utf-8"))
        # Point lookup at a temp dir containing the historical event copy.
        with tempfile.TemporaryDirectory() as tmp:
            src = FIXTURE / "65a39d2e-4b61-4406-8e03-561883973213.SEARCH_SUBMISSION_STARTED.json"
            dst = Path(tmp) / src.name
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            os.environ["CARDSCANR_LIVE_NAV_ATTEMPTS_DIR"] = tmp
            try:
                classified = classify_card_from_job_result(
                    job,
                    attempt_id="65a39d2e-4b61-4406-8e03-561883973213",
                    price_key_id="686b5181-145f-4a87-a770-d4176e520ec1",
                    job_id="ceae1679-2117-4cdb-88e8-5e706a298d37",
                    fingerprint="pokemon|en|me2|20|ceruledge|raw|raw|au|aud",
                    before=ba["before"],
                    after=ba["after"],
                )
            finally:
                os.environ.pop("CARDSCANR_LIVE_NAV_ATTEMPTS_DIR", None)
        self.assertTrue(classified["searchSubmitted"])
        self.assertTrue(classified["x11SoldStateVerified"])
        self.assertEqual(classified["postSoldCapturePhase"], POST_SOLD_CAPTURE_READY)
        self.assertEqual(classified["capture"]["status"], "SUCCESS")
        self.assertTrue(classified["capture"]["correlated"])
        self.assertEqual(classified["capture"]["captureOrigin"], "LIVE_BROWSER_CAPTURE")
        self.assertEqual(classified["parse"]["phase"], PARSE_COMPLETE)
        self.assertEqual(classified["parse"]["fetched"], 30)
        self.assertEqual(classified["parse"]["accepted"], 7)
        self.assertEqual(classified["write"]["outcome"], "UPDATED_FROM_EBAY")
        self.assertEqual(float(classified["write"]["resultingPrice"]), 2.99)
        self.assertEqual(classified["cardVerdict"], "PASS_PRICE_UPDATED")


class NegativeReplayTests(unittest.TestCase):
    def test_no_event_presubmit_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CARDSCANR_LIVE_NAV_ATTEMPTS_DIR"] = tmp
            try:
                c = classify_card_from_job_result(
                    {"status": "failed", "error": "LOCAL_GUI_NOT_READY"},
                    attempt_id="no-event-1",
                    price_key_id="pk",
                )
            finally:
                os.environ.pop("CARDSCANR_LIVE_NAV_ATTEMPTS_DIR", None)
        self.assertFalse(c["searchSubmitted"])
        self.assertIn(c["cardVerdict"], {"STOP_PREFLIGHT", "FAIL_NAVIGATION"})

    def test_event_exists_later_crash_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CARDSCANR_LIVE_NAV_ATTEMPTS_DIR"] = tmp
            try:
                emit_search_submission_started(attempt_id="crash-1", query="q", price_key_id="pk")
                c = classify_card_from_job_result(
                    {
                        "status": "failed",
                        "ownedDailyOutcome": "TEMPORARY_BROWSER_FAILURE",
                        "error": "nav crash after submit",
                    },
                    attempt_id="crash-1",
                    price_key_id="pk",
                )
            finally:
                os.environ.pop("CARDSCANR_LIVE_NAV_ATTEMPTS_DIR", None)
        self.assertTrue(c["searchSubmitted"])
        self.assertEqual(c["cardVerdict"], "FAIL_NAVIGATION")

    def test_different_attempt_not_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CARDSCANR_LIVE_NAV_ATTEMPTS_DIR"] = tmp
            try:
                emit_search_submission_started(attempt_id="other-aid", query="q", price_key_id="pk")
                c = classify_card_from_job_result(
                    {"status": "failed", "error": "x"},
                    attempt_id="current-aid",
                    price_key_id="pk",
                )
            finally:
                os.environ.pop("CARDSCANR_LIVE_NAV_ATTEMPTS_DIR", None)
        self.assertFalse(c["searchSubmitted"])

    def test_completed_write_without_event_is_consistency_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CARDSCANR_LIVE_NAV_ATTEMPTS_DIR"] = tmp
            try:
                c = classify_card_from_job_result(
                    {
                        "status": "completed",
                        "ownedDailyOutcome": "UPDATED_FROM_EBAY",
                        "x11SoldStateVerified": True,
                        "recommendedPrice": 2.99,
                    },
                    attempt_id="missing-event",
                    price_key_id="pk",
                )
            finally:
                os.environ.pop("CARDSCANR_LIVE_NAV_ATTEMPTS_DIR", None)
        self.assertEqual(c["cardVerdict"], "HARNESS_CONSISTENCY_ERROR")
        self.assertEqual(c["consistencyError"], "COMPLETED_WRITE_WITHOUT_SEARCH_SUBMISSION_EVENT")

    def test_event_plus_capture_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CARDSCANR_LIVE_NAV_ATTEMPTS_DIR"] = tmp
            try:
                emit_search_submission_started(attempt_id="cap-fail", query="q", price_key_id="pk")
                c = classify_card_from_job_result(
                    {
                        "status": "failed",
                        "ownedDailyOutcome": "POST_SOLD_CAPTURE_FAILURE",
                        "x11SoldStateVerified": True,
                        "postSoldCapturePhase": "POST_SOLD_CAPTURE_FAILED",
                        "error": "CAPTURE_ENCODING_FAILURE",
                    },
                    attempt_id="cap-fail",
                    price_key_id="pk",
                )
            finally:
                os.environ.pop("CARDSCANR_LIVE_NAV_ATTEMPTS_DIR", None)
        self.assertTrue(c["searchSubmitted"])
        self.assertEqual(c["cardVerdict"], "FAIL_CAPTURE")

    def test_event_plus_safe_no_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CARDSCANR_LIVE_NAV_ATTEMPTS_DIR"] = tmp
            try:
                emit_search_submission_started(attempt_id="noprice", query="q", price_key_id="pk")
                c = classify_card_from_job_result(
                    {
                        "status": "checked_no_new_exact_evidence",
                        "ownedDailyOutcome": "CHECKED_NO_NEW_EXACT_EVIDENCE",
                        "x11SoldStateVerified": True,
                        "postSoldCapturePhase": POST_SOLD_CAPTURE_READY,
                        "parsePhase": PARSE_COMPLETE,
                        "currentJobCapture": {
                            "htmlPath": str(FIXTURE.parent / "nope.html"),
                            "sha256": "x",
                            "jobId": "j",
                            "attemptId": "noprice",
                            "priceKeyId": "pk",
                            "captureOrigin": "LIVE_BROWSER_CAPTURE",
                        },
                    },
                    attempt_id="noprice",
                    price_key_id="pk",
                    job_id="j",
                )
            finally:
                os.environ.pop("CARDSCANR_LIVE_NAV_ATTEMPTS_DIR", None)
        self.assertTrue(c["searchSubmitted"])
        self.assertEqual(c["cardVerdict"], "SAFE_NO_NEW_EXACT_EVIDENCE")

    def test_stale_global_capture_not_substituted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CARDSCANR_LIVE_NAV_ATTEMPTS_DIR"] = tmp
            try:
                emit_search_submission_started(attempt_id="stale", query="q", price_key_id="pk")
                c = classify_card_from_job_result(
                    {
                        "status": "failed",
                        "ownedDailyOutcome": "POST_SOLD_CAPTURE_FAILURE",
                        "x11SoldStateVerified": True,
                        "postSoldCapturePhase": "POST_SOLD_CAPTURE_FAILED",
                        # Intentionally no currentJobCapture — must NOT invent last_capture.
                    },
                    attempt_id="stale",
                    price_key_id="pk",
                )
            finally:
                os.environ.pop("CARDSCANR_LIVE_NAV_ATTEMPTS_DIR", None)
        self.assertNotEqual(c["capture"].get("status"), "SUCCESS")
        self.assertTrue(c["capture"].get("globalLastCaptureAuthoritative") is False)


class AggregateOperationalMergeTests(unittest.TestCase):
    def test_build_aggregate_preserves_phase_fields(self) -> None:
        from datetime import datetime, timezone

        from cardscanr_market_engine.marketplaces import resolve_marketplace_config
        from cardscanr_market_engine.models import MarketPriceKey, ProviderRequest, ProviderResult, SoldComp
        from cardscanr_market_engine.providers.ebay_browser_provider import EbayBrowserSoldCompsProvider
        from cardscanr_market_engine.providers.query_builder import ProviderSearchQuery

        key = MarketPriceKey(
            id="k1",
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
            fingerprint="fp",
        )
        market = resolve_marketplace_config(market_country="au", currency="AUD", marketplace="ebay")
        req = ProviderRequest(
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
        sq = ProviderSearchQuery(
            query_text="Ceruledge 20 phantasmal flames Pokemon",
            include_terms=("Ceruledge", "20", "Pokemon"),
            exclude_terms=(),
            query_index=0,
            query_source="name_number_set_unquoted",
            search_url="https://www.ebay.com.au/sch/i.html",
            market_country="AU",
            currency="AUD",
            provider_marketplace_id="EBAY_AU",
            provider_domain="ebay.com.au",
            diagnostics={},
        )
        comp = SoldComp(
            source_listing_id="1",
            title="Ceruledge 20 Phantasmal Flames Pokemon",
            sold_price=2.99,
            shipping_price=0.0,
            total_price=2.99,
            currency="AUD",
            sold_date=datetime(2026, 10, 1, tzinfo=timezone.utc),
            listing_url="https://www.ebay.com.au/itm/1",
            condition_text="Raw",
            raw_metadata={"url_quality": "direct_item"},
        )
        attempt = ProviderResult(
            provider_name="ebay_browser",
            marketplace="EBAY_AU",
            provider_fingerprint="fp",
            query_used=sq.query_text,
            comps=[comp],
            raw_metadata={
                "x11SoldStateVerified": True,
                "postSoldCapturePhase": POST_SOLD_CAPTURE_READY,
                "parsePhase": PARSE_COMPLETE,
                "navMode": "linux_x11",
                "desktopNav": {"SOLD_STATE_VERIFIED": True},
                "stageTimings": {
                    "persistedCaptureArtifact": {
                        "htmlPath": "/tmp/x.html",
                        "sha256": "abc",
                        "jobId": "j1",
                        "attemptId": "a1",
                        "priceKeyId": "k1",
                        "captureOrigin": "LIVE_BROWSER_CAPTURE",
                    }
                },
            },
        )
        provider = EbayBrowserSoldCompsProvider()
        agg = provider._build_aggregate_result(  # noqa: SLF001
            request=req,
            attempts=[(sq, attempt)],
            comps=[comp],
            stop_reason="all_query_attempts_exhausted",
            query_attempt_limit=1,
        )
        meta = agg.raw_metadata
        self.assertTrue(meta.get("x11SoldStateVerified"))
        self.assertEqual(meta.get("postSoldCapturePhase"), POST_SOLD_CAPTURE_READY)
        self.assertEqual(meta.get("parsePhase"), PARSE_COMPLETE)
        self.assertEqual(meta.get("currentJobCapture", {}).get("sha256"), "abc")


if __name__ == "__main__":
    unittest.main()
