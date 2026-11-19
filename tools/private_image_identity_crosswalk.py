#!/usr/bin/env python3
"""Read-only exact-printing source-image crosswalk on four SHA-verified packs.

All joins require exact (language, source set ID, normalized printed number,
normalized native card name) and a UNIQUE CardScanR printing. No fuzzy set ID
or translation fallbacks, image downloads, R2 writes or public activation.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import sqlite3
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote
from tools.private_shard_contract import normalize_website_search
from cardscanr_search_index.normalization import split_collector_number
from tools.preflight_multilingual_crossrelease import CANARY, MAX_GZIP, get, inspect_pack
from tools.preflight_legacy_korean_pack import pick
from tools.tcgdex_metadata_batch import API, fetch_json, CARD_ID

TARGETS = (
    ("zh-tw", "SI", 422),
    ("id", "SV4a", 314),
    ("th", "SV7s", 223),
    ("ko", "SV4M", 92),
)


def _number(value: str) -> str:
    return split_collector_number(str(value or ""))[0]


def crosswalk_rows(pack_rows: list[dict], source_cards: list[dict], *, language: str, source_set_id: str) -> tuple[list[dict], dict]:
    expected_set = "tcgdex:asia:set:" + source_set_id
    index: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in pack_rows:
        if row["set_id"] != expected_set or row["language"] != language:
            raise ValueError("pack_set_language_mismatch")
        name = normalize_website_search(row["native_card_name"] or "")
        num = _number(row["printed_collector_number"] or "")
        if not name or not num:
            raise ValueError("incomplete_pack_identity")
        index[(num,name)].append(row)
    results = []
    seen_source_ids = set()
    for card in source_cards:
        if not isinstance(card,dict):
            continue
        cid = card.get("id")
        if not isinstance(cid,str) or not CARD_ID.fullmatch(cid) or not cid.startswith(source_set_id+"-"):
            continue
        if cid in seen_source_ids:
            raise ValueError("duplicate_source_card_identity")
        seen_source_ids.add(cid)
        name = normalize_website_search(str(card.get("name") or ""))
        num = _number(card.get("localId") or "")
        if not name or not num:
            status = "missing_source_name_or_collector"
            matches = []
        else:
            matches = index.get((num,name), [])
            status = "exact_unique" if len(matches)==1 else ("ambiguous" if len(matches)>1 else "unmatched")
        source_art = card.get("image") if isinstance(card.get("image"),str) else None
        matched = matches[0] if len(matches)==1 else None
        pack_art = matched and (matched.get("image_thumbnail_url") or matched.get("image_display_url"))
        results.append({
            "language":language,
            "provider_set_id":source_set_id,
            "provider_card_id":cid,
            "source_local_number":card.get("localId"),
            "status":status,
            "canonical_base_id":matched["canonical_base_id"] if matched else None,
            "canonical_printing_id":matched["canonical_printing_id"] if matched else None,
            "source_image_base_url":source_art,
            "pack_had_image_ref":bool(pack_art),
            "candidate_for_missing_pack_image_ref":bool(matched and source_art and not pack_art),
            "image_binaries_downloaded":0,
            "artwork_rights_verified":False,
        })
    counts=Counter(r["status"] for r in results)
    return results, {
        "language":language, "provider_set_id":source_set_id,
        "pack_rows_in_target_set":len(pack_rows),
        "source_cards_evaluated":len(results),
        "source_cards_with_image_url":sum(bool(x["source_image_base_url"]) for x in results),
        "status_counts":dict(sorted(counts.items())),
        "exact_matched_existing_image_url":sum(x["status"]=="exact_unique" and x["pack_had_image_ref"] for x in results),
        "exact_matched_missing_image_url_with_source_candidate":sum(x["candidate_for_missing_pack_image_ref"] for x in results),
        "canonical_id_mutation":False,
        "artwork_approved":False,
    }


def run(*, fetch=get, provider_fetch=fetch_json) -> tuple[list[dict], dict]:
    manifest = json.loads(fetch(CANARY,2_000_000))
    results = []
    summary = []
    receipts = []
    with tempfile.TemporaryDirectory(prefix="cardscanr-crosswalk-") as td:
        root=Path(td)
        for language, source_set_id, gap in TARGETS:
            desc=pick(manifest,language)
            pack_bytes=fetch(desc["compressedDatabaseUrl"],MAX_GZIP)
            destination=root/(language+".sqlite")
            inspect_pack(desc,pack_bytes,destination)
            uri="file:"+quote(destination.resolve().as_posix(),safe="/:")+"?mode=ro&immutable=1"
            with sqlite3.connect(uri,uri=True) as db:
                db.execute("PRAGMA query_only=ON")
                cols=("canonical_base_id","canonical_printing_id","set_id","language",
                      "printed_collector_number","native_card_name","image_thumbnail_url","image_display_url")
                selected=list(db.execute(
                    "SELECT "+",".join(cols)+" FROM cards WHERE set_id=? AND language=?",
                    ("tcgdex:asia:set:"+source_set_id,language),
                ))
                pack_rows=[dict(zip(cols,row)) for row in selected]
            if not pack_rows:
                raise ValueError("exact_audited_pack_set_not_found:"+language+":"+source_set_id)
            source_url=f"{API}/{language}/sets/{source_set_id}"
            source,raw=provider_fetch(source_url)
            if not isinstance(source,dict) or source.get("id")!=source_set_id or not isinstance(source.get("cards"),list):
                raise ValueError("wrong_source_set_or_missing_cards")
            rows,report=crosswalk_rows(pack_rows,source["cards"],language=language,source_set_id=source_set_id)
            report["expected_pack_missing_image_url_rows"]=gap
            report["pack_missing_image_url_rows"]=sum(
                not r.get("image_thumbnail_url") and not r.get("image_display_url")
                for r in pack_rows
            )
            report["pack_sha256_verified"]=True
            results.extend(rows)
            summary.append(report)
            receipts.append({
                "language":language,"provider_set_id":source_set_id,
                "source_url":source_url,
                "source_response_sha256":hashlib.sha256(raw).hexdigest(),
                "pack_sha256":desc["sha256"],
            })
    report={
        "classification":"PRIVATE_IDENTITY_CROSSWALK_REVIEW_ONLY",
        "source_packs_sha256_verified":4,
        "audited_gap_rows_in_targets":sum(x[2] for x in TARGETS),
        "source_candidates":len(results),
        "exact_unique_matches":sum(r["status"]=="exact_unique" for r in results),
        "matching_missing_image_url_with_source_ref":sum(r["candidate_for_missing_pack_image_ref"] for r in results),
        "by_language":summary,
        "source_receipts":receipts,
        "image_binaries_downloaded":0,"production_writes":0,
        "customer_data_read":False,"canonical_id_mutation":False,
        "artwork_rights_verified":False,"safe_to_publish":False,
    }
    return results,report


def write_report(output: Path, rows:list[dict], report:dict) -> None:
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise FileExistsError("output_must_be_new_empty_directory")
    output.mkdir(parents=True,exist_ok=True)
    row_data="".join(json.dumps(r,ensure_ascii=False,sort_keys=True)+"\n" for r in rows).encode("utf-8")
    (output/"crosswalk_candidates.jsonl").write_bytes(row_data)
    report["candidates_sha256"]=hashlib.sha256(row_data).hexdigest()
    report["candidates_bytes"]=len(row_data)
    (output/"receipt.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    rows,report=run()
    write_report(args.output,rows,report)
    print(json.dumps({
        "pack_sets":len(report["by_language"]),
        "source_candidates":report["source_candidates"],
        "exact_unique_matches":report["exact_unique_matches"],
        "matching_missing_image_url_with_source_ref":report["matching_missing_image_url_with_source_ref"],
        "by_language":report["by_language"],
        "image_binaries_downloaded":0,"safe_to_publish":False,
    },sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
