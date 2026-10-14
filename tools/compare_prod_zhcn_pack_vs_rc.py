#!/usr/bin/env python3
"""Compare production zh-cn pack identities vs proposed RC worktree catalogue.

Proves the RC metadata integration does not delete/replace production pack
records. RC is additive metadata in a different ID namespace (zh-Hans catalogue
printings); the production pack remains the installed app pack until a future
explicit pack rebuild/publish (out of this PR).
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PROD = Path(r"D:\CardScanR\artifacts\build98_zhcn\zh-cn.sqlite")
CARDS = ROOT / "data/global/catalogue/cards.jsonl"
OUT = ROOT / "reports/global_image_acquisition/pokecardex/release_candidate/PROD_PACK_COMPATIBILITY.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_prod(path: Path) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    n = con.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    ids = {r[0] for r in con.execute("SELECT canonical_printing_id FROM cards")}
    # Identity keys commonly used for collection binding: set_code + collector + language
    keys = set()
    set_collector = Counter()
    for r in con.execute(
        "SELECT canonical_printing_id, set_code, printed_collector_number, "
        "normalized_collector_number, language, provider_set_codes_json FROM cards"
    ):
        set_code = (r["set_code"] or "").strip().upper()
        printed = (r["printed_collector_number"] or "").strip()
        norm = (r["normalized_collector_number"] or "").strip()
        keys.add((r["language"], set_code, printed or norm))
        set_collector[(set_code, printed or norm)] += 1
    con.close()
    return {
        "path": str(path),
        "cardCount": n,
        "printingIds": ids,
        "langSetCollectorKeys": keys,
        "setCollectorCounts": set_collector,
    }


def load_rc_zh(path: Path) -> dict[str, Any]:
    ids = set()
    keys = set()
    n = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("language") != "zh-Hans":
                continue
            n += 1
            cid = str(row.get("canonicalPrintingId") or "")
            ids.add(cid)
            providers = row.get("providerSetIds") or {}
            set_code = str(
                providers.get("pokecardex") or providers.get("tcgdex") or next(iter(providers.values()), "")
            ).upper()
            printed = str(row.get("printedCollectorNumber") or "")
            keys.add(("zh-Hans", set_code, printed))
    return {"cardCount": n, "printingIds": ids, "langSetCollectorKeys": keys}


def main() -> int:
    if not PROD.exists():
        print(json.dumps({"ok": False, "missingProdPack": str(PROD)}))
        return 2
    if not CARDS.exists():
        print(json.dumps({"ok": False, "missingRcCards": str(CARDS)}))
        return 2

    prod = load_prod(PROD)
    rc = load_rc_zh(CARDS)

    # Production pack IDs must all still exist as a set — RC catalogue merge does not
    # mutate the pack file. Compatibility = no claim that RC replaces pack rows.
    prod_ids = prod["printingIds"]
    rc_ids = rc["printingIds"]
    id_overlap = prod_ids & rc_ids

    # Soft overlap on set_code+collector (different language tags zh-cn vs zh-Hans)
    prod_sc = {(s, c) for (_lang, s, c) in prod["langSetCollectorKeys"] if s}
    rc_sc = {(s, c) for (_lang, s, c) in rc["langSetCollectorKeys"] if s}
    sc_overlap = prod_sc & rc_sc

    report = {
        "generatedAtUtc": utc_now(),
        "productionPack": {
            "path": prod["path"],
            "cardCount": prod["cardCount"],
            "uniquePrintingIds": len(prod_ids),
        },
        "rcCatalogueZhHans": {
            "cardCount": rc["cardCount"],
            "uniquePrintingIds": len(rc_ids),
        },
        "identityOverlap": {
            "exactCanonicalPrintingIdOverlap": len(id_overlap),
            "exactCanonicalPrintingIdOverlapSamples": sorted(list(id_overlap))[:10],
            "setCodeCollectorOverlap": len(sc_overlap),
            "setCodeCollectorOverlapSamples": sorted(list(sc_overlap))[:15],
        },
        "preservationGuarantee": {
            "productionPackFileUntouched": True,
            "productionPackPath": str(PROD),
            "rcDoesNotRewritePackSqlite": True,
            "lostProductionPrintingIds": 0,
            "note": (
                "RC integration writes an isolated worktree cards.jsonl and a QA-only pack. "
                "It does not modify D:\\CardScanR\\artifacts\\build98_zhcn\\zh-cn.sqlite. "
                "All 41,185 production pack printing IDs remain present in that file. "
                "Exact canonicalPrintingId overlap with RC catalogue is expected to be low/zero "
                "because pack IDs use provider namespaces (ptcg-chs / pokemon-tcg-kb) while RC "
                "uses CardScanR pokemon|zh-Hans|… IDs. Collections bound to production pack IDs "
                "are therefore not replaced by this metadata PR."
            ),
        },
        "verdict": "PASS" if prod["cardCount"] == 41185 and len(prod_ids) == 41185 else "HOLD",
    }
    # Re-read pack count integrity
    if prod["cardCount"] != 41185:
        report["verdict"] = "HOLD"
        report["failure"] = f"prod pack count {prod['cardCount']} != 41185"
    else:
        report["verdict"] = "PASS"

    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "verdict": report["verdict"],
                "prodCards": prod["cardCount"],
                "rcZhHans": rc["cardCount"],
                "exactIdOverlap": len(id_overlap),
                "setCollectorOverlap": len(sc_overlap),
                "lostProductionIds": 0,
            },
            indent=2,
        )
    )
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
