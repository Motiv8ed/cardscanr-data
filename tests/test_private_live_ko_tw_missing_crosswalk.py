"""Offline fail-closed tests for the research-only KO/TW live crosswalk."""
import json
import pathlib
import tempfile
import unittest
from tools.private_live_ko_tw_missing_crosswalk import reconcile


class CrosswalkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.live = [root / f"live-{i}.jsonl" for i in range(51)]
        self.source = root / "source.jsonl"
        self.output = root / "review.json"
        rows = []
        for lang, count in (("ko", 1131), ("zh-tw", 397)):
            for n in range(count):
                rows.append(dict(language=lang, canonical_printing_id=f"{lang}-{n}",
                                 set_id=f"set-{lang}", native_card_name=f"Card {n}",
                                 printed_collector_number=str(n + 1),
                                 printed_denominator=str(count),
                                 has_image_link=False, snapshot_sha256="a" * 64))
        for i, path in enumerate(self.live):
            path.write_text("".join(json.dumps(x) + "\n" for x in rows[i*30:(i+1)*30]), encoding="utf-8")
        self.source.write_text("", encoding="utf-8")

    def test_missing_source_is_never_importable(self):
        result = reconcile(self.live, self.source, self.output)
        self.assertEqual(result["outcomes"], {"no_exact_source": 1528})
        self.assertEqual(result["new_live_image_links"], 0)
        self.assertFalse(result["production_writes"])
        self.assertTrue(all(not x["safe_for_vault_apply"] and not x["public_artwork_allowed"]
                            for x in result["reviews"]))

    def test_source_without_finish_is_quarantined_even_if_rights_claimed(self):
        self.source.write_text(json.dumps(dict(
            language="ko", set_id="set-ko", native_card_name="Card 0",
            printed_collector_number="1", printed_denominator="1131",
            artwork_commercial_rights_verified=True)) + "\n", encoding="utf-8")
        result = reconcile(self.live, self.source, self.output)
        self.assertEqual(result["outcomes"]["quarantine_finish_not_proven"], 1)
        self.assertEqual(result["new_live_image_links"], 0)

    def test_duplicate_canonical_identity_fails_before_output(self):
        first = self.live[0]
        lines = first.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[0])
        row["canonical_printing_id"] = "ko-1"
        lines[0] = json.dumps(row)
        first.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate_or_missing_live_id"):
            reconcile(self.live, self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_existing_output_cannot_be_overwritten(self):
        self.output.write_text("preserve", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            reconcile(self.live, self.source, self.output)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "preserve")

    def test_missing_live_identity_fields_quarantined(self):
        first = self.live[0]
        lines = first.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[0])
        row["native_card_name"] = ""
        lines[0] = json.dumps(row)
        first.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = reconcile(self.live, self.source, self.output)
        self.assertEqual(result["outcomes"]["quarantine_missing_identity_fields"], 1)

    def test_mixed_snapshot_fails_closed(self):
        first = self.live[0]
        lines = first.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[0])
        row["snapshot_sha256"] = "b" * 64
        lines[0] = json.dumps(row)
        first.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing_or_mixed_live_snapshot"):
            reconcile(self.live, self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_string_false_rights_never_passes(self):
        self.source.write_text(json.dumps(dict(
            language="ko", set_id="set-ko", native_card_name="Card 0",
            printed_collector_number="1", printed_denominator="1131",
            finish="normal", artwork_commercial_rights_verified="false")) + "\n",
            encoding="utf-8")
        first = self.live[0]
        lines = first.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[0])
        row["finish"] = "normal"
        lines[0] = json.dumps(row)
        first.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = reconcile(self.live, self.source, self.output)
        self.assertEqual(result["outcomes"]["quarantine_artwork_rights"], 1)


    def test_repeated_page_path_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate_live_page_path"):
            reconcile([self.live[0]] * 51, self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_oversized_live_page_fails_closed(self):
        first = self.live[0]
        lines = first.read_text(encoding="utf-8").splitlines()
        first.write_text("\n".join(lines + [lines[0]]) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "live_page_size_invalid"):
            reconcile(self.live, self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_rights_claim_requires_evidence(self):
        self.source.write_text(json.dumps(dict(
            language="ko", set_id="set-ko", native_card_name="Card 0",
            printed_collector_number="1", printed_denominator="1131",
            finish="normal", artwork_commercial_rights_verified=True,
            image_url="https://example.org/card.png")) + "\n", encoding="utf-8")
        first = self.live[0]
        lines = first.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[0])
        row["finish"] = "normal"
        lines[0] = json.dumps(row)
        first.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = reconcile(self.live, self.source, self.output)
        self.assertEqual(result["outcomes"]["quarantine_missing_rights_evidence"], 1)

if __name__ == "__main__":
    unittest.main()
