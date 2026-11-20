"""Regression safety checks for the 11,378-source-file research-only ledger."""
from __future__ import annotations
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from tools.verify_private_upstream_candidate_ledger import verify

ROOT=Path(__file__).resolve().parents[1]/"private"/"catalogue"/"2026-11-20"


class SourceLedgerTests(unittest.TestCase):
    def test_expected_source_count_is_only_research(self):
        result=verify(ROOT)
        self.assertEqual(result["candidate_card_file_records"],11378)
        self.assertEqual(result["unique_candidate_identities"],11378)
        self.assertEqual(result["by_language"],{"ja":6744,"en":4634})
        self.assertEqual(result["verified_new_canonical_cards"],0)
        self.assertEqual(result["vault_writes"],0)
        self.assertFalse(result["rights_granted"])
        self.assertEqual(result["artwork_binary_files"],0)

    def with_modified_copy(self, change):
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/"ledger"
            shutil.copytree(ROOT,target)
            change(target)
            with self.assertRaises(ValueError):
                verify(target)

    def test_claiming_canonical_additions_is_refused(self):
        def change(root):
            path=root/"upstream_source_candidate_manifest.json"
            doc=json.loads(path.read_text("utf-8"))
            doc["verified_new_canonical_card_count"]=11378
            path.write_text(json.dumps(doc),encoding="utf-8")
        self.with_modified_copy(change)

    def test_unknown_source_set_refused(self):
        def change(root):
            path=root/"ja_ADV_official_source_cardfiles.jsonl"
            rows=path.read_text("utf-8").splitlines()
            item=json.loads(rows[0]);item["source_set_id"]="UNKNOWN_MADEUP"
            rows[0]=json.dumps(item)
            path.write_text("\n".join(rows)+"\n",encoding="utf-8")
        self.with_modified_copy(change)

    def test_forged_git_blob_sha_refused(self):
        def change(root):
            path=root/"en_Mega_Evolution_official_source_cardfiles.jsonl"
            rows=path.read_text("utf-8").splitlines()
            item=json.loads(rows[0]);item["upstream_blob_sha1"]="im_not_a_git_blob"
            rows[0]=json.dumps(item)
            path.write_text("\n".join(rows)+"\n",encoding="utf-8")
        self.with_modified_copy(change)

    def test_duplicate_source_identity_refused(self):
        def change(root):
            path=root/"ja_ADV_official_source_cardfiles.jsonl"
            rows=path.read_text("utf-8").splitlines()
            rows[1]=rows[0]
            path.write_text("\n".join(rows)+"\n",encoding="utf-8")
        self.with_modified_copy(change)

    def test_excluded_existing_set_refused(self):
        def change(root):
            path=root/"en_Scarlet_Violet_official_source_cardfiles.jsonl"
            rows=path.read_text("utf-8").splitlines()
            item=json.loads(rows[0]);item["source_set_id"]="svp"
            rows[0]=json.dumps(item)
            path.write_text("\n".join(rows)+"\n",encoding="utf-8")
        self.with_modified_copy(change)

    def test_unapproved_source_tree_refused(self):
        def change(root):
            path=root/"upstream_source_candidate_manifest.json"
            doc=json.loads(path.read_text("utf-8"))
            doc["upstream_git_root_tree_sha"]="0"*40
            path.write_text(json.dumps(doc),encoding="utf-8")
        self.with_modified_copy(change)


if __name__=="__main__":
    unittest.main()
