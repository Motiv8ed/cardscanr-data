"""Synthetic, offline cross-release pack preflight fixtures."""
import gzip
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.preflight_multilingual_crossrelease import ACTIVE_PACKS, ARCHIVE_PACKS, ACTIVE, CANARY, analyze, run


def make_pack(name, *, collide=False, orphan=False, tamper=False, crosspack=False):
    with tempfile.TemporaryDirectory() as work:
        db_path=Path(work)/"file.sqlite"
        with sqlite3.connect(db_path) as db:
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE sets(set_id TEXT PRIMARY KEY);
                CREATE TABLE card_aliases(canonical_base_id TEXT, normalized_alias TEXT);
                CREATE TABLE cards(
                    canonical_base_id TEXT UNIQUE,
                    canonical_printing_id TEXT PRIMARY KEY,
                    set_id TEXT,
                    language TEXT,
                    mirror_permission_status TEXT
                );
                CREATE VIRTUAL TABLE cards_fts USING fts5(canonical_base_id);
                INSERT INTO meta VALUES ('schema_version','2.1.0');
                INSERT INTO meta VALUES ('packSchemaVersion','2.2.0');
            """)
            if name=="core" and crosspack:
                db.execute("INSERT INTO sets VALUES (?)",("shared_set",))
            if name!="core":
                sid=("shared_set" if crosspack and name=="ko" else
                     "missing" if orphan else name+"_set")
                db.execute("INSERT INTO sets VALUES (?)",(name+"_set",))
                key="shared" if collide else name+"_id"
                db.execute("INSERT INTO cards VALUES (?,?,?,?,?)",
                           (key,key,sid,name,None))
                db.execute("INSERT INTO cards_fts VALUES (?)",(key,))
        raw=db_path.read_bytes()
    packed=gzip.compress(raw)
    return ({
        "packId":name,
        "rawSqliteBytes":len(raw),
        "recordCount":0 if name=="core" else 1,
        "sha256":hashlib.sha256(raw).hexdigest(),
        "gzipSha256":hashlib.sha256(packed).hexdigest(),
        "compressedDatabaseUrl":f"https://assets.cardscanr.com/test/{name}.sqlite.gz",
    }, packed+b"wrong" if tamper else packed)


def fixtures(*,colliding=False,orphan=False,tamper=False,crosspack=False):
    datasets={n:make_pack(n,collide=colliding and n in ("en","ko"),
                          orphan=orphan and n=="ko",tamper=tamper and n=="ko",crosspack=crosspack)
              for n in ACTIVE_PACKS+ARCHIVE_PACKS}
    active={"catalogueReleaseId":"live","sourceDatabaseSha256":"a",
            "packs":[datasets[n][0] for n in ACTIVE_PACKS]}
    archive={"catalogueReleaseId":"canary4","sourceDatabaseSha256":"b",
             "packs":[datasets[n][0] for n in ARCHIVE_PACKS]}
    files={v[0]["compressedDatabaseUrl"]:v[1] for v in datasets.values()}
    def fetch(url,limit):
        if url==ACTIVE: return json.dumps(active).encode()
        if url==CANARY: return json.dumps(archive).encode()
        return files[url]
    return active,archive,fetch


class Tests(unittest.TestCase):
    def test_full_eight_pack_checks_do_not_grant_publication(self):
        a,b,fetch=fixtures()
        result=analyze(a,b,fetch=fetch)
        self.assertEqual(len(result["packs"]),8)
        self.assertTrue(result["structural_preconditions_passed"])
        self.assertEqual(result["canonical_id_collisions_found"],0)
        self.assertFalse(result["safe_to_publish_mixed_manifest"])
        self.assertFalse(result["artwork_rights_verified"])
        self.assertEqual(result["image_files_downloaded"],0)
        self.assertEqual(result["production_writes"],0)

    def test_cross_pack_canonical_collision_blocks(self):
        a,b,fetch=fixtures(colliding=True)
        result=analyze(a,b,fetch=fetch)
        self.assertFalse(result["structural_preconditions_passed"])
        self.assertGreater(result["canonical_id_collisions_found"],0)

    def test_missing_local_set_reference_blocks(self):
        a,b,fetch=fixtures(orphan=True)
        result=analyze(a,b,fetch=fetch)
        self.assertFalse(result["all_cross_pack_set_references_present"])
        self.assertFalse(result["structural_preconditions_passed"])

    def test_reference_supplied_by_core_is_not_false_orphan(self):
        a,b,fetch=fixtures(crosspack=True)
        result=analyze(a,b,fetch=fetch)
        self.assertTrue(result["all_cross_pack_set_references_present"])
        ko=next(p for p in result["packs"] if p["pack_id"]=="ko")
        self.assertEqual(ko["unresolved_set_references_within_pack"],1)
        self.assertEqual(ko["unresolved_set_references_across_all_selected_packs"],0)
        self.assertTrue(result["structural_preconditions_passed"])

    def test_hash_mismatch_rejected(self):
        a,b,fetch=fixtures(tamper=True)
        with self.assertRaisesRegex(ValueError,"gzip_sha256_mismatch"):
            analyze(a,b,fetch=fetch)

    def test_required_language_pack_missing(self):
        a,b,fetch=fixtures()
        b["packs"]=[x for x in b["packs"] if x["packId"]!="ko"]
        with self.assertRaisesRegex(ValueError,"required_pack"):
            analyze(a,b,fetch=fetch)


if __name__=="__main__":
    unittest.main()
