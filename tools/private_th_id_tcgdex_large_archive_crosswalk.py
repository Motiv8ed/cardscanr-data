#!/usr/bin/env python3
"""Match real native Thai/Indonesian TCGdex asset source URLs to pinned R2 card identities.

This is an archival source crosswalk, not confirmation of current live gaps.
No image binaries, public artwork grants, new canonical inserts, or live writes.
"""
from __future__ import annotations
import argparse,hashlib,json,sqlite3,tempfile
from collections import Counter,defaultdict
from pathlib import Path
from urllib.parse import quote
from tools.preflight_multilingual_crossrelease import CANARY,MAX_GZIP,get,inspect_pack
from tools.preflight_legacy_korean_pack import pick
from tools.private_image_identity_crosswalk import _number
from tools.private_shard_contract import normalize_website_search
from tools.private_th_id_tcgdex_set_image_source import safe_base

FIELDS=("canonical_printing_id","set_id","language","native_card_name",
        "printed_collector_number","image_thumbnail_url","image_display_url")
SOURCE_CLASS="PRIVATE_LARGE_TH_ID_PROVIDER_DECLARED_IMAGE_URL_SOURCES_NO_LIVE_IMPORT"
OUTPUT_CLASS="PRIVATE_TH_ID_LARGE_TCGDEX_PINNED_ARCHIVE_EXACT_CROSSWALK_ONLY"


def match(pack,source):
    index=defaultdict(list)
    for r in pack:
        lang=r["language"]
        code=str(r["set_id"]).removeprefix("tcgdex:asia:set:")
        if (lang not in ("th","id") or not r["set_id"].startswith("tcgdex:asia:set:")
                or not isinstance(r["native_card_name"],str)):
            continue
        key=(lang,code,_number(str(r["printed_collector_number"] or "")),
             normalize_website_search(r["native_card_name"]))
        if key[2] and key[3]:index[key].append(r)
    seen=set()
    candidates=[]
    quarantine=[]
    counts=Counter()
    for item in source:
        lang,code,num=item["language"],item["set_code"],item["local_collector_number"]
        ident=(lang,code,item["provider_card_id"])
        if ident in seen:
            raise ValueError("duplicate_source_printing_id")
        seen.add(ident)
        base=item["source_image_base"]
        url=item["source_low_webp_url"]
        if item["source_image_declared"] is not True:
            counts["source_card_no_declared_image"]+=1
            continue
        if (not safe_base(base,lang,code,num) or url!=base+"/low.webp"
                or not isinstance(item["source_native_name"],str)
                or not item["source_response_sha256"]):
            raise ValueError("invalid_language_card_source_image_ref")
        key=(lang,code,_number(num),normalize_website_search(item["source_native_name"]))
        matches=index.get(key,[])
        if len(matches)!=1:
            reason=("no_matching_archived_card" if not matches else
                    "archived_printing_identity_ambiguous")
            quarantine.append({"provider_card_id":item["provider_card_id"],
                "language":lang,"set_code":code,"source_image_url":url,
                "reason":reason,"match_count":len(matches)})
            counts[reason]+=1
            continue
        row=matches[0]
        if bool(row.get("image_thumbnail_url") or row.get("image_display_url")):
            counts["already_image_linked_in_archived_pack"]+=1
        # The live vault may differ. Do not claim these are current live gaps.
        candidates.append({
            "classification":"ARCHIVED_EXACT_SOURCE_CARD_IMAGE_REFERENCE_NOT_LIVE",
            "language":lang,"set_code":code,
            "canonical_printing_id_archived":row["canonical_printing_id"],
            "set_id_archived":row["set_id"],
            "native_card_name":row["native_card_name"],
            "printed_collector_number":row["printed_collector_number"],
            "provider_card_id":item["provider_card_id"],
            "source_image_url":url,
            "source_response_sha256":item["source_response_sha256"],
            "archived_image_links_present":bool(row.get("image_thumbnail_url") or row.get("image_display_url")),
            "live_identity_verified":False,
            "live_image_missing_verified":False,
            "image_binaries_downloaded":0,
            "public_artwork_rights_verified":False
        })
        counts["archived_source_identity_exact"]+=1
    ids=[(x["language"],x["canonical_printing_id_archived"]) for x in candidates]
    if len(ids)!=len(set(ids)):
        # One R2 printing mapped to multiple distinct provider cards: do not
        # stage potentially wrong variants without human quality evidence.
        duplicates={i for i in ids if ids.count(i)>1}
        candidates=[x for x in candidates if (x["language"],x["canonical_printing_id_archived"]) not in duplicates]
        counts["duplicate_archive_mapped_printing_quarantined"]+=len(duplicates)
    return candidates,quarantine,counts


def source_read(source_root):
    p=source_root/"receipt.json"
    if not p.is_file():raise ValueError("source_receipt_missing")
    receipt=json.loads(p.read_text(encoding="utf8"))
    if (receipt["classification"]!=SOURCE_CLASS
            or receipt["requested_per_language"]!=65
            or receipt["new_live_image_links"]!=0
            or receipt["artwork_images_downloaded"]!=0
            or receipt["public_artwork_rights_verified"]):
        raise ValueError("unexpected_source_receipt")
    file=source_root/"native_card_source_urls.jsonl"
    b=file.read_bytes()
    if hashlib.sha256(b).hexdigest()!=receipt["source_metadata_sha256"]:
        raise ValueError("source_card_list_digest_mismatch")
    rows=[json.loads(x) for x in b.splitlines()]
    if len(rows)!=receipt["source_card_details"]:
        raise ValueError("source_card_count_mismatch")
    return rows,receipt


def audit(source_root,out):
    if out.exists() or out.is_symlink():raise FileExistsError("output_must_be_fresh")
    source,source_receipt=source_read(source_root)
    manifest=json.loads(get(CANARY,2_000_000))
    packs=[]
    pack_shas={}
    with tempfile.TemporaryDirectory(prefix="cardscanr-thid-large-source-") as temp:
        for lang in ("th","id"):
            desc=pick(manifest,lang)
            zipped=get(desc["compressedDatabaseUrl"],MAX_GZIP)
            f=Path(temp)/(lang+".sqlite")
            checks,_,_,_,_,_=inspect_pack(desc,zipped,f)
            if checks["sqlite_quick_check"]!="ok":raise ValueError("archive_sqlite_integrity_failed")
            pack_shas[lang]=desc["sha256"]
            uri="file:"+quote(f.resolve().as_posix(),safe="/:")+"?mode=ro&immutable=1"
            with sqlite3.connect(uri,uri=True) as c:
                c.execute("PRAGMA query_only=ON")
                columns={x[1] for x in c.execute("PRAGMA table_info(cards)")}
                if not set(FIELDS).issubset(columns):raise ValueError("archive_required_fields_missing")
                for values in c.execute("SELECT "+",".join(FIELDS)+" FROM cards"):
                    row=dict(zip(FIELDS,values))
                    if row["language"]!=lang:raise ValueError("archive_locale_drift")
                    packs.append(row)
    candidates,quarantine,counts=match(packs,source)
    out.mkdir(parents=True)
    def write(name,records):
        raw=("".join(json.dumps(r,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n"
                     for r in records)).encode("utf8")
        (out/name).write_bytes(raw)
        return {"rows":len(records),"sha256":hashlib.sha256(raw).hexdigest()}
    a=write("archived_source_image_candidates.jsonl",candidates)
    b=write("quarantined_source_identities.jsonl",quarantine)
    receipt={
        "classification":OUTPUT_CLASS,
        "source_records_checked":len(source),
        "source_declared_image_refs":source_receipt["provider_declared_image_url_bases"],
        "archived_pack_card_rows":len(packs),
        "archived_pack_sha256":pack_shas,
        "archive_exact_image_source_refs":len(candidates),
        "archive_exact_already_with_image":sum(x["archived_image_links_present"] for x in candidates),
        "source_identity_quarantined":len(quarantine),
        "matching_reasons":dict(counts),
        "source_provenance_sha256":source_receipt["source_metadata_sha256"],
        "candidates_file":a,"quarantine_file":b,
        "new_live_links":0,"new_live_canonicals":0,
        "artwork_binaries_downloaded":0,"commercial_rights_verified":False,
        "current_live_printing_check_pending":True,
        "stage_allowed_without_live_exact_native_variant_check":False,
    }
    (out/"receipt.json").write_text(json.dumps(receipt,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf8")
    return receipt


def selftest():
    archive=[{
        "canonical_printing_id":"tcgdex_asia_SV6s_001:id:ID",
        "set_id":"tcgdex:asia:set:SV6s",
        "language":"id","native_card_name":"Tangela",
        "printed_collector_number":"001",
        "image_thumbnail_url":None,"image_display_url":None
    }]
    source=[{
        "language":"id","set_code":"SV6s","local_collector_number":"001",
        "provider_card_id":"SV6s-001","source_native_name":"Tangela",
        "source_image_base":"https://assets.tcgdex.net/id/SV/SV6s/001",
        "source_low_webp_url":"https://assets.tcgdex.net/id/SV/SV6s/001/low.webp",
        "source_image_declared":True,"source_response_sha256":"a"*64
    }]
    rows,quarantine,count=match(archive,source)
    assert len(rows)==1 and not quarantine
    assert not rows[0]["live_identity_verified"]
    wrong=[{**source[0],"source_native_name":"Other card"}]
    rows,quarantine,count=match(archive,wrong)
    assert not rows and len(quarantine)==1
    rows,quarantine,count=match(archive+archive,source)
    assert not rows and len(quarantine)==1
    print("PASS strict Thai/ID native set/name/number and archive collisions quarantine")


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--source-root",type=Path)
    p.add_argument("--out",type=Path)
    p.add_argument("--self-test",action="store_true")
    a=p.parse_args()
    if a.self_test:selftest()
    else:
        if a.source_root is None or a.out is None:p.error("--source-root and --out required")
        r=audit(a.source_root,a.out)
        print("TH_ID_2229_LARGE_ARCHIVE_CROSSWALK_RESULTS",json.dumps({
            k:r[k] for k in ("source_declared_image_refs",
                "archive_exact_image_source_refs",
                "archive_exact_already_with_image",
                "source_identity_quarantined",
                "new_live_links")
        },sort_keys=True))
