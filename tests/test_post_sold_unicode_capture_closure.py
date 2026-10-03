"""Unicode-safe post-Sold capture encoding contract + failure taxonomy tests.

LOCAL ONLY — no eBay network contact.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.providers.post_sold_capture import (  # noqa: E402
    CAPTURE_ARTIFACT_WRITE_FAILURE,
    CAPTURE_ENCODING_FAILURE,
    CAPTURE_PROTOCOL_FAILURE,
    CDP_CONNECT_FAILURE,
    CDP_EVALUATION_FAILURE,
    CDP_TARGET_FAILURE,
    CDP_TARGET_NOT_FOUND,
    POST_SOLD_CAPTURE_FAILED,
    POST_SOLD_CAPTURE_READY,
    write_utf8_bytes_atomic,
)
from cardscanr_market_engine.providers.post_sold_capture_process import (  # noqa: E402
    CaptureProcessResult,
    _hydrate_capture_payload,
    capture_process_result_to_sold_page,
    run_capture_worker_process,
)
from cardscanr_market_engine.providers import post_sold_capture_worker as worker  # noqa: E402


class AtomicUtf8ArtifactTests(unittest.TestCase):
    def test_write_utf8_preserves_zwnj_and_sha(self) -> None:
        text = "Ceruledge\u200c Phantasmal — Pokémon 🔥"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cap.html"
            info = write_utf8_bytes_atomic(path, text)
            raw = path.read_bytes()
            self.assertEqual(raw.decode("utf-8"), text)
            self.assertIn("\u200c".encode("utf-8"), raw)
            self.assertEqual(info["sha256"], __import__("hashlib").sha256(raw).hexdigest())
            self.assertEqual(info["encoding"], "utf-8")
            self.assertNotIn(b"\xef\xbf\xbd", raw)  # no U+FFFD

    def test_write_succeeds_when_locale_is_cp1252(self) -> None:
        text = "title\u200c with サーナイト and 🔥"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.html"
            with mock.patch("locale.getpreferredencoding", return_value="cp1252"):
                info = write_utf8_bytes_atomic(path, text)
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_text(encoding="utf-8"), text)
            self.assertGreater(info["byteLength"], 0)

    def test_failed_write_leaves_no_truncated_final(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "final.html"
            dest.write_bytes(b"PREVIOUS_VALID")
            with mock.patch("os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    write_utf8_bytes_atomic(dest, "new\u200ccontent")
            self.assertEqual(dest.read_bytes(), b"PREVIOUS_VALID")
            leftovers = list(Path(tmp).glob("final.html.tmp.*"))
            self.assertEqual(leftovers, [])


class ProtocolUtf8Tests(unittest.TestCase):
    def test_protocol_line_is_ascii_safe_json_bytes(self) -> None:
        payload = {
            "status": "SUCCESS",
            "target_title": "Ceruledge\u200c Pokémon 🔥",
            "failure_detail": "detail with サーナイト",
            "html_path": "C:/tmp/ポケモン/capture.html",
        }
        captured: list[bytes] = []

        class Buf:
            def write(self, data: bytes) -> int:
                captured.append(data)
                return len(data)

            def flush(self) -> None:
                return None

        class FakeStdout:
            buffer = Buf()

            def write(self, data: str) -> int:
                captured.append(data.encode("utf-8"))
                return len(data)

            def flush(self) -> None:
                return None

        with mock.patch.object(worker.sys, "stdout", FakeStdout()):
            worker._write_protocol_line(payload)
        self.assertEqual(len(captured), 1)
        line = captured[0].decode("utf-8")
        parsed = json.loads(line)
        self.assertEqual(parsed["target_title"], payload["target_title"])
        self.assertEqual(parsed["failure_detail"], payload["failure_detail"])
        self.assertEqual(parsed["html_path"], payload["html_path"])
        # ensure_ascii=True => wire bytes are ASCII-only JSON escapes.
        self.assertTrue(all(b < 128 for b in captured[0]))

    def test_emit_does_not_put_html_on_stdout(self) -> None:
        huge = "Ceruledge\u200c" + ("X" * 50_000) + "サーナイト🔥"
        captured: list[bytes] = []

        class Buf:
            def write(self, data: bytes) -> int:
                captured.append(data)
                return len(data)

            def flush(self) -> None:
                return None

        class FakeStdout:
            buffer = Buf()

            def write(self, data: str) -> int:
                captured.append(data.encode("utf-8"))
                return len(data)

            def flush(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            worker._LAST_DIR = Path(tmp) / "diag"  # type: ignore[misc]
            auth = Path(tmp) / "auth" / "capture.html"
            with mock.patch.object(worker.sys, "stdout", FakeStdout()):
                code = worker._emit(
                    {
                        "status": "SUCCESS",
                        "html": huge,
                        "body_text": "Sold 1 Oct 2026\nCeruledge\u200c",
                        "target_id": "T1",
                        "target_title": "Pokémon",
                        "elapsed_ms": 1,
                        "canonical_itm_href_count": 1,
                        "candidates": [{"href": "https://www.ebay.com.au/itm/1"}],
                    },
                    artifact_path=str(auth),
                )
            self.assertEqual(code, 0)
            self.assertEqual(len(captured), 1)
            line = captured[0].decode("utf-8")
            self.assertNotIn(huge[:100], line)
            parsed = json.loads(line)
            self.assertIn("html_path", parsed)
            self.assertIn("html_sha256", parsed)
            self.assertNotIn("html", parsed)
            self.assertEqual(Path(parsed["html_path"]), auth)
            self.assertNotIn("post_sold_capture_last", parsed["html_path"].replace("\\", "/"))
            artifact = Path(parsed["html_path"]).read_text(encoding="utf-8")
            self.assertIn("\u200c", artifact)
            self.assertIn("サーナイト", artifact)
            self.assertIn("🔥", artifact)


class FailureTaxonomyTests(unittest.TestCase):
    def test_connection_refused_is_connect_failure(self) -> None:
        result = run_capture_worker_process(
            cdp_endpoint="http://127.0.0.1:9",
            expected_url="https://www.ebay.com.au/sch/i.html?LH_Sold=1",
            expected_query="x",
            deadline_seconds=2.0,
            socket_timeout=0.4,
        )
        self.assertIn(
            result.status,
            {CDP_CONNECT_FAILURE, "CDP_ENDPOINT_UNAVAILABLE"},
        )
        sold = capture_process_result_to_sold_page(result, x11_sold_state_verified=True)
        self.assertEqual(sold.capture_phase, POST_SOLD_CAPTURE_FAILED)
        self.assertNotEqual(sold.failure_class, CAPTURE_ENCODING_FAILURE)

    def test_no_suitable_target_maps_to_target_failure(self) -> None:
        fake = CaptureProcessResult(
            status=CDP_TARGET_NOT_FOUND,
            payload={"status": CDP_TARGET_NOT_FOUND, "error": "no_target"},
            elapsed_ms=5,
        )
        sold = capture_process_result_to_sold_page(fake, x11_sold_state_verified=True)
        self.assertEqual(sold.failure_class, CDP_TARGET_NOT_FOUND)
        self.assertEqual(sold.capture_phase, POST_SOLD_CAPTURE_FAILED)

    def test_evaluation_failure_class(self) -> None:
        fake = CaptureProcessResult(
            status=CDP_EVALUATION_FAILURE,
            payload={"status": CDP_EVALUATION_FAILURE, "error": "Runtime.evaluate boom"},
            elapsed_ms=5,
        )
        sold = capture_process_result_to_sold_page(fake, x11_sold_state_verified=True)
        self.assertEqual(sold.failure_class, CDP_EVALUATION_FAILURE)

    def test_encoding_failure_not_connect(self) -> None:
        fake = CaptureProcessResult(
            status=CAPTURE_ENCODING_FAILURE,
            payload={
                "status": CAPTURE_ENCODING_FAILURE,
                "error": "UnicodeEncodeError:'charmap' codec can't encode character '\\u200c'",
            },
            elapsed_ms=5,
        )
        sold = capture_process_result_to_sold_page(fake, x11_sold_state_verified=True)
        self.assertEqual(sold.failure_class, CAPTURE_ENCODING_FAILURE)
        self.assertNotEqual(sold.failure_class, CDP_CONNECT_FAILURE)
        self.assertEqual(sold.capture_phase, POST_SOLD_CAPTURE_FAILED)

    def test_artifact_write_failure(self) -> None:
        fake = CaptureProcessResult(
            status=CAPTURE_ARTIFACT_WRITE_FAILURE,
            payload={"status": CAPTURE_ARTIFACT_WRITE_FAILURE, "error": "OSError:disk"},
            elapsed_ms=3,
        )
        sold = capture_process_result_to_sold_page(fake, x11_sold_state_verified=True)
        self.assertEqual(sold.failure_class, CAPTURE_ARTIFACT_WRITE_FAILURE)

    def test_protocol_failure(self) -> None:
        fake = CaptureProcessResult(
            status=CAPTURE_PROTOCOL_FAILURE,
            payload={"status": CAPTURE_PROTOCOL_FAILURE, "error": "empty_worker_output"},
            elapsed_ms=1,
        )
        sold = capture_process_result_to_sold_page(fake, x11_sold_state_verified=True)
        self.assertEqual(sold.failure_class, CAPTURE_PROTOCOL_FAILURE)

    def test_successful_unicode_artifact_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "job1" / "attempt1" / "capture.html"
            body_path = Path(tmp) / "job1" / "attempt1" / "capture_body.txt"
            html = (
                "<html><body>Sold items\nSold 1 Oct 2026\n"
                "Ceruledge\u200c Phantasmal Flames 20 Pokémon サーナイト 🔥\n"
                '<a href="https://www.ebay.com.au/itm/123">x</a></body></html>'
            )
            write_utf8_bytes_atomic(html_path, html)
            write_utf8_bytes_atomic(body_path, "Sold 1 Oct 2026\nCeruledge\u200c")
            payload = _hydrate_capture_payload(
                {
                    "status": "SUCCESS",
                    "html_path": str(html_path),
                    "body_path": str(body_path),
                    "canonical_itm_href_count": 1,
                    "candidates": [{"href": "https://www.ebay.com.au/itm/123"}],
                    "target_url": "https://www.ebay.com.au/sch/i.html?LH_Sold=1",
                    "target_title": "Ceruledge\u200c Pokémon",
                    "capture_method": "cdp_evaluate_outer_html",
                    "elapsed_ms": 12,
                }
            )
            fake = CaptureProcessResult(status="SUCCESS", payload=payload, elapsed_ms=12)
            sold = capture_process_result_to_sold_page(fake, x11_sold_state_verified=True)
            self.assertTrue(sold.success)
            self.assertEqual(sold.capture_phase, POST_SOLD_CAPTURE_READY)
            self.assertIn("\u200c", sold.html_or_text or "")

    def test_deliberate_encoding_failure_in_emit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker._LAST_DIR = Path(tmp) / "diag"  # type: ignore[attr-defined]
            auth = Path(tmp) / "capture.html"

            def boom(*_a, **_k):
                raise UnicodeEncodeError("charmap", "\u200c", 0, 1, "undefined")

            with mock.patch(
                "cardscanr_market_engine.providers.post_sold_capture_worker.write_utf8_bytes_atomic",
                side_effect=boom,
            ):
                with mock.patch.object(worker, "_write_protocol_line") as proto:
                    code = worker._emit(
                        {
                            "status": "SUCCESS",
                            "html": "<html>Ceruledge\u200c</html>",
                            "body_text": "Sold",
                            "elapsed_ms": 1,
                        },
                        artifact_path=str(auth),
                    )
            self.assertEqual(code, 1)
            self.assertTrue(proto.called)
            status = proto.call_args[0][0]["status"]
            self.assertEqual(status, CAPTURE_ENCODING_FAILURE)

    def test_deliberate_disk_failure_in_emit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker._LAST_DIR = Path(tmp) / "diag"  # type: ignore[attr-defined]
            auth = Path(tmp) / "capture.html"

            def boom(*_a, **_k):
                raise OSError("simulated_disk_full")

            with mock.patch(
                "cardscanr_market_engine.providers.post_sold_capture_worker.write_utf8_bytes_atomic",
                side_effect=boom,
            ):
                with mock.patch.object(worker, "_write_protocol_line") as proto:
                    code = worker._emit(
                        {
                            "status": "SUCCESS",
                            "html": "<html>ok</html>",
                            "body_text": "Sold",
                            "elapsed_ms": 1,
                        },
                        artifact_path=str(auth),
                    )
            self.assertEqual(code, 1)
            self.assertEqual(proto.call_args[0][0]["status"], CAPTURE_ARTIFACT_WRITE_FAILURE)

    def test_target_failure_constant_available(self) -> None:
        self.assertEqual(CDP_TARGET_FAILURE, "CDP_TARGET_FAILURE")


class ControlPlaneCaptureSemanticsTests(unittest.TestCase):
    def test_encoding_failure_keeps_fail_closed_phase(self) -> None:
        fake = CaptureProcessResult(
            status=CAPTURE_ENCODING_FAILURE,
            payload={"status": CAPTURE_ENCODING_FAILURE, "error": "UnicodeEncodeError"},
            elapsed_ms=1,
        )
        sold = capture_process_result_to_sold_page(fake, x11_sold_state_verified=True)
        self.assertEqual(sold.failure_class, CAPTURE_ENCODING_FAILURE)
        self.assertEqual(sold.capture_phase, POST_SOLD_CAPTURE_FAILED)
        self.assertFalse(sold.success)
        self.assertNotEqual(sold.failure_class, CDP_CONNECT_FAILURE)


if __name__ == "__main__":
    unittest.main()
