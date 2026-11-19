#!/usr/bin/env python3
"""Private large source refresh: 113 exact set-language IDs with card provenance.

Selected by matching the owner's 158803-card artwork vault to the independently
saved TCGdex 808-set roster. Missing counts reflect cards without ANY vault
image URL (original OR existing 2286 exact-candidate overlay), not proof that
a source image exists. Downloads 5 SHA-pinned catalogue packs and TCGdex set
JSON only. No image binaries, no customer data, no public write or rights grant.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import sqlite3
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from tools.preflight_multilingual_crossrelease import CANARY, MAX_GZIP, get, inspect_pack
from tools.preflight_legacy_korean_pack import pick
from tools.tcgdex_metadata_batch import API, fetch_json
from tools.private_image_identity_crosswalk import crosswalk_rows
from tools.private_artwork_takedown_controls import _safe_image_base

# Language-specific exact source ID; expected *remaining* currently-unlinked
# cards in owner vault. Original pack has at least this many gaps.
SOURCE_TARGETS = """
zh-cn SV8a 237
zh-tw SC1a 172
zh-tw SC1b 171
id SV6s 167
zh-tw SC1D 164
zh-tw SC2D 157
zh-tw SCB 156
zh-tw SVD 147
zh-tw SC2a 145
zh-tw SC2b 144
zh-tw SV3 138
zh-tw SCA 135
zh-cn SV7 132
zh-tw SV7 132
zh-tw SV6 130
zh-tw S12 114
zh-cn SV8 106
zh-tw SV8 106
th SV6 101
zh-cn SV9 100
zh-tw S11 100
zh-tw S4 100
zh-tw S8 100
zh-tw S9 100
zh-tw SV9 100
zh-cn SV10 98
zh-tw SV10 98
zh-tw SV5M 97
zh-tw SV-P 94
ko SV4M 92
th SV4K 92
th SV4M 92
zh-cn SV9a 92
zh-tw SV9a 92
th SV3a 89
zh-tw SV3a 89
ko SV4K 87
zh-tw S10b 79
id SV1V 78
th SV1S 78
th SV1V 78
zh-tw SV1S 78
zh-tw SV1V 78
id SV1a 73
th SV1a 73
zh-tw SV1a 73
id SV2D 71
id SV2P 71
th SV2D 71
th SV2P 71
zh-tw S10a 71
zh-tw SV2D 71
zh-tw SV2P 71
th S5a 70
zh-tw S5I 70
zh-tw S5R 70
zh-tw S5a 70
zh-tw S6H 70
zh-tw S6K 70
zh-tw S6a 69
th S5I 68
zh-tw S11a 68
zh-tw S10D 67
zh-tw S10P 67
zh-tw S7D 67
zh-tw S7R 67
zh-tw S9a 67
th SV5a 66
zh-tw SV4K 66
zh-tw SV4M 66
zh-tw SV5a 66
zh-cn SV7a 64
zh-tw SV6a 64
zh-tw SV7a 64
th S5R 59
zh-tw SH 59
zh-cn CSMPiC 48
th SV5K 46
zh-tw SV5K 46
zh-tw SVF 46
zh-tw SVB 36
zh-tw S8a 33
zh-tw SJ 30
zh-tw SK 30
id SVAM 25
id SVAW 25
zh-tw SN 25
zh-tw SVAM 25
zh-tw SVAW 25
id SVHK 24
id SVHM 24
ko SV5K 24
th SVHK 24
th SVHM 24
zh-tw SVHK 24
zh-tw SVHM 24
id SVAL 23
zh-tw SVAL 23
zh-tw SDM 22
zh-tw SDP 22
zh-tw SLL 22
zh-tw SVC 22
zh-tw SDL 21
zh-tw SLD 21
zh-tw SVEL 21
zh-tw SVEM 21
zh-tw SPD 20
zh-tw SPZ 20
th S10a 8
zh-tw SVP1 7
zh-tw SP6 6
zh-tw SP5 4
th SV-P 1
"""
TARGETS=tuple(
    (line.split()[0],line.split()[1],int(line.split()[2]))
    for line in SOURCE_TARGETS.splitlines() if line.strip()
)
LANGUAGES=("zh-tw","id","th","ko","zh-cn")
EXPECTED_REMAINING_UNLINKED_GAPS=8077
COLS=("canonical_base_id","canonical_printing_id","set_id","language",
      "printed_collector_number","native_card_name","image_thumbnail_url","image_display_url")


def validate_targets(targets=TARGETS)->None:
    if not targets or len(targets)>130 or len({(l,s) for l,s,_ in targets})!=len(targets):
        raise ValueError("duplicate_or_too_many_targets")
    if any(l not in LANGUAGES or not re.fullmatch(r"[A-Za-z0-9_.-]{1,48}",s)
           or not isinstance(g,int) or g<1 for l,s,g in targets):
        raise ValueError("invalid_target")


def source_summary(payload,lang,sid,raw)->dict:
    if not isinstance(payload,dict) or payload.get("id")!=sid or not isinstance(payload.get("cards"),list):
        raise ValueError("provider_source_set_mismatch")
    ids=[c.get("id") for c in payload["cards"] if isinstance(c,dict)]
    if len(ids)!=len(payload["cards"]) or len(ids)!=len(set(ids)):
        raise ValueError("bad_or_duplicate_source_cards")
    count=payload.get("cardCount")
    return {
        "language":lang,"provider_set_id":sid,
        "source_set_id":"tcgdex:asia:set:"+sid,
        "native_set_name":payload.get("name"),
        "release_date":payload.get("releaseDate"),
        "card_count_total":count.get("total") if isinstance(count,dict) else None,
        "card_count_official":count.get("official") if isinstance(count,dict) else None,
        "provider_card_briefs":len(ids),
        "source_url":API+"/"+lang+"/sets/"+sid,
        "source_response_sha256":hashlib.sha256(raw).hexdigest(),
        "source_provider":"tcgdex",
        "image_files_downloaded":0,"artwork_rights_granted":False,
    }


def run(*,fetch=get,source_fetch=fetch_json,pace=0.75,targets=TARGETS):
    validate_targets(targets)
    if not 0<=pace<=3:
        raise ValueError("invalid_pacing")
    manifest=json.loads(fetch(CANARY,2_000_000))
    all_matches=[]
    source_sets=[]
    failures=[]
    requests=0
    stopped_429=False
    pack_proofs=[]
    with tempfile.TemporaryDirectory(prefix="cardscanr-113sets-") as td:
        for lang in LANGUAGES:
            group=[(s,g) for l,s,g in targets if l==lang]
            if not group:
                continue
            desc=pick(manifest,lang)
            zipped=fetch(desc["compressedDatabaseUrl"],MAX_GZIP)
            target_db=Path(td)/(lang+".sqlite")
            inspect_pack(desc,zipped,target_db)
            pack_proofs.append({"language":lang,"sha256":desc["sha256"]})
            uri="file:"+quote(target_db.resolve().as_posix(),safe="/:")+"?mode=ro&immutable=1"
            with sqlite3.connect(uri,uri=True) as db:
                db.execute("PRAGMA query_only=ON")
                for sid,remaining_gap in group:
                    raw_rows=db.execute(
                        "SELECT "+",".join(COLS)+" FROM cards WHERE set_id=? AND language=?",
                        ("tcgdex:asia:set:"+sid,lang),
                    ).fetchall()
                    rows=[dict(zip(COLS,r)) for r in raw_rows]
                    original_gap=sum(not(r["image_thumbnail_url"] or r["image_display_url"]) for r in rows)
                    if not rows or original_gap<remaining_gap:
                        raise ValueError("source_pack_does_not_cover_owner_gap:"+lang+"/"+sid)
                    requests+=1
                    if requests>len(targets):
                        raise ValueError("source_request_budget_exceeded")
                    if requests>1 and pace:
                        time.sleep(pace)
                    try:
                        payload,raw=source_fetch(API+"/"+lang+"/sets/"+sid)
                        if not isinstance(raw,bytes) or len(raw)>2_000_000:
                            raise ValueError("source_response_too_large")
                        brief=source_summary(payload,lang,sid,raw)
                        projected,matching=crosswalk_rows(
                            rows,payload["cards"],language=lang,source_set_id=sid,
                        )
                    except (ValueError,TypeError,KeyError,OSError,URLError,HTTPError) as exc:
                        failures.append({
                            "language":lang,"provider_set_id":sid,
                            "exception_type":type(exc).__name__,
                            "http_status":getattr(exc,"code",None),
                        })
                        if isinstance(exc,HTTPError) and exc.code==429:
                            stopped_429=True
                            break
                        continue
                    candidates=0
                    for row in projected:
                        base=row.get("source_image_base_url")
                        row["source_set_sha256"]=brief["source_response_sha256"]
                        row["source_set_name"]=brief["native_set_name"]
                        row["source_set_release_date"]=brief["release_date"]
                        row["url_passes_host_allowlist"]=bool(base and _safe_image_base(base))
                        row["provider_low_webp_url"]=(base+"/low.webp") if row["url_passes_host_allowlist"] else None
                        row["candidate_for_missing_pack_image_ref"]=bool(
                            row["candidate_for_missing_pack_image_ref"] and row["url_passes_host_allowlist"]
                        )
                        if row["candidate_for_missing_pack_image_ref"]:
                            candidates+=1
                    brief.update({
                        "original_pack_missing_image_refs":original_gap,
                        "owner_remaining_unlinked_gap_minimum":remaining_gap,
                        "exact_unique_matches":matching["status_counts"].get("exact_unique",0),
                        "ambiguous_matches":matching["status_counts"].get("ambiguous",0),
                        "source_image_candidates":candidates,
                        "status_counts":matching["status_counts"],
                    })
                    source_sets.append(brief)
                    all_matches.extend(projected)
                if stopped_429:
                    break

    canonical_ids=[row["canonical_printing_id"] for row in all_matches
                   if row["status"]=="exact_unique"]
    if len(set(canonical_ids))!=len(canonical_ids):
        raise ValueError("duplicate_printing_across_provider_sets")
    new_candidates=[
        r for r in all_matches if r.get("candidate_for_missing_pack_image_ref")
    ]
    by_lang={
        lang:{
            "source_sets":sum(s["language"]==lang for s in source_sets),
            "exact_card_matches":sum(r["language"]==lang and r["status"]=="exact_unique" for r in all_matches),
            "image_url_candidates":sum(r["language"]==lang for r in new_candidates)
        } for lang in LANGUAGES
    }
    counts=Counter(r["status"] for r in all_matches)
    return all_matches,source_sets,{
        "classification":"PRIVATE_113_SET_METADATA_AND_IMAGE_LINK_REFRESH",
        "source_target_count":len(targets),
        "owner_remaining_unlinked_gaps_in_target_sets":sum(x[2] for x in targets),
        "source_requests":requests,"source_request_budget":len(targets),
        "source_sets_succeeded":len(source_sets),
        "source_sets_failed":len(failures),
        "source_failures":failures,"stopped_for_429":stopped_429,
        "exact_matched_cards":len(canonical_ids),
        "matched_source_card_briefs":len(all_matches),
        "match_status_counts":dict(sorted(counts.items())),
        "image_ref_candidates_for_original_pack_gaps":len(new_candidates),
        "image_source_count_by_language":by_lang,
        "pack_sha256_verified":pack_proofs,
        "source_set_evidence":source_sets,
        "retrieved_image_binary_files":0,
        "production_writes":0,
        "customer_data_read":False,
        "rights_granted":False,
        "safe_for_public_use":False,
        "generated_utc":datetime.now(timezone.utc).isoformat(),
    }


def save(path:Path,matches:list[dict],sets:list[dict],report:dict):
    if path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
        raise FileExistsError("new_empty_report_directory_required")
    path.mkdir(parents=True,exist_ok=True)
    for name,records in (
        ("matching_results.jsonl",matches),
        ("source_set_details.jsonl",sets),
        ("image_reference_candidates.jsonl",[
            r for r in matches if r["candidate_for_missing_pack_image_ref"]
        ]),
    ):
        body="".join(json.dumps(r,ensure_ascii=False,sort_keys=True)+"\n" for r in records).encode()
        (path/name).write_bytes(body)
        report[name]={"records":len(records),"bytes":len(body),
                      "sha256":hashlib.sha256(body).hexdigest()}
    (path/"receipt.json").write_text(
        json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2)+"\n",
        encoding="utf-8",
    )


def main()->int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--pace",type=float,default=0.75)
    args=parser.parse_args()
    result=run(pace=args.pace)
    save(args.output,*result)
    rows,sets,proof=result
    print(json.dumps({
        "source_sets_succeeded":proof["source_sets_succeeded"],
        "source_sets_failed":proof["source_sets_failed"],
        "matched_cards":proof["exact_matched_cards"],
        "image_url_candidates":proof["image_ref_candidates_for_original_pack_gaps"],
        "by_language":proof["image_source_count_by_language"],
        "failed":proof["source_failures"],
        "image_binaries_downloaded":0,
        "safe_for_public_use":False
    },ensure_ascii=False,sort_keys=True))
    return 0 if len(sets)>0 else 2


if __name__=="__main__":
    raise SystemExit(main())
