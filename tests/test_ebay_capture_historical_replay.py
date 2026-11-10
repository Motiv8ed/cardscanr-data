"""P1: freeze historical/corpus identity manifests (metadata oracle)."""
from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
P0 = ROOT / "docs/ebay_r6_p0/EBAY_R6_P0_20261110"


class HistoricalOracleTests(unittest.TestCase):
    def test_historical_case_count_249(self) -> None:
        data = json.loads((P0 / "HISTORICAL_CASE_MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data), 249)
        ids = {row["historical_case_id"] for row in data}
        self.assertEqual(len(ids), 249)

    def test_group_counts(self) -> None:
        data = json.loads((P0 / "HISTORICAL_CASE_MANIFEST.json").read_text(encoding="utf-8"))
        from collections import Counter

        groups = Counter(row.get("group_count") for row in data)
        expected = {5: 5, 38: 38, 86: 86, 31: 31, 33: 33, 56: 56}
        for key, want in expected.items():
            self.assertEqual(groups.get(key, 0), want, msg=f"group {key}")

    def test_corpus_manifest_873(self) -> None:
        corpus = json.loads((P0 / "CLASSIFIER_CORPUS_MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(corpus["count"], 873)
        self.assertEqual(len(corpus["cases"]), 873)

    def test_nine_disputed_controls_documented(self) -> None:
        disputed = json.loads((ROOT / "docs/ebay_worker_disputed_controls.json").read_text(encoding="utf-8"))
        self.assertEqual(disputed["count"], 9)
        for row in disputed["controls"]:
            self.assertIn("old_assertion", row)
            self.assertIn("receipt_sha256", row)
            self.assertIn("explicit_r6_disposition", row)
            self.assertIn("no_waiver", row["explicit_r6_disposition"].lower())

    def test_fixture_manifest_blockers(self) -> None:
        manifest = json.loads((ROOT / "tests/fixtures/ebay_worker_capture/manifest.json").read_text())
        self.assertEqual(len(manifest["handoff_fixture_blockers"]), 3)


class HandoffHarnessTests(unittest.TestCase):
    def test_handoff_test_module_imports(self) -> None:
        import tests.test_inter_card_provider_handoff_closure as mod

        self.assertTrue(hasattr(mod, "resolve_handoff_fixture_root"))

    def test_sealed_fresh_case_manifest_sha(self) -> None:
        path = ROOT / "tests/fixtures/ebay_worker_capture/fresh_independent_cases.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(meta["count"], 96)
        body = {k: meta[k] for k in meta if k != "seal_sha256"}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
        self.assertEqual(meta["seal_sha256"], digest)


if __name__ == "__main__":
    unittest.main()
