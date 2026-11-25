#!/usr/bin/env python3
"""Reproduce the exact five already-staged private CHS archives; no Vault writes."""
import argparse, hashlib, json, struct, zlib, zipfile
from pathlib import Path

IDS = ("90", "181", "314", "275", "157")
CROSSWALK_SHA = "525ed14cf3318d971a19557e4a1c70b1d2309131832f16a64e6534c90cd19c03"
def digest(b): return hashlib.sha256(b).hexdigest()
def encode(obj): return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
def stored_gzip(raw):
    out = bytearray(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff")
    for i in range(0, len(raw), 65535):
        chunk = raw[i:i+65535]
        n = len(chunk)
        out += struct.pack("<BHH", int(i+n==len(raw)), n, n^65535) + chunk
    out += struct.pack("<II", zlib.crc32(raw), len(raw))
    return bytes(out)

def build(d, receipt, cross):
    assert d["classification"] == "LIVE_MISSING_AND_PINNED_NATIVE_SINGLE_VARIANT_PROOF"
    assert d["rights_granted"] is False and d["live_imports"] == 0
    assert receipt["rights_granted"] is False and receipt["applied"] is False
    rows = d["rows"]; sid = receipt["batch_id"].split("set")[-1]
    assert len(rows)==d["verified_candidates"]==receipt["verified_candidates"]
    assert len({x["live"]["canonical_printing_id"] for x in rows})==len(rows)
    sc = d["source_code"].upper(); ref=d["source_commit"]; code="ptcg-"+sid
    tuples=[]; cards=[]; images=[]
    for x in rows:
        l=x["live"]; cid=l["canonical_printing_id"]; p=x["picture"]; variant=x["variant"]; n=l["printed_collector_number"]
        assert l["has_image_link"] is False and l["language"]=="zh-cn"
        assert l["set_id"]=="ptcg-chs-datasets:set:"+sid
        assert n.isascii() and n.isdigit() and len(n)==3 and variant in {"holo","non-holo"}
        if cross is not None:
            c=cross[cid]
            assert c["independent_native_identity_corroborated"] is True
            assert c["archived_native_chinese_name"]==l["native_card_name"]
            assert c["source_picture_id"]==p and c["source_set_id"]==d["source_set"]
            assert c["normalized_source_set_code"]==sc and int(c["printed_collector_number"].split("/")[0])==int(n)
        tuples.append([cid,l["native_card_name"],str(int(n)),p,sc,d["source_set"]])
        cards.append(dict(canonical_printing_id=cid,language="zh-cn",provider_set_id=code,status="exact_unique",provider_card_id=sc+"-"+n+"-"+variant,native_card_name=l["native_card_name"],printed_collector_number=n,source_picture_id=p,source_variant_id=variant,source_variant_verified=True,source_commit=ref,source_set_id=d["source_set"]))
        images.append(dict(canonical_printing_id=cid,language="zh-cn",provider_set_id=code,image_url="https://img.cardcorpus.com/s1/480/"+p+".avif"))
    assert digest(encode(tuples))==d["expected_crosswalk_tuple_sha256"]==d["actual_tuple_sha256"]
    payload=dict(schema_version=1,source="cardcorpus_ptcg_single_variant_reference",source_commit=ref,sets=[dict(language="zh-cn",provider_set_id=code,source_set_id=d["source_set"],source_response_sha256=d["source_content_sha256"],source_commit=ref)],cards=cards,images=images,image_binaries_downloaded=0,artwork_rights_verified=False)
    z=stored_gzip(encode(payload))
    assert digest(z)==receipt["gzip_sha256"] and len(z)==receipt["gzip_size"]
    assert json.loads(zlib.decompress(z,31))==payload
    return z,payload

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--crosswalk-zip",type=Path,required=True)
    p.add_argument("--reports",type=Path,default=Path("reports/live_source_proofs"))
    p.add_argument("--out",type=Path)
    a=p.parse_args()
    with zipfile.ZipFile(a.crosswalk_zip) as z:
        raw=z.read("private_crosswalk.jsonl")
    assert digest(raw)==CROSSWALK_SHA
    all_rows=[json.loads(x) for x in raw.splitlines()]
    cross={r["archived_canonical_printing_id"]:r for r in all_rows}
    assert len(cross)==len(all_rows)==5289
    total=0
    if a.out: a.out.mkdir(exist_ok=False)
    for sid in IDS:
        d=json.loads((a.reports/f"ptcg_next_set_{sid}_20261125.json").read_text("utf8"))
        r=json.loads((a.reports/f"ptcg_next_stage_{sid}_20261125.json").read_text("utf8"))
        z,_=build(d,r,cross)
        total+=r["verified_candidates"]
        if a.out: (a.out/(r["batch_id"]+".gz")).write_bytes(z)
        print("PASS",sid,r["verified_candidates"],digest(z))
    assert total==917
    print("PASS 917 exact private staged references; no live imports")
if __name__=="__main__": main()
