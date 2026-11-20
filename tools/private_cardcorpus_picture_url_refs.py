#!/usr/bin/env python3
"""Extract pinned CardCorpus picture *URL references* from verified rich metadata.

The input is a previously hashed, immutable research artifact. The metadata
licence does not grant copyright permission for the referenced images. This
collector never requests/downloading images, modifies CardScanR canonical
identities, publishes artworks or touches the owner's live PC.
"""
from __future__ import annotations
import argparse,gzip,hashlib,json,re
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path

COMMIT="c4f0b415d1dd3d4fbaadb8c32a0a591126746ed6"
INDEX_GIT_BLOB="bb13ca435a0a64870b87789c7a27b31a5d9e21ac"
PIC=re.compile(r"^[0-9a-f]{32}$")
ORIGINAL="https://img.cardcorpus.com/m/{id}"
SIZED="https://img.cardcorpus.com/s1/{width}/{id}.avif"
EXPECTED=67243

def git_blob(data:bytes)->str:
    return hashlib.sha1(f"blob {len(data)}\0".encode()+data).hexdigest()

def picture(value):
    return value if isinstance(value,str) and PIC.fullmatch(value) else None

def variant_picture(v):
    if not isinstance(v,dict):return None
    p=v.get("picture")
    f=p.get("front") if isinstance(p,dict) else None
    return picture(f.get("image")) if isinstance(f,dict) else None

def normalized_number(v):
    if not isinstance(v,dict):return None
    p=v.get("printed")
    return p if isinstance(p,str) and 0<len(p)<100 else None

def source_rows(data:bytes):
    seen=set()
    for line in data.splitlines():
        row=json.loads(line)
        sid,cid=row.get("source_set_id"),row.get("source_card_id")
        if not isinstance(sid,str) or not isinstance(cid,str) or (sid,cid) in seen:
            raise ValueError("duplicate_or_invalid_source_card_id")
        seen.add((sid,cid))
        fields=row.get("fields")
        if not isinstance(fields,dict):
            raise ValueError("missing_rich_card_details")
        variants=fields.get("variants") or []
        if not isinstance(variants,list):
            raise ValueError("source_variant_list_invalid")
        images={}
        base=picture(row.get("picture_id"))
        if base: images[base]={"is_default_front":True,"variant_ids":[]}
        for v in variants:
            vpic=variant_picture(v)
            if not vpic:continue
            vid=v.get("id")
            if not isinstance(vid,str) or len(vid)>120:continue
            record=images.setdefault(vpic,{"is_default_front":False,"variant_ids":[]})
            if vid not in record["variant_ids"]:record["variant_ids"].append(vid)
        yield row,images

def archive(input_dir:Path,index_path:Path,output:Path):
    if output.exists() or output.is_symlink():
        raise FileExistsError("output_requires_new_directory")
    receipt=json.loads((input_dir/"receipt.json").read_text("utf8"))
    if (receipt.get("source_commit")!=COMMIT or
            receipt.get("source_card_records")!=EXPECTED or
            receipt.get("validated_rich_card_details")!=EXPECTED or
            receipt.get("classification")!="PRIVATE_SOURCE_ONLY_NO_CANONICAL_IMPORT"):
        raise ValueError("untrusted_rich_source_receipt")
    idx_bytes=index_path.read_bytes()
    if git_blob(idx_bytes)!=INDEX_GIT_BLOB:
        raise ValueError("pinned_index_git_blob_mismatch")
    idx=json.loads(idx_bytes)
    img=idx.get("pictures")
    if (not isinstance(img,dict) or img.get("original")!=ORIGINAL or
            img.get("sized")!=SIZED or 480 not in img.get("widths",[])):
        raise ValueError("image_reference_template_drift")
    gzfile=input_dir/"rich_card_details.jsonl.gz"
    rawgz=gzfile.read_bytes()
    meta=receipt["file_hashes"]["rich_card_details.jsonl.gz"]
    if hashlib.sha256(rawgz).hexdigest()!=meta["compressed_sha256"]:
        raise ValueError("source_gzip_digest_mismatch")
    rows=gzip.decompress(rawgz)
    if hashlib.sha256(rows).hexdigest()!=meta["uncompressed_sha256"]:
        raise ValueError("source_jsonl_digest_mismatch")
    entries=[]
    langs=Counter()
    seen=Counter()
    count=0
    image_count=0
    by_language_base=Counter()
    for card,images in source_rows(rows):
        count+=1
        lang=card.get("language")
        if not isinstance(lang,str) or not 1<=len(lang)<=15:
            raise ValueError("invalid_source_language")
        seen[lang]+=1
        for pid,variant in images.items():
            if not PIC.fullmatch(pid):
                raise ValueError("invalid_picture_id")
            if variant["is_default_front"]:
                by_language_base[lang]+=1
            langs[lang]+=1
            entries.append({
                "provider":"cardcorpus","source_commit":COMMIT,
                "source_set_id":card["source_set_id"],
                "source_card_id":card["source_card_id"],
                "language":lang,
                "native_name":card["fields"].get("name"),
                "printed_card_number":normalized_number(card["fields"].get("number")),
                "source_picture_id":pid,
                "original_image_url_reference":ORIGINAL.replace("{id}",pid),
                "sized_480_avif_url_reference":SIZED.replace("{width}","480").replace("{id}",pid),
                "default_card_picture":variant["is_default_front"],
                "linked_variant_ids":variant["variant_ids"],
                "exact_cardscanr_canonical_identity":None,
                "artwork_rights_verified":False,
                "image_downloaded":False,
                "public_artwork_allowed":False,
            })
            image_count+=1
    if count!=EXPECTED or image_count<receipt["source_cards_with_picture_ids"]:
        raise ValueError("source_card_or_picture_count_drift")
    output.mkdir(parents=True)
    outpath=output/"cardcorpus_picture_url_references.jsonl.gz"
    with outpath.open("xb") as handle:
        with gzip.GzipFile(fileobj=handle,mode="wb",mtime=0) as archive:
            for row in entries:
                archive.write((json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n").encode("utf8"))
    report={
        "classification":"PRIVATE_SOURCE_PICTURE_URL_REFERENCES_ONLY",
        "source_repo":"cardcorpus/pokemon-card-dataset",
        "source_commit":COMMIT,"index_git_blob_sha1":INDEX_GIT_BLOB,
        "source_rich_details_sha256":meta["uncompressed_sha256"],
        "card_records_examined":count,
        "card_picture_references":image_count,
        "default_card_picture_references":sum(by_language_base.values()),
        "additional_variant_picture_references":image_count-sum(by_language_base.values()),
        "source_picture_refs_by_language":dict(sorted(langs.items())),
        "default_picture_refs_by_language":dict(sorted(by_language_base.items())),
        "source_card_records_by_language":dict(sorted(seen.items())),
        "compressed_file_sha256":hashlib.sha256(outpath.read_bytes()).hexdigest(),
        "compressed_file_size":outpath.stat().st_size,
        "generated_utc":datetime.now(timezone.utc).isoformat(),
        "no_image_binaries_downloaded":True,
        "images_downloaded":0,"canonical_cards_inserted":0,
        "vault_writes":0,"production_writes":0,
        "artwork_rights_cleared":False,
        "safe_to_publish":False,
        "exact_cardscanr_printing_match_verified":False,
        "note":"Private research only; CC BY metadata does not grant image/mirror display or redistribution rights",
    }
    (output/"receipt.json").write_text(json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf8")
    return report

def self_test():
    assert picture("a"*32)=="a"*32
    assert picture("../../etc") is None
    assert variant_picture({"picture":{"front":{"image":"b"*32}}})=="b"*32
    assert variant_picture({}) is None
    assert git_blob(b"hello")=="b6fc4c620b67d95f953a5c1c1230aaab5db5a1b0"
    d={"source_set_id":"zh-hans/sample","source_card_id":"001","language":"zh-Hans",
       "picture_id":"a"*32,"fields":{"name":"Example","number":{"printed":"001/100"},
       "variants":[{"id":"a","picture":{"front":{"image":"b"*32}}}]}}
    one=list(source_rows((json.dumps(d)+"\n").encode()))
    assert len(one)==1 and len(one[0][1])==2
    assert one[0][1]["a"*32]["is_default_front"]
    print("PASS pinned-image-reference-only guards")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--input-dir",type=Path)
    ap.add_argument("--index",type=Path)
    ap.add_argument("--output",type=Path)
    ap.add_argument("--self-test",action="store_true")
    args=ap.parse_args()
    if args.self_test:self_test();return
    if not args.input_dir or not args.index or not args.output:
        ap.error("input-dir, index and output required")
    d=archive(args.input_dir,args.index,args.output)
    print("PRIVATE_PICTURE_URL_REFS_PASS",json.dumps({
        "source_cards":d["card_records_examined"],
        "picture_links":d["card_picture_references"],
        "default_links":d["default_card_picture_references"],
        "variant_links":d["additional_variant_picture_references"],
        "by_language":d["source_picture_refs_by_language"],
        "vault_writes":0},ensure_ascii=False,sort_keys=True))
if __name__=="__main__":
    main()
