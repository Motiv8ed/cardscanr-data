#!/usr/bin/env python3
"""High-throughput TCGdex full-set source URL crosswalk for archived Asia gaps.

Uses one provider SET JSON request per set (not one request per card), against
SHA-verified CardScanR R2 language packs. Source-only, no art bodies or live PC.
"""
from __future__ import annotations
import argparse,hashlib,json,sqlite3,tempfile,time,re
from collections import Counter,defaultdict
from pathlib import Path
from urllib.parse import quote
from urllib.error import HTTPError,URLError
from tools.preflight_multilingual_crossrelease import CANARY,MAX_GZIP,get,inspect_pack
from tools.preflight_legacy_korean_pack import pick
from tools.private_image_identity_crosswalk import crosswalk_rows
from tools.private_image_crosswalk_bulk_29 import TARGETS as PRIOR_TARGETS
from tools.tcgdex_metadata_batch import API,fetch_json
from tools.private_artwork_takedown_controls import _safe_image_base

LANGS=("zh-tw","ko","th","id")
MAX_SETS=180
SAFE_SET=re.compile(r"^[A-Za-z0-9_.-]{1,48}$")
COLS=("canonical_base_id","canonical_printing_id","set_id","language",
      "printed_collector_number","native_card_name","image_thumbnail_url","image_display_url")
SKIP={(a,b) for a,b,_ in PRIOR_TARGETS}

def _url_ok(base):
    if not isinstance(base,str) or not _safe_image_base(base):return False
    from urllib.parse import urlsplit
    u=urlsplit(base+"/low.webp")
    return (u.scheme=="https" and u.hostname=="assets.tcgdex.net"
        and not u.query and not u.fragment and re.fullmatch(
        r"/[A-Za-z0-9-]+/[A-Za-z0-9-]+/[A-Za-z0-9-]+/[A-Za-z0-9-]+/low[.]webp",u.path))

def run(lang:str,offset:int,max_sets:int,pace:float=1.1):
    if lang not in LANGS or not 0<=offset<=1000 or not 1<=max_sets<=MAX_SETS or not 0.8<=pace<=4:
        raise ValueError("invalid_bounded_language_offset_or_request_rate")
    man=json.loads(get(CANARY,2_000_000))
    desc=pick(man,lang)
    compressed=get(desc["compressedDatabaseUrl"],MAX_GZIP)
    rows=[]
    recs=[]
    errors=[]
    with tempfile.TemporaryDirectory(prefix="cardscanr-bulk-set-crosswalk-") as tmp:
        f=Path(tmp)/(lang+".sqlite")
        check,_,_,_,_,_=inspect_pack(desc,compressed,f)
        if check["sqlite_quick_check"]!="ok":raise ValueError("source_pack_sqlite_corrupt")
        uri="file:"+quote(f.resolve().as_posix(),safe="/:")+"?mode=ro&immutable=1"
        with sqlite3.connect(uri,uri=True) as db:
            db.execute("PRAGMA query_only=ON")
            gap_rows=db.execute("""
             SELECT set_id,COUNT(*)
             FROM cards WHERE language=?
             AND set_id LIKE 'tcgdex:asia:set:%'
             AND COALESCE(TRIM(image_thumbnail_url),'')=''
             AND COALESCE(TRIM(image_display_url),'')=''
             GROUP BY set_id ORDER BY COUNT(*) DESC,set_id""",(lang,)).fetchall()
            plan=[]
            for full,count in gap_rows:
                code=full.removeprefix("tcgdex:asia:set:")
                if not SAFE_SET.fullmatch(code) or (lang,code) in SKIP:continue
                plan.append((code,int(count)))
            total=len(plan)
            part=plan[offset:offset+max_sets]
            for ix,(sid,gap) in enumerate(part):
                if ix:time.sleep(pace)
                url=f"{API}/{lang}/sets/{sid}"
                try:
                    source,body=fetch_json(url)
                    if not isinstance(source,dict) or source.get("id")!=sid:
                        raise ValueError("source_set_id_drift")
                    if not isinstance(body,bytes) or len(body)>2_000_000:
                        raise ValueError("source_set_payload_over_limit")
                    source_cards=source.get("cards")
                    if not isinstance(source_cards,list) or len(source_cards)>5000:
                        raise ValueError("invalid_source_card_roster")
                    local=[dict(zip(COLS,row)) for row in db.execute(
                      "SELECT "+",".join(COLS)+" FROM cards WHERE language=? AND set_id=?",
                      (lang,"tcgdex:asia:set:"+sid))]
                    matches,summary=crosswalk_rows(local,source_cards,language=lang,source_set_id=sid)
                    got=[]
                    for entry in matches:
                        if entry.get("status")!="exact_unique" or not entry.get("candidate_for_missing_pack_image_ref"):
                            continue
                        image=entry.get("source_image_base_url")
                        if not _url_ok(image):continue
                        got.append({
                          "classification":"TCGDEX_SOURCE_ONLY_EXACT_UNIQUE_ARCHIVED_PRINTING",
                          "canonical_printing_id":entry["canonical_printing_id"],
                          "language":lang,"provider_set_id":sid,
                          "provider_card_id":entry["provider_card_id"],
                          "source_image_url":image+"/low.webp",
                          "source_set_sha256":hashlib.sha256(body).hexdigest(),
                          "archive_set_gap":gap,
                          "live_vault_match_verified":False,
                          "rights_granted":False,
                          "artwork_downloaded":False,
                        })
                    rows.extend(got)
                    recs.append({"source_set":sid,"archive_gap":gap,
                      "source_card_briefs":len(source_cards),
                      "valid_exact_image_refs":len(got),
                      "source_json_sha256":hashlib.sha256(body).hexdigest(),
                      "status_counts":summary.get("status_counts")})
                except (HTTPError,URLError,OSError,ValueError,KeyError,TypeError) as exc:
                    errors.append({"set":sid,"kind":type(exc).__name__,
                                   "http_status":getattr(exc,"code",None)})
                    if isinstance(exc,HTTPError) and exc.code==429:
                        break
    if len({x["canonical_printing_id"] for x in rows})!=len(rows):
        raise ValueError("duplicate_exact_canonical_source_mapping")
    return rows,{
      "classification":"BULK_TCGDEX_FULL_SET_EXACT_SOURCE_URLS_NOT_LIVE",
      "language":lang,"offset":offset,"request_cap":max_sets,
      "targeted_sets":len(part),"available_gap_sets":total,
      "source_sets_succeeded":len(recs),"source_sets_failed":len(errors),
      "source_set_proofs":recs,"errors":errors,
      "verified_archive_pack_sha256":desc["sha256"],
      "source_url_candidates":len(rows),"source_url_rights_unverified":True,
      "new_canonical_records":0,"image_files_downloaded":0,"live_vault_writes":0,
      "published_artwork":False,
    }

def save(out:Path,rows:list,report:dict):
    if out.exists() or out.is_symlink():raise FileExistsError("new_out_required")
    out.mkdir(parents=True)
    blob=("".join(json.dumps(x,ensure_ascii=False,sort_keys=True)+"\n" for x in rows)).encode()
    (out/"image_source_candidates.jsonl").write_bytes(blob)
    report["candidate_rows_sha256"]=hashlib.sha256(blob).hexdigest()
    (out/"receipt.json").write_text(json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2)+"\n")
    return report

def test():
    assert _url_ok("https://assets.tcgdex.net/th/sv/sv7s/1")
    assert not _url_ok("https://evil.example/th/sv/sv7s/1")
    assert not _url_ok("http://assets.tcgdex.net/th/sv/sv7s/1")
    for value in ("",None,"?x=1"):
        assert not _url_ok(value)
    assert len(SKIP)==29
    print("PASS strictly bounded full-set URL metadata and exact crosswalk")

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--language",choices=LANGS)
    p.add_argument("--offset",type=int,default=0)
    p.add_argument("--max-sets",type=int,default=180)
    p.add_argument("--pace",type=float,default=1.1)
    p.add_argument("--output",type=Path)
    p.add_argument("--self-test",action="store_true")
    a=p.parse_args()
    if a.self_test:test()
    else:
        if not a.output or not a.language:p.error("--language and --output required")
        rows,report=run(a.language,a.offset,a.max_sets,a.pace)
        save(a.output,rows,report)
        print("CARD_SCANR_BULK_SET_SWEEP",json.dumps({
          "lang":a.language,"offset":a.offset,
          "sets":report["source_sets_succeeded"],
          "failed":report["source_sets_failed"],"image_url_candidates":len(rows),
          "no_card_or_image_file_writes":True},sort_keys=True))
