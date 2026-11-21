#!/usr/bin/env python3
"""Read-only TH/ID official source image URL versus SHA-verified R2 card packs.

Only exact same language + source-set code + normalized printed number +
native card name counts as a unique crosswalk. No card/image bodies, merges,
live-vault writes or rights grants.
"""
from __future__ import annotations
import argparse,hashlib,json,sqlite3,tempfile
from collections import Counter,defaultdict
from pathlib import Path
from urllib.parse import quote,urlsplit
from tools.preflight_multilingual_crossrelease import CANARY,MAX_GZIP,get,inspect_pack
from tools.preflight_legacy_korean_pack import pick
from tools.private_image_identity_crosswalk import _number
from tools.private_shard_contract import normalize_website_search

TARGETS={"th":"SV7s","id":"SV4a"}
FIELDS=("canonical_printing_id","set_id","language","native_card_name",
        "printed_collector_number","image_thumbnail_url","image_display_url")

def identity(language,code,num,name):
    if (language not in TARGETS or TARGETS[language]!=code or
            not isinstance(name,str) or not isinstance(num,str)):
        return None
    return language,"tcgdex:asia:set:"+code,_number(num),normalize_website_search(name)

def safe_url(url,language):
    if not isinstance(url,str):return False
    try:
        p=urlsplit(url)
        return (p.scheme=="https" and p.hostname=="asia.pokemon-card.com"
            and p.port in (None,443) and p.username is None and p.password is None
            and not p.query and not p.fragment and
            p.path.startswith("/"+language+"/card-img/") and p.path.endswith(".png"))
    except ValueError:
        return False

def check(pack_records,source_records):
    indexed=defaultdict(list)
    for row in pack_records:
        k=identity(row["language"],row["set_id"].removeprefix("tcgdex:asia:set:"),
                   str(row["printed_collector_number"] or ""),
                   str(row["native_card_name"] or ""))
        if k and k[2] and k[3]:indexed[k].append(row)
    found=[]
    counts=Counter()
    # Repeated provider identities are not independent printings. Quarantine
    # every occurrence rather than choosing an arbitrary image URL.
    source_identity_counts=Counter()
    for row in source_records:
        k=identity(row.get("language"),row.get("source_product_code"),
                   str(row.get("source_collector_number") or ""),
                   str(row.get("source_name_native") or ""))
        if k:
            source_identity_counts[k]+=1
    for row in source_records:
        lang=row["language"]
        k=identity(lang,row["source_product_code"],
                   str(row["source_collector_number"] or ""),row["source_name_native"])
        url=row["source_image_url_reference"]
        if not k or not safe_url(url,lang):
            counts["unsafe_or_missing_source_identity"]+=1
            continue
        if source_identity_counts[k]!=1:
            counts["ambiguous_source_printings"]+=1
            continue
        matches=indexed.get(k,[])
        if not matches:
            counts["no_exact_pack_identity"]+=1
            continue
        if len(matches)!=1:
            counts["ambiguous_card_variants"]+=1
            continue
        match=matches[0]
        already=bool(match.get("image_thumbnail_url") or match.get("image_display_url"))
        status="exact_already_image_in_archive" if already else "exact_missing_image_source_url_candidate"
        counts[status]+=1
        found.append({
          "classification":status,
          "language":lang,
          "set_id":match["set_id"],
          "canonical_printing_id_archived":match["canonical_printing_id"],
          "native_name":row["source_name_native"],
          "printed_number":row["source_collector_number"],
          "source_detail_id":row["official_detail_id"],
          "source_detail_url":row["official_detail_url"],
          "source_image_url_reference":url,
          "source_response_sha256":row["source_response_sha256"],
          "actual_live_vault_id_verified":False,
          "artwork_rights_verified":False,
          "artwork_downloaded":False,
          "safe_to_publish":False,
        })
    return found,counts

def audit(pilot:Path,out:Path):
    if out.exists() or out.is_symlink():raise FileExistsError("output_must_be_new")
    p=json.loads((pilot/"receipt.json").read_text("utf8"))
    if (p.get("classification")!="PRIVATE_OFFICIAL_ASIA_CARD_URL_RESEARCH_ONLY"
        or p.get("source_cards")!=40 or p.get("source_request_budget")!=42
        or p.get("image_binaries_downloaded")!=0):
        raise ValueError("pilot_receipt_drift")
    raw=(pilot/"official_asia_card_source_urls.jsonl").read_bytes()
    if hashlib.sha256(raw).hexdigest()!=p["source_url_manifest_sha256"]:
        raise ValueError("pilot_data_sha256_drift")
    src=[json.loads(x) for x in raw.splitlines()]
    if len(src)!=40:raise ValueError("pilot_count_drift")
    manifest=json.loads(get(CANARY,2000000))
    packs=[]
    pack_shas={}
    with tempfile.TemporaryDirectory(prefix="cardscanr-official-asian-crosswalk-") as d:
        for lang in TARGETS:
            desc=pick(manifest,lang)
            zipped=get(desc["compressedDatabaseUrl"],MAX_GZIP)
            f=Path(d)/(lang+".sqlite")
            checks,_,_,_,_,_=inspect_pack(desc,zipped,f)
            if checks["sqlite_quick_check"]!="ok":raise ValueError("pack_check_failed")
            pack_shas[lang]=desc["sha256"]
            uri="file:"+quote(f.resolve().as_posix(),safe="/:")+"?mode=ro&immutable=1"
            with sqlite3.connect(uri,uri=True) as db:
                db.execute("PRAGMA query_only=ON")
                cols={r[1] for r in db.execute("PRAGMA table_info(cards)")}
                if not set(FIELDS).issubset(cols):raise ValueError("pack_columns_drift")
                for row in db.execute("SELECT "+",".join(FIELDS)+" FROM cards"):
                    pack_record=dict(zip(FIELDS,row))
                    if pack_record["language"]!=lang:raise ValueError("pack_language_drift")
                    packs.append(pack_record)
    matches,counts=check(packs,src)
    out.mkdir(parents=True)
    b=("\n".join(json.dumps(x,ensure_ascii=False,sort_keys=True) for x in matches)+"\n").encode()
    (out/"archived_exact_crosswalk.jsonl").write_bytes(b)
    receipt={
      "classification":"OFFICIAL_TH_ID_ARCHIVED_EXACT_SOURCE_IMAGE_CROSSWALK_ONLY",
      "source_card_rows":len(src),
      "archived_pack_card_rows":len(packs),
      "archive_sha256_by_locale":pack_shas,
      "matching_results":dict(counts),
      "exact_crosswalk_rows":len(matches),
      "exact_candidate_records":sum(x["classification"]=="exact_missing_image_source_url_candidate" for x in matches),
      "crosswalk_sha256":hashlib.sha256(b).hexdigest(),
      "verified_live_id_and_image_missing":0,
      "artwork_binaries_downloaded":0,"live_vault_writes":0,"new_canonical_cards":0,
      "artwork_rights_verified":False,"safe_for_publication":False,
    }
    (out/"receipt.json").write_text(json.dumps(receipt,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf8")
    return receipt

def selftest():
    records=[dict(zip(FIELDS,("cid1","tcgdex:asia:set:SV7s","th","เรดิบา","001",None,None)))]
    source=[{"language":"th","source_product_code":"SV7s",
             "source_name_native":"เรดิบา","source_collector_number":"001",
             "source_image_url_reference":"https://asia.pokemon-card.com/th/card-img/th00009635.png",
             "official_detail_id":"9635","official_detail_url":"https://asia.pokemon-card.com/th/card-search/detail/9635/",
             "source_response_sha256":"a"*64}]
    hits,counts=check(records,source)
    assert len(hits)==1 and counts["exact_missing_image_source_url_candidate"]==1
    hits,counts=check(records+records,source)
    assert len(hits)==0 and counts["ambiguous_card_variants"]==1
    duplicated=[dict(source[0]),dict(source[0],official_detail_id="9636",
                     source_image_url_reference="https://asia.pokemon-card.com/th/card-img/th00009636.png")]
    hits,counts=check(records,duplicated)
    assert len(hits)==0 and counts["ambiguous_source_printings"]==2
    assert not safe_url("https://asia.pokemon-card.com/id/card-img/id00009901.png","th")
    print("PASS strict official Thailand/Indonesia native name and number crosswalk")

if __name__=="__main__":
    a=argparse.ArgumentParser()
    a.add_argument("--pilot",type=Path)
    a.add_argument("--out",type=Path)
    a.add_argument("--self-test",action="store_true")
    args=a.parse_args()
    if args.self_test:selftest()
    else:
        if not args.pilot or not args.out:a.error("--pilot and --out required")
        result=audit(args.pilot,args.out)
        print("OFFICIAL_TH_ID_EXACT_CROSSWALK_PASS",json.dumps({
           "source_rows":result["source_card_rows"],
           "matched":result["exact_crosswalk_rows"],
           "new_archive_image_candidates":result["exact_candidate_records"],
           "counts":result["matching_results"],
           "live_vault_writes":0,"artwork_downloaded":0,
        },ensure_ascii=False,sort_keys=True))
