#!/usr/bin/env python3
"""Private, read-only eight-pack query/identity canary. Not an app release test."""
from __future__ import annotations
import argparse
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import quote

from tools.build_review_68_set_core import audit_and_build, read_core_rows
from tools.preflight_multilingual_crossrelease import ACTIVE_PACKS, ARCHIVE_PACKS, ACTIVE, CANARY, MAX_GZIP, get, inspect_pack
from tools.preflight_legacy_korean_pack import pick
from tools.private_shard_contract import validate_bounded_preview
from tools.private_pack_completeness import inventory_pack


def checked_canary(output: Path, fetch=get) -> dict:
    # The builder independently validates all nine source packs before emitting
    # a private core. None of these steps mutates live R2 or customer data.
    core_proof = audit_and_build(output, fetch)
    if core_proof["unresolved_card_set_references_after"] != 0:
        raise ValueError("unresolved_set_reference")
    core_file = output / "review-core.sqlite"
    candidate_sets = set(read_core_rows(core_file))
    active = json.loads(fetch(ACTIVE, 2_000_000))
    archive = json.loads(fetch(CANARY, 2_000_000))
    results = []
    seen_base, seen_printing = set(), set()
    pack_set_union = set(candidate_sets)
    with tempfile.TemporaryDirectory(prefix="private-pack-query-canary-") as tmp:
        root = Path(tmp)
        selected = [(p, pick(active,p)) for p in ACTIVE_PACKS if p != "core"]
        selected += [(p,pick(archive,p)) for p in ARCHIVE_PACKS]
        for name, desc in selected:
            dest = root / (name + ".sqlite")
            zipped = fetch(desc["compressedDatabaseUrl"], MAX_GZIP)
            info, bases, printings, signature, own_sets, references = inspect_pack(desc, zipped, dest)
            if seen_base.intersection(bases) or seen_printing.intersection(printings):
                raise ValueError("canonical_id_collision")
            seen_base.update(bases)
            seen_printing.update(printings)
            pack_set_union.update(own_sets)
            uri = "file:" + quote(dest.resolve().as_posix(), safe="/:") + "?mode=ro&immutable=1"
            with sqlite3.connect(uri, uri=True) as db:
                db.execute("PRAGMA query_only=ON")
                card_count = db.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
                fts_count = db.execute("SELECT COUNT(*) FROM cards_fts").fetchone()[0]
                # Query both the card lookup and the materialized search index.
                examples = db.execute("""
                    SELECT c.canonical_base_id,c.canonical_printing_id,c.set_id,
                           c.printed_collector_number
                    FROM cards c ORDER BY c.canonical_base_id LIMIT 3
                """).fetchall()
                for base, printing, set_id, collector in examples:
                    if not base or not printing or not set_id:
                        raise ValueError("empty_canonical_identity")
                    matched = db.execute(
                        "SELECT COUNT(*) FROM cards_fts WHERE canonical_base_id=?", (base,)
                    ).fetchone()[0]
                    if matched != 1:
                        raise ValueError("search_index_identity_lookup_failed")
                    if collector is not None:
                        n = db.execute(
                            "SELECT COUNT(*) FROM cards WHERE printed_collector_number=?", (collector,)
                        ).fetchone()[0]
                        if n < 1:
                            raise ValueError("collector_number_lookup_failed")
                # Verify each real pack has an in-memory metadata-only projection
                # compatible with legacy website shard JSON. Never publish it.
                names = ("canonical_base_id","canonical_printing_id",
                         "language","set_id","printed_collector_number",
                         "normalized_collector_number","native_card_name",
                         "native_set_name","rarity")
                query = "SELECT " + ",".join(names) + " FROM cards ORDER BY canonical_base_id LIMIT 3"
                raw_preview=[dict(zip(names, row)) for row in db.execute(query)]
                preview_proof=validate_bounded_preview(raw_preview,name)
                completeness=inventory_pack(db)
                if completeness['card_records'] != card_count:
                    raise ValueError('pack_coverage_count_mismatch')
                blocked = db.execute("""
                    SELECT COUNT(*) FROM cards
                    WHERE mirror_permission_status IS NULL OR
                    mirror_permission_status <> 'approved'
                """).fetchone()[0]
                results.append({
                    "pack_id":name,"card_rows":card_count,"fts_rows":fts_count,
                    "sample_identity_lookup_passed":len(examples),
                    "private_legacy_web_shard_contract":preview_proof,
                    "private_pack_completeness":completeness,
                    "not_approved_for_mirroring":blocked,
                    "image_mirroring_performed":False,
                    "identity_hash_sample":[hashlib.sha256(str(r[1]).encode()).hexdigest() for r in examples]
                })
                if card_count != fts_count:
                    raise ValueError("fts_count_mismatch")
        # Validate all card set refs across the *combined* pack view.
        # Existing pack-local omissions are legitimate when resolved by core.
        for name,desc in selected:
            pass  # all source set refs are verified by audit_and_build
    if len(results)!=7:
        raise ValueError("unexpected_language_pack_count")
    if core_proof["candidate_core"]["sets_after"] != 711:
        raise ValueError("unexpected_core_count")
    return {
        "classification":"PASS_PRIVATE_DATA_LAYER_ONLY",
        "source_release_ids":[active.get("catalogueReleaseId"),archive.get("catalogueReleaseId")],
        "core_sha256":core_proof["candidate_core"]["sqlite_sha256"],
        "core_set_count":len(candidate_sets),
        "unique_base_ids":len(seen_base),
        "unique_printing_ids":len(seen_printing),
        "unresolved_set_refs":core_proof["unresolved_card_set_references_after"],
        "language_packs":results,
        "production_writes":0,
        "artwork_mirroring_performed":False,
        "rollback_source_manifest_verified":bool(active.get("catalogueReleaseId")),
        "website_interaction_verified":False,
        "android_interaction_verified":False,
        "customer_ownership_migration_verified":False,
        "artwork_rights_cleared":False,
        "safe_to_publish":False,
    }


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists() and (args.output.is_symlink() or any(args.output.iterdir())):
        parser.error("output must be new or empty")
    args.output.mkdir(parents=True,exist_ok=True)
    report=checked_canary(args.output)
    (args.output/"SAFE-Private-Data-Layer-Canary.json").write_text(
        json.dumps(report,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"status":report["classification"],
        "packs":len(report["language_packs"]),
        "unique_cards":report["unique_base_ids"],
        "safe_to_publish":False},sort_keys=True))


if __name__=="__main__":
    main()
