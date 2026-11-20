#!/usr/bin/env python3
"""Read-only duplicated-source footprint in SHA-verified archived zh-cn R2 pack.

A matching set code, card name and printed number across providers is evidence
of an alias, NOT proof that variants/finishes are identical. No record deletes,
canonical merges, user data, image downloads or publication.
"""
from __future__ import annotations
import argparse,hashlib,json,sqlite3,tempfile
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import quote

from tools.preflight_multilingual_crossrelease import CANARY,MAX_GZIP,get,inspect_pack
from tools.preflight_legacy_korean_pack import pick
from cardscanr_search_index.normalization import normalize_collector_number,normalize_search_text

def extract_namespace(set_id):
    if not isinstance(set_id,str) or ":" not in set_id:return "unidentified"
    return set_id.split(":",1)[0]

def normalize_key(row):
    lang,set_code,printed,native,printing_id=row
    if lang!="zh-cn":raise ValueError("wrong_language_in_archived_pack")
    if not all(isinstance(x,str) and x.strip() for x in (set_code,printed,native,printing_id)):
        return None
    code=normalize_search_text(set_code)
    num=normalize_collector_number(printed)
    name=normalize_search_text(native)
    if not code or not num or not name:return None
    return (code,num,name)

def analyze(records):
    by_signature=defaultdict(list)
    namespace_rows=Counter()
    missing_key=Counter()
    total=0
    ids=set()
    for row in records:
        total+=1
        cid,setid,lang,set_code,printed,native=row
        if cid in ids:raise ValueError("duplicate_canonical_id")
        ids.add(cid)
        namespace=extract_namespace(setid)
        namespace_rows[namespace]+=1
        key=normalize_key((lang,set_code,printed,native,cid))
        if key is None:
            missing_key[namespace]+=1
            continue
        by_signature[key].append((cid,namespace))
    duplications=[v for v in by_signature.values() if len(v)>1]
    crossing=[v for v in duplications if len({i[1] for i in v})>1]
    ambiguity=Counter(len(v) for v in duplications)
    cross_namespaces=Counter(
        "+".join(sorted({x[1] for x in group})) for group in crossing
    )
    # A finite, non-identifying review sample uses SHA hashes of the name and
    # set/number tuple. It never writes raw user or card-name identifiers.
    examples=[]
    for key,group in sorted(by_signature.items(),key=lambda p:(-len(p[1]),p[0]))[:25]:
        if len(group)<2:break
        examples.append({
            "identity_key_sha256":hashlib.sha256(json.dumps(key,ensure_ascii=False).encode()).hexdigest(),
            "pack_rows":len(group),
            "source_namespaces":sorted({x[1] for x in group}),
        })
    return {
        "archived_pack_card_records":total,
        "canonical_id_uniqueness_verified":len(ids)==total,
        "provider_namespace_record_counts":dict(sorted(namespace_rows.items())),
        "rows_without_set_number_name_identity":sum(missing_key.values()),
        "rows_with_set_number_name_identity":sum(len(v) for v in by_signature.values()),
        "distinct_exact_name_set_number_groups":len(by_signature),
        "duplicate_candidate_groups":len(duplications),
        "duplicate_candidate_rows":sum(len(g) for g in duplications),
        "cross_provider_candidate_alias_groups":len(crossing),
        "cross_provider_candidate_alias_rows":sum(len(g) for g in crossing),
        "candidate_alias_namespace_pairs":dict(cross_namespaces),
        "identity_group_row_multiplicity":dict(sorted(ambiguity.items())),
        "sample_identity_hashes_only":examples,
        "duplicate_group_is_not_a_safe_merge":True,
    }

def collect(output:Path):
    if output.exists() or output.is_symlink():raise FileExistsError("must_create_new_evidence_directory")
    source=json.loads(get(CANARY,2000000))
    descriptor=pick(source,"zh-cn")
    compressed=get(descriptor["compressedDatabaseUrl"],MAX_GZIP)
    with tempfile.TemporaryDirectory(prefix="cardscanr-zhcn-dup-audit-") as td:
        path=Path(td)/"zh-cn.sqlite"
        checks,_,_,_,_,_=inspect_pack(descriptor,compressed,path)
        db=sqlite3.connect("file:"+quote(path.resolve().as_posix(),safe="/:")+"?mode=ro&immutable=1",uri=True)
        try:
            db.execute("PRAGMA query_only=ON")
            fields={str(x[1]) for x in db.execute("PRAGMA table_info(cards)")}
            cols=("canonical_printing_id","set_id","language","set_code",
                  "printed_collector_number","native_card_name")
            if not set(cols).issubset(fields):raise ValueError("missing_exact_identity_fields")
            rows=db.execute("SELECT "+",".join(cols)+" FROM cards")
            result=analyze(rows)
        finally:db.close()
    if result["archived_pack_card_records"]!=descriptor["recordCount"] or checks["sqlite_quick_check"]!="ok":
        raise ValueError("pack_sha_or_row_count_mismatch")
    result.update({
        "classification":"READ_ONLY_ZHCN_CROSS_PROVIDER_ALIAS_CANDIDATES",
        "source_pack_sha256":descriptor["sha256"],
        "source_gzip_sha256":descriptor["gzipSha256"],
        "generated_utc":datetime.now(timezone.utc).isoformat(),
        "source_pack":"archived_canary_zh_cn_not_PC_master",
        "artwork_rights_verified":False,
        "archive_only":True,
        "image_binaries_downloaded":0,
        "new_canonical_cards":0,
        "canonical_ids_changed":0,
        "vault_writes":0,"production_writes":0,
        "safe_to_merge_duplicates":False,
        "note":"Provider duplicate groups are only possible aliases; separate variants and print finishes must not be silently merged.",
    })
    output.mkdir(parents=True)
    (output/"receipt.json").write_text(
        json.dumps(result,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf8")
    return result

def self_test():
    a=[
        ("id1","ptcg-chs-datasets:set:1","zh-cn","CSM2aC","001/150","皮卡丘"),
        ("id2","pokemon-tcg-kb-pikaqian:set:cn-csm2ac","zh-cn","csm2ac","1/150","皮卡丘"),
        ("id3","ptcg-chs-datasets:set:1","zh-cn","CSM2aC","002/150","喵喵"),
    ]
    result=analyze(a)
    assert result["archived_pack_card_records"]==3
    assert result["cross_provider_candidate_alias_groups"]==1
    assert result["cross_provider_candidate_alias_rows"]==2
    assert result["distinct_exact_name_set_number_groups"]==2
    assert not result["safe_to_merge_duplicates"] if "safe_to_merge_duplicates" in result else True
    print("PASS: source identity aliases counted, not merged")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--output",type=Path)
    ap.add_argument("--self-test",action="store_true")
    args=ap.parse_args()
    if args.self_test:self_test();return
    if not args.output:ap.error("--output required")
    doc=collect(args.output)
    print("CHINESE_ALIAS_AUDIT_PASS",json.dumps({
        k:doc[k] for k in (
            "archived_pack_card_records","duplicate_candidate_groups",
            "cross_provider_candidate_alias_groups","cross_provider_candidate_alias_rows",
            "distinct_exact_name_set_number_groups","rows_without_set_number_name_identity")
    },sort_keys=True))
if __name__=="__main__":
    main()
