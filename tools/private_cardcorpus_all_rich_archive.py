#!/usr/bin/env python3
"""Private CardCorpus full-source card, picture-ID and per-set detail archive.

No published images, image-file downloads, customer reads, production writes or
assertion that provider IDs are canonical CardScanR identities. CardCorpus data
is CC BY 4.0; artwork and card text remain separately protected.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime,timezone
import gzip,hashlib,json,re
from pathlib import Path

COMMIT="c4f0b415d1dd3d4fbaadb8c32a0a591126746ed6"
BLOB_SHA1="923ec1121aa95a91b775c5f1828810ee37bee43f"
CARDS_SHA256="7d4056541e6f7d2165799304cc524c35f7aed5d40c87e3b5bac8f9aa01ceba65"
EXPECTED_CARDS=67243
EXPECTED_SETS=916
SET_PATH=re.compile(r"^(?:international|japanese|zh-hans)/[a-z0-9][a-z0-9_.-]{0,140}$")
PICTURE=re.compile(r"^[0-9a-f]{32}$")
LANGS={"ja","en","zh-Hans","fr","de","es","it","ko","pt","pl","nl","zh-Hant"}
FIELDS=("name","number","language","cardType","rarity","hp","stage",
        "types","type","evolvesFrom","credit","artists","illustrator",
        "weaknesses","resistances","retreatCost","regulationMark",
        "energy","attacks","abilities","rules","species","subjects",
        "released","picture","variants","copies","level","tags",
        "distributions","trainer","supertype","subtypes","cardText",
        "flavorText","effect","pokedex","legalities")

def blob_hash(data:bytes)->str:
    return hashlib.sha1(f"blob {len(data)}\0".encode()+data).hexdigest()

def read_doc(path:Path,max_bytes=12000000):
    if path.is_symlink() or not path.is_file() or path.stat().st_size>max_bytes:
        raise ValueError("unsafe_missing_or_large_source_document")
    return json.loads(path.read_bytes())

def picture_id(card):
    obj=card.get("picture")
    front=obj.get("front") if isinstance(obj,dict) else None
    candidate=front.get("image") if isinstance(front,dict) else None
    return candidate if isinstance(candidate,str) and PICTURE.fullmatch(candidate) else None

def slim_row(record):
    if not isinstance(record,dict) or record.get("$kind")!="card":
        raise ValueError("invalid_provider_card_pointer")
    lang,sid,cid,name=(record.get(x) for x in ("language","set","card","name"))
    if (lang not in LANGS or not isinstance(sid,str) or not SET_PATH.fullmatch(sid)
            or not isinstance(cid,str) or not 1<=len(cid)<=220
            or not isinstance(name,str) or not 1<=len(name)<=360):
        raise ValueError("unsafe_provider_card_identity")
    return {"provider":"cardcorpus","source_set_id":sid,"source_card_id":cid,
            "language":lang,"name":name,"number":record.get("number"),
            "picture_id":picture_id(record),"canonical_printing_id":None,
            "exact_cardscanr_match":False,"image_downloaded":False,
            "artwork_rights_verified":False}

def dump_gzip(path,rows):
    sha=hashlib.sha256()
    count=0
    with path.open("xb") as b:
        with gzip.GzipFile(fileobj=b,mode="wb",mtime=0) as f:
            for row in rows:
                data=(json.dumps(row,ensure_ascii=False,separators=(",",":"),sort_keys=True)+"\n").encode()
                sha.update(data)
                f.write(data)
                count+=1
    return {"records":count,"uncompressed_sha256":sha.hexdigest(),
            "compressed_sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
            "compressed_bytes":path.stat().st_size}

def harvest(upstream:Path,output:Path):
    if output.exists() or output.is_symlink():
        raise FileExistsError("output_requires_nonexistent_path")
    root=(upstream/"v1").resolve(strict=True)
    cards_raw=(root/"cards.json").read_bytes()
    if hashlib.sha256(cards_raw).hexdigest()!=CARDS_SHA256 or blob_hash(cards_raw)!=BLOB_SHA1:
        raise ValueError("upstream_immutable_card_index_drift")
    cdoc=json.loads(cards_raw)
    if (cdoc.get("cardCount")!=EXPECTED_CARDS or
        not isinstance(cdoc.get("cards"),list) or len(cdoc["cards"])!=EXPECTED_CARDS):
        raise ValueError("published_card_count_drift")
    slim=[slim_row(row) for row in cdoc["cards"]]
    index={}
    for row in slim:
        key=(row["source_set_id"],row["source_card_id"])
        if key in index: raise ValueError("duplicate_provider_printing")
        index[key]=row
    sdoc=read_doc(root/"sets.json",4000000)
    sets=sdoc.get("sets")
    if (sdoc.get("setCount")!=EXPECTED_SETS or not isinstance(sets,list)
            or len(sets)!=EXPECTED_SETS):
        raise ValueError("published_set_count_drift")
    ids=set()
    for setrow in sets:
        sid=setrow.get("id") if isinstance(setrow,dict) else None
        if not isinstance(sid,str) or not SET_PATH.fullmatch(sid) or sid in ids:
            raise ValueError("invalid_or_duplicate_source_set_identity")
        ids.add(sid)
    if any(x["source_set_id"] not in ids for x in slim):
        raise ValueError("provider_card_without_source_set")

    output.mkdir(parents=True)
    rich=[]
    set_briefs=[]
    mismatches=[]
    seen_details=set()
    languages=Counter()
    detail_picture_ids=0
    variants=0
    for setrow in sets:
        sid=setrow["id"]
        path=root/"sets"/(sid+".json")
        try:
            detail=read_doc(path)
            cards=detail.get("cards")
            if detail.get("id")!=sid or not isinstance(cards,list):
                raise ValueError("set_source_identity_or_cards_mismatch")
            if detail.get("cardCount")!=len(cards):
                raise ValueError("set_card_count_mismatch")
            for card in cards:
                if not isinstance(card,dict) or not isinstance(card.get("id"),str):
                    raise ValueError("invalid_source_card_details")
                key=(sid,card["id"])
                if key in seen_details: raise ValueError("duplicate_source_details")
                seen_details.add(key)
                canonical=index.get(key)
                if canonical is None or canonical["language"]!=card.get("language") or canonical["name"]!=card.get("name"):
                    mismatches.append({"source_set_id":sid,"card_id_sha256":hashlib.sha256(card["id"].encode()).hexdigest(),
                                       "reason":"card_detail_index_drift"})
                    continue
                detail_picture_ids+=picture_id(card) is not None
                if isinstance(card.get("variants"),list):
                    variants+=len(card["variants"])
                languages[canonical["language"]]+=1
                rich.append({"source_provider":"cardcorpus","source_set_id":sid,
                             "source_card_id":card["id"],"language":canonical["language"],
                             "fields":{k:card[k] for k in FIELDS if k in card},
                             "picture_id":picture_id(card),
                             "canonical_printing_id":None,
                             "exact_cardscanr_match":False,
                             "artwork_rights_verified":False,
                             "image_downloaded":False})
            set_briefs.append({"source_set_id":sid,"name":detail.get("name"),
                               "language":detail.get("language"),
                               "released":detail.get("released"),
                               "card_count":len(cards),
                               "source_git_blob_sha1":blob_hash(path.read_bytes()),
                               "details_verified":True})
        except (ValueError,OSError,TypeError,json.JSONDecodeError) as exc:
            mismatches.append({"source_set_id":sid,"reason":type(exc).__name__})
            set_briefs.append({"source_set_id":sid,"name":setrow.get("name"),
                               "language":setrow.get("language"),
                               "details_verified":False})
    files={
        "all_provider_cards.jsonl.gz":dump_gzip(output/"all_provider_cards.jsonl.gz",slim),
        "rich_card_details.jsonl.gz":dump_gzip(output/"rich_card_details.jsonl.gz",rich),
        "provider_sets.jsonl.gz":dump_gzip(output/"provider_sets.jsonl.gz",set_briefs),
        "detail_quarantine.jsonl.gz":dump_gzip(output/"detail_quarantine.jsonl.gz",mismatches),
    }
    proof={
        "classification":"PRIVATE_SOURCE_ONLY_NO_CANONICAL_IMPORT",
        "source":"cardcorpus/pokemon-card-dataset",
        "source_commit":COMMIT,"source_git_blob_sha1":BLOB_SHA1,
        "source_cards_sha256":CARDS_SHA256,
        "generated_utc":datetime.now(timezone.utc).isoformat(),
        "source_card_records":len(slim),
        "source_set_records":len(sets),
        "source_cards_with_picture_ids":sum(bool(x["picture_id"]) for x in slim),
        "validated_rich_card_details":len(rich),
        "rich_details_by_language":dict(sorted(languages.items())),
        "rich_details_with_picture_ids":detail_picture_ids,
        "source_variant_rows":variants,
        "verified_source_set_files":sum(x["details_verified"] for x in set_briefs),
        "quarantined_source_detail_rows":len(mismatches),
        "file_hashes":files,
        "attribution":"CardCorpus (CC BY 4.0) https://github.com/cardcorpus/pokemon-card-dataset",
        "artwork_rights":"Card images and text © respective rights holders; CC BY 4.0 metadata licence does not grant image rights",
        "image_binaries_downloaded":0,"new_canonical_card_records":0,
        "production_writes":0,"vault_writes":0,
        "public_artwork_publication_allowed":False,
        "all_provider_ids_provisional":True,
    }
    (output/"receipt.json").write_text(json.dumps(proof,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf8")
    (output/"NOTICE.txt").write_text(
        "CardCorpus metadata © 2026 The CardCorpus Authors, CC BY 4.0. https://github.com/cardcorpus/pokemon-card-dataset\n"
        "Card artwork and original card text © the relevant rights holders. No artwork downloads/public permissions. Source-only research.\n",
        encoding="utf8")
    return proof

def self_test():
    assert blob_hash(b"hello")=="b6fc4c620b67d95f953a5c1c1230aaab5db5a1b0"
    c={"$kind":"card","language":"ja","set":"japanese/test-set",
       "card":"001","name":"Example","picture":{"front":{"image":"a"*32}}}
    assert slim_row(c)["picture_id"]=="a"*32
    assert slim_row(c)["canonical_printing_id"] is None
    for bad in ({"set":"../../etc"},{"language":"invalid"},{"card":""}):
        try: slim_row({**c,**bad})
        except ValueError: pass
        else: raise AssertionError("unsafe_provider_identity_accepted")
    print("PASS: source identity and copyright guards")

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--upstream",type=Path)
    p.add_argument("--output",type=Path)
    p.add_argument("--self-test",action="store_true")
    a=p.parse_args()
    if a.self_test: self_test();return
    if not a.upstream or not a.output: p.error("--upstream and --output required")
    doc=harvest(a.upstream,a.output)
    print(json.dumps({"result":"PRIVATE_ALL_SOURCE_METADATA_PASS",
                      "cards":doc["source_card_records"],
                      "sets":doc["source_set_records"],
                      "rich_details":doc["validated_rich_card_details"],
                      "picture_ids":doc["source_cards_with_picture_ids"],
                      "downloaded_images":0,"vault_writes":0},sort_keys=True))
if __name__=="__main__":main()
