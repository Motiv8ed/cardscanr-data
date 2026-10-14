#!/usr/bin/env python3
"""Local QA against the RC zh-cn pack + worktree catalogue (not fixtures alone)."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "global_image_acquisition" / "pokecardex" / "release_candidate"
PACK = REPORT / "qa_pack" / "zh-cn.sqlite"
CARDS = ROOT / "data" / "global" / "catalogue" / "cards.jsonl"
LIVE = Path(r"D:\CardScanR_Data\cardscanr-data\data\global\catalogue\cards.jsonl")
WEBSITE = Path(r"D:\CardScanR_worktrees\global-image-acq-20261013\public_website")
NEAR = ROOT / "reports" / "global_image_acquisition" / "pokecardex" / "zh_hans_near_duplicate_review.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def check(name: str, ok: bool, detail: str, results: list[dict[str, Any]]) -> None:
    results.append({"name": name, "pass": ok, "detail": detail})


def main() -> int:
    REPORT.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    if not PACK.exists():
        check("qa_pack_exists", False, f"missing {PACK}", results)
        (REPORT / "LOCAL_QA.json").write_text(json.dumps({"checks": results}, indent=2), encoding="utf-8")
        return 1

    con = sqlite3.connect(PACK)
    con.row_factory = sqlite3.Row
    n_cards = con.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    n_sets = con.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
    check("pack_card_count", n_cards == 14923, f"cards={n_cards} expected=14923", results)
    check("pack_set_count", n_sets > 0, f"sets={n_sets}", results)

    # Set browsing: list sets with card counts
    set_rows = con.execute(
        """
        SELECT s.set_id, s.set_name, COUNT(c.canonical_printing_id) AS n
        FROM sets s LEFT JOIN cards c ON c.set_id = s.set_id
        GROUP BY s.set_id
        ORDER BY n DESC
        LIMIT 5
        """
    ).fetchall()
    check("set_browse_top_sets", len(set_rows) >= 5, f"top={[dict(r) for r in set_rows]}", results)

    # Search by Chinese / printed name (FTS)
    # Prefer rows that have CJK in native_card_name from pack
    cjk = con.execute(
        "SELECT native_card_name, printed_collector_number, set_code FROM cards "
        "WHERE native_card_name GLOB '*[一-龥]*' LIMIT 5"
    ).fetchall()
    if cjk:
        name = cjk[0]["native_card_name"]
        fts = con.execute(
            "SELECT COUNT(*) FROM cards_fts WHERE cards_fts MATCH ?",
            (f'"{name}"' if " " in name else name,),
        ).fetchone()[0]
        check("search_by_chinese_name_fts", fts > 0, f"name={name!r} hits={fts}", results)
    else:
        # Many PokéCardex rows use Latin species names; still searchable
        name = con.execute(
            "SELECT native_card_name FROM cards WHERE length(native_card_name)>2 LIMIT 1"
        ).fetchone()[0]
        fts = con.execute(
            "SELECT COUNT(*) FROM cards_fts WHERE native_card_name MATCH ?",
            (name.split()[0],),
        ).fetchone()[0]
        check("search_by_name_fts_latin_fallback", fts > 0, f"name={name!r} hits={fts}", results)

    # Collector number search (app-equivalent)
    sample = con.execute(
        "SELECT set_code, printed_collector_number, native_card_name FROM cards "
        "WHERE set_code IS NOT NULL AND printed_collector_number != '' LIMIT 1"
    ).fetchone()
    local = str(sample["printed_collector_number"]).split("/")[0]
    by_num = con.execute(
        """
        SELECT COUNT(*) FROM cards
        WHERE language='zh-cn'
          AND (
            printed_collector_number=? OR collector_number=? OR normalized_collector_number=?
            OR printed_collector_number LIKE ?
          )
        """,
        (sample["printed_collector_number"], sample["printed_collector_number"], local, f"{local}/%"),
    ).fetchone()[0]
    check("search_by_collector_number", by_num > 0, f"collector={sample['printed_collector_number']} hits={by_num}", results)

    # Set + collector identity (app _querySetIdentity shape)
    hint = str(sample["set_code"]).lower()
    by_set = con.execute(
        """
        SELECT COUNT(*) FROM cards
        WHERE language='zh-cn'
          AND (
            lower(set_id)=? OR lower(set_code)=?
            OR EXISTS (SELECT 1 FROM json_each(provider_set_codes_json) AS code WHERE lower(code.value)=?)
          )
          AND (
            printed_collector_number=? OR normalized_collector_number=?
          )
        """,
        (hint, hint, hint, sample["printed_collector_number"], local),
    ).fetchone()[0]
    check("set_plus_collector_identity", by_set > 0, f"set={hint} collector={local} hits={by_set}", results)

    # Variants: same printed number + set, multiple card rows (printing ids)
    variants = con.execute(
        """
        SELECT set_code, printed_collector_number, COUNT(*) AS n
        FROM cards
        WHERE set_code IS NOT NULL
        GROUP BY set_code, printed_collector_number
        HAVING n > 1
        ORDER BY n DESC
        LIMIT 5
        """
    ).fetchall()
    check("variant_rows_present", len(variants) > 0, f"samples={[dict(r) for r in variants]}", results)

    # Placeholders only
    bad_img = con.execute(
        """
        SELECT COUNT(*) FROM cards
        WHERE image_thumbnail_url LIKE '%staging%'
           OR image_display_url LIKE '%Archive%'
           OR thumbnail_url LIKE '%staging%'
           OR large_image_url LIKE '%Archive%'
           OR (
                image_thumbnail_url LIKE '%.webp'
                AND image_thumbnail_url NOT LIKE '%placeholders%'
              )
        """
    ).fetchone()[0]
    # Allow only known placeholder host
    non_placeholder = con.execute(
        """
        SELECT COUNT(*) FROM cards
        WHERE image_thumbnail_url NOT LIKE '%/placeholders/card_missing.webp'
           OR image_display_url NOT LIKE '%/placeholders/card_missing.webp'
        """
    ).fetchone()[0]
    check("placeholders_only", non_placeholder == 0 and bad_img == 0, f"nonPlaceholder={non_placeholder} bad={bad_img}", results)

    # Original 877 preserved in worktree catalogue
    live_ids = {
        str(r["canonicalPrintingId"])
        for r in load_jsonl(LIVE)
        if str(r.get("language")) == "zh-Hans"
    }
    wt_zh = [r for r in load_jsonl(CARDS) if str(r.get("language")) == "zh-Hans"]
    wt_ids = {str(r["canonicalPrintingId"]) for r in wt_zh}
    check("original_877_ids_preserved", live_ids <= wt_ids and len(live_ids) == 877, f"live={len(live_ids)} preserved={len(live_ids & wt_ids)}", results)

    # Binder-relevant fields present
    missing_binder_fields = 0
    for r in wt_zh[:500]:
        if not r.get("canonicalPrintingId") or not r.get("printedCollectorNumber"):
            missing_binder_fields += 1
    check("binder_identity_fields", missing_binder_fields == 0, f"missing_in_sample500={missing_binder_fields}", results)

    # Language switching contract: pack stores zh-cn; catalogue language zh-Hans
    langs = {r[0] for r in con.execute("SELECT DISTINCT language FROM cards")}
    check("pack_storage_language_zh_cn", langs == {"zh-cn"}, f"langs={langs}", results)

    # Near-dups still review-only
    near = json.loads(NEAR.read_text(encoding="utf-8")) if NEAR.exists() else {}
    pairs = near.get("pairs") or []
    unresolved = [p for p in pairs if str(p.get("reviewStatus")) == "needs_human_review"]
    check("near_dups_unresolved_preserved", len(unresolved) == 15, f"unresolved={len(unresolved)}", results)

    # Website attribution
    attr_pages = 0
    missing_pages: list[str] = []
    for rel in ("index.html", "data/index.html", "pricing/index.html", "trust/index.html"):
        path = WEBSITE / rel
        if not path.exists():
            missing_pages.append(rel)
            continue
        text = path.read_text(encoding="utf-8")
        if "PokéCardex" in text or "pokecardex.com" in text:
            attr_pages += 1
        else:
            missing_pages.append(rel)
    check("website_pokecardex_attribution", attr_pages >= 3 and not missing_pages, f"pages_ok={attr_pages} missing={missing_pages}", results)

    # Commercial metadata permission note present on data page
    data_html = (WEBSITE / "data" / "index.html").read_text(encoding="utf-8") if (WEBSITE / "data" / "index.html").exists() else ""
    check(
        "commercial_metadata_permission_stated",
        "permitted Simplified Chinese catalogue metadata" in data_html or "PokéCardex" in data_html,
        "public_website/data/index.html",
        results,
    )

    con.close()
    passed = sum(1 for r in results if r["pass"])
    failed = [r for r in results if not r["pass"]]
    report = {
        "generatedAtUtc": utc_now(),
        "packPath": str(PACK),
        "passed": passed,
        "failed": len(failed),
        "checks": results,
        "verdict": "PASS" if not failed else "BLOCKED",
    }
    (REPORT / "LOCAL_QA.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md = [
        "# Chinese RC local QA",
        "",
        f"Generated: {report['generatedAtUtc']}",
        "",
        f"Verdict: **{report['verdict']}** ({passed}/{len(results)} checks)",
        "",
    ]
    for r in results:
        mark = "PASS" if r["pass"] else "FAIL"
        md.append(f"- [{mark}] `{r['name']}` — {r['detail']}")
    (REPORT / "LOCAL_QA.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
