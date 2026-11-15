"""Unattended *private archive* catalogue cycles and app-import candidate staging.

This is deliberately not a production publisher. Local source discovery feeds bounded
resumable downloads; only independently matched + rights-cleared art may be emitted
as an app-import candidate. The installed Windows worker is a separate deployment.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PRIORITIES = {"KR": 0, "TW": 1, "HK": 2, "CN": 3, "JP": 4,
              "US": 5, "TH": 6, "ID": 7}


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fd:
        for part in iter(lambda: fd.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def world_gaps(database: Path | None) -> dict[tuple[str, str], dict[str, int]]:
    """Count actual printings and validated image candidates, not URL guesses.

    A source archive without a staging DB still operates; its coverage is unknown.
    """
    if database is None or not database.exists():
        return {}
    conn = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    try:
        rows = conn.execute("""
        SELECT sr.region_code, sr.language_code,
            COUNT(DISTINCT cp.id) AS known_printings,
            COUNT(DISTINCT CASE WHEN ci.id IS NOT NULL THEN cp.id END) AS with_image_candidates,
            COUNT(DISTINCT CASE WHEN iv.status='pass' THEN cp.id END) AS with_verified_image_candidates
        FROM card_printing cp
        JOIN set_release sr ON sr.id=cp.set_release_id
        LEFT JOIN card_variant cv ON cv.card_printing_id=cp.id
        LEFT JOIN card_image_candidate ci ON ci.card_variant_id=cv.id
        LEFT JOIN image_validation_result iv ON iv.card_image_candidate_id=ci.id
        GROUP BY sr.region_code,sr.language_code
        """)
        return {(r[0], r[1]): {"known_printings": r[2],
                "with_image_candidates": r[3],
                "with_verified_image_candidates": r[4],
                "without_verified_image_candidates": r[2]-r[4]} for r in rows}
    finally:
        conn.close()


def plan(feed: Path, *, database: Path | None = None) -> dict[str, Any]:
    from cardscanr_worldwide.source_feed import load_feed
    _, sources = load_feed(feed)
    gaps = world_gaps(database)
    entries = []
    for sid, src in sources.items():
        region, language = src["region"], src["language"]
        coverage = gaps.get((region, language))
        entries.append({
            "source_id": sid, "region": region, "language": language,
            "priority": PRIORITIES.get(region, 90), "enabled": src["enabled"],
            "seed_count": len(src["seeds"]), "has_autodiscovery_seed": any(
                seed.get("kind") == "index" for seed in src["seeds"]),
            "rights_status": src["rights_status"],
            "coverage": coverage, "status": "eligible" if src["enabled"] and src["seeds"]
                 else "disabled_needs_source_review" if not src["enabled"] else "missing_index_seed"
        })
    entries.sort(key=lambda a: (a["priority"],-(a["coverage"] or {}).get("without_verified_image_candidates", 0),a["source_id"]))
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "automated_source_discovery": "preconfigured_official_index_seeds_and_recursive_card_links",
            "coverage_is_historical_staging_not_live": True,
            "sources": entries}


def run_cycle(feed: Path, archive: Path, *, database: Path | None = None,
              per_source_limit: int = 25, total_limit: int = 50,
              run_batch_fn: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """Advance existing approved sources automatically, without user-provided URLs."""
    if not 1 <= per_source_limit <= 100 or not 1 <= total_limit <= 100:
        raise ValueError("Bounded invocation requires 1..100 items")
    p = plan(feed, database=database)
    if run_batch_fn is None:
        from cardscanr_worldwide.source_feed import run_batch
        run = run_batch
    else:
        run = run_batch_fn
    results: list[dict[str, Any]] = []
    budget = total_limit
    for src in p["sources"]:
        if budget <= 0:
            break
        if src["status"] != "eligible":
            continue
        amount = min(per_source_limit, budget)
        try:
            result = run(feed, src["source_id"], archive, max_items=amount, pace=2.0)
            results.append({"source": src["source_id"], "status": "ran", "receipt": result})
            # Budget represents reserved attempt capacity, not verified downloads.
        except (PermissionError, RuntimeError, OSError, ValueError) as exc:
            results.append({"source": src["source_id"], "status": "blocked", "error": str(exc)[:500]})
        budget -= amount
    return {"plan": p, "batches": results,
            "attempt_budget_reserved": total_limit-budget,
            "verified_downloads": sum(int(x.get("receipt", {}).get("new_images", 0)) for x in results),
            "worker_connected": False, "local_execution_only": True,
            "nothing_run_reason": "no_enabled_sources_or_seeds" if not results else None}


def stage_app_candidates(archive: Path, output: Path, *,
                         crosswalk: Path | None = None,
                         worldwide_db: Path | None = None) -> dict[str, Any]:
    """Export a review pack and conservative app-ready image candidates.

    A match must have an explicit reviewed crosswalk plus existing staged foreign
    keys, and `approved_for_mirror` image rights. Unmatched work stays research-only.
    This does not write any production app or Supabase pack.
    """
    archive = archive.resolve()
    source_db = archive / "source-feed-checkpoint.sqlite"
    if not source_db.exists():
        raise FileNotFoundError(f"Missing acquisition checkpoint {source_db}")
    output = output.resolve()
    if output == archive or archive.is_relative_to(output) or output.is_relative_to(archive):
        raise ValueError("Output and original archive must be separate paths")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Use an empty, versioned staging output directory")
    source = sqlite3.connect(f"file:{source_db}?mode=ro",uri=True)
    source.row_factory = sqlite3.Row
    world = None
    if worldwide_db is not None and worldwide_db.exists():
        world = sqlite3.connect(f"file:{worldwide_db.resolve()}?mode=ro",uri=True)
    try:
        allrows = [dict(row) for row in source.execute(
            "SELECT source_id,kind,url,sha256,byte_size,relative_path,metadata_json,fetched_at "
            "FROM artifacts ORDER BY source_id,kind,url")]
        review = []
        images_by_parent: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        bad_hashes = []
        for row in allrows:
            rel = Path(row["relative_path"])
            file = (archive / rel).resolve()
            verified = (file.is_relative_to(archive) and file.is_file() and
                        not file.is_symlink() and _sha(file) == row["sha256"] and
                        file.stat().st_size == row["byte_size"])
            row["bytes_verified"] = verified
            try:
                row["metadata"] = json.loads(row.pop("metadata_json"))
            except json.JSONDecodeError:
                row["metadata"] = {}
                row["bytes_verified"] = False
            if not verified:
                bad_hashes.append({"source_id": row["source_id"], "url": row["url"]})
            review.append(row)
            if row["kind"] == "image":
                parent = row["metadata"].get("parent_url")
                if parent:
                    images_by_parent[(row["source_id"], parent)].append(row)
        maps = {}
        if crosswalk is not None:
            for line in crosswalk.read_text(encoding="utf-8").splitlines():
                if not line.strip(): continue
                value = json.loads(line)
                key = (value["source_id"], value["source_card_url"])
                if key in maps: raise ValueError("Duplicate crosswalk identity")
                maps[key] = value
        cards = []
        app_images = []
        for row in review:
            if row["kind"] != "card":
                continue
            md = row["metadata"]
            mapping = maps.get((row["source_id"],row["url"]))
            images = images_by_parent.get((row["source_id"],row["url"]), [])
            reasons = []
            if not mapping or mapping.get("match_status") != "verified":
                reasons.append("no_verified_exact_identity_crosswalk")
            if md.get("rights_status") != "approved_for_mirror":
                reasons.append("image_publication_rights_not_cleared")
            if not row["bytes_verified"]:
                reasons.append("source_card_bytes_unverified")
            if not images or not any(x["bytes_verified"] and x["metadata"].get("rights_status") == "approved_for_mirror" for x in images):
                reasons.append("no_sha_verified_image_bytes")
            if world is None:
                reasons.append("no_read_only_worldwide_identity_check")
            if mapping and world is not None:
                cid, vid = mapping.get("canonical_printing_id"),mapping.get("card_variant_id")
                found = world.execute("SELECT 1 FROM card_variant v JOIN card_printing p "
                    "ON p.id=v.card_printing_id WHERE p.id=? AND v.id=?",(cid,vid)).fetchone() if cid and vid else None
                if not found: reasons.append("crosswalk_ids_not_in_worldwide_staging")
            record = {
                "source_id": row["source_id"], "source_card_url": row["url"],
                "region": md.get("region"),"language": md.get("language"),
                "source_metadata": md,
                "images": [{"source_url": im["url"], "relative_path": im["relative_path"],
                            "sha256": im["sha256"],"byte_size": im["byte_size"],
                            "verified": im["bytes_verified"]} for im in images],
                "exact_identity": mapping,
                "app_image_import_eligible": not reasons,
                "blocked_reasons": reasons,
            }
            cards.append(record)
            if not reasons:
                for im in images:
                    if im["bytes_verified"] and im["metadata"].get("rights_status") == "approved_for_mirror":
                        app_images.append({
                            "cardPrintingId": mapping["canonical_printing_id"],
                            "cardVariantId": mapping["card_variant_id"],
                            "language": md.get("language"), "region":md.get("region"),
                            "sourceUrl": im["url"], "localPath":im["relative_path"],
                            "sha256":im["sha256"],"byteSize":im["byte_size"],
                            "publicationApproved": False,
                        })
        output.mkdir(parents=True,exist_ok=True)
        for name, values in (("source_records.jsonl",review),
                             ("card_candidates.jsonl",cards),
                             ("app_image_import_candidates.jsonl",app_images)):
            with (output/name).open("w",encoding="utf-8",newline="\n") as f:
                for value in values: f.write(_json(value)+"\n")
        summary={"archive":str(archive),"generated_at":datetime.now(timezone.utc).isoformat(),
                 "source_artifacts":len(review),"card_records":len(cards),
                 "image_files_checked":sum(1 for x in review if x["kind"]=="image"),
                 "files_with_missing_or_bad_hashes":bad_hashes,
                 "app_image_import_candidates":len(app_images),
                 "production_pack_published":False,
                 "app_installable_pack_built":False,
                 "next_step":"import verified exact-match metadata into canonical staging; validate and publish using existing versioned app bundle pipeline after rights gate"}
        (output/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        return summary
    finally:
        source.close()
        if world is not None:world.close()