"""Fixture-only tests for exact-source read-only catalogue reconciliation."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.reconcile_tcgdex_metadata_batch import read_candidates, reconcile
from tools.probe_korean_tcgdex import API, probe, sample_set_ids


def row(card_id="set1-001", language="ja", local="001"):
    return {
        "source_provider": "tcgdex", "source_language": language,
        "source_set_id": "set1", "source_card_id": card_id,
        "local_id": local, "source_response_sha256": "a" * 64,
        "artwork_downloaded": False, "production_published": False
    }


def make_zip(root: Path, rows: list[dict], *, tamper: bool = False) -> Path:
    raw = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
    manifest = {
        "provider": "tcgdex", "app_release_allowed": False,
        "candidate_sha256": "b" * 64 if tamper else hashlib.sha256(raw).hexdigest(),
        "candidate_bytes": len(raw), "candidate_cards": len(rows),
        "languages_requested": ["ja", "ko"]
    }
    target = root / "metadata.zip"
    with zipfile.ZipFile(target, "w") as z:
        z.writestr("card_candidates.jsonl", raw)
        z.writestr("manifest.json", json.dumps(manifest))
    return target


def fixture_db(path: Path):
    with sqlite3.connect(path) as db:
        db.executescript("""
          CREATE TABLE card_set(id TEXT, provider_id TEXT);
          CREATE TABLE set_release(id TEXT, card_set_id TEXT, language_code TEXT,
                                   region_code TEXT, release_code TEXT);
          CREATE TABLE source_record(id TEXT, provider_id TEXT, provider_record_id TEXT);
          CREATE TABLE card_printing(id TEXT, set_release_id TEXT,
                                     source_record_id TEXT, collector_number TEXT);
          CREATE TABLE card_variant(id TEXT, card_printing_id TEXT);
          INSERT INTO card_set VALUES('s1','tcgdex-cards-database');
          INSERT INTO set_release VALUES('sr1','s1','ja','JP','set1');
          INSERT INTO source_record VALUES('source1','tcgdex-cards-database','ja/cards/set1-001');
          INSERT INTO card_printing VALUES('print-001','sr1','source1','001');
          INSERT INTO card_variant VALUES('v1','print-001');
          INSERT INTO card_variant VALUES('v2','print-001');
        """)


class ReconciliationTests(unittest.TestCase):
    def test_archive_integrity_and_missing_db_classification(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            archive = make_zip(p, [row(), row("set1-002", local="002")])
            report = reconcile(archive, None, p / "out")
            self.assertEqual(report["results"], {"requires_staging_snapshot": 2})
            self.assertFalse(report["staging_snapshot_provided"])
            self.assertEqual(report["canonical_database_writes"], 0)
            with self.assertRaises(FileExistsError):
                reconcile(archive, None, p / "out")

    def test_exact_match_does_not_verify_variant_and_preserves_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            database = p / "worldwide.sqlite"
            fixture_db(database)
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            report = reconcile(make_zip(p, [row(), row("set1-002", local="002")]),
                               database, p / "out")
            self.assertEqual(report["results"]["represented_exact_set_language_number"], 1)
            self.assertEqual(report["provider_card_id_confirmed_matches"], 1)
            self.assertEqual(report["provider_card_id_unconfirmed_matches"], 0)
            self.assertEqual(report["existing_printings_with_multiple_variants"], 1)
            self.assertEqual(report["results"]["not_found_by_exact_provider_set_language_number"], 1)
            found = [json.loads(s) for s in (p / "out/reconciliation.jsonl").read_text().splitlines()]
            self.assertEqual(found[0]["existing_printing_id"], "print-001")
            self.assertEqual(found[0]["existing_variant_count"], 2)
            self.assertTrue(found[0]["source_card_id_provenance_confirmed"])
            self.assertFalse(found[0]["variant_identity_verified"])
            self.assertFalse(found[0]["canonical_write_approved"])
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)
            self.assertFalse((p / "worldwide.sqlite-wal").exists())
            self.assertFalse((p / "worldwide.sqlite-shm").exists())

    def test_set_and_number_match_with_wrong_provider_card_id_is_not_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            database = p / "worldwide.sqlite"
            fixture_db(database)
            with sqlite3.connect(database) as db:
                db.execute("UPDATE source_record SET provider_record_id='unrelated-provider-id'")
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            report = reconcile(make_zip(p, [row()]), database, p / "out")
            self.assertEqual(report["results"],
                             {"set_language_number_match_provider_card_id_unconfirmed": 1})
            self.assertEqual(report["provider_card_id_unconfirmed_matches"], 1)
            self.assertEqual(report["provider_card_id_confirmed_matches"], 0)
            self.assertEqual(report["existing_printings_with_multiple_variants"], 1)
            self.assertEqual(report["canonical_database_writes"], 0)
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)

    def test_ambiguous_different_regions_is_not_silently_matched(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            database = p / "worldwide.sqlite"
            fixture_db(database)
            with sqlite3.connect(database) as db:
                db.executescript("""
                  INSERT INTO set_release VALUES('sr2','s1','ja','INTL','set1');
                  INSERT INTO card_printing VALUES('print2','sr2','source1','001');
                """)
            result = reconcile(make_zip(p, [row()]), database, p / "out")
            self.assertEqual(result["results"], {"ambiguous_multiple_printings": 1})

    def test_tampered_hash_and_duplicate_provider_keys_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                read_candidates(make_zip(p, [row()], tamper=True))
            with self.assertRaisesRegex(ValueError, "duplicate provider"):
                read_candidates(make_zip(p, [row(), row()]))

    def test_zip_extra_member_and_publication_marker_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            archive = make_zip(p, [row()])
            with zipfile.ZipFile(archive, "a") as z:
                z.writestr("../unsafe", "bad")
            with self.assertRaisesRegex(ValueError, "unexpected archive members"):
                read_candidates(archive)
            bad = row()
            bad["artwork_downloaded"] = True
            with self.assertRaisesRegex(ValueError, "unsafe publication marker"):
                read_candidates(make_zip(p, [bad]))

    def test_live_wal_requires_checkpointed_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            database = p / "worldwide.sqlite"
            fixture_db(database)
            (p / "worldwide.sqlite-wal").write_bytes(b"active")
            with self.assertRaisesRegex(ValueError, "live WAL"):
                reconcile(make_zip(p, [row()]), database, p / "out")


class KoreanCoverageTests(unittest.TestCase):
    @staticmethod
    def fake(url):
        if url == API + "/ko/sets":
            payload = [{"id": "SV6", "cardCount": {"total": 0}},
                       {"id": "populated", "cardCount": {"total": 30}},
                       {"id": "sv5K"}]
        elif url == API + "/ko/cards":
            payload = [{"id": "populated-001"}, {"id": "populated-002"}]
        elif url.endswith("populated"):
            payload = {"id": "populated", "cards": [{"id": "populated-001"}]}
        else:
            payload = {"id": url.rsplit("/", 1)[-1], "cards": []}
        return payload, json.dumps(payload).encode("utf-8")

    def test_prioritize_populated_set_and_cross_check_card_index(self):
        res = probe(samples=2, get=self.fake, sleep_seconds=0)
        self.assertEqual(res["card_index_total"], 2)
        self.assertEqual(res["sampled_sets_with_cards"], 1)
        self.assertEqual(res["set_details"][0]["set_id"], "populated")
        self.assertEqual(res["interpretation"], "provider_has_card_data")
        self.assertEqual(res["artwork_downloaded"], 0)

    def test_empty_provider_does_not_claim_complete_coverage(self):
        def empty(url):
            d = [] if url.endswith("/sets") or url.endswith("/cards") else {}
            return d, json.dumps(d).encode()
        res = probe(samples=3, get=empty, sleep_seconds=0)
        self.assertEqual(res["interpretation"], "provider_coverage_incomplete_or_empty")
        self.assertEqual(res["sampled_sets"], 0)

    def test_input_budget_guard(self):
        self.assertEqual(sample_set_ids([{"id": "A"}, {"id": "B"}], 2), ["A", "B"])
        with self.assertRaises(ValueError):
            probe(samples=13, get=self.fake, sleep_seconds=0)


if __name__ == "__main__":
    unittest.main()
