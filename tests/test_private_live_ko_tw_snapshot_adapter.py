import json
import tempfile
import unittest
from pathlib import Path
from tools.private_live_ko_tw_snapshot_adapter import adapt

class AdapterTests(unittest.TestCase):
    def test_real_schema_quarantined_not_inferred(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            paths = []
            for i, (lang, count) in enumerate((("ko", 300), ("ko", 300), ("ko", 300),
                                                ("ko", 231), ("zh-tw", 300), ("zh-tw", 97))):
                p = root / f"chunk_{i}.jsonl"
                rows = [dict(canonical_printing_id=f"{lang}:{i}:{j}",language=lang,
                             set_id="tcgdex:asia:set:SV4K",native_card_name="native",
                             printed_collector_number=str(j),has_image_link=False)
                        for j in range(count)]
                p.write_text("\n".join(json.dumps(r) for r in rows)+"\n")
                paths.append(p)
            result = adapt(paths, root/"result.json", {"ko": 1131, "zh-tw": 397})
            self.assertEqual(result["rows"], 1528)
            self.assertEqual(result["unproven_printings"], 1528)
            self.assertFalse(result["production_writes"])
            self.assertEqual(result["new_source_urls"], 0)
            self.assertEqual(len(result["input_sha256"]), 6)
            with self.assertRaises(FileExistsError):
                adapt(paths, root/"result.json", {"ko": 1131, "zh-tw": 397})
            paths[0].write_text(paths[0].read_text().replace('"has_image_link": false', '"has_image_link": true', 1))
            with self.assertRaises(ValueError):
                adapt(paths, root/"new.json", {"ko": 1131, "zh-tw": 397})

    def test_snapshot_counts_required_and_changed_counts_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            paths = []
            for lang, count in (("ko", 2), ("zh-tw", 1)):
                p = root / (lang + ".jsonl")
                p.write_text("".join(json.dumps(dict(
                    canonical_printing_id=f"{lang}:{j}", language=lang,
                    set_id="set", native_card_name="name",
                    printed_collector_number=str(j+1), has_image_link=False
                )) + "\\n" for j in range(count)))
                paths.append(p)
            output = root / "result.json"
            with self.assertRaisesRegex(ValueError, "missing_or_invalid_snapshot_counts"):
                adapt(paths, output)
            with self.assertRaisesRegex(ValueError, "snapshot_count_drift"):
                adapt(paths, output, {"ko": 2, "zh-tw": 2})
            self.assertFalse(output.exists())
            result = adapt(paths, output, {"ko": 2, "zh-tw": 1})
            self.assertEqual(result["counts"], {"ko": 2, "zh-tw": 1})
            self.assertFalse(result["production_writes"])

    def test_duplicate_chunk_basenames_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            paths = []
            for i in range(6):
                folder = root / str(i)
                folder.mkdir()
                p = folder / "chunk.jsonl"
                p.write_text(json.dumps({"language": "ko"}) + "\n", encoding="utf-8")
                paths.append(p)
            output = root / "result.json"
            with self.assertRaisesRegex(ValueError, "duplicate_chunk_basename"):
                adapt(paths, output, {"ko": 1131, "zh-tw": 397})
            self.assertFalse(output.exists())

    def test_52_language_scoped_bounded_pages_cover_all_live_gaps(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            paths = []
            for lang, count in (("ko", 1131), ("zh-tw", 397)):
                rows = [dict(canonical_printing_id=f"{lang}:{j}", language=lang,
                             set_id=f"set-{lang}", native_card_name=f"native-{j}",
                             printed_collector_number=str(j + 1), has_image_link=False)
                        for j in range(count)]
                for start in range(0, count, 30):
                    path = root / f"{lang}_page_{start // 30:02d}.jsonl"
                    page_rows = rows[start:start + 30]
                    path.write_text("".join(json.dumps(r) + "\n" for r in page_rows),
                                    encoding="utf-8")
                    self.assertLessEqual(len(page_rows), 30)
                    self.assertEqual({r["language"] for r in page_rows}, {lang})
                    paths.append(path)
            # Workbench pagination is language-scoped: ceil(1131/30)+ceil(397/30)=52.
            self.assertEqual(len(paths), 52)
            result = adapt(paths, root / "manifest.json", {"ko": 1131, "zh-tw": 397})
            self.assertEqual(result["rows"], 1528)
            self.assertEqual(result["new_live_image_links"], 0)
            self.assertFalse(result["production_writes"])

if __name__ == "__main__":
    unittest.main()
