"""Offline canary pack gzip and SQLite structural checks."""
from __future__ import annotations
import gzip
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.preflight_legacy_korean_pack import ACTIVE, CANARY, get, run, unpack


def make_pack(pack_id: str, records: int):
    with tempfile.TemporaryDirectory() as t:
        p=Path(t)/"pack.sqlite"
        with sqlite3.connect(p) as db:
            db.executescript("""
              CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
              CREATE TABLE sets(id TEXT PRIMARY KEY,language TEXT);
              CREATE TABLE cards(id TEXT PRIMARY KEY,language TEXT);
              INSERT INTO meta VALUES('schema_version','2.1.0');
              INSERT INTO meta VALUES('packSchemaVersion','2.2.0');
              INSERT INTO sets VALUES('test','ko');
            """)
            for i in range(records):
                db.execute("INSERT INTO cards VALUES(?,?)",(f"id{i}", "ko"))
        raw=p.read_bytes()
    zipped=gzip.compress(raw)
    pack={"packId":pack_id, "recordCount":records, "rawSqliteBytes":len(raw),
          "sha256":hashlib.sha256(raw).hexdigest(),
          "gzipSha256":hashlib.sha256(zipped).hexdigest(),
          "compressedDatabaseUrl":f"https://assets.cardscanr.com/test/{pack_id}.gz"}
    return pack,zipped


class LegacyKoreanPackTests(unittest.TestCase):
    def test_structural_probe_passes_not_auto_eligible(self):
        core, core_zip=make_pack("core",0)
        ko, ko_zip=make_pack("ko",3)
        def fake(url, limit):
            if url==ACTIVE:
                return json.dumps({"catalogueReleaseId":"active", "packs":[core]}).encode()
            if url==CANARY:
                return json.dumps({"catalogueReleaseId":"old", "packs":[ko]}).encode()
            if url.endswith("core.gz"):
                return core_zip
            if url.endswith("ko.gz"):
                return ko_zip
            raise AssertionError(url)
        report=run(fetch=fake)
        self.assertTrue(report["structural_preconditions_passed"])
        self.assertEqual(report["packs"][1]["cards"],3)
        self.assertFalse(report["safe_to_change_production_pointer"])
        self.assertFalse(report["cross_release_content_and_canonical_id_compatibility_verified"])
        self.assertEqual(report["production_writes"],0)

    def test_corrupt_gzip_hash_rejected(self):
        pack, compressed=make_pack("ko",2)
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaisesRegex(ValueError,"gzip_sha256_mismatch"):
                unpack(pack, compressed+b"tamper", Path(t)/"pack.sqlite")

    def test_invalid_url_blocked_before_network(self):
        with self.assertRaisesRegex(ValueError, "unapproved_url"):
            get("https://evil.example.com/my.db",2048)


if __name__=="__main__":
    unittest.main()
