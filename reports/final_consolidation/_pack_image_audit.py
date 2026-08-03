#!/usr/bin/env python3
"""Audit local catalogue packs for image URL quality gates."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(r"D:\CardScanR_worldwide_runtime_20260802\publication\packs_canary4_20260803\sqlite")
OUT = Path(__file__).resolve().parent / "PACK_IMAGE_AUDIT.json"


def audit_db(db: Path) -> dict:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    cur = con.cursor()
    tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    info: dict = {
        "path": str(db),
        "bytes": db.stat().st_size,
        "tables": sorted(tables),
        "quick_check": cur.execute("PRAGMA quick_check").fetchone()[0],
    }
    if "cards" in tables:
        cols = {r[1] for r in cur.execute("PRAGMA table_info(cards)")}
        display = (
            "image_display_url"
            if "image_display_url" in cols
            else ("display_image_url" if "display_image_url" in cols else None)
        )
        info["card_columns_has_display"] = display
        if display:
            info["total_cards"] = cur.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
            info["null_card_display"] = cur.execute(
                f"SELECT COUNT(*) FROM cards WHERE {display} IS NULL OR trim(coalesce({display}, '')) = ''"
            ).fetchone()[0]
            info["placeholder_cards"] = cur.execute(
                f"SELECT COUNT(*) FROM cards WHERE coalesce({display}, '') LIKE '%placeholder%'"
            ).fetchone()[0]
            info["r2_cards"] = cur.execute(
                f"SELECT COUNT(*) FROM cards WHERE coalesce({display}, '') LIKE '%r2.dev%' "
                f"OR coalesce({display}, '') LIKE '%cardscanr%'"
            ).fetchone()[0]
            info["third_party_cards"] = cur.execute(
                f"""
                SELECT COUNT(*) FROM cards
                WHERE coalesce({display}, '') != ''
                  AND {display} NOT LIKE '%r2.dev%'
                  AND {display} NOT LIKE '%cardscanr%'
                  AND {display} NOT LIKE '%placeholder%'
                  AND {display} NOT LIKE '%pages.dev%'
                  AND {display} NOT LIKE '%workers.dev%'
                """
            ).fetchone()[0]
            info["localhost_or_file"] = cur.execute(
                f"""
                SELECT COUNT(*) FROM cards
                WHERE coalesce({display}, '') LIKE '%localhost%'
                   OR coalesce({display}, '') LIKE 'file:%'
                   OR coalesce({display}, '') LIKE 'C:\\%'
                   OR coalesce({display}, '') LIKE 'D:\\%'
                """
            ).fetchone()[0]
            samples = cur.execute(
                f"""
                SELECT {display} FROM cards
                WHERE coalesce({display}, '') != ''
                  AND {display} NOT LIKE '%r2.dev%'
                  AND {display} NOT LIKE '%placeholder%'
                LIMIT 5
                """
            ).fetchall()
            info["non_r2_samples"] = [s[0][:160] if s[0] else None for s in samples]
            fts = cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%fts%'"
            ).fetchall()
            info["fts_tables"] = [r[0] for r in fts]
            if fts:
                try:
                    info["fts_probe"] = cur.execute(
                        f"SELECT COUNT(*) FROM {fts[0][0]} WHERE {fts[0][0]} MATCH 'pikachu'"
                    ).fetchone()[0]
                except sqlite3.Error as exc:
                    info["fts_probe_error"] = str(exc)
    if "sealed_products" in tables:
        cols = {r[1] for r in cur.execute("PRAGMA table_info(sealed_products)")}
        col = "image_url" if "image_url" in cols else None
        if col:
            info["product_total"] = cur.execute(
                "SELECT COUNT(*) FROM sealed_products"
            ).fetchone()[0]
            info["null_product"] = cur.execute(
                f"SELECT COUNT(*) FROM sealed_products WHERE {col} IS NULL OR trim(coalesce({col}, '')) = ''"
            ).fetchone()[0]
            info["placeholder_products"] = cur.execute(
                f"SELECT COUNT(*) FROM sealed_products WHERE coalesce({col}, '') LIKE '%placeholder%'"
            ).fetchone()[0]
            info["third_party_products"] = cur.execute(
                f"""
                SELECT COUNT(*) FROM sealed_products
                WHERE coalesce({col}, '') != ''
                  AND {col} NOT LIKE '%r2.dev%'
                  AND {col} NOT LIKE '%cardscanr%'
                  AND {col} NOT LIKE '%placeholder%'
                  AND {col} NOT LIKE '%pages.dev%'
                  AND {col} NOT LIKE '%workers.dev%'
                """
            ).fetchone()[0]
    con.close()
    return info


def main() -> None:
    results = {}
    for db in sorted(ROOT.glob("*.sqlite")):
        print(f"auditing {db.name}...", flush=True)
        results[db.name] = audit_db(db)
    OUT.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "tables"} for k, v in results.items()}, indent=2))


if __name__ == "__main__":
    main()
