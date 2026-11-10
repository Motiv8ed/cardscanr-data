"""P1: sealed fresh independent adversarial matrix (96 cases, metadata-first)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AdversarialMatrixSealTests(unittest.TestCase):
    def test_ninety_six_categories_present(self) -> None:
        data = json.loads(
            (ROOT / "tests/fixtures/ebay_worker_capture/fresh_independent_cases.json").read_text(encoding="utf-8")
        )
        from collections import Counter

        counts = Counter(c["category"] for c in data["cases"])
        self.assertEqual(sum(counts.values()), 96)
        for key in (
            "auth_composition",
            "visibility_source_html",
            "binding_replay_race",
            "precedence_locale_empty",
        ):
            self.assertEqual(counts[key], 24)

    def test_matrix_references_policy_not_waivers(self) -> None:
        manifest = json.loads((ROOT / "tests/fixtures/ebay_worker_capture/manifest.json").read_text())
        self.assertEqual(manifest["fresh_independent_case_target"], 96)
        disputed = json.loads((ROOT / "docs/ebay_worker_disputed_controls.json").read_text())
        self.assertEqual(disputed["count"], 9)


if __name__ == "__main__":
    unittest.main()
