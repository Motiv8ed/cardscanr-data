#!/usr/bin/env python3
"""Read-only SHA-verified Chinese catalogue pack consistency of source candidates.

Independent source/name corroboration is NOT proof of variant, finish, URL
permission or current live vault identity. No writes outside private CI output.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import quote

from tools.preflight_multilingual_crossrelease import CANARY, MAX_GZIP, get, inspect_pack
from tools.preflight_legacy_korean_pack import pick

EXPECTED_PACK_COUNT = 41185
EXPECTED_SOURCE_CANDIDATES = 5289
EXPECTED_NAME_CORROBORATED = 5238

def code(s):
    return "".join(c for c in s.strip().upper() if c.isalnum()) if isinstance(s, str) else ""

def number(s):
    if not isinstance(s, str): return ""
    p = s.strip().upper().split("/", 1)[0].lstrip("0")
    return (p or "0") if len(p) <= 50 else ""

def exact(s):
    import unicodedata
    return unicodedata.normalize("NFC",s).strip() if isinstance(s,str) else ""

def compare(candidate, packed):
    cid = candidate.get("archived_canonical_printing_id")
    if not isinstance(cid,str) or not cid: raise ValueError("missing_archived_canonical_id")
    if not isinstance(packed,dict):
        return "PACK_PRINTING_ID_ABSENT", "RARITY_NOT_COMPARABLE"
    if packed.get("language") != "zh-cn":
        return "PACK_LANGUAGE_CHANGED", "RARITY_NOT_COMPARABLE"
    if code(packed.get("set_code")) != candidate.get("normalized_source_set_code"):
        return "PACK_SET_CODE_CHANGED", "RARITY_NOT_COMPARABLE"
    if number(packed.get("printed_collector_number")) != number(candidate.get("printed_collector_number")):
        return "PACK_COLLECTOR_CHANGED", "RARITY_NOT_COMPARABLE"
    if exact(packed.get("native_card_name")) != exact(candidate.get("archived_native_chinese_name")):
        return "PACK_NATIVE_NAME_CHANGED", "RARITY_NOT_COMPARABLE"
    source_rarity = exact(candidate.get("independent_native_rarity")).upper()
    archive_rarity = exact(packed.get("rarity")).upper()
    if not source_rarity or not archive_rarity:
        rarity_result = "RARITY_MISSING_ON_ONE_SIDE"
    elif source_rarity == archive_rarity:
        rarity_result = "RARITY_EXACT_MATCH"
    else:
        rarity_result = "RARITY_CONFLICT_OR_DIFFERENT_VOCABULARY"
    return "ARCHIVED_PACK_CARD_FIELDS_AGREE", rarity_result

def audit(rows, pack):
    counts=Counter()
    rarities=Counter()
    permissions=Counter()
    by_origin=Counter()
    result=[]
    seen=set()
    for row in rows:
        if row.get("safe_for_vault_apply") is not False or row.get("artwork_rights_granted") is not False:
            raise ValueError("untrusted_candidate_rights")
        cid=row.get("archived_canonical_printing_id")
        if cid in seen: raise ValueError("duplicate_canonical_in_crosswalk")
        seen.add(cid)
        packed=pack.get(cid)
        status,rarity=compare(row, packed)
        counts[status]+=1
        if row.get("independent_native_identity_corroborated") is True:
            rarities[rarity]+=1
        if packed is not None:
            permissions[str(packed.get("mirror_permission_status"))]+=1
            by_origin[str(packed.get("provider_set_id"))]+=1
        result.append({
            "archived_canonical_printing_id":cid,
            "source_picture_id": row.get("source_picture_id"),
            "source_picture_url_reference": row.get("source_picture_url_reference"),
            "native_corrob": row.get("independent_native_identity_corroborated") is True,
            "pack_fields":status,
            "native_rarity_comparison":rarity,
            "source_mirror_permission_status":packed.get("mirror_permission_status") if packed else None,
            "live_vault_printing_verified":False,
            "finish_or_variant_verified":False,
            "third_party_artwork_rights_granted":False,
            "safe_to_apply":False,
        })
    return result,dict(sorted(counts.items())),dict(sorted(rarities.items())),dict(sorted(permissions.items()))

def run(crosswalk_dir,provisional_dir,output):
    if output.exists() or output.is_symlink():raise FileExistsError("new_output_required")
    cross_r=json.loads((crosswalk_dir/"receipt.json").read_text("utf8"))
    raw=(crosswalk_dir/"private_crosswalk.jsonl").read_bytes()
    if hashlib.sha256(raw).hexdigest()!=cross_r.get("private_crosswalk_jsonl_sha256"):
        raise ValueError("crosswalk_payload_sha_drift")
    rows=[json.loads(x) for x in raw.splitlines()]
    if (len(rows)!=EXPECTED_SOURCE_CANDIDATES or
            cross_r.get("native_name_corroborated_candidates")!=EXPECTED_NAME_CORROBORATED or
            cross_r.get("new_image_links")!=0 or cross_r.get("safe_for_vault_apply") is not False):
        raise ValueError("crosswalk_receipt_baseline_drift")
    prov_r=json.loads((provisional_dir/"receipt.json").read_text("utf8"))
    descriptor=pick(json.loads(get(CANARY,2000000)),"zh-cn")
    if descriptor["recordCount"] != EXPECTED_PACK_COUNT or descriptor.get("sha256") != prov_r.get("archived_pack_sha256"):
        raise ValueError("archived_pack_identity_changed")
    gz=get(descriptor["compressedDatabaseUrl"],MAX_GZIP)
    with tempfile.TemporaryDirectory(prefix="cardscanr-private-zhcn-pack-audit-") as tmp:
        f=Path(tmp)/"zh-cn.sqlite"
        checks,_,_,_,_,_=inspect_pack(descriptor,gz,f)
        if checks["sqlite_quick_check"]!="ok":raise ValueError("pack_integrity_failed")
        uri="file:"+quote(f.resolve().as_posix(),safe="/:")+"?mode=ro&immutable=1"
        with sqlite3.connect(uri,uri=True) as db:
            db.execute("PRAGMA query_only=ON")
            names={r[1] for r in db.execute("PRAGMA table_info(cards)")}
            required={"canonical_printing_id","language","set_code","printed_collector_number",
                      "native_card_name","rarity","mirror_permission_status","provider_set_id"}
            if not required.issubset(names):raise ValueError("pack_required_columns_missing")
            pack={r[0]:dict(zip(sorted(required),r)) for r in db.execute(
                "SELECT "+",".join(sorted(required))+" FROM cards")}
            # Map by printing id, but SQL SELECT is alphabetically sorted for
            # stable field alignment.
            if len(pack)!=EXPECTED_PACK_COUNT:raise ValueError("duplicate_pack_printing_ids")
        result,counts,rarity_counts,permission_counts=audit(rows,pack)
    output.mkdir(parents=True)
    payload="".join(json.dumps(r,sort_keys=True,ensure_ascii=False)+"\n" for r in result).encode()
    (output/"candidate_pack_audit.jsonl").write_bytes(payload)
    receipt={
        "classification":"PRIVATE_CHINESE_ARCHIVED_PACK_CONSISTENCY_ONLY",
        "archived_pack_sha256":descriptor["sha256"],
        "archived_pack_count":EXPECTED_PACK_COUNT,
        "candidates_checked":len(rows),
        "native_name_corroborated_baseline":EXPECTED_NAME_CORROBORATED,
        "pack_field_status_counts":counts,
        "native_corroborated_rarity_comparison_counts":rarity_counts,
        "source_permission_status_counts":permission_counts,
        "row_payload_sha256":hashlib.sha256(payload).hexdigest(),
        "live_vault_printing_verified":0,"variant_finish_verified":0,
        "rights_granted":False,"safe_to_import":False,
        "new_image_links":0,"production_writes":0,"image_binaries_downloaded":0,
        "note":"Pack consistency is historical SHA-pinned archive, not a current live vault identity assertion or rights grant.",
    }
    (output/"receipt.json").write_text(json.dumps(receipt,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf8")
    return receipt

def self_test():
    p={"archived_canonical_printing_id":"can1","normalized_source_set_code":"CSV1C","printed_collector_number":"001/100",
       "archived_native_chinese_name":"皮卡丘","independent_native_rarity":"R",
       "safe_for_vault_apply":False,"artwork_rights_granted":False,
       "independent_native_identity_corroborated":True,"source_picture_id":"a"*32,
       "source_picture_url_reference":"https://img.cardcorpus.com/m/"+"a"*32}
    pack={"can1":{"canonical_printing_id":"can1","language":"zh-cn","set_code":"csv1c",
         "printed_collector_number":"001","native_card_name":"皮卡丘","rarity":"R",
         "mirror_permission_status":"link_only","provider_set_id":"provider:set"}}
    out,c,r,per=audit([p],pack)
    assert c["ARCHIVED_PACK_CARD_FIELDS_AGREE"]==1
    assert r["RARITY_EXACT_MATCH"]==1 and per["link_only"]==1
    assert out[0]["safe_to_apply"] is False
    altered={"can1":dict(pack["can1"],rarity="U")}
    _,_,r,_=audit([p],altered)
    assert r["RARITY_CONFLICT_OR_DIFFERENT_VOCABULARY"]==1
    altered={"can1":dict(pack["can1"],native_card_name="错误")}
    _,c,_,_=audit([p],altered)
    assert c["PACK_NATIVE_NAME_CHANGED"]==1
    try:
        audit([p,p],pack)
        raise AssertionError("duplicate canonical accepted")
    except ValueError as e:
        assert str(e)=="duplicate_canonical_in_crosswalk"
    print("PASS offline pack source/number/rarity comparator, duplicates fail closed")

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--crosswalk",type=Path)
    parser.add_argument("--provisional",type=Path)
    parser.add_argument("--output",type=Path)
    parser.add_argument("--self-test",action="store_true")
    a=parser.parse_args()
    if a.self_test:self_test()
    else:
        if not a.crosswalk or not a.provisional or not a.output:parser.error("missing inputs")
        r=run(a.crosswalk,a.provisional,a.output)
        print("PRIVATE_ARCHIVED_PACK_AUDIT_PASS",json.dumps({
            "pack":r["archived_pack_count"],"candidates":r["candidates_checked"],
            "card_fields":r["pack_field_status_counts"],
            "rarity":r["native_corroborated_rarity_comparison_counts"],
            "new_image_links":0},sort_keys=True))
