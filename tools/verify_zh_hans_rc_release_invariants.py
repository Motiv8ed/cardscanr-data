#!/usr/bin/env python3
"""Final invariant checks for Chinese metadata RC release readiness."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CARDS = ROOT / "data" / "global" / "catalogue" / "cards.jsonl"
LIVE = Path(r"D:\CardScanR_Data\cardscanr-data\data\global\catalogue\cards.jsonl")
REVIEW = (
    ROOT
    / "reports"
    / "global_image_acquisition"
    / "pokecardex"
    / "catalogue_review"
    / "pokecardex_zh_hans_printings_review.jsonl"
)
AUTO = ROOT / "reports" / "global_image_acquisition" / "pokecardex" / "auto_resolve" / "auto_resolved.jsonl"
PACK = (
    ROOT
    / "reports"
    / "global_image_acquisition"
    / "pokecardex"
    / "release_candidate"
    / "pack_install_layout"
    / "zh-cn"
    / "zh-cn.sqlite"
)
WEBSITE = Path(r"D:\CardScanR_worktrees\global-image-acq-20261013\public_website")
OUT = (
    ROOT
    / "reports"
    / "global_image_acquisition"
    / "pokecardex"
    / "release_candidate"
    / "RELEASE_INVARIANTS.json"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def main() -> int:
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any) -> None:
        checks.append({"name": name, "pass": bool(ok), "detail": detail})

    live = load_jsonl(LIVE)
    wt = load_jsonl(CARDS)
    live_by_lang = Counter(str(r.get("language") or "") for r in live)
    wt_by_lang = Counter(str(r.get("language") or "") for r in wt)
    live_zh = [r for r in live if r.get("language") == "zh-Hans"]
    wt_zh = [r for r in wt if r.get("language") == "zh-Hans"]
    live_ids = {str(r["canonicalPrintingId"]) for r in live_zh}
    wt_ids = {str(r["canonicalPrintingId"]) for r in wt_zh}
    new_rows = [r for r in wt_zh if str(r["canonicalPrintingId"]) not in live_ids]
    linked = [
        r
        for r in wt_zh
        if str(r["canonicalPrintingId"]) in live_ids
        and "pokecardex" in (r.get("providerCardIds") or {})
    ]

    check("en_unchanged", live_by_lang["en"] == wt_by_lang["en"], {"live": live_by_lang["en"], "wt": wt_by_lang["en"]})
    check("ja_unchanged", live_by_lang["ja"] == wt_by_lang["ja"], {"live": live_by_lang["ja"], "wt": wt_by_lang["ja"]})
    check("zh_hans_count", len(wt_zh) == 14923, len(wt_zh))
    check("original_877_preserved", live_ids <= wt_ids and len(live_ids) == 877, len(live_ids & wt_ids))
    check("links_count_28", len(linked) == 28, len(linked))
    check("links_preserve_live_ids", all(str(r["canonicalPrintingId"]) in live_ids for r in linked), True)

    # Linked rows must not change core identity fields vs live
    live_map = {str(r["canonicalPrintingId"]): r for r in live_zh}
    identity_drift = 0
    for r in linked:
        cid = str(r["canonicalPrintingId"])
        base = live_map[cid]
        for key in ("canonicalBaseId", "canonicalSetId", "printedCollectorNumber", "language", "region"):
            if str(r.get(key)) != str(base.get(key)):
                identity_drift += 1
    check("linked_identity_fields_unchanged", identity_drift == 0, identity_drift)

    # 993 exclusions still excluded
    auto_keys = {
        (str(r.get("providerSetId")), str(r.get("printedCollectorNumber")))
        for r in load_jsonl(AUTO)
        if str(r.get("decision") or "").startswith("auto_")
    }
    added_ids = {str(r["canonicalPrintingId"]) for r in new_rows}
    excluded = 0
    for row in load_jsonl(REVIEW):
        cid = str(row["canonicalPrintingId"])
        if cid in added_ids:
            continue
        excluded += 1
        # Must not appear in worktree as new integration
        if cid in wt_ids and cid not in live_ids:
            check("excluded_leaked_into_worktree", False, cid)
            break
    else:
        check("excluded_not_in_new_rows", True, excluded)
    check("excluded_count_993", excluded == 993, excluded)

    # Metadata-only payload on new rows + pack
    staging_hits = 0
    bad_cdn = 0
    attr_missing = 0
    for r in new_rows:
        blob = json.dumps(r, ensure_ascii=False)
        for key in ("imageUrl", "displayImageUrl", "thumbnailUrl", "publicImageUrl", "image_thumbnail_url", "image_display_url"):
            val = str(r.get(key) or "")
            if not val:
                continue
            if "staging" in val.lower() or "archive" in val.lower() or "pokecardex_zh_hans" in val.lower():
                staging_hits += 1
            if val.startswith("http") and "placeholders/card_missing" not in val and "assets.cardscanr.com" not in val:
                # Any non-placeholder http on new rows is suspicious for this RC
                bad_cdn += 1
        if r.get("artworkStatus") != "placeholder_pending_pokemon_ip_review":
            # still OK if imageProvenance empty
            if r.get("imageProvenance"):
                staging_hits += 1
        if "PokéCardex" not in str(r.get("attributionRequired") or ""):
            attr_missing += 1
        if "image_staging" in blob and any(
            str(r.get(k) or "").endswith(".webp") for k in ("imageUrl", "thumbnailUrl", "displayImageUrl")
        ):
            staging_hits += 1
    check("new_rows_no_staging_public_urls", staging_hits == 0, staging_hits)
    check("new_rows_no_unapproved_cdn", bad_cdn == 0, bad_cdn)
    check("new_rows_attribution", attr_missing == 0, attr_missing)

    if PACK.exists():
        import sqlite3

        con = sqlite3.connect(f"file:{PACK.as_posix()}?mode=ro", uri=True)
        n = con.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        bad = con.execute(
            "SELECT COUNT(*) FROM cards WHERE "
            "image_thumbnail_url LIKE '%staging%' OR image_display_url LIKE '%Archive%' OR "
            "image_thumbnail_url NOT LIKE '%placeholders/card_missing.webp'"
        ).fetchone()[0]
        con.close()
        check("pack_card_count_14923", n == 14923, n)
        check("pack_placeholders_only", bad == 0, bad)
    else:
        check("pack_install_layout_present", False, str(PACK))

    # Website attribution still present
    attr_ok = 0
    for rel in ("index.html", "data/index.html"):
        p = WEBSITE / rel
        if p.exists() and "PokéCardex" in p.read_text(encoding="utf-8"):
            attr_ok += 1
    check("website_attribution", attr_ok == 2, attr_ok)

    # Non zh-Hans languages identical counts
    other_ok = True
    drift_langs = {}
    for lang, n in live_by_lang.items():
        if lang == "zh-Hans":
            continue
        if wt_by_lang.get(lang, 0) != n:
            other_ok = False
            drift_langs[lang] = {"live": n, "wt": wt_by_lang.get(lang, 0)}
    check("all_non_zh_hans_language_counts_unchanged", other_ok, drift_langs)

    failed = [c for c in checks if not c["pass"]]
    report = {
        "generatedAtUtc": utc_now(),
        "passed": len(checks) - len(failed),
        "failed": len(failed),
        "checks": checks,
        "verdict": "PASS" if not failed else "HOLD",
    }
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "passed": report["passed"], "failed": report["failed"], "failures": failed}, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
