#!/usr/bin/env python3
"""Strict archived-live Chinese CN-151C variant-to-picture source reconciliation.

Compares the authenticated pinned live 470 missing printing identities against
immutable CardCorpus Chinese Collect 151 set cards AND each explicit variant.
Stores third-party artwork URL REFERENCES privately, not image bytes.
CC BY metadata does NOT license the copyrighted artwork.
"""
from __future__ import annotations
import argparse
from collections import Counter,defaultdict
import hashlib
import json
from pathlib import Path
import re
import unicodedata

COMMIT="6fabdd3411c14d2d4c4ab84f4f24af79e1814ddc"
LIVE_SET="pokemon-tcg-kb-pikaqian:set:cn-151c"
CORPUS_SET="zh-hans/collect-151"
URL_PREFIX="https://img.cardcorpus.com/s1/480/"
PICTURE=re.compile(r"^[0-9a-f]{32}$")
EXPECTED=470


def norm(value):
    return unicodedata.normalize("NFC",str(value or "")).strip()


def advertised_name(card,variant):
    """Only literal known suffixes, never substitute a guessed holo treatment."""
    name=norm(card.get("name"))
    variants=card.get("variants")
    if not name or not isinstance(variants,list):return None
    if len(variants)==1:return name
    suffix={"non-holo":"","holo":"","master-ball":" Master Ball Holo",
            "poke-ball":" Pokeball Holo"}.get(variant.get("id"))
    return name+suffix if suffix is not None else None


def index_source(cards):
    index=defaultdict(list)
    vrefs=set()
    for c in cards:
        n=c.get("number",{}).get("short")
        if not isinstance(n,str) or not re.fullmatch(r"[0-9]{3}",n):
            raise ValueError("bad_source_collector_number")
        if not isinstance(c.get("variants"),list) or not c["variants"]:
            raise ValueError("source_missing_variants")
        for v in c["variants"]:
            ident=(c["id"],v.get("id"))
            if ident in vrefs:raise ValueError("duplicate_source_variant")
            vrefs.add(ident)
            pic=v.get("picture",{}).get("front",{}).get("image")
            if not isinstance(pic,str) or not PICTURE.fullmatch(pic):
                continue
            label=advertised_name(c,v)
            if label:index[(n,label)].append({
                "source_card_id":c["id"],"source_variant_id":v["id"],
                "source_variant_finish":v.get("finish"),"source_picture_id":pic,
                "source_picture_url_reference":URL_PREFIX+pic+".avif",
                "source_card_name":c["name"]
            })
    return index,len(vrefs)


def match(live,source):
    if (source.get("id")!=CORPUS_SET or source.get("catalog")!="zh-hans"
        or source.get("language")!="zh-Hans" or source.get("code")!="151C"
        or len(source.get("cards",[]))!=192):
        raise ValueError("wrong_source_set")
    if (live.get("classification")!="PINNED_LIVE_ZHCN_C151C_VARIANT_IDENTITIES"
        or live.get("set_id")!=LIVE_SET or live.get("source_commit")!=COMMIT
        or live.get("source_set")!=CORPUS_SET or len(live.get("rows",[]))!=EXPECTED
        or not re.fullmatch(r"[0-9a-f]{64}",live.get("snapshot_sha256",""))):
        raise ValueError("live_snapshot_or_set_drift")
    index,variants=index_source(source["cards"])
    if variants!=EXPECTED:raise ValueError("source_variant_total_drift")
    matches=[];used=set();count=Counter()
    for r in live["rows"]:
        if (set(r)!={"canonical_printing_id","language","set_id","native_card_name","printed_collector_number"}
            or r["language"]!="zh-cn" or r["set_id"]!=LIVE_SET
            or not isinstance(r["canonical_printing_id"],str)
            or not re.fullmatch(r"[0-9]{3}",str(r["printed_collector_number"]))):
            raise ValueError("invalid_live_printing_row")
        k=(str(r["printed_collector_number"]),norm(r["native_card_name"]))
        options=index.get(k,[])
        unique=len(options)==1
        source_ref=options[0] if unique else None
        v=(source_ref["source_card_id"],source_ref["source_variant_id"]) if unique else None
        if v in used:unique=False
        if unique:used.add(v)
        reason="EXACT_ONE_TO_ONE_NATIVE_PRINTING_VARIANT" if unique else (
            "NO_EXACT_SOURCE_VARIANT" if not options else "AMBIGUOUS_SOURCE_PRINTING_VARIANT")
        count[reason]+=1
        matches.append({
            **r, "classification":reason,
            "source":source_ref if unique else None,
            "source_repository":"cardcorpus/pokemon-card-dataset",
            "source_commit":COMMIT,"source_set_id":CORPUS_SET,
            "live_identity_snapshot_sha256":live["snapshot_sha256"],
            "image_bytes_downloaded":0,"artwork_rights_verified":False,
            "commercial_artwork_license":None,
            "live_vault_preflight_passed":False,
            "safe_to_publish":False,"safe_for_vault_apply":False})
    if len({r["canonical_printing_id"] for r in matches})!=len(matches):
        raise ValueError("duplicate_live_canonical")
    return matches,dict(sorted(count.items())),variants


def run(live_path,source_path,out):
    if out.exists() or out.is_symlink():raise FileExistsError("must_use_new_output")
    live=json.loads(live_path.read_text(encoding="utf8"))
    original=source_path.read_bytes()
    source=json.loads(original)
    rows,counts,variants=match(live,source)
    ordered=sorted(rows,key=lambda r:r["canonical_printing_id"])
    data="".join(json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n" for x in ordered).encode()
    receipt={
        "classification":"PRIVATE_CHINESE_151C_LIVE_VARIANT_PICTURE_SOURCE_CROSSWALK",
        "source_commit":COMMIT,"source_set":CORPUS_SET,
        "source_set_file_sha256":hashlib.sha256(original).hexdigest(),
        "live_snapshot_sha256":live["snapshot_sha256"],
        "live_missing_printings_examined":len(rows),
        "source_cards":len(source["cards"]),"source_variants":variants,
        "classification_counts":counts,"candidate_sha256":hashlib.sha256(data).hexdigest(),
        "images_downloaded":0,"live_new_image_links":0,"production_writes":False,
        "artwork_rights_verified":False,"ready_for_vault_apply":False,
        "note":"One-to-one printing variant identity research, not a rights grant. Current PC Vault cannot import PikaQian/Chinese source refs; new guarded importer needed."
    }
    out.mkdir()
    (out/"candidate_crosswalk.jsonl").write_bytes(data)
    (out/"receipt.json").write_text(json.dumps(receipt,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf8")
    return receipt


def self_test():
    mk=lambda id,finish=None:{"id":id,"finish":finish,"picture":{"front":{"image":"a"*32}}}
    c={"id":"001","name":"Bulbasaur","number":{"short":"001"},
       "variants":[mk("non-holo","non_holo"),mk("master-ball","master_ball"),mk("poke-ball","poke_ball")]}
    idx,n=index_source([c])
    assert n==3
    assert ("001","Bulbasaur") in idx
    assert ("001","Bulbasaur Master Ball Holo") in idx
    assert ("001","Bulbasaur Pokeball Holo") in idx
    assert ("001","Bulbasaur Holo") not in idx
    multiholo={"id":"015","name":"Beedrill","number":{"short":"015"},
               "variants":[mk("holo","holo"),mk("master-ball","master_ball"),mk("poke-ball")]}
    hx,hn=index_source([multiholo])
    assert hn==3 and ("015","Beedrill") in hx
    single={"id":"003","name":"Venusaur ex","number":{"short":"003"},"variants":[mk("holo","holo")]}
    idx,n=index_source([single])
    assert n==1 and ("003","Venusaur ex") in idx
    assert URL_PREFIX=="https://img.cardcorpus.com/s1/480/"
    print("PASS exact standard, master-ball, poke-ball and singleton holo variant identities")


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--live",type=Path)
    p.add_argument("--source",type=Path)
    p.add_argument("--out",type=Path)
    p.add_argument("--self-test",action="store_true")
    a=p.parse_args()
    if a.self_test:self_test()
    else:
        if not all((a.live,a.source,a.out)):p.error("required input")
        print("ZHCN_C151C_PRIVATE_CROSSWALK_RESULT",json.dumps(run(a.live,a.source,a.out),ensure_ascii=False))
