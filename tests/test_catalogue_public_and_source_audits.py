"""Offline proof that catalogue audits cannot change production or trust fuzzy IDs."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.audit_source_ids_readonly import checked_rows, open_readonly, audit, classify
from tools.audit_public_catalogue_pointers import URLS, audit as public_audit, summarize


def seed(tmp: Path):
    db_path = tmp / "worldwide-catalogue.sqlite"
    with sqlite3.connect(db_path) as db:
        db.executescript("""
            CREATE TABLE card_set(id TEXT, provider_id TEXT);
            CREATE TABLE set_release(id TEXT, card_set_id TEXT, release_code TEXT, language_code TEXT);
            CREATE TABLE source_record(id TEXT, provider_id TEXT, provider_record_id TEXT,
                                       record_type TEXT, source_path TEXT);
            CREATE TABLE card_printing(id TEXT, set_release_id TEXT,
                                       source_record_id TEXT, collector_number TEXT);
            INSERT INTO card_set VALUES ('s1', 'tcgdex-cards-database');
            INSERT INTO set_release VALUES ('r1','s1','PMCG1','ja');
            INSERT INTO source_record VALUES ('src1','tcgdex-cards-database',
                'archive/v2/PMCG1-001', 'card', 'cards/PMCG1-001.json');
            INSERT INTO card_printing VALUES ('printing1','r1','src1','001');
        """)
    row = {"source_language": "ja", "source_card_id": "PMCG1-001",
           "source_set_id": "PMCG1", "local_id": "001", "existing_printing_id": "printing1",
           "canonical_write_approved": False}
    raw = (json.dumps(row) + "\n").encode()
    report = {"input_rows": 1, "result_sha256": hashlib.sha256(raw).hexdigest()}
    (tmp / "reconciliation.jsonl").write_bytes(raw)
    (tmp / "report.json").write_text(json.dumps(report))
    return db_path, row


class ProviderIdTests(unittest.TestCase):
    def test_read_only_db_and_unconfirmed_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database, row = seed(root)
            data_before = hashlib.sha256(database.read_bytes()).hexdigest()
            rows = checked_rows(root / "report.json", root / "reconciliation.jsonl")
            with open_readonly(database) as db:
                report = audit(rows, db)
            self.assertEqual(report["classifications"],
                             {"provider_path_suffix_candidate_not_confirmed": 1})
            self.assertEqual(report["aliases_automatically_approved"], 0)
            self.assertEqual(report["database_writes"], 0)
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), data_before)
            self.assertFalse(Path(str(database) + "-wal").exists())

    def test_exact_vs_unresolved_are_distinct(self):
        self.assertEqual(classify("PMCG1-001", "PMCG1-001"), "provider_id_exact")
        self.assertEqual(classify("legacy_PMCG1_001", "PMCG1-001"),
                         "provider_id_format_or_identity_unresolved")

    def test_tampered_input_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed(root)
            (root / "reconciliation.jsonl").write_text('{"changed":true}\n')
            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                checked_rows(root / "report.json", root / "reconciliation.jsonl")

    def test_nonzero_wal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database, row = seed(root)
            Path(str(database) + "-wal").write_bytes(b"active")
            with self.assertRaisesRegex(ValueError, "active SQLite WAL"):
                open_readonly(database)


class PublicPointerTests(unittest.TestCase):
    def test_two_fixed_pointer_receipts(self):
        def get(url):
            if url == URLS["packs"]:
                payload = {"catalogueReleaseId": "sample2026", "packs": [
                    {"kind": "language", "packId": "ko", "languages": ["ko"], "recordCount": 239},
                    {"kind": "core", "packId": "core"}], "optionalPackIds": ["ko"]}
            else:
                payload = {"schemaVersion": "2.1", "databaseSha256": "a"*64}
            return payload, json.dumps(payload).encode()
        out = public_audit(get)
        self.assertEqual(out["classification"], "PASS")
        self.assertEqual(out["checks"]["packs"]["language_packs"][0]["records"], 239)
        self.assertEqual(out["production_writes"], 0)
        self.assertFalse(out["app_install_verified"])

    def test_manifest_failure_partial_not_success(self):
        def failing(url):
            raise OSError("missing")
        out = public_audit(failing)
        self.assertEqual(out["classification"], "PARTIAL")
        self.assertEqual(len(out["errors"]), 2)

    def test_invalid_pack_shape_fails(self):
        with self.assertRaisesRegex(ValueError, "no_packs"):
            summarize("packs", {"packs": 99}, b"{}")


if __name__ == "__main__":
    unittest.main()
