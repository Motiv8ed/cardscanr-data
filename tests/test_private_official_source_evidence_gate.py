"""Regression tests for the offline TH/ID evidence gate. No network or vault writes."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "tools/private_official_source_evidence_gate.py"
spec = importlib.util.spec_from_file_location("official_gate", MODULE)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def source_row(ident="123", batch="new-batch"):
    return {
        "language": "th", "official_detail_id": ident,
        "official_detail_url": f"https://asia.pokemon-card.com/th/card-search/detail/{ident}/",
        "source_image_url_reference": f"https://asia.pokemon-card.com/th/card-img/th{ident.zfill(8)}.png",
        "source_response_sha256": "a" * 64,
        "source_name_native": "ชื่อ", "source_collector_number": "001/100",
        "batch_id": batch,
    }


class EvidenceGateTests(unittest.TestCase):
    def audit_lines(self, lines, completed=()):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.jsonl"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return gate.audit([path], set(completed))

    def test_valid_row_remains_offline_only(self):
        result = self.audit_lines([json.dumps(source_row(), ensure_ascii=False)])
        self.assertEqual(result["unique_evidence_candidates"], 1)
        self.assertIsNone(result["live_new_links"])
        self.assertFalse(result["artwork_rights_verified"])

    def test_malformed_json_and_nonobjects_quarantined(self):
        result = self.audit_lines(["{", "[]", "null", '"string"', "17"])
        self.assertEqual(result["unique_evidence_candidates"], 0)
        self.assertEqual(result["quarantined"], 5)
        self.assertEqual({r["reason"] for r in result["quarantine"]}, {"invalid_json_or_nonobject_row"})

    def test_missing_batch_id_fails_closed(self):
        row = source_row()
        del row["batch_id"]
        result = self.audit_lines([json.dumps(row)])
        self.assertEqual(result["quarantine"][0]["reason"], "missing_batch_id_replay_evidence")

    def test_completed_batch_replay_quarantined(self):
        result = self.audit_lines([json.dumps(source_row())], completed={"new-batch"})
        self.assertEqual(result["quarantine"][0]["reason"], "completed_batch_replay")

    def test_duplicate_identity_quarantines_both(self):
        row = json.dumps(source_row())
        result = self.audit_lines([row, row])
        self.assertEqual(result["quarantined"], 2)
        self.assertEqual(result["unique_evidence_candidates"], 0)

    def test_nonmatching_image_variant_quarantined(self):
        row = source_row()
        row["source_image_url_reference"] = "https://asia.pokemon-card.com/id/card-img/id00000123.png"
        result = self.audit_lines([json.dumps(row)])
        self.assertEqual(result["quarantine"][0]["reason"], "image_identity_url_mismatch")


if __name__ == "__main__":
    unittest.main()
