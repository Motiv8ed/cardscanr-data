import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from tools.private_workbench_identity_page_export import normalize_pages

class WorkbenchEnvelopeTests(unittest.TestCase):
    def test_complete_authentic_envelope_shape_and_tamper(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            paths = []
            for lang, count in (("ko", 1131), ("zh-tw", 397)):
                records = [dict(canonical_printing_id=f"{lang}:{i:04d}", language=lang,
                                set_id="tcgdex:asia:set:SV4K", native_card_name="native",
                                printed_collector_number=str(i), has_image_link=False)
                           for i in range(count)]
                snap = hashlib.sha256(json.dumps(records, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()
                for start in range(0, count, 30):
                    chunk = records[start:start+30]
                    raw = json.dumps(chunk, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()
                    page = dict(classification="LIVE_MISSING_CARD_IDENTITY_READ_ONLY_V1",
                                language=lang, rows=chunk, page_size=len(chunk), page_limit=30,
                                missing_cards_in_snapshot=count, snapshot_sha256=snap,
                                page_sha256=hashlib.sha256(raw).hexdigest(),
                                next_cursor=chunk[-1]["canonical_printing_id"] if start+30 < count else None,
                                production_writes=0, artwork_rights_granted=False,
                                image_urls_exposed=False)
                    p = root / f"{lang}-{start:04d}.json"
                    p.write_text(json.dumps(page), encoding="utf-8")
                    paths.append(p)
            out = root / "output.json"
            result = normalize_pages(paths, out)
            self.assertEqual(len(result["records"]), 1528)
            self.assertEqual(result["new_live_image_links"], 0)
            self.assertNotIn("printed_denominator", result["records"][0])
            self.assertFalse(result["rights_granted"])
            with self.assertRaises(FileExistsError):
                normalize_pages(paths, out)
            page = json.loads(paths[0].read_text())
            page["rows"][0]["native_card_name"] = "tampered"
            paths[0].write_text(json.dumps(page))
            with self.assertRaisesRegex(ValueError, "page_hash_mismatch"):
                normalize_pages(paths, root / "other.json")

if __name__ == "__main__":
    unittest.main()
