#!/usr/bin/env python3
"""Resumable private CHS source evidence collector. Never applies/imports or fetches artwork.

Runs with an owner-supplied original crosswalk ZIP, reviewed source plan and
existing local Bridge worker key. Source plan is a list of work, not permission.
Outputs deterministic proof JSON to a private local checkpoint directory.
"""
import argparse
import hashlib
import json
import re
import time
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path
from urllib.request import Request, urlopen

REF = "6fabdd3411c14d2d4c4ab84f4f24af79e1814ddc"
CROSSWALK = "525ed14cf3318d971a19557e4a1c70b1d2309131832f16a64e6534c90cd19c03"
RESERVED = {"9","4","29","157","90","181","314","275","329","330","494","495","493","328","224","31","3","475","485","458","482","324","1"}
MAX_SOURCE = 8_000_000
MAX_PAGE = 2_000_000
SOURCE = "https://raw.githubusercontent.com/cardcorpus/pokemon-card-dataset/" + REF + "/v1/sets/"
BRIDGE = "http://127.0.0.1:8855/v1/action"

def require(condition, reason):
    if not condition:
        raise ValueError(reason)

def sha(raw): return hashlib.sha256(raw).hexdigest()
def encoded(obj): return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf8")
def norm(s): return re.sub("[^A-Z0-9]", "", s.upper())
def number(s):
    n = str(s).split("/")[0]
    return str(int(n)) if re.fullmatch("[0-9]+", n) else None
def name(s): return unicodedata.normalize("NFC", s)

def read_crosswalk(path):
    with zipfile.ZipFile(path) as z:
        info = z.getinfo("private_crosswalk.jsonl")
        require(info.file_size <= MAX_SOURCE, "oversized_crosswalk")
        raw = z.read(info)
    require(sha(raw) == CROSSWALK, "crosswalk_sha_mismatch")
    rows = [json.loads(x) for x in raw.splitlines()]
    require(len(rows)==5289 and len({x["archived_canonical_printing_id"] for x in rows})==5289, "crosswalk_count_or_identity")
    return rows

def expected(rows, sid, slug):
    prefix = "ptcg-chs-datasets:printing:" + sid + "_"
    selected = [r for r in rows if r["archived_canonical_printing_id"].startswith(prefix)
                and r["independent_native_identity_corroborated"] is True
                and r["source_set_id"] == slug]
    return sorted([[r["archived_canonical_printing_id"],r["archived_native_chinese_name"],
                    number(r["printed_collector_number"]),r["source_picture_id"],
                    r["normalized_source_set_code"],slug] for r in selected])

def match(source, live, archive, scope):
    sid, slug = scope["id"], scope["source_set"]
    require(source["id"] == slug and source.get("catalog")=="zh-hans" and source.get("language")=="zh-Hans", "source_set_mismatch")
    tuples = expected(archive, sid, slug)
    require(len(tuples)==scope["archive_candidate_count"]
            and sha(encoded(tuples))==scope["expected_tuple_sha256"], "plan_not_derived_from_crosswalk")
    exact = {r[0]:r for r in tuples}
    cards = source["cards"]
    sc = norm(source["code"])
    counts = Counter(number(c["number"]["printed"]) for c in cards)
    by_number = {}
    for c in cards:
        n = number(c["number"]["printed"])
        if n is not None and counts[n]==1:
            by_number[n] = c
    live_counts = Counter(number(r["printed_collector_number"]) for r in live)
    prefix = "ptcg-chs-datasets:printing:" + sid + "_"
    out = []
    rejected = Counter()
    require(len({r["canonical_printing_id"] for r in live})==len(live), "duplicate_live_id")
    for r in live:
        cid = r["canonical_printing_id"]
        require(cid.startswith(prefix) and r["set_id"]=="ptcg-chs-datasets:set:"+sid
                and r["language"]=="zh-cn" and r["has_image_link"] is False, "invalid_live_identity")
        if cid not in exact: continue
        n=number(r["printed_collector_number"])
        c=by_number.get(n)
        if n is None or not re.fullmatch("[0-9]{3}",r["printed_collector_number"]) or live_counts[n]!=1 or c is None:
            rejected["ambiguous_number"]+=1; continue
        variants=c.get("variants",[])
        if len(variants)!=1 or variants[0].get("id") not in ("holo","non-holo"):
            rejected["variant_not_unique"]+=1; continue
        v=variants[0]
        picture=v.get("picture",{}).get("front",{}).get("image","")
        if not re.fullmatch("[0-9a-f]{32}",picture) or exact[cid]!=[cid,name(r["native_card_name"]),n,picture,sc,slug]:
            rejected["archive_tuple_mismatch"]+=1; continue
        out.append({"live":r,"number":n,"picture":picture,"variant":v["id"],"finish":v.get("finish")})
    out.sort(key=lambda x:x["live"]["canonical_printing_id"])
    return out, dict(rejected)

def fetch_json(req, limit):
    # Network destinations are fixed; never follow redirects to another endpoint.
    from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs): return None
    with build_opener(ProxyHandler({}), NoRedirect()).open(req,timeout=25) as r:
        raw=r.read(limit+1)
    require(len(raw)<=limit,"response_limit")
    return json.loads(raw),raw

def source_get(slug):
    require(re.fullmatch(r"zh-hans/[a-z0-9-]{1,180}",slug),"invalid_source_slug")
    return fetch_json(Request(SOURCE+slug+".json",headers={"User-Agent":"CardScanR-private-metadata-research"}),MAX_SOURCE)

def bridge_read(secret, **args):
    doc,_=fetch_json(Request(BRIDGE,data=encoded({"action":"artwork_vault_identity_page",**args}),
        headers={"Content-Type":"application/json","X-Motiv8ed-Worker-Key":secret},method="POST"),MAX_PAGE)
    require(doc.get("ok") is True,"bridge_refused")
    return doc["result"]

def live_get(secret,sid,read=bridge_read):
    rows=[];cursor=None;snapshot=None;total=None;seen=set()
    for _ in range(100):
        args=dict(text="zh-cn",draft_id="ptcg-chs-datasets:set:"+sid,limit=100)
        if cursor: args["message_id"]=cursor
        page=read(secret,**args)
        if snapshot is None: snapshot,total=page["snapshot_sha256"],page["missing_cards_in_snapshot"]
        require(re.fullmatch("[0-9a-f]{64}",snapshot) and page["snapshot_sha256"]==snapshot
                and page["missing_cards_in_snapshot"]==total,"snapshot_drift")
        require(len(page["rows"])<=100,"page_bound")
        rows.extend(page["rows"])
        cursor=page["next_cursor"]
        if not cursor:
            require(len(rows)==total and len({r["canonical_printing_id"] for r in rows})==total,"snapshot_count_mismatch")
            raw=json.dumps(rows,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode("utf8")
            require(sha(raw)==snapshot,"snapshot_hash_mismatch")
            return rows,snapshot
        require(cursor not in seen and page["rows"],"cursor_loop")
        seen.add(cursor)
    raise ValueError("page_limit")

def atomic_new(path,obj):
    raw=encoded(obj)
    if path.exists():
        require(path.read_bytes()==raw,"checkpoint_conflict")
        return
    # Flush a temporary sibling and publish with an exclusive hard link.
    # An interrupted write cannot leave a partially written final checkpoint.
    import os, tempfile
    fd,tmp=tempfile.mkstemp(prefix=".proof-",suffix=".tmp",dir=path.parent)
    try:
        with os.fdopen(fd,"wb") as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        try: os.link(tmp,path)
        except FileExistsError: require(path.read_bytes()==raw,"checkpoint_conflict")
    finally:
        os.unlink(tmp)

def run(plan,archive,out,secret,max_sets=5,source_reader=source_get,live_reader=live_get):
    require(plan["crosswalk_sha256"]==CROSSWALK and plan["source_commit"]==REF,"invalid_plan")
    require(1<=max_sets<=30,"set_limit")
    out.mkdir(parents=True,exist_ok=True)
    completed=0
    count=0
    for scope in plan["sets"]:
        sid=scope["id"]
        require(re.fullmatch("[0-9]{1,6}",sid),"invalid_scope_id")
        if sid in RESERVED: continue
        checkpoint=out/("set_"+sid+".json")
        if checkpoint.exists():
            saved=json.loads(checkpoint.read_text("utf8"))
            require(saved.get("classification")=="PRIVATE_SOURCE_PROOF_ONLY" and saved.get("id")==sid and saved.get("source_commit")==REF and saved.get("new_live_links")==0 and saved.get("verified_candidates")==len(saved.get("rows",[])), "invalid_checkpoint")
            continue
        if completed>=max_sets: break
        completed+=1
        try:
            src,raw=source_reader(scope["source_set"])
            rows,snap=live_reader(secret,sid)
            matches,rejected=match(src,rows,archive,scope)
            # Empty or incomplete result remains visible; do not silently count candidates as imports.
            proof={"classification":"PRIVATE_SOURCE_PROOF_ONLY","id":sid,"source_set":scope["source_set"],
                   "source_commit":REF,"source_sha256":sha(raw),"live_snapshot_sha256":snap,
                   "verified_candidates":len(matches),"quarantined":rejected,"rows":matches,
                   "new_live_links":0,"safe_for_publication":False,"artwork_rights_granted":False}
            atomic_new(out/("set_"+sid+".json"),proof)
            count+=len(matches)
            print(json.dumps({"set":sid,"verified_candidates":len(matches),"quarantined":rejected}),flush=True)
        except (OSError,ValueError,KeyError,TypeError) as e:
            print(json.dumps({"set":sid,"state":"retry_next_run","reason":type(e).__name__}),flush=True)
        time.sleep(1)
    return {"attempted_sets":completed,"verified_candidates":count,"new_live_links":0}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--crosswalk",type=Path,required=True)
    p.add_argument("--plan",type=Path,required=True)
    p.add_argument("--out",type=Path,required=True)
    p.add_argument("--worker-key-file",type=Path,required=True)
    p.add_argument("--max-sets",type=int,default=5)
    a=p.parse_args()
    require(a.worker_key_file.name=="worker.key" and a.worker_key_file.is_file()
            and not a.worker_key_file.is_symlink() and a.worker_key_file.stat().st_size<=512,"invalid_local_key")
    secret=a.worker_key_file.read_text("utf8").strip()
    require(bool(secret),"empty_key")
    print(json.dumps(run(json.loads(a.plan.read_text("utf8")),read_crosswalk(a.crosswalk),a.out,secret,a.max_sets)))

if __name__=="__main__": main()
