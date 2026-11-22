#!/usr/bin/env python3
"""Acquire 5 Indonesian official sets, exact-match R2 archive, emit private vault batch.

Official Pokemon Asia robots-aware HTML fetch only. Strict set-language-card
identity and official image URL validation. No image binaries, customer data,
public artwork permissions or live writes. Live VaultSync separately preflights.
"""
from __future__ import annotations

import argparse,base64,gzip,hashlib,json,re,sqlite3,tempfile
from collections import Counter,defaultdict
from pathlib import Path
from urllib.parse import quote,urlsplit

from cardscanr_worldwide.collectors.pokemon_asia import BASE_URL,Collector,parse_card_list,parse_card_detail
from tools.preflight_multilingual_crossrelease import CANARY,MAX_GZIP,get,inspect_pack
from tools.preflight_legacy_korean_pack import pick
from tools.private_image_identity_crosswalk import _number
from tools.private_shard_contract import normalize_website_search

SETS=("SV3s","SV5s","SV9s","SV4s","SV7s")
LOCALE="id"
BATCH_PREFIX="idmore20261122"
DETAIL=re.compile(r"^[0-9]{1,10}$")
OFFICIAL_IMAGE=re.compile(r"^/id/card-img/id[0-9]{8}\.png$")
PACK_COLUMNS=("canonical_printing_id","set_id","language","native_card_name",
              "printed_collector_number","image_thumbnail_url","image_display_url")
MAX_PAGES=12
MAX_PER_PAGE=20


def source_image_valid(url,detail_id):
    if not isinstance(url,str) or not isinstance(detail_id,str) or not DETAIL.fullmatch(detail_id):
        return False
    try:
        u=urlsplit(url)
        return (u.scheme=="https" and u.hostname=="asia.pokemon-card.com"
                and u.port in (None,443) and not u.username and not u.password
                and not u.query and not u.fragment
                and OFFICIAL_IMAGE.fullmatch(u.path) is not None
                and u.path==f"/id/card-img/id{detail_id.zfill(8)}.png")
    except ValueError:return False


def validate_page_ids(ids,seen):
    if not isinstance(ids,list) or not 0<=len(ids)<=50:
        raise ValueError("official_page_card_count_outside_bound")
    if any(not isinstance(x,str) or not DETAIL.fullmatch(x) for x in ids):
        raise ValueError("official_invalid_detail_id")
    if len(ids)!=len(set(ids)):
        raise ValueError("official_same_page_duplicate_detail")
    if any(x in seen for x in ids):
        raise ValueError("official_detail_repeated_across_pages")
    return ids[:MAX_PER_PAGE]


def acquire(code,out,delay=0.75):
    if code not in SETS or not 0.5<=delay<=3.0:
        raise ValueError("set_or_pace_outside_pinned_scope")
    if out.exists() or out.is_symlink():
        raise FileExistsError("output_must_be_new")
    rows=[];provenance=[];seen=set()
    with tempfile.TemporaryDirectory(prefix="cardscanr-official-indonesian-"+code+"-") as tmp:
        coll=Collector(LOCALE,Path(tmp),delay)
        try:
            for page in range(1,MAX_PAGES+1):
                url=f"{BASE_URL}/{LOCALE}/card-search/list/?expansionCodes={code}"
                if page>1:url+="&pageNo="+str(page)
                response=coll.fetch(url)
                if response.status!=200 or len(response.content)>3_000_000:
                    raise ValueError("official_list_http_or_size_wrong")
                found,_=parse_card_list(response.content.decode("utf8"),LOCALE)
                chosen=validate_page_ids(found,seen)
                list_sha=hashlib.sha256(response.content).hexdigest()
                if not chosen:
                    provenance.append({"page":page,"cards":0,
                                       "sha256":list_sha,"ended":"empty"})
                    break
                for detail_id in chosen:
                    detail_url=f"{BASE_URL}/{LOCALE}/card-search/detail/{detail_id}/"
                    detail=coll.fetch(detail_url)
                    if detail.status!=200 or len(detail.content)>1_500_000:
                        raise ValueError("official_card_detail_http_or_size_wrong")
                    d=parse_card_detail(detail.content.decode("utf8"),detail_url)
                    name=d.get("local_name");num=d.get("collector_number")
                    image=d.get("image_url")
                    if (not isinstance(name,str) or not name.strip() or len(name)>400
                        or not isinstance(num,str) or not num.strip() or len(num)>80
                        or not source_image_valid(image,detail_id)):
                        raise ValueError("unverified_detail_name_number_or_image_url")
                    rows.append({
                        "language":"id","source_product_code":code,
                        "official_detail_id":detail_id,"source_list_page":page,
                        "official_detail_url":detail_url,
                        "source_name_native":name,
                        "source_collector_number":num,
                        "source_printed_set_code":d.get("printed_set_code"),
                        "source_image_url_reference":image,
                        "source_response_sha256":hashlib.sha256(detail.content).hexdigest(),
                        "source_list_sha256":list_sha,
                        "artwork_rights_verified":False,
                        "image_binaries_downloaded":0,
                        "live_identity_verified":False,
                    })
                    seen.add(detail_id)
                provenance.append({"page":page,"cards":len(chosen),
                                   "sha256":list_sha,
                                   "ended":"partial" if len(found)<MAX_PER_PAGE else "continue"})
                if len(found)<MAX_PER_PAGE:break
        finally:coll.close()
    if len(rows)>240 or len(rows)!=len(seen):
        raise ValueError("source_card_count_or_duplicates_wrong")
    out.mkdir(parents=True)
    payload="".join(json.dumps(z,sort_keys=True,ensure_ascii=False,separators=(",",":"))+"\n"
                    for z in rows).encode("utf8")
    (out/"official_source.jsonl").write_bytes(payload)
    record={"classification":"PRIVATE_OFFICIAL_ID_MULTI_SET_SOURCE_CARD_DETAILS_NO_LIVE_IMPORT",
            "language":"id","set_code":code,"source_cards":len(rows),
            "pages":provenance,
            "source_sha256":hashlib.sha256(payload).hexdigest(),
            "image_urls_observed":len(rows),
            "artwork_files_downloaded":0,"new_live_image_links":0,
            "new_live_canonical_cards":0,"public_artwork_rights_granted":False}
    (out/"source_receipt.json").write_text(json.dumps(record,indent=2,sort_keys=True)+"\n")
    return rows,record


def normalize(lang,code,num,name):
    if lang!="id" or code not in SETS or not isinstance(num,str) or not isinstance(name,str):
        return None
    n=_number(num);s=normalize_website_search(name)
    if not n or not s:return None
    return ("id","tcgdex:asia:set:"+code,n,s)


def match_archive(code,rows,out):
    if code not in SETS:raise ValueError("set_not_allowlisted")
    index_counts=Counter();detail_counts=Counter()
    for r in rows:
        index_counts[normalize(r["language"],r["source_product_code"],
                               r["source_collector_number"],r["source_name_native"])]+=1
        detail_counts[(r["language"],r["official_detail_id"])]+=1
    quarantined=[];exact=[]
    if rows:
        manifest=json.loads(get(CANARY,2_000_000))
        desc=pick(manifest,"id")
        with tempfile.TemporaryDirectory(prefix="cardscanr-indonesian-R2-"+code+"-") as tmp:
            pack=Path(tmp)/"indonesian.sqlite"
            z=get(desc["compressedDatabaseUrl"],MAX_GZIP)
            check,*_=inspect_pack(desc,z,pack)
            if check["sqlite_quick_check"]!="ok":
                raise ValueError("R2_pack_sqlite_inconsistent")
            uri="file:"+quote(pack.resolve().as_posix(),safe="/:")+"?mode=ro&immutable=1"
            with sqlite3.connect(uri,uri=True) as con:
                con.execute("PRAGMA query_only=ON")
                cols={x[1] for x in con.execute("PRAGMA table_info(cards)")}
                if not set(PACK_COLUMNS).issubset(cols):
                    raise ValueError("archived_pack_missing_columns")
                select="SELECT "+",".join(PACK_COLUMNS)+" FROM cards WHERE language=? AND set_id=?"
                archived=[dict(zip(PACK_COLUMNS,r)) for r in con.execute(select,("id","tcgdex:asia:set:"+code))]
            indexed=defaultdict(list)
            for r in archived:
                indexed[normalize(r["language"],code,
                       str(r["printed_collector_number"] or ""),
                       str(r["native_card_name"] or ""))].append(r)
            for r in rows:
                k=normalize(r["language"],code,
                            r["source_collector_number"],r["source_name_native"])
                if (k is None or index_counts[k]!=1
                    or detail_counts[("id",r["official_detail_id"])]!=1):
                    quarantined.append({"detail_id":r["official_detail_id"],
                        "reason":"ambiguous_source_printing_identity"});continue
                matched=indexed.get(k,[])
                if len(matched)!=1:
                    quarantined.append({"detail_id":r["official_detail_id"],
                        "reason":"archived_unmatched_or_variant_ambiguous",
                        "archive_hits":len(matched)});continue
                x=matched[0]
                if x.get("image_thumbnail_url") or x.get("image_display_url"):
                    # Already has an image in original archived R2. The live
                    # vault could differ, but skip until independently proven.
                    quarantined.append({"detail_id":r["official_detail_id"],
                        "reason":"already_has_image_in_archived_reference_pack"});continue
                exact.append({
                    "canonical_printing_id_archived":x["canonical_printing_id"],
                    "language":"id","set_id":"tcgdex:asia:set:"+code,
                    "printed_number":r["source_collector_number"],
                    "native_name":r["source_name_native"],
                    "source_image_url_reference":r["source_image_url_reference"],
                    "official_detail_id":r["official_detail_id"],
                    "source_response_sha256":r["source_response_sha256"],
                    "artwork_rights_verified":False,
                    "live_identity_verified":False})
            pack_sha=desc["sha256"]
    else:pack_sha=None
    if len(exact)+len(quarantined)!=len(rows):
        raise ValueError("source_record_accounting_mismatch")
    canonical=[x["canonical_printing_id_archived"] for x in exact]
    if len(canonical)!=len(set(canonical)):raise ValueError("archive_duplicate_canonical_identity")
    b="".join(json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(",",":"))+"\n"
              for x in exact).encode("utf8")
    (out/"archive_unique_image_candidates.jsonl").write_bytes(b)
    q="".join(json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(",",":"))+"\n"
              for x in quarantined).encode("utf8")
    (out/"source_quarantine.jsonl").write_bytes(q)
    proof={"classification":"PRIVATE_OFFICIAL_ID_ARCHIVED_EXACT_MATCH_ONLY",
           "set_code":code,"source_rows":len(rows),"archive_sha256":pack_sha,
           "exact_archive_missing_candidates":len(exact),
           "quarantined":len(quarantined),
           "candidate_sha256":hashlib.sha256(b).hexdigest(),
           "quarantine_sha256":hashlib.sha256(q).hexdigest(),
           "live_new_card_links":0,"artwork_rights_verified":False,
           "public_artwork_enabled":False,"requires_live_preflight":True}
    (out/"crosswalk_receipt.json").write_text(json.dumps(proof,indent=2,sort_keys=True)+"\n")
    return exact,proof


def stage(code,exact,out):
    bid=BATCH_PREFIX+code.lower()
    if not exact:
        return {"batch_id":bid,"cards":0,"stage_payload":False}
    items=[x for x in exact if not x["artwork_rights_verified"] and not x["live_identity_verified"]]
    if len(items)!=len(exact) or len(items)>240:
        raise ValueError("stage_count_or_rights_invalid")
    source_shas=sorted(x["source_response_sha256"] for x in items)
    doc={
        "schema_version":1,"source":"official_crosswalk",
        "sets":[{"language":"id","provider_set_id":code,
                 "source_response_sha256":hashlib.sha256("".join(source_shas).encode()).hexdigest()}],
        "cards":[{"canonical_printing_id":x["canonical_printing_id_archived"],
                  "language":"id","provider_set_id":code,"status":"exact_unique",
                  "provider_card_id":code+"-"+x["official_detail_id"],
                  "native_card_name":x["native_name"],
                  "printed_collector_number":x["printed_number"]} for x in items],
        "images":[{"canonical_printing_id":x["canonical_printing_id_archived"],
                  "language":"id","provider_set_id":code,
                  "image_url":x["source_image_url_reference"]} for x in items],
        "artwork_rights_verified":False,"image_binaries_downloaded":0
    }
    compressed=gzip.compress(json.dumps(doc,ensure_ascii=False,sort_keys=True,
                   separators=(",",":")).encode("utf8"),mtime=0)
    if len(compressed)>72_000:raise ValueError("stage_exceeds_single_chunk_limit")
    digest=hashlib.sha256(compressed).hexdigest()
    (out/(bid+".gz")).write_bytes(compressed)
    metadata={"classification":"PRIVATE_STAGE_READY_NOT_LIVE",
              "batch_id":bid,"cards":len(items),"gzip_sha256":digest,
              "compressed_bytes":len(compressed),
              "new_live_links":0,"artwork_files_downloaded":0,
              "public_rights_granted":False,"requires_current_live_preflight":True}
    (out/"stage_receipt.json").write_text(json.dumps(metadata,indent=2,sort_keys=True)+"\n")
    print("OFFICIAL_ID_BULK_STAGE_CONTENT",json.dumps({
        "action":"artwork_vault_stage","draft_id":bid,
        "expected_sha256":digest,"chunk_index":0,"limit":1,
        "content":base64.b64encode(compressed).decode("ascii")
    },sort_keys=True))
    return metadata


def test():
    assert source_image_valid("https://asia.pokemon-card.com/id/card-img/id00008110.png","8110")
    assert not source_image_valid("https://asia.pokemon-card.com/th/card-img/th00008110.png","8110")
    assert not source_image_valid("https://evil.example/id/card-img/id00008110.png","8110")
    assert not source_image_valid("https://asia.pokemon-card.com/id/card-img/id00008110.png?bad=x","8110")
    assert validate_page_ids(["123","124"],{"100"})==["123","124"]
    for seq in (["1","1"],["foo"],["1","2"]):
        try:validate_page_ids(seq,{"1"} if seq==["1","2"] else set())
        except ValueError:pass
        else:raise AssertionError("duplicate source ID accepted")
    assert normalize("id","SV3s","001","Dondozo")==normalize("id","SV3s","001","Dondozo")
    assert normalize("th","SV1S","001","Dondozo") is None
    print("PASS exact ID official locale, identity collisions and private URLs")


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--set",dest="code",choices=SETS)
    p.add_argument("--out",type=Path)
    p.add_argument("--self-test",action="store_true")
    args=p.parse_args()
    if args.self_test:test();return
    if not args.code or args.out is None:p.error("--set and --out required")
    code=args.code
    rows,src=acquire(code,args.out)
    exact,cross=match_archive(code,rows,args.out)
    staged=stage(code,exact,args.out)
    receipt={"classification":"OFFICIAL_ID_BULK_ACQUISITION_ARCHIVE_AND_PRIVATE_STAGE",
             "set_code":code,"source_cards":len(rows),
             "archive_exact_missing_candidates":len(exact),
             "source_quarantined":cross["quarantined"],
             "archive_sha256":cross["archive_sha256"],
             "stage":staged,
             "new_live_links":0,"new_live_canonical_cards":0,
             "artwork_files_downloaded":0,"public_artwork_rights_granted":False}
    (args.out/"receipt.json").write_text(json.dumps(receipt,indent=2,sort_keys=True)+"\n")
    print("OFFICIAL_ID_BULK_REAL_SOURCE_FINAL",json.dumps({
        "set":code,"source_cards":len(rows),
        "archive_exact_missing_candidates":len(exact),
        "source_quarantined":cross["quarantined"],
        "new_live_links":0
    },sort_keys=True))


if __name__=="__main__":main()
