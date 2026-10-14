#!/usr/bin/env python3
"""Export set+collector soft overlaps as *candidate* identity aliases.

Never confirms mappings. Never rewrites production printing IDs.
Output is consumed by the QA app dual-read helper.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PROD = Path(r"D:\CardScanR\artifacts\build98_zhcn\zh-cn.sqlite")
CARDS = ROOT / "data/global/catalogue/cards.jsonl"
OUT = (
    ROOT
    / "reports/global_image_acquisition/pokecardex/release_candidate"
    / "SOFT_OVERLAP_CANDIDATE_ALIASES.json"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    if not PROD.exists() or not CARDS.exists():
        raise SystemExit("missing prod pack or worktree cards.jsonl")

    prod_by_key: dict[tuple[str, str], list[str]] = {}
    con = sqlite3.connect(f"file:{PROD.as_posix()}?mode=ro", uri=True)
    for cid, set_code, printed, norm in con.execute(
        "SELECT canonical_printing_id, set_code, printed_collector_number, "
        "normalized_collector_number FROM cards"
    ):
        key = ((set_code or "").strip().upper(), (printed or norm or "").strip())
        if not key[0] or not key[1]:
            continue
        prod_by_key.setdefault(key, []).append(str(cid))
    con.close()

    rc_by_key: dict[tuple[str, str], list[str]] = {}
    with CARDS.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("language") != "zh-Hans":
                continue
            providers = row.get("providerSetIds") or {}
            set_code = str(
                providers.get("pokecardex")
                or providers.get("tcgdex")
                or next(iter(providers.values()), "")
            ).upper()
            printed = str(row.get("printedCollectorNumber") or "").strip()
            key = (set_code, printed)
            if not set_code or not printed:
                continue
            rc_by_key.setdefault(key, []).append(str(row.get("canonicalPrintingId") or ""))

    candidates: list[dict[str, Any]] = []
    for key in sorted(set(prod_by_key) & set(rc_by_key)):
        set_code, collector = key
        candidates.append(
            {
                "status": "candidate",
                "confidence": "soft_set_collector_only",
                "confirmed": False,
                "setCode": set_code,
                "collectorNumber": collector,
                "productionCanonicalPrintingIds": sorted(prod_by_key[key]),
                "rcCanonicalPrintingIds": sorted(x for x in rc_by_key[key] if x),
                "note": (
                    "Soft overlap only. Do not rewrite owned collection IDs. "
                    "Variant/finish/language must be reviewed before confirmation."
                ),
            }
        )

    report = {
        "generatedAtUtc": utc_now(),
        "candidateCount": len(candidates),
        "expectedSoftOverlap": 875,
        "matchesExpected": len(candidates) == 875,
        "policy": {
            "additiveOnly": True,
            "neverRewriteOwnedProductionIds": True,
            "treatAsConfirmed": False,
            "dualRead": "lookups may consult candidates; ownership keys stay production IDs",
        },
        "candidates": candidates,
    }
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # Slim asset for app fixture (ids only, no bulky nested dumps if too large)
    asset = {
        "generatedAtUtc": utc_now(),
        "candidateCount": len(candidates),
        "status": "candidate",
        "entries": [
            {
                "setCode": c["setCode"],
                "collectorNumber": c["collectorNumber"],
                "productionIds": c["productionCanonicalPrintingIds"],
                "rcIds": c["rcCanonicalPrintingIds"],
            }
            for c in candidates
        ],
    }
    app_asset = (
        Path(r"D:\CardScanR_worktrees\global-image-acq-20261013")
        / "card_scanner_app"
        / "assets"
        / "catalogue"
        / "zh_hans_qa_soft_overlap_candidates.json"
    )
    app_asset.parent.mkdir(parents=True, exist_ok=True)
    app_asset.write_text(json.dumps(asset, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "candidateCount": len(candidates),
                "matchesExpected875": len(candidates) == 875,
                "out": str(OUT),
                "appAsset": str(app_asset),
            },
            indent=2,
        )
    )
    return 0 if len(candidates) == 875 else 1


if __name__ == "__main__":
    raise SystemExit(main())
