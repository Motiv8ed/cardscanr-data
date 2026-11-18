"""Fixture tests for review-only exact 68-set core merge safeguards."""
import collections
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from tools.build_review_68_set_core import missing_ids, make_review_core, read_core_rows


def create_core(path:Path,set_ids:list[str]):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE sets(
              set_id TEXT PRIMARY KEY,language TEXT,region TEXT,
              set_name TEXT,normalized_set_name TEXT,release_date TEXT);
            CREATE TABLE cards(id TEXT);
            INSERT INTO meta VALUES('setCount','0');
        """)
        for sid in set_ids:
            db.execute("INSERT INTO sets VALUES(?,?,?,?,?,?)",
                       (sid,"ko","KR","Set "+sid,"set "+sid,"2026-01-01"))


class ReviewCoreTests(unittest.TestCase):
    def test_only_missing_sets_appended_original_rows_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            a,b,out=(root/"active.sqlite",root/"old.sqlite",root/"review.sqlite")
            create_core(a,["active1","active2"])
            create_core(b,["legacy1","legacy2","active1"])
            original_checksum=hashlib.sha256(a.read_bytes()).hexdigest()
            result=make_review_core(a,b,{"legacy1","legacy2"},out)
            self.assertEqual(result["sets_added"],2)
            self.assertEqual(result["sets_after"],4)
            self.assertTrue(result["original_set_rows_preserved"])
            self.assertEqual(hashlib.sha256(a.read_bytes()).hexdigest(),original_checksum)
            with sqlite3.connect(out) as db:
                self.assertEqual(db.execute("SELECT value FROM meta WHERE key='setCount'").fetchone()[0],"4")
                self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0],"ok")

    def test_never_overwrites_existing_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            a,b=root/"a.sqlite",root/"b.sqlite"
            create_core(a,["same"])
            create_core(b,["same","new"])
            with self.assertRaisesRegex(ValueError,"overwrite"):
                make_review_core(a,b,{"same"},root/"out.sqlite")
            self.assertFalse((root/"out.sqlite").exists())

    def test_missing_from_legacy_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            a,b=root/"a.sqlite",root/"b.sqlite"
            create_core(a,["original"])
            create_core(b,["old"])
            with self.assertRaisesRegex(ValueError,"cannot_resolve"):
                make_review_core(a,b,{"unknown"},root/"out.sqlite")

    def test_set_union_honors_other_pack_sets(self):
        refs={"ko":collections.Counter({"k1":80,"k2":10}),
              "zh-tw":collections.Counter({"c1":25,"k2":5})}
        self.assertEqual(missing_ids({"k1","c1"},refs),{"k2"})

    def test_requires_nonempty_bounded_delta_and_new_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            a,b=root/"a.sqlite",root/"b.sqlite"
            create_core(a,["a"])
            create_core(b,["b"])
            with self.assertRaisesRegex(ValueError,"expected_1_to_500"):
                make_review_core(a,b,set(),root/"out.sqlite")
            target=root/"existing.sqlite"
            target.write_bytes(b"no overwrite")
            with self.assertRaisesRegex(FileExistsError,"already_exists"):
                make_review_core(a,b,{"b"},target)
            self.assertEqual(target.read_bytes(),b"no overwrite")


if __name__=="__main__":
    unittest.main()
