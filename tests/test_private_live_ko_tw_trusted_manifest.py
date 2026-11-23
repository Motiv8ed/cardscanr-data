"""Offline negative regressions for independently pinned live snapshot provenance."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.private_live_ko_tw_trusted_manifest import SCHEMA, verify, verified_adapt


class TrustedManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ko = self.root / "ko.jsonl"
        self.tw = self.root / "tw.jsonl"
        for path, language in ((self.ko, "ko"), (self.tw, "zh-tw")):
            row = dict(canonical_printing_id=language + "-1", language=language,
                       set_id="sv1", native_card_name="test", printed_collector_number="001",
                       has_image_link=False)
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        self.manifest = self.root / "manifest.json"
        self.doc = {"schema": SCHEMA, "counts": {"ko": 1, "zh-tw": 1},
                    "chunks": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in (self.ko, self.tw)}}
        self.save()

    def save(self):
        self.manifest.write_text(json.dumps(self.doc, sort_keys=True), encoding="utf-8")
        self.pin = hashlib.sha256(self.manifest.read_bytes()).hexdigest()

    def check(self, digest=None):
        return verify(self.manifest, self.pin if digest is None else digest, [self.ko, self.tw])

    def test_valid_pinned_manifest_and_read_only_quarantine(self):
        self.assertEqual(self.check(), {"ko": 1, "zh-tw": 1})
        output = self.root / "research.json"
        result = verified_adapt(self.manifest, self.pin, [self.ko], output)
        self.assertTrue(output.exists())
        self.assertFalse(result["production_writes"])
        self.assertEqual(result["new_live_image_links"], 0)

    def test_untrusted_digest_fails(self):
        with self.assertRaisesRegex(ValueError, "untrusted_manifest"):
            self.check("0" * 64)

    def test_tampered_chunk_fails_without_output(self):
        self.ko.write_bytes(self.ko.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "chunk_digest_mismatch"):
            verified_adapt(self.manifest, self.pin, [self.ko, self.tw], self.root / "out.json")
        self.assertFalse((self.root / "out.json").exists())

    def test_missing_chunk_fails(self):
        with self.assertRaisesRegex(ValueError, "chunk_inventory_mismatch"):
            verify(self.manifest, self.pin, [self.ko])

    def test_duplicate_manifest_keys_fail(self):
        self.manifest.write_text('{"schema":"x","schema":"y"}', encoding="utf-8")
        self.pin = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "duplicate_manifest_key"):
            self.check()

    def test_self_consistent_forged_counts_do_not_pass_original_pin(self):
        original = self.pin
        self.doc["counts"]["ko"] = 999
        self.save()
        with self.assertRaisesRegex(ValueError, "untrusted_manifest"):
            self.check(original)

    def test_zero_gap_language_is_valid(self):
        # One language can legitimately have no missing-image records.
        self.tw.write_text("", encoding="utf-8")
        self.doc["counts"]["zh-tw"] = 0
        del self.doc["chunks"]["tw.jsonl"]
        self.save()
        self.assertEqual(verify(self.manifest, self.pin, [self.ko]), {"ko": 1, "zh-tw": 0})
        output = self.root / "zero-gap-research.json"
        result = verified_adapt(self.manifest, self.pin, [self.ko, self.tw], output)
        self.assertEqual(result["rows"], 1)
        self.assertFalse(result["production_writes"])

    def test_boolean_counts_rejected(self):
        self.doc["counts"]["ko"] = True
        self.save()
        with self.assertRaisesRegex(ValueError, "invalid_manifest_counts"):
            self.check()

    def test_path_traversal_name_rejected(self):
        self.doc["chunks"]["../ko.jsonl"] = self.doc["chunks"].pop("ko.jsonl")
        self.save()
        with self.assertRaisesRegex(ValueError, "invalid_manifest_chunk_entry"):
            self.check()


if __name__ == "__main__":
    unittest.main()
