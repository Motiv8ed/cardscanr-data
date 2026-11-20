#!/usr/bin/env python3
"""Private Chinese CardCorpus-to-CardScanR candidate image-link reconciliation.

This is NOT a canonical import or a rights grant. Only set code + printed
number are matched as candidate evidence: CardCorpus names are commonly
English while the R2 Chinese pack stores native Chinese card names.
Any record with multiple possible cards is quarantined. No image requests.
"""
from __future__ import annotations
import argparse,gzip,hashlib,json,sqlite3,tempfile
from collections import defaultdict,Counter
from pathlib import Path
from urllib.parse import quote
from tools.preflight_multilingual_crossrelease import CANARY,MAX_GZIP,get,inspect_pack
from tools.preflight_legacy_korean_pack import pick

PINNED="c4f0b415d1dd3d4fbaadb8c32a0a591126746ed6"
SOURCE_SHA="f1a03450386925c8e212eab4b6acdbafbfc0483af2d04d4bdb8211240fd49950"
SOURCE_GZIP_SHA="e814490daaaefb016c68b3ef2ea49cdb70e7dcef3941bb897435dcac76d70c0d"
PIC="https://img.cardcorpus.com/m/"
PHOTO_ID_CHARS=set("0123456789abcdef")

def keycode(v):
    return "".join(c for c in v.strip().upper() if c.isalnum()) if isinstance(v,str) else ""

def number(v):
    if not isinstance(v,str):return ""
    p=v.strip().upper().split("/",1)[0]
    p=p.lstrip("0") or "0"
    return p if len(p)<=50 else ""

def set_mapping(root:Path):
    mapping=defaultdict(set)
    folder=root/"v1"/"sets"/"zh-hans"
    if not folder.is_dir():raise ValueError("missing_pinned_zh_hans_sets")
    for path in sorted(folder.glob("*.json")):
        if path.is_symlink() or path.stat().st_size>4_000_000:raise ValueError("unsafe_source_set")
        d=json.loads(path.read_text("utf8"))
        if d.get("catalog")!="zh-hans" or d.get("language")!="zh-Hans":
            raise ValueError("source_set_language_drift")
        if d.get("id")!="zh-hans/"+path.stem:raise ValueError("source_set_path_drift")
        code=keycode(d.get("code"))
        if code:mapping[code].add(d["id"])
    return mapping

def analysis(rows,sets,source_cards):
    target=defaultdict(list)
    for r in rows:
        cid,sid,lang,code,printed,name=r
        if lang!="zh-cn":raise ValueError("wrong_live_archived_pack_language")
        key=(keycode(code),number(printed))
        if all(key):target[key].append((cid,sid,name))
    candidates=[]
    reasons=Counter()
    by_set=Counter()
    by_host=Counter()
    for doc in source_cards:
        srcset=doc.get("source_set_id")
        if not isinstance(srcset,str) or not srcset.startswith("zh-hans/"):continue
        fields=doc.get("fields")
        if not isinstance(fields,dict):raise ValueError("source_fields_invalid")
        name=fields.get("name")
        number_value=fields.get("number")
        printed=number_value.get("printed") if isinstance(number_value,dict) else None
        if not isinstance(name,str) or not name.strip() or not isinstance(printed,str):
            reasons["source_missing_name_or_number"]+=1
            continue
        fromset=sets.get(srcset)
        if fromset is None or not fromset:
            reasons["source_set_without_unique_code"]+=1
            continue
        key=(fromset,number(printed))
        local=target.get(key,[])
        if not local:
            reasons["source_printed_number_not_in_pack_code"]+=1
            continue
        if len(local)!=1:
            reasons["ambiguous_multiple_canonical_rows_same_code_and_number"]+=1
            continue
        pid=doc.get("picture_id")
        if not isinstance(pid,str) or len(pid)!=32 or set(pid)-PHOTO_ID_CHARS:
            reasons["source_missing_valid_picture_id"]+=1
            continue
        canonical,sid,native=local[0]
        # No exact match claimed, because English display name may not equal
        # Chinese native card name. Do not pass these to artwork_vault_apply.
        row={
            "classification":"PROVISIONAL_SET_CODE_COLLECTOR_NUMBER_ONLY",
            "source_repository":"cardcorpus/pokemon-card-dataset",
            "source_commit":PINNED,
            "source_set_id":srcset,
            "source_card_id":doc["source_card_id"],
            "source_english_name":name,
            "archived_canonical_printing_id":canonical,
            "archived_set_id":sid,
            "archived_native_chinese_name":native,
            "printed_collector_number":printed,
            "source_picture_id":pid,
            "source_picture_url_reference":PIC+pid,
            "exact_native_name_verified":False,
            "live_vault_identity_verified":False,
            "image_rights_cleared":False,
            "safe_for_vault_apply":False,
        }
        candidates.append(row)
        by_set[srcset]+=1
        reasons["provisional_unique_code_number_candidate"]+=1
    return candidates,dict(reasons),dict(by_set)

def source_details(zipped:Path):
    from zipfile import ZipFile
    with ZipFile(zipped) as z:
        receipt=json.loads(z.read("receipt.json"))
        if receipt.get("source_commit")!=PINNED or receipt.get("validated_rich_card_details")!=67243:
            raise ValueError("source_receipt_identity_drift")
        payload=z.read("rich_card_details.jsonl.gz")
        if hashlib.sha256(payload).hexdigest()!=SOURCE_GZIP_SHA:raise ValueError("source_gzip_hash")
        raw=gzip.decompress(payload)
        if hashlib.sha256(raw).hexdigest()!=SOURCE_SHA:raise ValueError("source_raw_hash")
        rows=[json.loads(x) for x in raw.splitlines()]
        if len(rows)!=67243:raise ValueError("source_row_count_drift")
        return rows

def collect(corpus:Path,source_zip:Path,output:Path):
    if output.exists() or output.is_symlink():raise FileExistsError("output_must_be_new")
    setcodes=set_mapping(corpus)
    source_to_code={sid:code for code,sids in setcodes.items() if len(sids)==1 for sid in sids}
    manifest=json.loads(get(CANARY,2000000))
    descriptor=pick(manifest,"zh-cn")
    gz=get(descriptor["compressedDatabaseUrl"],MAX_GZIP)
    with tempfile.TemporaryDirectory(prefix="cardscanr-cn-provider-preview-") as td:
        f=Path(td)/"zh-cn.sqlite"
        checks,_,_,_,_,_=inspect_pack(descriptor,gz,f)
        with sqlite3.connect("file:"+quote(f.resolve().as_posix(),safe="/:")+"?mode=ro&immutable=1",uri=True) as db:
            db.execute("PRAGMA query_only=ON")
            columns={row[1] for row in db.execute("PRAGMA table_info(cards)")}
            required=("canonical_printing_id","set_id","language","set_code","printed_collector_number","native_card_name")
            if not set(required).issubset(columns):raise ValueError("missing_archive_columns")
            pack=list(db.execute("SELECT "+",".join(required)+" FROM cards"))
    if len(pack)!=41185 or checks["sqlite_quick_check"]!="ok":raise ValueError("archived_pack_integrity_drift")
    sources=source_details(source_zip)
    candidates,reasons,by_set=analysis(pack,source_to_code,sources)
    ids=[(x["source_set_id"],x["source_card_id"]) for x in candidates]
    if len(ids)!=len(set(ids)):raise ValueError("duplicate_source_candidate")
    result={
        "classification":"PRIVATE_ZH_CN_PICTURE_SOURCE_SET_NUMBER_PROVISIONAL",
        "source_commit":PINNED,
        "source_set_codes_unique":len(source_to_code),
        "source_set_codes_ambiguous":sum(len(v) for v in setcodes.values() if len(v)>1),
        "source_cards_examined":67243,
        "archived_chinese_pack_rows":41185,
        "archived_pack_sha256":descriptor["sha256"],
        "provisional_candidates":len(candidates),
        "reasons":reasons,"matching_source_sets":len(by_set),
        "candidate_counts_by_source_set":dict(sorted(by_set.items())),
        "image_binaries_downloaded":0,
        "new_canonical_cards":0,"live_vault_writes":0,
        "verified_live_printing_matches":0,
        "rights_granted":False,"public_artwork_safe":False,
        "note":"CODE+NUMBER ONLY. Candidate English names do not prove exact Chinese native names or variant identity.",
    }
    output.mkdir(parents=True)
    data="\n".join(json.dumps(x,ensure_ascii=False,sort_keys=True) for x in candidates)
    (output/"candidates.jsonl").write_text(data+("\n" if data else ""),encoding="utf8")
    b=(output/"candidates.jsonl").read_bytes()
    result["candidates_jsonl_sha256"]=hashlib.sha256(b).hexdigest()
    (output/"receipt.json").write_text(json.dumps(result,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf8")
    return result

def selftest():
    assert number("001/150")=="1"
    assert number("000/SM-P")=="0"
    assert keycode("csm2aC")=="CSM2AC"
    records=[("one","ptcg:set:12","zh-cn","CSM2aC","001/150","中文名")]
    source=[{"source_set_id":"zh-hans/shower","source_card_id":"1",
             "fields":{"name":"English Name","number":{"printed":"001/150"}},"picture_id":"a"*32}]
    c,r,_=analysis(records,{"zh-hans/shower":"CSM2AC"},source)
    assert len(c)==1 and not c[0]["safe_for_vault_apply"]
    assert r["provisional_unique_code_number_candidate"]==1
    c,r,_=analysis(records+records,{"zh-hans/shower":"CSM2AC"},source)
    assert len(c)==0 and r["ambiguous_multiple_canonical_rows_same_code_and_number"]==1
    print("PASS: no code-only matches treated as verified live identity")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--corpus",type=Path)
    ap.add_argument("--source-zip",type=Path)
    ap.add_argument("--output",type=Path)
    ap.add_argument("--self-test",action="store_true")
    a=ap.parse_args()
    if a.self_test:selftest();return
    if not a.corpus or not a.source_zip or not a.output:ap.error("input paths required")
    x=collect(a.corpus,a.source_zip,a.output)
    print("ZHCN_PROVISIONAL_MATCH_PASS",json.dumps({
        "candidate_count":x["provisional_candidates"],
        "matched_sets":x["matching_source_sets"],
        "reasons":x["reasons"],
        "verified_live_printing_matches":0,"writes":0
    },sort_keys=True))
if __name__=="__main__":
    main()
