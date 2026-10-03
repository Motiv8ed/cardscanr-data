"""Capture evidence must correlate to the current job — never stale last_capture."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.capture_evidence_correlation import (
    CAPTURE_ORIGIN_LOCAL_FIXTURE,
    correlate_capture_evidence,
    not_run_capture_block,
)
from cardscanr_market_engine.providers.post_sold_capture_process import (
    CaptureProcessResult,
    _hydrate_capture_payload,
    capture_process_result_to_sold_page,
)
from tools.ebay_au_five_consecutive_post_unicode_run import _extract_capture


class CaptureCorrelationTests(unittest.TestCase):
    def test_null_phase_cannot_hydrate_old_last_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            last = Path(tmp) / "last_capture_meta.json"
            last.write_text(
                json.dumps(
                    {
                        "html_path": str(Path(tmp) / "last_capture.html"),
                        "html_sha256": "deadbeef",
                        "target_id": "STALE",
                        "capture_origin": CAPTURE_ORIGIN_LOCAL_FIXTURE,
                    }
                ),
                encoding="utf-8",
            )
            (Path(tmp) / "last_capture.html").write_text("<html>stale</html>", encoding="utf-8")
            with mock.patch(
                "cardscanr_market_engine.capture_evidence_correlation.LAST_CAPTURE_DIR",
                Path(tmp),
            ):
                corr = correlate_capture_evidence(
                    result={"status": "skipped_already_fresh", "ownedDailyOutcome": "already_fresh_noop"},
                    expected_attempt_id="attempt-1",
                    expected_job_id="job-1",
                    allow_global_last_capture=False,
                )
            self.assertEqual(corr.status, "NOT_RUN")
            self.assertIsNone(corr.artifact_path)
            self.assertIsNone(corr.target_id)

    def test_skipped_already_fresh_capture_not_run(self) -> None:
        block = _extract_capture(
            {"status": "skipped_already_fresh", "ownedDailyOutcome": "already_fresh_noop"},
            {},
            job_id="j1",
            attempt_id="a1",
            price_key_id="pk1",
        )
        self.assertEqual(block["status"], "NOT_RUN")
        self.assertIsNone(block["phase"])
        self.assertIsNone(block["artifactPath"])
        self.assertIsNone(block["sha256"])
        self.assertIsNone(block["targetId"])

    def test_successful_capture_attaches_own_artifact_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            art = Path(tmp) / "job_a.html"
            art.write_text("<html>current</html>", encoding="utf-8")
            result = {
                "postSoldCapturePhase": "POST_SOLD_CAPTURE_READY",
                "x11SoldStateVerified": True,
                "postSoldCapture": {
                    "capturePhase": "POST_SOLD_CAPTURE_READY",
                    "targetId": "T_CURRENT",
                    "diagnostics": {
                        "html_path": str(art),
                        "html_sha256": "abc",
                        "job_id": "job-a",
                        "attempt_id": "att-a",
                        "price_key_id": "pk-a",
                        "capture_origin": "LIVE_BROWSER_CAPTURE",
                    },
                },
            }
            corr = correlate_capture_evidence(
                result=result,
                expected_job_id="job-a",
                expected_attempt_id="att-a",
                expected_price_key_id="pk-a",
            )
            self.assertEqual(corr.status, "ATTACHED")
            self.assertEqual(corr.artifact_path, str(art))
            self.assertEqual(corr.target_id, "T_CURRENT")

    def test_failed_capture_cannot_attach_prior_artifact(self) -> None:
        result = {
            "postSoldCapturePhase": "POST_SOLD_CAPTURE_FAILED",
            "x11SoldStateVerified": True,
            "postSoldCapture": {
                "capturePhase": "POST_SOLD_CAPTURE_FAILED",
                "failureClass": "CAPTURE_ENCODING_FAILURE",
                "targetId": None,
                "diagnostics": {},
            },
        }
        corr = correlate_capture_evidence(result=result, expected_job_id="job-b")
        self.assertEqual(corr.status, "FAILED_NO_ARTIFACT")
        self.assertIsNone(corr.artifact_path)

    def test_attempt_id_mismatch_rejects(self) -> None:
        corr = correlate_capture_evidence(
            result={
                "postSoldCapturePhase": "POST_SOLD_CAPTURE_READY",
                "x11SoldStateVerified": True,
                "postSoldCapture": {
                    "targetId": "T",
                    "diagnostics": {
                        "html_path": "/tmp/x.html",
                        "attempt_id": "att-other",
                        "job_id": "job-1",
                        "capture_origin": "LIVE_BROWSER_CAPTURE",
                    },
                },
            },
            expected_attempt_id="att-expected",
            expected_job_id="job-1",
        )
        self.assertEqual(corr.status, "REJECTED_MISMATCH")
        self.assertIn("attemptId_mismatch", corr.rejection_reasons)

    def test_job_id_mismatch_rejects(self) -> None:
        corr = correlate_capture_evidence(
            result={
                "postSoldCapturePhase": "POST_SOLD_CAPTURE_READY",
                "x11SoldStateVerified": True,
                "postSoldCapture": {
                    "targetId": "T",
                    "diagnostics": {
                        "html_path": "/tmp/x.html",
                        "attempt_id": "a",
                        "job_id": "job-other",
                        "capture_origin": "LIVE_BROWSER_CAPTURE",
                    },
                },
            },
            expected_attempt_id="a",
            expected_job_id="job-expected",
        )
        self.assertEqual(corr.status, "REJECTED_MISMATCH")
        self.assertIn("jobId_mismatch", corr.rejection_reasons)

    def test_price_key_mismatch_rejects(self) -> None:
        corr = correlate_capture_evidence(
            result={
                "postSoldCapturePhase": "POST_SOLD_CAPTURE_READY",
                "x11SoldStateVerified": True,
                "postSoldCapture": {
                    "targetId": "T",
                    "diagnostics": {
                        "html_path": "/tmp/x.html",
                        "job_id": "j",
                        "attempt_id": "a",
                        "price_key_id": "pk-other",
                        "capture_origin": "LIVE_BROWSER_CAPTURE",
                    },
                },
            },
            expected_job_id="j",
            expected_attempt_id="a",
            expected_price_key_id="pk-expected",
        )
        self.assertEqual(corr.status, "REJECTED_MISMATCH")
        self.assertIn("priceKeyId_mismatch", corr.rejection_reasons)

    def test_local_fixture_cannot_appear_as_live_card_capture(self) -> None:
        corr = correlate_capture_evidence(
            result={
                "postSoldCapturePhase": "POST_SOLD_CAPTURE_READY",
                "x11SoldStateVerified": True,
                "postSoldCapture": {
                    "targetId": "E3DBEB8A",
                    "diagnostics": {
                        "html_path": "/tmp/fixture.html",
                        "job_id": "local-unicode-fixture-proof",
                        "attempt_id": "local-unicode-fixture-attempt",
                        "capture_origin": CAPTURE_ORIGIN_LOCAL_FIXTURE,
                    },
                },
            },
            expected_job_id="local-unicode-fixture-proof",
            expected_attempt_id="live-attempt-1",
        )
        self.assertEqual(corr.status, "REJECTED_MISMATCH")
        self.assertTrue(
            "local_fixture_not_live_evidence" in corr.rejection_reasons
            or "attemptId_mismatch" in corr.rejection_reasons
        )

    def test_hydrate_only_on_success_no_stale_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stale = Path(tmp) / "last_capture.html"
            stale.write_text("<html>stale fixture</html>", encoding="utf-8")
            # Failure payload must not load html from anywhere.
            out = _hydrate_capture_payload(
                {"status": "CDP_CONNECT_FAILURE", "html_path": str(stale), "error": "x"}
            )
            self.assertNotIn("html", out)

    def test_failed_process_result_no_prior_html(self) -> None:
        fake = CaptureProcessResult(
            status="CAPTURE_ENCODING_FAILURE",
            payload={"status": "CAPTURE_ENCODING_FAILURE", "error": "UnicodeEncodeError"},
            elapsed_ms=1,
        )
        sold = capture_process_result_to_sold_page(fake, x11_sold_state_verified=True)
        self.assertFalse(sold.success)
        self.assertFalse(sold.html_or_text)

    def test_sequential_jobs_cannot_cross_hydrate_via_extract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.html"
            a.write_text("<html>A</html>", encoding="utf-8")
            result_a = {
                "postSoldCapturePhase": "POST_SOLD_CAPTURE_READY",
                "x11SoldStateVerified": True,
                "postSoldCapture": {
                    "targetId": "TA",
                    "diagnostics": {
                        "html_path": str(a),
                        "job_id": "job-a",
                        "attempt_id": "att-a",
                        "price_key_id": "pk-a",
                        "capture_origin": "LIVE_BROWSER_CAPTURE",
                    },
                },
            }
            # Job B skipped — must not see job A artifact.
            block_b = _extract_capture(
                {"status": "skipped_already_fresh"},
                {},
                job_id="job-b",
                attempt_id="att-b",
                price_key_id="pk-b",
            )
            self.assertEqual(block_b["status"], "NOT_RUN")
            self.assertIsNone(block_b["artifactPath"])
            # Job A attaches only its own.
            block_a = _extract_capture(
                result_a,
                {},
                job_id="job-a",
                attempt_id="att-a",
                price_key_id="pk-a",
            )
            self.assertEqual(block_a["status"], "ATTACHED")
            self.assertEqual(block_a["artifactPath"], str(a))

    def test_not_run_contract_shape(self) -> None:
        block = not_run_capture_block()
        self.assertEqual(block["status"], "NOT_RUN")
        for key in ("phase", "artifactPath", "sha256", "targetId"):
            self.assertIsNone(block[key])

    def test_production_provider_has_no_stale_last_capture_fallback(self) -> None:
        """Part K: parser must never read prior last_capture.html as current HTML."""
        src = (
            ROOT / "cardscanr_market_engine" / "providers" / "ebay_browser_provider.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn('post_sold_capture_last" / "last_capture.html"', src)
        self.assertNotIn("post_sold_capture_last' / 'last_capture.html'", src)
        # Hydrate path remains SUCCESS-only.
        hydrate_src = (
            ROOT / "cardscanr_market_engine" / "providers" / "post_sold_capture_process.py"
        ).read_text(encoding="utf-8")
        self.assertIn("Only hydrates on SUCCESS", hydrate_src)
        self.assertIn("Never backfills from a previous run", hydrate_src)


if __name__ == "__main__":
    unittest.main()
