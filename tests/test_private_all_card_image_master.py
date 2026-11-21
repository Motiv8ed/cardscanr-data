"""Offline safety tests for the full card/set/image master catalogue."""
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.private_all_card_image_master import (
    SCHEMA, EXPECTED_TOTAL, EXPECTED_CARD_ROWS,
    ingest_pack, merge_overlay, _url_host,
)


def minimal_source():
    db=sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE cards(
          canonical_printing_id TEXT,canonical_base_id TEXT,
          language TEXT,set_id TEXT,printed_collector_number TEXT,
          native_card_name TEXT,image_thumbnail_url TEXT,image_display_url TEXT
        );
        CREATE TABLE sets(set_id TEXT,language TEXT,native_set_name TEXT,release_date TEXT);
    """)
    db.execute("INSERT INTO cards VALUES (?,?,?,?,?,?,?,?)",
        ("tcgdex_asia_SV7s_001:th:TH","tcgdex_asia_SV7s_001:th:TH",
         "th","tcgdex:asia:set:SV7s","001/223","โปเกมอน",None,None))
    db.execute("INSERT INTO cards VALUES (?,?,?,?,?,?,?,?)",
        ("tcgdex_asia_SV7s_002:th:TH","tcgdex_asia_SV7s_002:th:TH",
         "th","tcgdex:asia:set:SV7s","002/223","โปเกมอนสอง",
         "https://assets.tcgdex.net/th/SV/SV7s/002/low.webp",None))
    db.execute("INSERT INTO sets VALUES (?,?,?,?)",
               ("tcgdex:asia:set:SV7s","th","Stellar Miracle","2024-01-02"))
    db.commit()
    return db


def overlay_example(cid="tcgdex_asia_SV7s_001:th:TH"):
    base="https://assets.tcgdex.net/th/SV/SV7s/001"
    return {
        "canonical_printing_id":cid, "identity_match":"exact_unique",
        "language":"th","provider_set_id":"SV7s",
        "source_local_number":"001","source_image_base_url":base,
        "provider_low_webp_url":base+"/low.webp",
        "source_set_sha256":"a"*64,
    }


class ArtworkMasterIndexTests(unittest.TestCase):
    def test_seven_pack_expected_totals(self):
        self.assertEqual(len(EXPECTED_CARD_ROWS),7)
        self.assertEqual(EXPECTED_TOTAL,158803)

    def test_ingest_original_card_and_set_metadata(self):
        src=minimal_source()
        with sqlite3.connect(":memory:") as dst:
            dst.executescript(SCHEMA)
            counts=ingest_pack(src,dst,pack_id="th",manifest_origin="archived_canary")
            self.assertEqual(counts["cards"],2)
            self.assertEqual(counts["sets"],1)
            self.assertEqual(counts["cards_with_existing_image_ref"],1)
            self.assertEqual(counts["existing_unique_source_url_rows"],1)
            row=dst.execute("SELECT source_metadata_json FROM cards WHERE printed_collector_number='001/223'").fetchone()
            self.assertEqual(json.loads(row[0])["native_card_name"],"โปเกมอน")
            img=dst.execute("SELECT rights_status,local_download_status FROM image_sources").fetchone()
            self.assertEqual(img,("unverified","not_downloaded"))
        src.close()

    def test_language_drift_refused(self):
        src=minimal_source()
        with sqlite3.connect(":memory:") as dst:
            dst.executescript(SCHEMA)
            with self.assertRaisesRegex(ValueError,"card_language_drift"):
                ingest_pack(src,dst,pack_id="id",manifest_origin="archived_canary")
        src.close()

    def test_duplicate_printing_identity_denied(self):
        src=minimal_source()
        with sqlite3.connect(":memory:") as dst:
            dst.executescript(SCHEMA)
            ingest_pack(src,dst,pack_id="th",manifest_origin="archived_canary")
            with self.assertRaisesRegex(ValueError,"duplicate_canonical_printing"):
                ingest_pack(src,dst,pack_id="th",manifest_origin="archived_canary")
        src.close()

    def test_overlay_exact_identity_and_safe_image_without_rights_escalation(self):
        src=minimal_source()
        with tempfile.TemporaryDirectory() as td:
            master=Path(td)/"master.sqlite"
            with sqlite3.connect(master) as db:
                db.executescript(SCHEMA)
                ingest_pack(src,db,pack_id="th",manifest_origin="archive")
            overlay=Path(td)/"two-thousand.zip"
            with zipfile.ZipFile(overlay,"w") as z:
                z.writestr("artwork_source_registry.jsonl",
                           json.dumps(overlay_example(),ensure_ascii=False)+"\n")
            proof=merge_overlay(master,overlay)
            self.assertEqual(proof["overlay_records_validated"],1)
            self.assertEqual(proof["new_image_source_rows"],1)
            proof2=merge_overlay(master,overlay)
            self.assertEqual(proof2["new_image_source_rows"],0)
            with sqlite3.connect(master) as db:
                status=db.execute("SELECT rights_status,local_download_status FROM image_sources WHERE source_kind='tcgdex_exact_source_candidate'").fetchone()
                self.assertEqual(status,("unverified","not_downloaded"))
                self.assertEqual(db.execute("SELECT count(*) FROM cards").fetchone()[0],2)
        src.close()

    def test_overlay_wrong_set_or_unsafe_url_fails_closed(self):
        src=minimal_source()
        with tempfile.TemporaryDirectory() as td:
            master=Path(td)/"master.sqlite"
            with sqlite3.connect(master) as db:
                db.executescript(SCHEMA)
                ingest_pack(src,db,pack_id="th",manifest_origin="archive")
            overlay=Path(td)/"bad.zip"
            bad=overlay_example()
            bad["language"]="id"
            with zipfile.ZipFile(overlay,"w") as z:
                z.writestr("artwork_source_registry.jsonl",json.dumps(bad)+"\n")
            with self.assertRaisesRegex(ValueError,"overlay_wrong_language_or_set"):
                merge_overlay(master,overlay)
            bad=overlay_example()
            bad["source_image_base_url"]="https://example.com/bad"
            with zipfile.ZipFile(overlay,"w") as z:
                z.writestr("artwork_source_registry.jsonl",json.dumps(bad)+"\n")
            with self.assertRaisesRegex(ValueError,"overlay_invalid_card_identity_or_url"):
                merge_overlay(master,overlay)
            with sqlite3.connect(master) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM image_sources").fetchone()[0],1)
        src.close()

    def test_https_host_parsing_no_userinfo(self):
        self.assertEqual(_url_host("https://assets.tcgdex.net/id/SV/SV4a/001"),"assets.tcgdex.net")
        self.assertIsNone(_url_host("http://assets.tcgdex.net/test"))
        self.assertIsNone(_url_host("https://user:secret@assets.tcgdex.net/test"))


if __name__=="__main__":
    unittest.main()
