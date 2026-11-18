"""Offline, no-network tests for pinned source path proof."""
import hashlib
import json
import unittest
from tools.verify_tcgdex_upstream_source import SET_PATHS, paths_for_set, set_file_id, inspect_set, verify


def card(lang, sid, local, cid=None):
    return {"source_language": lang, "source_set_id": sid, "local_id": local,
            "source_card_id": cid or f"{sid}-{local}"}


class Tests(unittest.TestCase):
    def test_explicit_set_paths_no_guesses(self):
        self.assertEqual(SET_PATHS["base1"], "data/Base/Base Set")
        self.assertEqual(SET_PATHS["base2"], "data/Base/Jungle")
        self.assertEqual(SET_PATHS["miscp"], "data/Miscellaneous/Miscellaneous Promos")
        self.assertEqual(SET_PATHS["SV4K"], "data-asia/SV/SV4K")
        self.assertIn("Base%20Set", paths_for_set("base1")[0])
        with self.assertRaisesRegex(ValueError, "unapproved"):
            paths_for_set("../../secret")

    def test_set_id_is_read_from_source_file(self):
        self.assertEqual(set_file_id('const set = { id: "base1", name: {} }'), "base1")
        self.assertEqual(set_file_id("const set = { id: 'SV4K', name: {} }"), "SV4K")
        self.assertIsNone(set_file_id("export default set"))

    @staticmethod
    def lookup(sid):
        return {"upstream_set_file_id": sid,
                "upstream_set_file_sha256": hashlib.sha256(sid.encode()).hexdigest(),
                "source_card_file_stems": {"1", "001"},
                "source_card_files_count": 2}

    def test_evidence_is_never_variant_approval(self):
        details, report = verify([card("en", "base1", "1"), card("ko", "SV4K", "001")],
                                 lookup=self.lookup)
        self.assertEqual(report["classifications"],
                         {"source_file_and_set_identity_corroborated": 2})
        self.assertFalse(report["safe_to_auto_import"])
        self.assertTrue(all(not row["physical_variant_confirmed"] for row in details))
        self.assertEqual(report["artwork_bytes_downloaded"], 0)
        self.assertEqual(report["production_database_writes"], 0)

    def test_wrong_card_id_not_corroborated(self):
        _, report = verify([card("ja","PMCG1","001","not-the-id")],lookup=self.lookup)
        self.assertEqual(report["classifications"], {"not_corroborated_review_required":1})

    def test_wrong_set_file_id_not_corroborated(self):
        def wrong(sid):
            return {**self.lookup(sid), "upstream_set_file_id": "base4"}
        _, report=verify([card("en","base2","1")],lookup=wrong)
        self.assertEqual(report["classifications"], {"not_corroborated_review_required":1})

    def test_directory_listing_parsed_without_other_files(self):
        def fake(url):
            if "raw.githubusercontent.com" in url:
                return b"const set = { id: 'SV4K', name: { ko: 'test' } }"
            return json.dumps([{"name":"001.ts","type":"file"},
                               {"name":"../../secret.ts","type":"file"},
                               {"name":"002.md","type":"file"}]).encode()
        result=inspect_set("SV4K", read=fake)
        self.assertEqual(result["source_card_file_stems"], {"001"})
        self.assertEqual(result["upstream_set_file_id"], "SV4K")

    def test_unsupported_set_rejected(self):
        with self.assertRaisesRegex(ValueError,"unapproved"):
            verify([card("ja","UNKNOWNSET","001")], lookup=self.lookup)


if __name__=="__main__":
    unittest.main()
