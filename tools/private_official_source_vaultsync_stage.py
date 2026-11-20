#!/usr/bin/env python3
"""Build private, immutable, rights-unverified official TW/KR URL source batches.

Every candidate is independently revalidated against a pinned upstream JSON
card file and the SHA-signed archival CardScanR pack crosswalk. The resulting
bounded gzip batches target *existing* canonical printings and are NOT applied.
A live vault apply must still validate current local identities and backups.
"""
from __future__ import annotations

import argparse
from collections import defaultdict, Counter
import gzip
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

SOURCE_COMMIT = "18e0de2feb7a35ebe4296ede0ba6cc768961489b"
CROSSWALK_RUN = 44664424074
EXPECTED = {"zh-tw": 6916, "ko": 196}
PRINTING = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SET = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")
SOURCEPRINT = re.compile(r"^asia:[A-Za-z0-9_.-]{1,48}-[A-Za-z0-9_.-]{1,48}$")
MAX_ROWS = 900

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def safe_image(url, lang, sid):
    if not isinstance(url,str) or len(url)>1000:return False
    try:
        u=urlsplit(url)
        if (u.scheme!="https" or u.username or u.password or
                u.port not in (None,443) or u.query or u.fragment):
            return False
        if lang=="zh-tw":
            return u.hostname=="asia.pokemon-card.com" and bool(re.fullmatch(
                r"/tw/card-img/tw[0-9]{8}\.png",u.path))
        if lang=="ko":
            return u.hostname=="cards.image.pokemonkorea.co.kr" and bool(re.fullmatch(
                r"/data/wmimages/[A-Za-z0-9_-]{1,16}/"
                +re.escape(sid)+"/"+re.escape(sid)+r"_[0-9]{3}\.png",u.path))
        return False
    except ValueError:return False

def normalize_num(n):
    if not isinstance(n,str) or not n:return ""
    s=n.strip().split("/",1)[0].casefold()
    if not re.fullmatch(r"[a-z0-9]+",s):return ""
    return re.sub(r"\d+",lambda m:str(int(m.group(0))),s)

def stage(upstream:Path,crosswalk:Path,out:Path):
    if out.exists() or out.is_symlink():raise FileExistsError("stage_destination_must_be_new")
    receipt=json.loads((crosswalk/"receipt.json").read_text("utf8"))
    if (receipt.get("classification")!="PRIVATE_EXACT_SOURCE_URL_RESEARCH_ONLY"
        or receipt.get("unique_matched_missing_pack_image_reference_candidates")!=7112
        or receipt.get("source_commit")!=SOURCE_COMMIT
        or receipt.get("artwork_rights_granted") is not False
        or len(receipt.get("pack_provenance_sha256",[]))!=5):
        raise ValueError("source_crosswalk_receipt_not_pinned")
    packed=(crosswalk/"exact_source_url_candidates.jsonl.gz").read_bytes()
    if sha(packed)!=receipt["candidate_compressed_sha256"]:
        raise ValueError("crosswalk_sha_mismatch")
    decoded=gzip.decompress(packed)
    if sha(decoded)!=receipt["candidate_uncompressed_sha256"]:
        raise ValueError("crosswalk_uncompressed_sha_mismatch")
    rows=[json.loads(x) for x in decoded.splitlines()]
    if len(rows)!=7112:raise ValueError("crosswalk_card_count_drift")
    candidates=[]
    counts=Counter()
    used=set()
    by_set=defaultdict(list)
    for cand in rows:
        lang=cand["language"]
        sid=cand["set_id"].removeprefix("tcgdex:asia:set:")
        rel=Path(cand["source_file"])
        folder="data_tc" if lang=="zh-tw" else "data_ko" if lang=="ko" else None
        if (not folder or rel.parts[0]!=folder or len(rel.parts)!=3
            or not rel.name.endswith(".json") or
            not SET.fullmatch(sid) or rel.parts[1]!=sid
            or not PRINTING.fullmatch(cand["canonical_printing_id"])
            or not SOURCEPRINT.fullmatch(cand["source_print_key"])
            or cand.get("status")!="exact_unique_pack_identity_source_url_candidate"
            or cand.get("existing_pack_image_was_missing") is not True
            or cand.get("artwork_rights_verified") is not False):
            raise ValueError("unsafe_crosswalk_record")
        file=upstream/rel
        if file.is_symlink() or not file.is_file() or file.stat().st_size>240_000:
            raise ValueError("source_detail_file_missing_or_unsafe")
        raw=file.read_bytes()
        src=json.loads(raw)
        if (not isinstance(src,dict)
            or src.get("name")!=cand.get("native_name")
            or normalize_num(str(src.get("number") or "")) != normalize_num(str(cand["printed_collector_number"]))
            or src.get("print_key")!=cand["source_print_key"]
            or src.get("set_name")!=sid
            or src.get("img")!=cand["source_image_url_reference"]
            or not safe_image(cand["source_image_url_reference"],lang,sid)):
            raise ValueError("pinned_source_file_does_not_match_crosswalk")
        key=cand["canonical_printing_id"]
        if key in used:raise ValueError("duplicate_canonical_identity")
        used.add(key)
        by_set[(lang,sid)].append((rel.as_posix(),sha(raw)))
        counts[lang]+=1
        candidates.append({
            "language":lang,"provider_set_id":sid,
            "provider_card_id":cand["source_print_key"],
            "canonical_printing_id":key,
            "native_card_name":cand["native_name"],
            "printed_collector_number":cand["printed_collector_number"],
            "native_set_name":src.get("set_full_name") if isinstance(src.get("set_full_name"),str) else sid,
            "image_url":cand["source_image_url_reference"],
            "source_rel":rel.as_posix(),
            "source_file_sha256":sha(raw),
        })
    if dict(counts)!=EXPECTED:
        raise ValueError("language_total_drift:"+repr(dict(counts)))
    group=defaultdict(list)
    for c in candidates: group[(c["language"],c["provider_set_id"])].append(c)
    # Never split printing identities within the same physical batch.
    packs=[]; current=[]; group_names=[]
    for groupkey,items in sorted(group.items(),key=lambda k:(k[0][0],-len(k[1]),k[0][1])):
        if len(items)>MAX_ROWS:raise ValueError("single_set_over_batch_size")
        if current and len(current)+len(items)>MAX_ROWS:
            packs.append((group_names,current));current=[];group_names=[]
        current.extend(items);group_names.append(groupkey)
    if current:packs.append((group_names,current))
    out.mkdir(parents=True)
    batches=[]
    for idx,(setkeys,cards) in enumerate(packs):
        sets=[]
        for lang,sid in sorted(setkeys):
            originals=sorted(by_set[(lang,sid)])
            raw=("".join(p+"\0"+h+"\n" for p,h in originals)).encode()
            sets.append({
                "language":lang,"provider_set_id":sid,
                "native_set_name":next(c["native_set_name"] for c in cards
                                       if c["language"]==lang and c["provider_set_id"]==sid),
                "source_response_sha256":sha(raw),
                "source_response_sha_note":"sha256 of pinned source files path + content sha256 sorted; not a network response",
            })
        cards_out=[{
            "canonical_printing_id":c["canonical_printing_id"],
            "language":c["language"],"provider_set_id":c["provider_set_id"],
            "provider_card_id":c["provider_card_id"],"status":"exact_unique",
            "native_card_name":c["native_card_name"],
            "printed_collector_number":c["printed_collector_number"],
            "source_file_sha256":c["source_file_sha256"],
        } for c in cards]
        images=[{
            "canonical_printing_id":c["canonical_printing_id"],
            "language":c["language"],"provider_set_id":c["provider_set_id"],
            "image_url":c["image_url"],
        } for c in cards]
        doc={"schema_version":1,"source":"official_crosswalk",
             "sets":sets,"cards":cards_out,"images":images}
        serialized=json.dumps(doc,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()
        compressed=gzip.compress(serialized,mtime=0,compresslevel=9)
        if (len(serialized)>65_000_000 or len(compressed)>20_000_000
            or len(cards)>900 or len(sets)>500):
            raise ValueError("payload_over_vault_contract_limits")
        name=f"official_source_batch_{idx+1:02d}_of_{len(packs):02d}.json.gz"
        (out/name).write_bytes(compressed)
        batches.append({
            "file":name,"batch_id":f"off20261120_{idx+1:03d}",
            "sha256":sha(compressed),"payload_bytes":len(compressed),
            "uncompressed_sha256":sha(serialized),
            "cards":len(cards),"sets":len(sets),
            "language_counts":dict(sorted(Counter(c["language"] for c in cards).items())),
            "image_binaries_downloaded":0,
            "live_vault_write_completed":False,
            "public_artwork_rights_granted":False,
        })
    report={
        "classification":"PRIVATE_EXACT_OFFICIAL_SOURCE_BATCHES_NOT_APPLIED",
        "upstream_commit":SOURCE_COMMIT,
        "source_crosswalk_workflow":CROSSWALK_RUN,
        "source_crosswalk_sha256":receipt["candidate_uncompressed_sha256"],
        "source_pack_hashes":receipt["pack_provenance_sha256"],
        "cards_verified_against_exact_source_files":len(candidates),
        "language_counts":dict(sorted(counts.items())),
        "sets":len(group),
        "batches":batches,
        "artwork_images_downloaded":0,
        "canonical_card_ids_modified":0,
        "live_vault_source_links_added":0,
        "live_vault_identity_check_still_required":True,
        "source_urls_not_licensed_for_public_display":True,
    }
    (out/"receipt.json").write_text(json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2)+"\n")
    (out/"NOTICE.txt").write_text(
        "Private official source URL research only, not a grant of rights to reproduce Pokemon artwork. "
        "All records require exact live-vault printing identity revalidation before recording private links. "
        "Source code metadata sourced from type-null/PTCG-database (MIT), original artwork/text rights separate.\n",
        encoding="utf8")
    return report

def self_test():
    assert safe_image("https://asia.pokemon-card.com/tw/card-img/tw00004285.png","zh-tw","AC1D")
    assert safe_image("https://cards.image.pokemonkorea.co.kr/data/wmimages/MEGA/M1L/M1L_001.png","ko","M1L")
    assert not safe_image("https://evil.example/tw/card-img/tw00004285.png","zh-tw","AC1D")
    assert not safe_image("https://asia.pokemon-card.com/tw/card-img/tw00004285.png?x=1","zh-tw","AC1D")
    assert normalize_num("001/157")==normalize_num("1")
    print("PASS official URL, provenance and number guards")

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--source-root",type=Path)
    p.add_argument("--crosswalk-root",type=Path)
    p.add_argument("--output",type=Path)
    p.add_argument("--self-test",action="store_true")
    a=p.parse_args()
    if a.self_test:self_test();return
    if not all([a.source_root,a.crosswalk_root,a.output]):
        p.error("Source, crosswalk and output are required")
    r=stage(a.source_root,a.crosswalk_root,a.output)
    print(json.dumps({"result":"VERIFIED_STAGED_BATCHES_NOT_APPLIED",
                     "cards":r["cards_verified_against_exact_source_files"],
                     "batches":len(r["batches"]), "sets":r["sets"],
                     "live_vault_writes":0},sort_keys=True))
if __name__=="__main__":main()
