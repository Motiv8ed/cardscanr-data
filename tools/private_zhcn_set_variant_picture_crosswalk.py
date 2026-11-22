#!/usr/bin/env python3
"""Strict per-set Chinese live printing/variant picture source crosswalk.

Only pinned CardCorpus card metadata and authenticated live missing identities.
Actual URL references follow the provider's signed/pinned public index recipe.
No image binaries, public artwork, database writes or art rights claims.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

COMMIT="6fabdd3411c14d2d4c4ab84f4f24af79e1814ddc"
SPECS={
    "csv8c": ("CSV8C","zh-hans/brilliant-illusions",616),
    "csv7c": ("CSV7C","zh-hans/blade-awakening",605),
    "csv9.5c": ("CSV9.5C","zh-hans/terastal-gathering",601),
    "csv4c": ("CSV4C","zh-hans/bonus-round",395),
    "csv5c": ("CSV5C","zh-hans/dark-crystal-blaze",394),
    "csv1c": ("CSV1C","zh-hans/eternal-birth",393),
    "csv2c": ("CSV2C","zh-hans/miracle-journey",393),
    "csv3c": ("CSV3C","zh-hans/fearless-terastal",386),
    "csv6c": ("CSV6C","zh-hans/true-mystery",393),
    "cs1dc": ("CS1DC","zh-hans/dynamax-clash-v-starter-deck",222),
    "csm2dc": ("CSM2DC","zh-hans/shining-synergy-gx-starter-deck",357),
    "csm1dc": ("CSM1DC","zh-hans/storming-emergence-gx-starter-deck",336),
    "csm1cc": ("CSM1cC","zh-hans/storming-emergence-abundant",332),
    "cs1ac": ("CS1aC","zh-hans/dynamax-clash-thunder",329),
    "csm1ac": ("CSM1aC","zh-hans/storming-emergence-radiant",331),
    "csm1bc": ("CSM1bC","zh-hans/storming-emergence-verdant",324),
    "csm2ac": ("CSM2aC","zh-hans/shining-synergy-shower",328),
    "csm2bc": ("CSM2bC","zh-hans/shining-synergy-supreme",327),
    "csm2cc": ("CSM2cC","zh-hans/shining-synergy-summon",322),
    "cs1bc": ("CS1bC","zh-hans/dynamax-clash-flame",315),
    "cs4dac": ("CS4DaC","zh-hans/start-deck-100",566),
    "cs4ac": ("CS4aC","zh-hans/nine-colours-gathering-friend",295),
    "cs3ac": ("CS3aC","zh-hans/primordial-arts-overgrow",290),
    "cs4bc": ("CS4bC","zh-hans/nine-colours-gathering-origin",288),
    "cs3bc": ("CS3bC","zh-hans/primordial-arts-torrent",283),
    "cs6bc": ("CS6bC","zh-hans/shadow-of-the-blue-sea-banish",282),
}
NEXT_SIX=frozenset({"cs4dac","cs4ac","cs3ac","cs4bc","cs3bc","cs6bc"})
PICTURE=re.compile(r"^[0-9a-f]{32}$")
SNAPSHOT=re.compile(r"^[0-9a-f]{64}$")
COLLECTOR=re.compile(r"^[0-9]{3}$")
PUBLISHING_DENIED=True


def norm(value):
    return unicodedata.normalize("NFC",str(value or "")).strip()


def provider_url_recipe(index):
    pictures=index.get("pictures",{})
    template=pictures.get("sized")
    widths=pictures.get("widths")
    if template!="https://img.cardcorpus.com/s1/{width}/{id}.avif" or not isinstance(widths,list) or 480 not in widths:
        raise ValueError("source_declared_picture_url_template_changed")
    return template


def printing_label(card, variant):
    name=norm(card.get("name"))
    variants=card.get("variants")
    if not name or not isinstance(variants,list) or not variants:
        raise ValueError("invalid_source_printing")
    if len(variants)==1:return name
    suffix={"non-holo":"","holo":"","reverse-holo":" Reverse Holo",
            "master-ball":" Master Ball Holo",
            "poke-ball":" Pokeball Holo"}.get(variant.get("id"))
    return name+suffix if suffix is not None else None


def source_index(cards,template):
    seen=set(); by_print=defaultdict(list); variant_count=0; no_picture=0
    for card in cards:
        number=card.get("number",{}).get("short")
        if not isinstance(number,str) or not COLLECTOR.fullmatch(number):
            raise ValueError("source_number_not_three_digits")
        variants=card.get("variants")
        if not isinstance(variants,list) or not variants:
            raise ValueError("source_card_without_variants")
        for variant in variants:
            variant_count+=1
            vkey=(card.get("id"),variant.get("id"))
            if vkey in seen:raise ValueError("source_variant_repeated")
            seen.add(vkey)
            picture=variant.get("picture",{}).get("front",{}).get("image")
            if not isinstance(picture,str) or not PICTURE.fullmatch(picture):
                no_picture+=1;continue
            label=printing_label(card,variant)
            if label is None:continue
            url=template.format(width=480,id=picture)
            p=urlsplit(url)
            if (p.scheme!="https" or p.hostname!="img.cardcorpus.com"
                    or p.path!=f"/s1/480/{picture}.avif" or p.query or p.fragment):
                raise ValueError("image_url_does_not_match_declared_template")
            labels=[(label,"exact_source_name")]
            # One explicitly bounded typography equivalence: terminal -GX vs GX.
            # Never drop a tag-team partner, translate names, or fuzzy match.
            if card["name"].endswith("-GX") and label.startswith(card["name"]):
                labels.append((card["name"][:-3]+" GX"+label[len(card["name"]):],
                               "terminal_gx_hyphen_only"))
            for printed_label,method in labels:
                by_print[(number,printed_label)].append({
                    "source_card_id":card["id"],
                    "source_variant_id":variant["id"],
                    "source_variant_finish":variant.get("finish"),
                    "source_picture_id":picture,
                    "source_picture_url_reference":url,
                    "source_card_name":card["name"],
                    "source_name_match_method":method,
                })
    return by_print,variant_count,no_picture


def reconcile(live, source, index, slug):
    if slug not in SPECS:raise ValueError("set_not_allowlisted")
    code, source_id, expected_live=SPECS[slug]
    expected_vault="pokemon-tcg-kb-pikaqian:set:cn-"+slug
    if (source.get("id")!=source_id or source.get("catalog")!="zh-hans"
            or source.get("language")!="zh-Hans"
            or source.get("code","").upper()!=code.upper()):
        raise ValueError("wrong_source_set")
    expected_label=("PINNED_LIVE_ZHCN_PIKAQIAN_SET_PRINTINGS_20261123"
                    if slug in NEXT_SIX else "PINNED_LIVE_ZHCN_PIKAQIAN_SET_PRINTINGS_20261122")
    if (live.get("classification")!=expected_label
            or live.get("source_commit")!=COMMIT
            or live.get("set_id")!=expected_vault
            or live.get("source_set_id")!=source_id
            or live.get("source_set_code","").upper()!=code.upper()
            or not isinstance(live.get("rows"),list)
            or len(live["rows"])!=expected_live
            or not SNAPSHOT.fullmatch(live.get("snapshot_sha256",""))):
        raise ValueError("live_identity_snapshot_or_set_drift")
    template=provider_url_recipe(index)
    by_print,source_variants,no_picture=source_index(source["cards"],template)
    if source_variants < expected_live:raise ValueError("insufficient_source_variants")
    seen_ids=set();seen_variants=set();matched=[];counts=Counter()
    for row in live["rows"]:
        if (set(row)!={"canonical_printing_id","language","set_id","native_card_name","printed_collector_number"}
                or row["set_id"]!=expected_vault or row["language"]!="zh-cn"
                or not isinstance(row["canonical_printing_id"],str)
                or not row["canonical_printing_id"].startswith("pokemon-tcg-kb-pikaqian:printing:cn-"+slug+"-")
                or row["canonical_printing_id"] in seen_ids or
                not COLLECTOR.fullmatch(str(row["printed_collector_number"]))):
            raise ValueError("invalid_live_printing_identity")
        cid=row["canonical_printing_id"]; seen_ids.add(cid)
        matches=by_print.get((row["printed_collector_number"],norm(row["native_card_name"])),[])
        selected=matches[0] if len(matches)==1 else None
        source_variant=(selected["source_card_id"],selected["source_variant_id"]) if selected else None
        if source_variant and source_variant in seen_variants:
            selected=None
        if selected:seen_variants.add(source_variant)
        status=("EXACT_ONE_TO_ONE_NATIVE_PRINTING_VARIANT" if selected else
                "NO_EXACT_SOURCE_VARIANT" if not matches else
                "AMBIGUOUS_SOURCE_PRINTING_VARIANT")
        counts[status]+=1
        matched.append({
            **row,"classification":status,"source":selected,
            "source_set_id":source_id,"source_repository":"cardcorpus/pokemon-card-dataset",
            "source_commit":COMMIT,"live_snapshot_sha256":live["snapshot_sha256"],
            "image_bytes_downloaded":0,"artwork_rights_verified":False,
            "commercial_artwork_licence":None,"safe_for_vault_apply":False,
            "public_artwork_enabled":False,"live_preflight_verified":False,
        })
    return matched,dict(sorted(counts.items())),source_variants,no_picture


def run(live_path, source_path,index_path, out,slug):
    if out.exists() or out.is_symlink():raise FileExistsError("output_must_be_new")
    live=json.loads(live_path.read_text(encoding="utf8"))
    source_data=source_path.read_bytes();source=json.loads(source_data)
    index_data=index_path.read_bytes();index=json.loads(index_data)
    results,counts,total,missing=reconcile(live,source,index,slug)
    raw="".join(json.dumps(row,sort_keys=True,ensure_ascii=False,separators=(",",":"))+"\n"
               for row in sorted(results,key=lambda x:x["canonical_printing_id"])).encode()
    receipt={
        "classification":"PRIVATE_ZHCN_LIVE_VARIANT_SOURCE_PICTURE_CROSSWALK_V1",
        "source_commit":COMMIT,"source_set":SPECS[slug][1],"source_code":SPECS[slug][0],
        "source_sha256":hashlib.sha256(source_data).hexdigest(),
        "provider_index_sha256":hashlib.sha256(index_data).hexdigest(),
        "provider_picture_url_recipe":provider_url_recipe(index),
        "live_snapshot_sha256":live["snapshot_sha256"],
        "live_missing_printings_checked":len(results),
        "source_cards":len(source["cards"]),"source_variants":total,
        "source_variants_without_picture":missing,"match_counts":counts,
        "source_name_match_methods":dict(sorted(Counter(
            r["source"]["source_name_match_method"] for r in results if r["source"]
        ).items())),
        "candidate_sha256":hashlib.sha256(raw).hexdigest(),
        "new_live_links":0,"images_downloaded":0,"public_artwork_enabled":False,
        "rights_verified":False,"live_import_allowed":False,
        "source_reference_only":True,
        "note":"Artwork rights and independent guarded import remain separate. This is a private source reference, not a licence."
    }
    out.mkdir(parents=True)
    (out/"candidates.jsonl").write_bytes(raw)
    (out/"receipt.json").write_text(json.dumps(receipt,sort_keys=True,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
    return receipt


def self_test():
    assert provider_url_recipe({"pictures":{"sized":"https://img.cardcorpus.com/s1/{width}/{id}.avif","widths":[240,480,960]}})
    try:provider_url_recipe({"pictures":{"sized":"https://img.cardcorpus.com/m/{id}","widths":[480]}})
    except ValueError:pass
    else:raise AssertionError("bad_image_url_contract_accepted")
    c={"id":"001","name":"Exeggcute","number":{"short":"001"},
       "variants":[{"id":"non-holo","finish":"non_holo","picture":{"front":{"image":"a"*32}}},
                   {"id":"master-ball","finish":"master_ball","picture":{"front":{"image":"b"*32}}},
                   {"id":"poke-ball","finish":"poke_ball","picture":{"front":{"image":"c"*32}}}]}
    index,n,bad=source_index([c],"https://img.cardcorpus.com/s1/{width}/{id}.avif")
    assert n==3 and bad==0
    assert index[("001","Exeggcute Master Ball Holo")][0]["source_picture_url_reference"]=="https://img.cardcorpus.com/s1/480/"+"b"*32+".avif"
    assert ("001","Exeggcute Holo") not in index
    gx={"id":"003","name":"Charizard-GX","number":{"short":"003"},
        "variants":[{"id":"holo","finish":"holo","picture":{"front":{"image":"d"*32}}}]}
    gx_index,gx_count,gx_missing=source_index([gx],"https://img.cardcorpus.com/s1/{width}/{id}.avif")
    assert gx_count==1 and gx_missing==0
    assert gx_index[("003","Charizard GX")][0]["source_name_match_method"]=="terminal_gx_hyphen_only"
    assert ("003","Charizard Ex") not in gx_index
    tag={"id":"004","name":"Celebi & Venusaur-GX","number":{"short":"004"},
         "variants":[{"id":"holo","picture":{"front":{"image":"e"*32}}}]}
    tag_index,_,_=source_index([tag],"https://img.cardcorpus.com/s1/{width}/{id}.avif")
    assert ("004","Celebi & Venusaur GX") in tag_index
    assert ("004","Celebi GX") not in tag_index
    rev={"id":"002","name":"Sandshrew","number":{"short":"002"},
         "variants":[{"id":"non-holo","finish":"non_holo","picture":{"front":{"image":"a"*32}}},
                     {"id":"reverse-holo","finish":"reverse_holo","picture":{"front":{"image":"b"*32}}}]}
    r,n,bad=source_index([rev],"https://img.cardcorpus.com/s1/{width}/{id}.avif")
    assert n==2 and bad==0 and ("002","Sandshrew Reverse Holo") in r
    assert ("002","Sandshrew Holo") not in r
    print("PASS source-declared URL template, exact set/native labels, no guessed aliases")


if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--slug",choices=sorted(SPECS))
    ap.add_argument("--live",type=Path)
    ap.add_argument("--source",type=Path)
    ap.add_argument("--provider-index",type=Path)
    ap.add_argument("--out",type=Path)
    ap.add_argument("--self-test",action="store_true")
    args=ap.parse_args()
    if args.self_test:self_test()
    else:
        if not all((args.slug,args.live,args.source,args.provider_index,args.out)):
            ap.error("missing arguments")
        print("ZH_CN_GENERAL_VARIANT_RESULT",json.dumps(run(args.live,args.source,args.provider_index,args.out,args.slug),sort_keys=True))
