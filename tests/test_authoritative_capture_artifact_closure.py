"""Authoritative per-job capture artifact + non-fatal diagnostic mirror.

LOCAL ONLY — no eBay network contact.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.providers.post_sold_capture import (  # noqa: E402
    CAPTURE_ARTIFACT_WRITE_FAILURE,
    CAPTURE_PROTOCOL_FAILURE,
    CDP_CAPTURE_PROCESS_TIMEOUT,
    CDP_CONNECT_FAILURE,
    CDP_EVALUATION_FAILURE,
    CDP_TARGET_FAILURE,
    CDP_TARGET_NOT_FOUND,
    POST_SOLD_CAPTURE_READY,
    resolve_authoritative_capture_html_path,
    write_utf8_bytes_atomic,
)
from cardscanr_market_engine.providers.post_sold_capture_process import (  # noqa: E402
    CaptureProcessResult,
    _hydrate_capture_payload,
    capture_process_result_to_sold_page,
)
from cardscanr_market_engine.providers import post_sold_capture_worker as worker  # noqa: E402
from cardscanr_market_engine.reliability_harness_evidence import (  # noqa: E402
    build_capture_evidence,
    build_write_evidence,
)


def _fixture_html(*, megabytes: float = 0.01) -> str:
    marker = (
        "Tropius\u200c Pitch Black — Pokémon サーナイト 🔥 "
        "smart…quotes “ok”\n"
    )
    pad_unit = "x" * 1024
    pad = pad_unit * max(1, int(megabytes * 1024))
    return (
        "<html><body><h1>Sold items</h1>\n"
        f"<div>{marker}</div>\n"
        f"<pre>{pad}</pre>\n"
        '<a href="https://www.ebay.com.au/itm/111222333444">listing</a>\n'
        "Sold 2 Oct 2026\n"
        "</body></html>"
    )


class AuthoritativePathTests(unittest.TestCase):
    def test_unique_paths_per_job_attempt(self) -> None:
        a = resolve_authoritative_capture_html_path(
            job_id="job-A", attempt_id="attempt-A", price_key_id="pk1"
        )
        b = resolve_authoritative_capture_html_path(
            job_id="job-B", attempt_id="attempt-B", price_key_id="pk2"
        )
        self.assertNotEqual(str(a), str(b))
        self.assertIn("job-A", str(a).replace("\\", "/"))
        self.assertIn("attempt-A", str(a).replace("\\", "/"))
        self.assertIn("job-B", str(b).replace("\\", "/"))
        self.assertTrue(str(a).endswith(".html"))

    def test_explicit_artifact_path_honoured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "custom" / "cap.html"
            resolved = resolve_authoritative_capture_html_path(explicit_path=dest)
            self.assertEqual(resolved, dest)


class LockedDiagnosticMirrorTests(unittest.TestCase):
    def test_old_architecture_replace_denied_on_locked_last_capture(self) -> None:
        """Reproduce WinError 5 / Access denied class on shared last_capture."""
        if sys.platform != "win32":
            self.skipTest("Windows lock reproduction")
        import ctypes
        from ctypes import wintypes

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "last_capture.html"
            dest.write_text("OLD", encoding="utf-8")
            # Exclusive share-mode 0 handle → os.replace gets WinError 5.
            GENERIC_READ = 0x80000000
            OPEN_EXISTING = 3
            INVALID = wintypes.HANDLE(-1).value
            handle = ctypes.windll.kernel32.CreateFileW(
                str(dest), GENERIC_READ, 0, None, OPEN_EXISTING, 0, None
            )
            self.assertNotEqual(handle, INVALID)
            try:
                with self.assertRaises(OSError) as ctx:
                    write_utf8_bytes_atomic(dest, "NEW\u200cCONTENT")
                err = ctx.exception
                winerr = getattr(err, "winerror", None)
                self.assertTrue(
                    winerr == 5 or isinstance(err, PermissionError) or "Access is denied" in str(err),
                    msg=repr(err),
                )
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)

    def test_locked_mirror_does_not_fail_authoritative_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            last_dir = Path(tmp) / "post_sold_capture_last"
            last_dir.mkdir(parents=True)
            locked = last_dir / "last_capture_meta.json"
            locked.write_text("{}", encoding="utf-8")
            auth = Path(tmp) / "auth" / "job1" / "attempt1" / "capture.html"
            worker._LAST_DIR = last_dir  # type: ignore[misc]
            html = _fixture_html(megabytes=0.05)
            captured: list[dict] = []

            # Patch diagnostic mirror helper to simulate locked shared destination.
            with mock.patch.object(
                worker,
                "_update_diagnostic_mirror",
                return_value=(False, "PermissionError:[WinError 5] Access is denied"),
            ):
                with mock.patch.object(worker, "_write_protocol_line", side_effect=lambda p: captured.append(dict(p))):
                    code = worker._emit(
                        {
                            "status": "SUCCESS",
                            "html": html,
                            "body_text": "Sold 2 Oct 2026\nTropius\u200c",
                            "target_id": "C0F435F2279C4D81D23588CB8A85300D",
                            "target_url": "https://www.ebay.com.au/sch/i.html?LH_Sold=1",
                            "elapsed_ms": 12,
                            "canonical_itm_href_count": 1,
                            "candidates": [{"href": "https://www.ebay.com.au/itm/111222333444"}],
                        },
                        artifact_path=str(auth),
                    )
            self.assertEqual(code, 0)
            self.assertTrue(auth.is_file())
            self.assertEqual(captured[0]["status"], "SUCCESS")
            self.assertFalse(captured[0].get("diagnosticMirrorUpdated"))
            self.assertIn("WinError 5", str(captured[0].get("diagnosticMirrorWarning") or ""))
            raw = auth.read_bytes()
            self.assertEqual(captured[0]["html_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertIn("\u200c".encode("utf-8"), raw)
            sold = capture_process_result_to_sold_page(
                CaptureProcessResult(
                    status="SUCCESS",
                    payload=_hydrate_capture_payload(captured[0]),
                    elapsed_ms=12,
                ),
                x11_sold_state_verified=True,
            )
            self.assertTrue(sold.success)
            self.assertEqual(sold.capture_phase, POST_SOLD_CAPTURE_READY)


class ConcurrentLockedMirrorTests(unittest.TestCase):
    def test_worker_b_succeeds_while_reader_holds_diagnostic(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows lock semantics")
        with tempfile.TemporaryDirectory() as tmp:
            last_dir = Path(tmp) / "post_sold_capture_last"
            last_dir.mkdir()
            # Seed HTML mirror and hold it open (old-style destination).
            legacy = last_dir / "last_capture.html"
            legacy.write_bytes(b"<html>held</html>")
            holder = open(legacy, "rb")  # noqa: SIM115
            stop = threading.Event()

            def hold() -> None:
                while not stop.wait(0.05):
                    pass

            t = threading.Thread(target=hold, daemon=True)
            t.start()
            try:
                worker._LAST_DIR = last_dir  # type: ignore[misc]
                # Force optional HTML mirror on so replace would hit locked file.
                os.environ["CARDSCANR_DIAGNOSTIC_MIRROR_HTML"] = "1"
                auth = Path(tmp) / "jobB" / "attemptB" / "capture.html"
                captured: list[dict] = []
                with mock.patch.object(worker, "_write_protocol_line", side_effect=lambda p: captured.append(dict(p))):
                    code = worker._emit(
                        {
                            "status": "SUCCESS",
                            "html": _fixture_html(),
                            "body_text": "Sold",
                            "elapsed_ms": 1,
                            "canonical_itm_href_count": 1,
                            "candidates": [{"href": "https://www.ebay.com.au/itm/1"}],
                        },
                        artifact_path=str(auth),
                    )
                self.assertEqual(code, 0)
                self.assertTrue(auth.is_file())
                self.assertEqual(captured[0]["status"], "SUCCESS")
                # Mirror HTML replace may warn; capture must still succeed.
                self.assertTrue(captured[0].get("authoritativeArtifact"))
            finally:
                os.environ.pop("CARDSCANR_DIAGNOSTIC_MIRROR_HTML", None)
                stop.set()
                holder.close()
                t.join(timeout=2)


class CollisionSafetyTests(unittest.TestCase):
    def test_reject_overwrite_of_different_authoritative_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "capture.html"
            write_utf8_bytes_atomic(dest, "job-A-content", reject_existing=True)
            with self.assertRaises(FileExistsError):
                write_utf8_bytes_atomic(dest, "job-B-DIFFERENT", reject_existing=True)

    def test_idempotent_same_bytes_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "capture.html"
            a = write_utf8_bytes_atomic(dest, "same", reject_existing=True)
            b = write_utf8_bytes_atomic(dest, "same", reject_existing=True)
            self.assertEqual(a["sha256"], b["sha256"])
            self.assertTrue(b.get("idempotentReuse"))


class HydrateCurrentArtifactOnlyTests(unittest.TestCase):
    def test_refuses_shared_last_capture_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            last = Path(tmp) / "post_sold_capture_last" / "last_capture.html"
            last.parent.mkdir(parents=True)
            last.write_text(_fixture_html(), encoding="utf-8")
            out = _hydrate_capture_payload(
                {
                    "status": "SUCCESS",
                    "html_path": str(last),
                    "canonical_itm_href_count": 1,
                }
            )
            self.assertEqual(out.get("status"), CAPTURE_ARTIFACT_WRITE_FAILURE)
            self.assertNotIn("html", out)

    def test_parser_chain_uses_returned_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            auth = Path(tmp) / "jobX" / "attX" / "capture.html"
            worker._LAST_DIR = Path(tmp) / "diag"  # type: ignore[misc]
            captured: list[dict] = []
            html = _fixture_html(megabytes=0.02)
            with mock.patch.object(worker, "_write_protocol_line", side_effect=lambda p: captured.append(dict(p))):
                code = worker._emit(
                    {
                        "status": "SUCCESS",
                        "html": html,
                        "body_text": "Sold 2 Oct 2026\nTropius\u200c",
                        "elapsed_ms": 3,
                        "canonical_itm_href_count": 1,
                        "candidates": [
                            {
                                "href": "https://www.ebay.com.au/itm/111222333444",
                                "text": "Tropius Pitch Black 1 Sold A$1.00",
                            }
                        ],
                    },
                    artifact_path=str(auth),
                )
            self.assertEqual(code, 0)
            meta = captured[0]
            self.assertEqual(Path(meta["html_path"]), auth)
            self.assertNotIn("post_sold_capture_last", meta["html_path"].replace("\\", "/"))
            hydrated = _hydrate_capture_payload(meta)
            self.assertIn("\u200c", hydrated.get("html") or "")
            sold = capture_process_result_to_sold_page(
                CaptureProcessResult(status="SUCCESS", payload=hydrated, elapsed_ms=3),
                x11_sold_state_verified=True,
            )
            self.assertTrue(sold.success)
            self.assertEqual(sold.capture_phase, POST_SOLD_CAPTURE_READY)
            self.assertEqual(sold.diagnostics.get("html_path"), str(auth))


class FiveMbUnicodeCaptureTests(unittest.TestCase):
    def test_five_mb_unicode_with_locked_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker._LAST_DIR = Path(tmp) / "post_sold_capture_last"  # type: ignore[misc]
            auth = Path(tmp) / "job5" / "att5" / "capture.html"
            html = _fixture_html(megabytes=5.1)
            self.assertGreater(len(html.encode("utf-8")), 5 * 1024 * 1024)
            captured: list[dict] = []
            with mock.patch.object(
                worker,
                "_update_diagnostic_mirror",
                return_value=(False, "PermissionError:[WinError 5] Access is denied"),
            ):
                with mock.patch.object(worker, "_write_protocol_line", side_effect=lambda p: captured.append(dict(p))):
                    code = worker._emit(
                        {
                            "status": "SUCCESS",
                            "html": html,
                            "body_text": "Sold\nTropius\u200c サーナイト 🔥",
                            "elapsed_ms": 40,
                            "canonical_itm_href_count": 1,
                            "candidates": [{"href": "https://www.ebay.com.au/itm/1"}],
                        },
                        artifact_path=str(auth),
                    )
            self.assertEqual(code, 0)
            raw = auth.read_bytes()
            text = raw.decode("utf-8")
            self.assertIn("\u200c", text)
            self.assertIn("サーナイト", text)
            self.assertIn("🔥", text)
            self.assertNotIn("\ufffd", text)
            self.assertEqual(captured[0]["html_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertFalse(captured[0].get("diagnosticMirrorUpdated"))
            sold = capture_process_result_to_sold_page(
                CaptureProcessResult(
                    status="SUCCESS",
                    payload=_hydrate_capture_payload(captured[0]),
                    elapsed_ms=40,
                ),
                x11_sold_state_verified=True,
            )
            self.assertEqual(sold.capture_phase, POST_SOLD_CAPTURE_READY)


class FailureTaxonomyTests(unittest.TestCase):
    def test_authoritative_write_failure_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker._LAST_DIR = Path(tmp) / "diag"  # type: ignore[misc]
            auth = Path(tmp) / "cap.html"

            def boom(path, text, **kwargs):
                raise OSError("simulated_auth_disk_full")

            with mock.patch(
                "cardscanr_market_engine.providers.post_sold_capture_worker.write_utf8_bytes_atomic",
                side_effect=boom,
            ):
                with mock.patch.object(worker, "_write_protocol_line") as proto:
                    code = worker._emit(
                        {
                            "status": "SUCCESS",
                            "html": "<html>x</html>",
                            "body_text": "Sold",
                            "elapsed_ms": 1,
                        },
                        artifact_path=str(auth),
                    )
            self.assertEqual(code, 1)
            self.assertEqual(proto.call_args[0][0]["status"], CAPTURE_ARTIFACT_WRITE_FAILURE)

    def test_taxonomy_classes_not_collapsed(self) -> None:
        cases = [
            (CDP_CONNECT_FAILURE, CDP_CONNECT_FAILURE),
            (CDP_TARGET_NOT_FOUND, CDP_TARGET_NOT_FOUND),
            (CDP_TARGET_FAILURE, CDP_TARGET_FAILURE),
            (CDP_EVALUATION_FAILURE, CDP_EVALUATION_FAILURE),
            (CAPTURE_PROTOCOL_FAILURE, CAPTURE_PROTOCOL_FAILURE),
            (CDP_CAPTURE_PROCESS_TIMEOUT, CDP_CAPTURE_PROCESS_TIMEOUT),
            (CAPTURE_ARTIFACT_WRITE_FAILURE, CAPTURE_ARTIFACT_WRITE_FAILURE),
        ]
        for status, expected in cases:
            sold = capture_process_result_to_sold_page(
                CaptureProcessResult(status=status, payload={"status": status, "error": "x"}, elapsed_ms=1),
                x11_sold_state_verified=True,
            )
            self.assertEqual(sold.failure_class, expected, msg=status)


class FailureClassPropagationTests(unittest.TestCase):
    def test_top_level_capture_failure_class_not_null(self) -> None:
        result = {
            "status": "failed",
            "ownedDailyOutcome": "POST_SOLD_CAPTURE_FAILURE",
            "postSoldCapturePhase": "POST_SOLD_CAPTURE_FAILED",
            "failureClass": CAPTURE_ARTIFACT_WRITE_FAILURE,
            "failureDetail": "PermissionError:[WinError 5] Access is denied",
            "postSoldCapture": {
                "failureClass": CAPTURE_ARTIFACT_WRITE_FAILURE,
                "failureDetail": "PermissionError:[WinError 5] Access is denied",
                "success": False,
            },
            "providerDiagnostics": {
                "failureClass": CAPTURE_ARTIFACT_WRITE_FAILURE,
                "failureDetail": "PermissionError:[WinError 5] Access is denied",
                "postSoldCapturePhase": "POST_SOLD_CAPTURE_FAILED",
            },
        }
        cap = build_capture_evidence(
            result,
            job_id="96bdd0f7-9f8c-44fa-84b4-e46be96e83b5",
            attempt_id="4b5f4b65-51a9-4760-9255-a81893d98004",
            price_key_id="pk-tropius",
        )
        self.assertEqual(cap.get("failureClass"), CAPTURE_ARTIFACT_WRITE_FAILURE)
        self.assertIn("WinError 5", str(cap.get("failureDetail") or ""))


class FreshnessSemanticsTests(unittest.TestCase):
    def test_reference_only_failure_null_verified_freshness(self) -> None:
        write = build_write_evidence(
            {
                "ownedDailyOutcome": "POST_SOLD_CAPTURE_FAILURE",
                "status": "failed",
            },
            before={"price": 0.07, "sourceClass": "REFERENCE", "verifiedLocal": False},
            after={
                "price": 0.07,
                "freshness": "2026-10-02T10:57:44+00:00",
                "sourceClass": "REFERENCE",
                "verifiedLocal": False,
                "displayPriceSource": "reference",
            },
        )
        self.assertIsNone(write.get("verifiedSuccessFreshness"))
        self.assertEqual(write.get("referenceUpdatedAt"), "2026-10-02T10:57:44+00:00")
        self.assertFalse(write.get("verifiedLocalAfter"))

    def test_verified_local_success_exposes_freshness(self) -> None:
        write = build_write_evidence(
            {
                "ownedDailyOutcome": "UPDATED_FROM_EBAY",
                "status": "completed",
                "verifiedSuccessFreshness": "2026-10-03T01:00:00Z",
            },
            before={"price": 0.07, "verifiedLocal": False},
            after={
                "price": 1.00,
                "freshness": "2026-10-03T01:00:00Z",
                "sourceClass": "VERIFIED_LOCAL",
                "verifiedLocal": True,
                "displayPriceSource": "ebay_verified_local",
            },
        )
        self.assertEqual(write.get("verifiedSuccessFreshness"), "2026-10-03T01:00:00Z")


class TropiusOfflineReplayTests(unittest.TestCase):
    def test_corrected_architecture_would_be_ready(self) -> None:
        """Historical Tropius remains FAIL_CAPTURE; corrected path would be READY."""
        with tempfile.TemporaryDirectory() as tmp:
            worker._LAST_DIR = Path(tmp) / "post_sold_capture_last"  # type: ignore[misc]
            auth = (
                Path(tmp)
                / "96bdd0f7-9f8c-44fa-84b4-e46be96e83b5"
                / "4b5f4b65-51a9-4760-9255-a81893d98004"
                / "capture.html"
            )
            os.environ["CARDSCANR_JOB_ID"] = "96bdd0f7-9f8c-44fa-84b4-e46be96e83b5"
            os.environ["CARDSCANR_LIVE_ATTEMPT_ID"] = "4b5f4b65-51a9-4760-9255-a81893d98004"
            os.environ["CARDSCANR_PRICE_KEY_ID"] = "tropius-pk"
            os.environ["CARDSCANR_FINGERPRINT"] = "tropius-fp"
            try:
                captured: list[dict] = []
                with mock.patch.object(
                    worker,
                    "_update_diagnostic_mirror",
                    return_value=(False, "PermissionError:[WinError 5] Access is denied"),
                ):
                    with mock.patch.object(
                        worker, "_write_protocol_line", side_effect=lambda p: captured.append(dict(p))
                    ):
                        code = worker._emit(
                            {
                                "status": "SUCCESS",
                                "html": _fixture_html(megabytes=1.0),
                                "body_text": "Sold items\nTropius\u200c Pitch Black 1",
                                "target_id": "C0F435F2279C4D81D23588CB8A85300D",
                                "elapsed_ms": 100,
                                "canonical_itm_href_count": 1,
                                "candidates": [{"href": "https://www.ebay.com.au/itm/999"}],
                                "capture_method": "cdp_evaluate_outer_html",
                            },
                            artifact_path=str(auth),
                        )
                self.assertEqual(code, 0)
                sold = capture_process_result_to_sold_page(
                    CaptureProcessResult(
                        status="SUCCESS",
                        payload=_hydrate_capture_payload(captured[0]),
                        elapsed_ms=100,
                    ),
                    x11_sold_state_verified=True,
                )
                self.assertEqual(sold.capture_phase, POST_SOLD_CAPTURE_READY)
                self.assertEqual(sold.target_id, "C0F435F2279C4D81D23588CB8A85300D")
                self.assertEqual(captured[0].get("job_id"), "96bdd0f7-9f8c-44fa-84b4-e46be96e83b5")
                self.assertEqual(
                    captured[0].get("attempt_id"), "4b5f4b65-51a9-4760-9255-a81893d98004"
                )
                # Document historical verdict unchanged by this offline proof.
                historical = {
                    "cardVerdict": "FAIL_CAPTURE",
                    "proof": "ROOT_CAUSE_FIXED_OFFLINE",
                    "historyModified": False,
                }
                self.assertEqual(historical["cardVerdict"], "FAIL_CAPTURE")
            finally:
                for k in (
                    "CARDSCANR_JOB_ID",
                    "CARDSCANR_LIVE_ATTEMPT_ID",
                    "CARDSCANR_PRICE_KEY_ID",
                    "CARDSCANR_FINGERPRINT",
                ):
                    os.environ.pop(k, None)


class TempfileOwnershipTests(unittest.TestCase):
    def test_failed_write_cleans_only_own_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "capture.html"
            foreign = dest.with_name(f"{dest.name}.tmp.99999.1")
            foreign.write_bytes(b"foreign")
            with mock.patch("os.replace", side_effect=OSError("deny")):
                with self.assertRaises(OSError):
                    write_utf8_bytes_atomic(dest, "new")
            self.assertTrue(foreign.is_file())
            leftovers = [p for p in Path(tmp).glob("capture.html.tmp.*") if p != foreign]
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
