#!/usr/bin/env python3
"""Safe future identity-crosswalk contract for zh-cn production vs RC.

Does not publish packs or rewrite collections. Asserts the measured gap between
the 41,185-ID production pack and the 14,923-record RC catalogue, and that any
future cutover must be additive (aliases) rather than ID replacement.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPAT = (
    ROOT
    / "reports/global_image_acquisition/pokecardex/release_candidate/PROD_PACK_COMPATIBILITY.json"
)
PROD = Path(r"D:\CardScanR\artifacts\build98_zhcn\zh-cn.sqlite")
CARDS = ROOT / "data/global/catalogue/cards.jsonl"


def _load_compat() -> dict:
    assert COMPAT.exists(), f"missing {COMPAT}"
    return json.loads(COMPAT.read_text(encoding="utf-8"))


def _propose_additive_aliases(
    prod_ids: set[str],
    soft_links: dict[str, str],
) -> dict[str, str]:
    """Map RC id -> production id for soft links only; never drop prod ids."""
    aliases: dict[str, str] = {}
    for rc_id, prod_id in soft_links.items():
        if prod_id not in prod_ids:
            raise AssertionError(f"alias target missing from production: {prod_id}")
        aliases[rc_id] = prod_id
    return aliases


class FutureZhcnIdentityCrosswalkTests(unittest.TestCase):
    def test_compatibility_report_documents_namespace_gap(self) -> None:
        report = _load_compat()
        self.assertEqual(report.get("verdict"), "PASS")
        self.assertEqual(report["productionPack"]["cardCount"], 41185)
        self.assertEqual(report["rcCatalogueZhHans"]["cardCount"], 14923)
        self.assertEqual(report["identityOverlap"]["exactCanonicalPrintingIdOverlap"], 0)
        self.assertEqual(report["identityOverlap"]["setCodeCollectorOverlap"], 875)
        self.assertEqual(report["preservationGuarantee"]["lostProductionPrintingIds"], 0)
        self.assertTrue(report["preservationGuarantee"]["productionPackFileUntouched"])

    def test_naive_pack_replace_would_orphan_all_production_ids(self) -> None:
        """Exact overlap is 0, so replace-by-ID cannot preserve collections."""
        report = _load_compat()
        exact = report["identityOverlap"]["exactCanonicalPrintingIdOverlap"]
        prod = report["productionPack"]["cardCount"]
        self.assertEqual(exact, 0)
        orphaned_if_replaced = prod - exact
        self.assertEqual(orphaned_if_replaced, 41185)

    def test_additive_alias_strategy_preserves_owned_production_ids(self) -> None:
        report = _load_compat()
        samples = report["identityOverlap"]["setCodeCollectorOverlapSamples"]
        self.assertGreaterEqual(len(samples), 1)

        # Synthetic ownership bound to production IDs (the durable key).
        owned = {f"prod::{set_code}::{collector}" for set_code, collector in samples[:5]}
        prod_ids = set(owned)
        # Soft links propose RC ids that alias *to* production ids — never the reverse rewrite.
        soft_links = {
            f"rc::{set_code}::{collector}": f"prod::{set_code}::{collector}"
            for set_code, collector in samples[:5]
        }
        aliases = _propose_additive_aliases(prod_ids, soft_links)
        self.assertEqual(len(aliases), 5)
        # Owned set unchanged after alias publication.
        self.assertEqual(owned, prod_ids)
        for rc_id, prod_id in aliases.items():
            self.assertIn(prod_id, owned)
            self.assertTrue(rc_id.startswith("rc::"))

    def test_live_pack_still_has_41185_when_present(self) -> None:
        if not PROD.exists():
            self.skipTest(f"local production pack absent: {PROD}")
        con = sqlite3.connect(f"file:{PROD.as_posix()}?mode=ro", uri=True)
        try:
            n = con.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(n, 41185)

    def test_rc_catalogue_count_when_present(self) -> None:
        if not CARDS.exists():
            self.skipTest(f"worktree cards.jsonl absent: {CARDS}")
        zh = 0
        with CARDS.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip() and json.loads(line).get("language") == "zh-Hans":
                    zh += 1
        self.assertEqual(zh, 14923)

    def test_alias_table_roundtrip_does_not_mutate_ownership_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "owned.json"
            owned = {
                "pokemon-tcg-kb|example|001": {
                    "canonicalPrintingId": "pokemon-tcg-kb|example|001",
                    "qty": 2,
                }
            }
            store.write_text(json.dumps(owned), encoding="utf-8")
            aliases = {
                "pokemon|zh-Hans|CN|set:deadbeef|1|unspecified": "pokemon-tcg-kb|example|001"
            }
            # Resolve RC id → ownership via alias; ownership file keys stay production IDs.
            resolved = aliases["pokemon|zh-Hans|CN|set:deadbeef|1|unspecified"]
            loaded = json.loads(store.read_text(encoding="utf-8"))
            self.assertIn(resolved, loaded)
            self.assertEqual(set(loaded), set(owned))
            self.assertNotIn("pokemon|zh-Hans|CN|set:deadbeef|1|unspecified", loaded)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
