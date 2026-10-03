#!/usr/bin/env python3
"""Regression: postSoldCapturePhase harness/reporting propagation (no live eBay)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.owned_daily_outcomes import (
    CHECKED_NO_NEW_EXACT_EVIDENCE,
    UPDATED_FROM_EBAY,
)
from cardscanr_market_engine.pipeline_phase_diagnostics import (
    build_provider_diagnostics_for_result,
    capture_phase_acknowledged,
    extract_pipeline_phases,
)
from cardscanr_market_engine.providers.post_sold_capture import (
    POST_SOLD_CAPTURE_FAILED,
    POST_SOLD_CAPTURE_PENDING,
    POST_SOLD_CAPTURE_READY,
)
from tools.linux_x11_single_probe_ready import _classify_verdict


class _FakeProviderResult:
    def __init__(self, raw_metadata: dict) -> None:
        self.raw_metadata = raw_metadata


def _sold_ok() -> dict:
    return {"SOLD_STATE_VERIFIED": True, "soldClickSuccess": True}


def _job_result_from_metadata(meta: dict, *, outcome: str = UPDATED_FROM_EBAY) -> dict:
    """Mirror job_runner success attachment of phases + diagnostics."""
    fake = _FakeProviderResult(meta)
    diag = build_provider_diagnostics_for_result(fake)
    phases = extract_pipeline_phases(meta)
    return {
        "status": "completed" if outcome == UPDATED_FROM_EBAY else "checked_no_new_exact_evidence",
        "ownedDailyOutcome": outcome,
        "outcomeClass": outcome,
        "providerDiagnostics": diag,
        "postSoldCapturePhase": phases.get("postSoldCapturePhase"),
        "parsePhase": phases.get("parsePhase"),
        "x11SoldStateVerified": bool(phases.get("x11SoldStateVerified")),
    }


def _harness_capture_phase(result: dict) -> str | None:
    """Mirror linux_x11_single_probe_ready phase extraction for a job result."""
    diag = result.get("providerDiagnostics") or {}
    nested = diag.get("diagnostics") if isinstance(diag.get("diagnostics"), dict) else (
        diag if isinstance(diag, dict) else {}
    )
    stage = nested.get("stageTimings") if isinstance(nested.get("stageTimings"), dict) else {}
    stage_fields = stage.get("fields") if isinstance(stage.get("fields"), dict) else {}
    phase_view = extract_pipeline_phases(
        {
            **(nested if isinstance(nested, dict) else {}),
            "stageTimings": stage,
            "postSoldCapturePhase": result.get("postSoldCapturePhase") or nested.get("postSoldCapturePhase"),
            "parsePhase": result.get("parsePhase") or nested.get("parsePhase"),
            "x11SoldStateVerified": result.get("x11SoldStateVerified"),
        }
    )
    return (
        phase_view.get("postSoldCapturePhase")
        or nested.get("postSoldCapturePhase")
        or stage.get("postSoldCapturePhase")
        or stage_fields.get("postSoldCapturePhase")
    )


def _verdict_for(result: dict, *, capture_phase: str | None, parse_phase: str | None = None) -> str:
    return _classify_verdict(
        outcome=str(result.get("ownedDailyOutcome") or result.get("outcomeClass") or ""),
        err=str(result.get("error") or ""),
        url="https://www.ebay.com.au/sch/i.html?LH_Sold=1&_nkw=Pikachu",
        search={"ordinaryResults": True},
        sold=_sold_ok(),
        http_status=200,
        capture_phase=capture_phase,
        parse_phase=parse_phase,
        fifth_probe=True,
    )


class PostSoldCapturePhaseReportingTests(unittest.TestCase):
    def test_01_sold_capture_parse_write_success(self) -> None:
        meta = {
            "x11SoldStateVerified": True,
            "postSoldCapturePhase": POST_SOLD_CAPTURE_READY,
            "parsePhase": "PARSE_COMPLETE",
            "finalizeTerminal": "FINALIZE_SUCCESS",
            "stageTimings": {
                "postSoldCapturePhase": POST_SOLD_CAPTURE_READY,
                "parsePhase": "PARSE_COMPLETE",
                "elapsedMs": 1200,
            },
        }
        result = _job_result_from_metadata(meta)
        capture = _harness_capture_phase(result)
        self.assertTrue(capture_phase_acknowledged(capture))
        self.assertEqual(result.get("postSoldCapturePhase"), POST_SOLD_CAPTURE_READY)
        self.assertEqual(result.get("parsePhase"), "PARSE_COMPLETE")
        self.assertEqual(
            _verdict_for(result, capture_phase=capture, parse_phase="PARSE_COMPLETE"),
            "FIFTH_SINGLE_PROBE_PASS",
        )

    def test_02_sold_verified_capture_timeout_fails(self) -> None:
        meta = {
            "x11SoldStateVerified": True,
            "postSoldCapturePhase": POST_SOLD_CAPTURE_FAILED,
            "stageTimings": {
                "postSoldCapturePhase": "CDP_TIMEOUT",
                "postSoldCapture": {"failure_class": "CDP_TIMEOUT", "success": False},
            },
        }
        result = _job_result_from_metadata(meta, outcome="POST_SOLD_CAPTURE_FAILURE")
        result["status"] = "failed"
        result["error"] = "POST_SOLD_CAPTURE_FAILURE: CDP_TIMEOUT"
        result["ownedDailyOutcome"] = "POST_SOLD_CAPTURE_FAILURE"
        capture = _harness_capture_phase(result)
        self.assertFalse(capture_phase_acknowledged(capture))
        verdict = _verdict_for(result, capture_phase=capture)
        self.assertIn("LOCAL_FAILURE", verdict)
        self.assertNotEqual(verdict, "FIFTH_SINGLE_PROBE_PASS")

    def test_03_sold_verified_capture_nonzero_exit_fails(self) -> None:
        meta = {
            "x11SoldStateVerified": True,
            "postSoldCapturePhase": POST_SOLD_CAPTURE_FAILED,
            "stageTimings": {
                "postSoldCapturePhase": POST_SOLD_CAPTURE_FAILED,
                "captureProcess": {"exitCode": 2, "ok": False},
            },
        }
        result = _job_result_from_metadata(meta, outcome="POST_SOLD_CAPTURE_FAILURE")
        result["ownedDailyOutcome"] = "POST_SOLD_CAPTURE_FAILURE"
        result["error"] = "capture process exit 2"
        capture = _harness_capture_phase(result)
        self.assertFalse(capture_phase_acknowledged(capture))
        self.assertIn("LOCAL_FAILURE", _verdict_for(result, capture_phase=capture))

    def test_04_sold_verified_empty_invalid_body_fails(self) -> None:
        meta = {
            "x11SoldStateVerified": True,
            "postSoldCapturePhase": POST_SOLD_CAPTURE_FAILED,
            "stageTimings": {
                "postSoldCapturePhase": "CDP_DOCUMENT_NOT_READY",
                "postSoldCapture": {"success": False, "html_or_text": ""},
            },
        }
        result = _job_result_from_metadata(meta, outcome="POST_SOLD_CAPTURE_FAILURE")
        result["ownedDailyOutcome"] = "POST_SOLD_CAPTURE_FAILURE"
        capture = _harness_capture_phase(result)
        self.assertFalse(capture_phase_acknowledged(capture))
        self.assertIn("LOCAL_FAILURE", _verdict_for(result, capture_phase=capture))

    def test_05_capture_success_parse_failure_keeps_capture_ack(self) -> None:
        meta = {
            "x11SoldStateVerified": True,
            "postSoldCapturePhase": POST_SOLD_CAPTURE_READY,
            "parsePhase": "PARSE_FAILED",
            "stageTimings": {
                "postSoldCapturePhase": POST_SOLD_CAPTURE_READY,
                "parsePhase": "PARSE_FAILED",
            },
        }
        # Simulate failure after successful capture: diagnostics still carry READY.
        fake = _FakeProviderResult(meta)
        diag = build_provider_diagnostics_for_result(fake)
        phases = extract_pipeline_phases(meta)
        result = {
            "status": "failed",
            "ownedDailyOutcome": "PARSE_FAILED",
            "error": "parser exploded",
            "providerDiagnostics": diag,
            "postSoldCapturePhase": phases.get("postSoldCapturePhase"),
            "parsePhase": phases.get("parsePhase"),
        }
        capture = _harness_capture_phase(result)
        self.assertTrue(capture_phase_acknowledged(capture))
        self.assertEqual(result.get("parsePhase"), "PARSE_FAILED")
        verdict = _verdict_for(result, capture_phase=capture, parse_phase="PARSE_FAILED")
        self.assertIn("LOCAL_FAILURE", verdict)
        self.assertNotEqual(verdict, "FIFTH_SINGLE_PROBE_PASS")

    def test_06_capture_parse_ok_write_failure_fails(self) -> None:
        meta = {
            "x11SoldStateVerified": True,
            "postSoldCapturePhase": POST_SOLD_CAPTURE_READY,
            "parsePhase": "PARSE_COMPLETE",
            "stageTimings": {
                "postSoldCapturePhase": POST_SOLD_CAPTURE_READY,
                "parsePhase": "PARSE_COMPLETE",
            },
        }
        fake = _FakeProviderResult(meta)
        diag = build_provider_diagnostics_for_result(fake)
        phases = extract_pipeline_phases(meta)
        result = {
            "status": "failed",
            "ownedDailyOutcome": "PERSISTENCE_FAILED",
            "error": "snapshot write failed",
            "providerDiagnostics": diag,
            "postSoldCapturePhase": phases.get("postSoldCapturePhase"),
            "parsePhase": phases.get("parsePhase"),
        }
        capture = _harness_capture_phase(result)
        self.assertTrue(capture_phase_acknowledged(capture))
        verdict = _verdict_for(result, capture_phase=capture, parse_phase="PARSE_COMPLETE")
        self.assertIn("LOCAL_FAILURE", verdict)

    def test_07_successful_write_cannot_hide_missing_capture_ack(self) -> None:
        # Price/write succeeded but capture phase never positively acknowledged.
        meta = {
            "x11SoldStateVerified": True,
            "recommendedPrice": 3.76,
            "stageTimings": {
                # Flat snapshot only has pending — never READY.
                "postSoldCapturePhase": POST_SOLD_CAPTURE_PENDING,
                "parsePhase": "PARSE_COMPLETE",
            },
        }
        result = _job_result_from_metadata(meta)
        # Simulate a buggy caller that only looks at price outcome.
        self.assertEqual(result.get("ownedDailyOutcome"), UPDATED_FROM_EBAY)
        capture = _harness_capture_phase(result)
        self.assertFalse(capture_phase_acknowledged(capture))
        verdict = _verdict_for(result, capture_phase=capture, parse_phase="PARSE_COMPLETE")
        self.assertEqual(verdict, "FIFTH_SINGLE_PROBE_LOCAL_FAILURE")

    def test_08_old_diagnostics_serde_preserves_phase_field(self) -> None:
        # Old consumers stored stageTimings nested under {"fields": ...}.
        # New StageTimings.snapshot() is flat. Both must preserve the phase.
        nested_legacy = {
            "x11SoldStateVerified": True,
            "stageTimings": {
                "fields": {
                    "postSoldCapturePhase": POST_SOLD_CAPTURE_READY,
                    "parsePhase": "PARSE_COMPLETE",
                }
            },
        }
        flat_modern = {
            "x11SoldStateVerified": True,
            "stageTimings": {
                "postSoldCapturePhase": POST_SOLD_CAPTURE_READY,
                "parsePhase": "PARSE_COMPLETE",
                "elapsedMs": 99,
            },
        }
        for label, meta in (("legacy_nested", nested_legacy), ("modern_flat", flat_modern)):
            with self.subTest(label=label):
                # Round-trip through JSON like a diagnostics bubble would.
                blob = json.loads(json.dumps(build_provider_diagnostics_for_result(_FakeProviderResult(meta))))
                nested = blob.get("diagnostics") or {}
                phases = extract_pipeline_phases(nested)
                self.assertEqual(phases.get("postSoldCapturePhase"), POST_SOLD_CAPTURE_READY, label)
                # Ensure serialize did not erase the phase key.
                self.assertTrue(
                    nested.get("postSoldCapturePhase") == POST_SOLD_CAPTURE_READY
                    or (nested.get("stageTimings") or {}).get("postSoldCapturePhase")
                    == POST_SOLD_CAPTURE_READY
                    or ((nested.get("stageTimings") or {}).get("fields") or {}).get(
                        "postSoldCapturePhase"
                    )
                    == POST_SOLD_CAPTURE_READY,
                    label,
                )
                result = _job_result_from_metadata(meta)
                capture = _harness_capture_phase(result)
                self.assertTrue(capture_phase_acknowledged(capture), label)
                self.assertEqual(
                    _verdict_for(result, capture_phase=capture, parse_phase="PARSE_COMPLETE"),
                    "FIFTH_SINGLE_PROBE_PASS",
                    label,
                )

    def test_checked_no_new_evidence_still_requires_capture_ack(self) -> None:
        meta = {
            "x11SoldStateVerified": True,
            "postSoldCapturePhase": POST_SOLD_CAPTURE_READY,
            "parsePhase": "PARSE_COMPLETE",
        }
        result = _job_result_from_metadata(meta, outcome=CHECKED_NO_NEW_EXACT_EVIDENCE)
        capture = _harness_capture_phase(result)
        self.assertEqual(
            _verdict_for(result, capture_phase=capture, parse_phase="PARSE_COMPLETE"),
            "FIFTH_SINGLE_PROBE_NO_DATA_PASS",
        )


if __name__ == "__main__":
    unittest.main()
