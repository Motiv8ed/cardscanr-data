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
                                image_urls_exposed=False, customer_data_exposed=False,
                                variant_identity_verified=False,
                                snapshot_consistency_must_match_across_pages=True)
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
            # Python bool is an int subclass: True == 1 and False == 0.
            # The safety envelope must require an integer zero, not False.
            page["rows"][0]["native_card_name"] = "native"
            page["production_writes"] = False
            paths[0].write_text(json.dumps(page), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsafe_page_flags"):
                normalize_pages(paths, root / "boolean-write-flag.json")
            page["production_writes"] = 0
            for key, unsafe in (("customer_data_exposed", True),
                                ("variant_identity_verified", True),
                                ("snapshot_consistency_must_match_across_pages", False)):
                page[key] = unsafe
                paths[0].write_text(json.dumps(page), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "unsafe_page_flags"):
                    normalize_pages(paths, root / f"unsafe-{key}.json")
                page[key] = not unsafe
            # A short page cannot advertise a continuation cursor.
            page["rows"] = page["rows"][:1]
            page["page_size"] = 1
            page["next_cursor"] = page["rows"][-1]["canonical_printing_id"]
            page["page_sha256"] = hashlib.sha256(json.dumps(
                page["rows"], ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode()).hexdigest()
            paths[0].write_text(json.dumps(page), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "short_nonterminal_page"):
                normalize_pages(paths, root / "short-middle.json")
            page["rows"] = json.loads(paths[1].read_text())["rows"]
            # Restore first page from its original sequence.
            page["rows"] = [dict(canonical_printing_id=f"ko:{i:04d}", language="ko",
                                set_id="tcgdex:asia:set:SV4K", native_card_name="native",
                                printed_collector_number=str(i), has_image_link=False)
                            for i in range(30)]
            page["page_size"] = 30
            page["next_cursor"] = page["rows"][-1]["canonical_printing_id"]
            page["page_sha256"] = hashlib.sha256(json.dumps(
                page["rows"], ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode()).hexdigest()
            # Recalculate a valid page hash after shuffling IDs: ordering
            # still must fail, independent of hash integrity.
            page["rows"][0]["native_card_name"] = "native"
            page["rows"][0], page["rows"][1] = page["rows"][1], page["rows"][0]
            page["page_sha256"] = hashlib.sha256(json.dumps(
                page["rows"], ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest()
            paths[0].write_text(json.dumps(page), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "nonmonotonic_page"):
                normalize_pages(paths, root / "shuffled.json")


    def test_duplicate_json_keys_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            page = root / "ambiguous.json"
            # A permissive JSON decoder silently takes the last value,
            # allowing a misleading provenance or safety flag.
            page.write_text('{"classification":"LIVE_MISSING_CARD_IDENTITY_READ_ONLY_V1",'
                            '"classification":"UNTRUSTED"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate_json_key"):
                normalize_pages([page], root / "out.json")
            self.assertFalse((root / "out.json").exists())
            page.write_text('{"rows":[{"canonical_printing_id":"a",'
                            '"canonical_printing_id":"b"}]}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate_json_key"):
                normalize_pages([page], root / "out.json")

    def test_updated_tw_count_and_mixed_count_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            paths = []
            for lang, count in (("ko", 31), ("zh-tw", 35)):
                records = [dict(canonical_printing_id=f"{lang}:{i:04d}",
                                language=lang, set_id="test-set",
                                native_card_name="native",
                                printed_collector_number=str(i),
                                has_image_link=False) for i in range(count)]
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
                        next_cursor=chunk[-1]["canonical_printing_id"] if start+30<count else None,
                        production_writes=0, artwork_rights_granted=False,
                        image_urls_exposed=False, customer_data_exposed=False,
                        variant_identity_verified=False,
                        snapshot_consistency_must_match_across_pages=True)
                    p = root / f"{lang}-{start:04d}.json"
                    p.write_text(json.dumps(page), encoding="utf-8")
                    paths.append(p)
            result = normalize_pages(paths, root / "valid.json")
            self.assertEqual(result["counts"], {"ko": 31, "zh-tw": 35})
            self.assertEqual(len(result["records"]), 66)
            self.assertFalse(result["production_writes"])
            altered = json.loads(paths[-1].read_text(encoding="utf-8"))
            altered["missing_cards_in_snapshot"] = 36
            paths[-1].write_text(json.dumps(altered), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "snapshot_or_chain_drift"):
                normalize_pages(paths, root / "invalid.json")
            self.assertFalse((root / "invalid.json").exists())

if __name__ == "__main__":
    unittest.main()
