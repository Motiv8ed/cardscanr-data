"""P3: provenance-aware adapter and hydration guards."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cardscanr_market_engine.providers.capture_adapters import (
    CaptureProvenanceEnvelope,
    ProvenanceError,
    build_page_snapshot_from_adapter_payload,
    hydrate_capture_payload_strict,
    network_disabled,
    validate_artifact_file,
)
from cardscanr_market_engine.providers.capture_decision import classify_snapshot

ROOT = Path(__file__).resolve().parents[1]
INERT_HTML = ROOT / "tests/fixtures/ebay_worker_capture/inert/au_sold_minimal.html"


class ProvenanceAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["CARDSCANR_NETWORK_DISABLED"] = "1"

    def test_network_disabled_flag(self) -> None:
        self.assertTrue(network_disabled())

    def test_forged_hash_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body.txt"
            path.write_text("hello", encoding="utf-8")
            with self.assertRaises(ProvenanceError):
                validate_artifact_file(path, expected_sha256="0" * 64, artifact_root=tmp, max_bytes=1000)

    def test_symlink_escape_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "secret.txt"
            secret.write_text("x", encoding="utf-8")
            link = root / "link.txt"
            link.symlink_to(secret)
            with self.assertRaises(ProvenanceError):
                validate_artifact_file(link, expected_sha256=None, artifact_root=root, max_bytes=1000)

    def test_cdp_inert_fixture_builds_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            html = INERT_HTML.read_bytes()
            dest = root / "job-1" / "attempt-1" / "capture.html"
            dest.parent.mkdir(parents=True)
            dest.write_bytes(html)
            import hashlib

            digest = hashlib.sha256(html).hexdigest()
            now = datetime.now(timezone.utc)
            envelope = CaptureProvenanceEnvelope(
                job_id="job-1",
                attempt_id="attempt-1",
                price_key_id="pk-1",
                worker_id="worker-1",
                lease_id="lease-1",
                lease_expiry=now + timedelta(hours=1),
                target_id="t1",
                document_id="d1",
                navigation_generation=2,
                expected_job_id="job-1",
                expected_attempt_id="attempt-1",
                artifact_root=str(root),
                adapter_id="network_disabled_inert_fixture",
            )
            payload = {
                "html_path": str(dest),
                "html_sha256": digest,
                "body_path": str(dest),
                "body_sha256": digest,
                "query_fingerprint": "f" * 64,
                "target_url": "https://www.ebay.com.au/sch/i.html?_nkw=test&LH_Sold=1&LH_Complete=1",
                "facts": [
                    {
                        "kind": "complete_current_sold_document",
                        "state": "TRUE",
                        "evidence_id": "inert:complete",
                        "assurance": "QUALIFIED_CURRENT",
                        "source_origin": "CURRENT_CAPTURE",
                    }
                ],
            }
            snapshot = build_page_snapshot_from_adapter_payload(
                payload, envelope=envelope, artifact_root=root
            )
            decision = classify_snapshot(snapshot)
            self.assertTrue(decision.may_parse_price)

    def test_html_source_adapter_not_price_eligible_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = root / "source.html"
            body.write_text("<html><script>auth</script></html>", encoding="utf-8")
            import hashlib

            digest = hashlib.sha256(body.read_bytes()).hexdigest()
            now = datetime.now(timezone.utc)
            envelope = CaptureProvenanceEnvelope(
                job_id="job-2",
                attempt_id="attempt-2",
                price_key_id="pk-2",
                worker_id="worker-1",
                lease_id="lease-1",
                lease_expiry=now + timedelta(hours=1),
                target_id="t1",
                document_id="d1",
                navigation_generation=1,
                expected_job_id="job-2",
                expected_attempt_id="attempt-2",
                artifact_root=str(root),
                adapter_id="html_source_inert",
            )
            snap = build_page_snapshot_from_adapter_payload(
                {
                    "html_path": str(body),
                    "html_sha256": digest,
                    "body_path": str(body),
                    "body_sha256": digest,
                    "query_fingerprint": "a" * 64,
                    "facts": [
                        {
                            "kind": "complete_current_sold_document",
                            "state": "TRUE",
                            "evidence_id": "x",
                            "assurance": "QUALIFIED_CURRENT",
                            "source_origin": "RAW_SOURCE",
                        }
                    ],
                },
                envelope=envelope,
                artifact_root=root,
            )
            decision = classify_snapshot(snap)
            self.assertFalse(decision.may_parse_price)

    def test_hydration_rejects_job_mismatch(self) -> None:
        out = hydrate_capture_payload_strict(
            {"status": "SUCCESS", "job_id": "a", "attempt_id": "1", "html_path": "x"},
            expected_job_id="b",
            expected_attempt_id="1",
            artifact_root="/tmp",
        )
        self.assertEqual(out.get("error"), "job_or_attempt_mismatch")


if __name__ == "__main__":
    unittest.main()
