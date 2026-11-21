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
            result = adapt(paths, root/"result.json")
            self.assertEqual(result["rows"], 1528)
            self.assertEqual(result["unproven_printings"], 1528)
            self.assertFalse(result["production_writes"])
            self.assertEqual(result["new_source_urls"], 0)
            self.assertEqual(len(result["input_sha256"]), 6)
            with self.assertRaises(FileExistsError):
                adapt(paths, root/"result.json")
            paths[0].write_text(paths[0].read_text().replace('"has_image_link": false', '"has_image_link": true', 1))
            with self.assertRaises(ValueError):
                adapt(paths, root/"new.json")

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
                adapt(paths, output)
            self.assertFalse(output.exists())

if __name__ == "__main__":
    unittest.main()
