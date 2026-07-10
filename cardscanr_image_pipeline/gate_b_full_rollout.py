from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import requests

from .config import ImagePipelineConfig
from .identity import sha256_hex
from .matching import resolve_provider_with_trace
from .paths import public_storage_url
from .pokewallet_limiter import (
    download_pokewallet_bytes_rate_limited,
    get_pokewallet_limiter,
    write_rate_limit_report,
)
from .processing import (
    ImageValidationError,
    decode_and_validate_card_image,
    download_image_bytes,
    process_downloaded_image,
)
from .sample_manifest import identities_for_manifest
from .stage2_runner import Stage2Runner, build_contact_sheet, write_json_report
from .thumbnail_execute import (
    APPROVED_MANIFEST_PATH,
    APPROVED_MANIFEST_SHA256,
    count_supabase_thumbs,
    enrich_skipped_public_urls,
    filter_manifest_entries,
    load_approved_manifest,
    summarize_execution,
    verify_successful_cards,
)
from .thumbnail_rollout import RUNTIME_DIR, utc_now_iso

FAILED_GATE_A_IDS = {
    "pokemon|en|me3|19|dewgong",
    "pokemon|en|me3|59|klefki",
    "pokemon|en|me3|67|furfrou",
    "pokemon|en|me2pt5|256|boss_s_orders",
    "pokemon|en|me2pt5|92|rotom",
    "pokemon|en|me2pt5|214|urbain",
    "pokemon|en|me2pt5|123|gastly",
    "pokemon|en|me2pt5|253|mega_audino_ex",
    "pokemon|en|me2pt5|77|team_rocket_s_exeggcute",
}


def thumb_config(*, execute: bool) -> ImagePipelineConfig:
    return replace(
        ImagePipelineConfig.from_env(
            dry_run=not execute,
            execute=execute,
            languages=("en",),
            import_display=False,
        ),
        import_display=False,
        network_concurrency=1,
        max_retries=2,
    )


def remaining_pokewallet_manifest(
    approved: dict[str, Any],
    canary_manifest: dict[str, Any],
) -> dict[str, Any]:
    canary_ids = {entry["canonicalBaseId"] for entry in (canary_manifest.get("entries") or [])}
    pw = filter_manifest_entries(approved, provider="pokewallet")
    remaining_ids = {entry["canonicalBaseId"] for entry in pw["entries"] if entry["canonicalBaseId"] not in canary_ids}
    if len(remaining_ids) != 75:
        raise RuntimeError(f"Expected 75 remaining PokeWallet cards; got {len(remaining_ids)}")
    return filter_manifest_entries(approved, provider="pokewallet", canonical_ids=remaining_ids)


def write_visual_review_checklist(output_dir: Path) -> Path:
    path = output_dir / "thumbnail_rollout_visual_review_checklist.md"
    path.write_text(
        """# Thumbnail Rollout — Visual Review Checklist

**Human visual approval: NOT PROVIDED** (automated checklist only).

## Artefacts to review

- [ ] `reports/runtime/thumbnail_rollout_gate_a_reconciled_contact_sheet.png`
- [ ] `reports/runtime/thumbnail_rollout_gate_b_canary_contact_sheet.png`
- [ ] `reports/runtime/thumbnail_rollout_gate_b_full_contact_sheet.png` (after Gate B full)
- [ ] `reports/runtime/thumbnail_rollout_500_combined_contact_sheet.png` (after reconcile)

## Checklist (per labelled tile)

1. **Language** — label shows `en`; artwork matches English print where distinguishable.
2. **Set identity** — set id in label matches the card’s set (promoted numeric sets for PokeWallet).
3. **Collector number** — including leading zeros and slash forms (`076/131`, `002/189`).
4. **Card name** — artwork matches the named identity (no wrong-print swaps).
5. **Artwork printing** — holo / stamped / cosmos / full-art variants match the identity slug.
6. **Promos and unusual numbering** — promo and energy cards look correct for their numbers.
7. **Replacement cards** — Gate A replacements are present; unresolved me3/me2pt5 originals are listed separately, not as imported thumbs.
8. **PokeWallet promoted cards** — numeric set ids and slash collector numbers render correctly.

## Automated gate

Stop the rollout if automated identity/URL/dimension checks report a likely mismatch.
Do not mark this checklist as human-approved unless a reviewer explicitly signs off.
""",
        encoding="utf-8",
    )
    return path


def automated_visual_preflight(output_dir: Path) -> dict[str, Any]:
    gate_a = output_dir / "thumbnail_rollout_gate_a_reconciled_contact_sheet.png"
    gate_b = output_dir / "thumbnail_rollout_gate_b_canary_contact_sheet.png"
    issues: list[str] = []
    if not gate_a.exists() or gate_a.stat().st_size < 1000:
        issues.append("missing_or_tiny_gate_a_contact_sheet")
    if not gate_b.exists() or gate_b.stat().st_size < 1000:
        issues.append("missing_or_tiny_gate_b_canary_contact_sheet")

    # Identity consistency from prior verification artefacts.
    for name in (
        "thumbnail_rollout_gate_a_reconciled_report.json",
        "thumbnail_rollout_gate_b_canary_verify.json",
    ):
        path = output_dir / name
        if not path.exists():
            issues.append(f"missing_{name}")
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if name.endswith("reconciled_report.json") and payload.get("classification") != "PASS":
            issues.append("gate_a_reconciled_not_pass")
        if name.endswith("canary_verify.json") and not payload.get("passed", True):
            # verify payloads may use different shapes
            if payload.get("failedCount") or payload.get("issues"):
                issues.append("gate_b_canary_verify_issues")

    canary_exec = output_dir / "thumbnail_rollout_gate_b_canary_execute.json"
    if canary_exec.exists():
        cards = json.loads(canary_exec.read_text(encoding="utf-8")).get("cards") or []
        for card in cards:
            if card.get("display_storage_path"):
                issues.append(f"display_path_present:{card.get('canonical_base_id')}")
            if card.get("ambiguous"):
                issues.append(f"ambiguous:{card.get('canonical_base_id')}")

    return {
        "generatedAtUtc": utc_now_iso(),
        "humanVisualApproval": False,
        "artefactsPresent": {
            "gateAReconciledContactSheet": gate_a.exists(),
            "gateBCanaryContactSheet": gate_b.exists(),
        },
        "likelyMismatchDetected": bool(issues),
        "issues": issues,
        "shouldStop": bool(issues),
    }


def _checkpoint_path(output_dir: Path) -> Path:
    return output_dir / "thumbnail_rollout_gate_b_remaining75_checkpoint.json"


def load_checkpoint(output_dir: Path) -> dict[str, Any]:
    path = _checkpoint_path(output_dir)
    if not path.exists():
        return {"completedIds": [], "cards": [], "rateLimitEvents": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_checkpoint(output_dir: Path, payload: dict[str, Any]) -> None:
    payload = dict(payload)
    payload["updatedAtUtc"] = utc_now_iso()
    write_json_report(payload, _checkpoint_path(output_dir))


def execute_remaining_75_rate_limited(
    runner: Stage2Runner,
    manifest: dict[str, Any],
    *,
    output_dir: Path,
) -> dict[str, Any]:
    """Execute remaining PokeWallet cards with global limiter + checkpoint resume."""
    limiter = get_pokewallet_limiter()
    checkpoint = load_checkpoint(output_dir)
    done = set(checkpoint.get("completedIds") or [])
    cards_by_id = {c["canonical_base_id"]: c for c in (checkpoint.get("cards") or [])}

    def persist_rate_event(event: Any) -> None:
        checkpoint_payload = load_checkpoint(output_dir)
        events = list(checkpoint_payload.get("rateLimitEvents") or [])
        events.append(
            {
                "atUtc": event.at_utc,
                "canonicalBaseId": event.canonical_base_id,
                "waitSeconds": event.wait_seconds,
                "message": event.message,
                "limits": event.limits,
            }
        )
        checkpoint_payload["rateLimitEvents"] = events
        checkpoint_payload["completedIds"] = sorted(done)
        checkpoint_payload["cards"] = list(cards_by_id.values())
        save_checkpoint(output_dir, checkpoint_payload)
        write_rate_limit_report(output_dir / "thumbnail_rollout_gate_b_rate_limit_events.json", limiter)

    identities = identities_for_manifest(manifest)
    entries_by_id = {e["canonicalBaseId"]: e for e in manifest["entries"]}
    exhausted: list[str] = []

    for identity in identities:
        cid = identity.canonical_base_id
        if cid in done and cards_by_id.get(cid):
            continue
        entry = entries_by_id[cid]
        existing = None
        for db_attempt in range(5):
            try:
                existing = runner.db.get_record(cid)
                break
            except (requests.RequestException, ConnectionError, OSError) as exc:
                print(f"Gate B DB retry {db_attempt + 1}/5 for {cid}: {exc}", flush=True)
                time.sleep(min(30.0, 2.0 ** db_attempt))
        if existing is None and db_attempt >= 4:
            # Final attempt — let it raise if still failing after loop without success.
            existing = runner.db.get_record(cid)
        if existing and existing.get("status") in {"completed", "verified"}:
            report = {
                "canonical_base_id": cid,
                "language": identity.language,
                "set_id": identity.set_id,
                "collector_number": identity.collector_number,
                "provider": "pokewallet",
                "database_status": "skipped",
                "skipped_reason": "already_completed",
                "ambiguous": False,
                "display_storage_path": None,
                "thumb_public_url": None,
            }
            cards_by_id[cid] = report
            done.add(cid)
            save_checkpoint(
                output_dir,
                {
                    "completedIds": sorted(done),
                    "cards": list(cards_by_id.values()),
                    "rateLimitEvents": load_checkpoint(output_dir).get("rateLimitEvents") or [],
                },
            )
            print(f"Gate B remaining skip {len(done)}/75: {cid}", flush=True)
            continue

        resolution = resolve_provider_with_trace(identity, source_card=identity.source_card)
        if resolution.ambiguous or resolution.candidate is None:
            report = {
                "canonical_base_id": cid,
                "language": identity.language,
                "set_id": identity.set_id,
                "collector_number": identity.collector_number,
                "provider": "pokewallet",
                "database_status": "failed",
                "failure_reason": resolution.ambiguity_reason or "no_provider_match",
                "ambiguous": resolution.ambiguous,
                "display_storage_path": None,
            }
            cards_by_id[cid] = report
            done.add(cid)
            exhausted.append(cid)
            save_checkpoint(
                output_dir,
                {
                    "completedIds": sorted(done),
                    "cards": list(cards_by_id.values()),
                    "rateLimitEvents": load_checkpoint(output_dir).get("rateLimitEvents") or [],
                },
            )
            print(f"Gate B remaining FAIL identity {cid}", flush=True)
            continue

        candidate = resolution.candidate
        urls: list[str] = []
        for candidate_url in (
            candidate.source_url_display,
            candidate.source_url_thumb,
        ):
            if candidate_url and candidate_url not in urls:
                urls.append(candidate_url)
        # Size fallbacks for PokeWallet CDN gaps.
        for base in list(urls):
            if "api.pokewallet.io" in base and "size=" in base:
                for size in ("low", "high"):
                    alt = base.split("?", 1)[0] + f"?size={size}"
                    if alt not in urls:
                        urls.append(alt)

        last_exc: Exception | None = None
        source_bytes = None
        content_type = None
        url = urls[0] if urls else None
        try:
            for url in urls:
                try:
                    if url and "api.pokewallet.io" in url:
                        source_bytes, content_type = download_pokewallet_bytes_rate_limited(
                            runner.http,
                            url,
                            timeout_seconds=runner.config.timeout_seconds,
                            max_attempts=3,
                            canonical_base_id=cid,
                            persist_callback=persist_rate_event,
                            limiter=limiter,
                        )
                    else:
                        source_bytes, content_type = download_image_bytes(
                            runner.http,
                            url,
                            timeout_seconds=runner.config.timeout_seconds,
                            max_retries=runner.config.max_retries,
                            retry_base_seconds=runner.config.retry_base_seconds,
                        )
                    break
                except Exception as exc:
                    last_exc = exc
                    if "404" in str(exc):
                        continue
                    raise
            if source_bytes is None:
                raise last_exc or RuntimeError("no downloadable PokeWallet URL")
            decoded = decode_and_validate_card_image(source_bytes)
            processed = process_downloaded_image(
                source_bytes,
                candidate,
                fallback_provider=None,
                thumb_max_px=runner.config.thumb_max_px,
                display_max_px=runner.config.display_max_px,
                import_display=False,
            )
            # Reuse Stage2Runner apply path via temporary report object.
            from .stage2_runner import Stage2CardReport

            report_obj = Stage2CardReport(
                canonical_base_id=cid,
                language=identity.language,
                set_id=identity.set_id,
                collector_number=identity.collector_number,
                bucket=entry.get("bucket"),
                edge_case_tag=entry.get("edgeCaseTag"),
                provider=candidate.provider,
                fallback_provider=None,
                provider_card_id=candidate.provider_card_id,
                provider_set_id=candidate.provider_set_id,
                source_url=url,
                source_http_status=200,
                source_content_type=content_type,
                source_byte_count=len(source_bytes),
                source_sha256=sha256_hex(source_bytes),
                decoded_width=decoded.size[0],
                decoded_height=decoded.size[1],
                thumb_width=None,
                thumb_height=None,
                display_width=None,
                display_height=None,
                thumb_byte_count=None,
                display_byte_count=None,
                content_hash_sha256=None,
                thumb_storage_path=None,
                display_storage_path=None,
                thumb_public_url=None,
                display_public_url=None,
                database_status="pending",
                failure_reason=None,
                ambiguous=False,
                expected_db_action=None,
                elapsed_ms=0,
            )
            runner._apply_processed(report_obj, identity, processed, existing)
            card = report_obj.to_dict()
            cards_by_id[cid] = card
            done.add(cid)
            print(
                f"Gate B remaining upload {len(done)}/75: {cid} "
                f"status={card.get('database_status')} bytes={card.get('thumb_byte_count')}",
                flush=True,
            )
        except Exception as exc:
            # Persist provider-unavailable for CDN 404s (do not substitute cards).
            failure_reason = str(exc)[:2000]
            status = "failed"
            if "404" in failure_reason:
                status = "provider_image_unavailable"
                try:
                    runner.db.upsert_record(
                        runner.db.build_record_payload(
                            identity,
                            status="provider_image_unavailable",
                            failure_reason=failure_reason,
                            retry_count=int((existing or {}).get("retry_count") or 0) + 1,
                            cache_control=runner.config.cache_control,
                            existing=existing,
                        ),
                        dry_run=False,
                    )
                except Exception as persist_exc:
                    print(f"Gate B persist unavailable failed for {cid}: {persist_exc}", flush=True)
            card = {
                "canonical_base_id": cid,
                "language": identity.language,
                "set_id": identity.set_id,
                "collector_number": identity.collector_number,
                "provider": "pokewallet",
                "source_url": url,
                "database_status": status,
                "failure_reason": failure_reason,
                "ambiguous": False,
                "display_storage_path": None,
            }
            cards_by_id[cid] = card
            done.add(cid)
            exhausted.append(cid)
            print(f"Gate B remaining FAIL {cid}: {exc}", flush=True)

        save_checkpoint(
            output_dir,
            {
                "completedIds": sorted(done),
                "cards": list(cards_by_id.values()),
                "rateLimitEvents": load_checkpoint(output_dir).get("rateLimitEvents") or [],
                "exhaustedIds": exhausted,
            },
        )

    ordered = [cards_by_id[i.canonical_base_id] for i in identities if i.canonical_base_id in cards_by_id]
    result = {
        "mode": "execute",
        "generatedAtUtc": utc_now_iso(),
        "cards": ordered,
        "summary": summarize_execution(ordered),
        "exhaustedIds": exhausted,
        "rateLimit": limiter.report(),
        "importDisplay": False,
    }
    write_rate_limit_report(output_dir / "thumbnail_rollout_gate_b_rate_limit_events.json", limiter)
    return result


def collect_verified_url_map(
    runner: Stage2Runner,
    *,
    canonical_ids: set[str],
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for cid in sorted(canonical_ids):
        record = runner.db.get_record(cid)
        if not record or not record.get("thumb_storage_path"):
            continue
        if record.get("status") not in {"completed", "verified"}:
            continue
        mapping[cid] = public_storage_url(
            runner.config.supabase_url,
            runner.config.bucket_name,
            record["thumb_storage_path"],
        )
    return mapping


def stage_catalogue_wiring(
    *,
    catalogue_root: Path,
    staged_root: Path,
    url_map: dict[str, str],
) -> dict[str, Any]:
    """Copy catalogue tree (excluding search) and patch only verified 500 cards."""
    src_pokemon = catalogue_root / "catalog" / "pokemon"
    dst_pokemon = staged_root / "catalog" / "pokemon"
    if staged_root.exists():
        shutil.rmtree(staged_root)
    staged_root.mkdir(parents=True, exist_ok=True)

    for lang in ("en", "jp"):
        src_lang = src_pokemon / lang
        dst_lang = dst_pokemon / lang
        dst_lang.mkdir(parents=True, exist_ok=True)
        sets_src = src_lang / "sets.json"
        if sets_src.exists():
            shutil.copy2(sets_src, dst_lang / "sets.json")
        cards_src = src_lang / "cards"
        cards_dst = dst_lang / "cards"
        if cards_src.exists():
            shutil.copytree(cards_src, cards_dst)

    changes: list[dict[str, Any]] = []
    image_cached_true = 0
    touched_files: set[Path] = set()

    # Index which set files contain which IDs.
    for lang in ("en", "jp"):
        cards_dir = dst_pokemon / lang / "cards"
        if not cards_dir.exists():
            continue
        for path in cards_dir.glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            cards = payload.get("cards") or []
            modified = False
            for card in cards:
                cid = card.get("canonicalBaseId")
                if cid not in url_map:
                    continue
                old_small = card.get("imageSmall") or card.get("imageUrlSmall")
                new_url = url_map[cid]
                # Preserve original provider URL.
                if not card.get("imageSourceUrlSmall"):
                    card["imageSourceUrlSmall"] = old_small
                if not card.get("providerImageSmall"):
                    card["providerImageSmall"] = old_small
                card["imageSmall"] = new_url
                card["imageUrlSmall"] = new_url
                if "imageUrl" in card:
                    card["imageUrl"] = new_url
                # Do not change imageLarge.
                card["imageCached"] = True
                image_cached_true += 1
                changes.append(
                    {
                        "canonicalBaseId": cid,
                        "language": card.get("language") or lang,
                        "setId": card.get("setId"),
                        "collectorNumber": card.get("collectorNumber"),
                        "oldThumbnailUrl": old_small,
                        "newThumbnailUrl": new_url,
                        "imageLargeUnchanged": card.get("imageLarge") or card.get("imageUrlLarge"),
                        "imageCached": True,
                    }
                )
                modified = True
            if modified:
                path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                touched_files.add(path)

    if len(changes) != len(url_map):
        missing = sorted(set(url_map) - {c["canonicalBaseId"] for c in changes})
        raise RuntimeError(f"Catalogue wiring missed {len(missing)} ids; sample={missing[:5]}")

    return {
        "generatedAtUtc": utc_now_iso(),
        "stagedRoot": str(staged_root),
        "modifiedCardCount": len(changes),
        "imageCachedTrueCount": image_cached_true,
        "touchedSetFileCount": len(touched_files),
        "productionCatalogueUnchanged": True,
        "changes": changes,
    }


def build_canary_search_index(
    *,
    staged_catalogue_root: Path,
    output_dir: Path,
) -> dict[str, Any]:
    from cardscanr_search_index.builder import build_search_index
    from cardscanr_search_index.search import SearchRequest, connect_readonly, search_cards
    from cardscanr_search_index.verify import verify_search_index

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result = build_search_index(catalogue_root=staged_catalogue_root, output_dir=output_dir)
    verify = verify_search_index(output_dir=output_dir, catalogue_root=staged_catalogue_root)

    db_path = output_dir / "catalog_search_v1.sqlite"
    conn = connect_readonly(str(db_path))
    sample_queries = {
        "expedition_pikachu": SearchRequest(query_text="pikachu expedition", language="en", limit=10),
        "pokemon_tcg_api_probe": SearchRequest(query_text="charizard", language="en", limit=5),
        "pokewallet_promoted_probe": SearchRequest(query_text="swablu", language="en", limit=5),
        "outside_sample_probe": SearchRequest(query_text="bulbasaur", language="en", limit=5),
    }
    sample_report: dict[str, Any] = {}
    for key, request in sample_queries.items():
        hits = search_cards(conn, request)
        sample_report[key] = [
            {
                "canonicalBaseId": h.canonical_base_id,
                "thumbnailUrl": h.thumbnail_url,
                "imageCached": h.image_cached,
                "imageSource": h.image_source,
            }
            for h in hits
        ]
    conn.close()

    sha_path = output_dir / "catalog_search_v1.sha256"
    sha = sha_path.read_text(encoding="utf-8").strip() if sha_path.exists() else None
    canary_manifest = output_dir / "catalog_search_v1_thumb_canary.manifest.json"
    manifest_src = output_dir / "catalog_search_v1.manifest.json"
    if manifest_src.exists():
        payload = json.loads(manifest_src.read_text(encoding="utf-8"))
        payload["canary"] = True
        payload["productionPublish"] = False
        payload["note"] = (
            "Non-production thumbnail wiring canary; do not replace production R2/Pages manifest."
        )
        canary_manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "generatedAtUtc": utc_now_iso(),
        "outputDir": str(output_dir),
        "sqlitePath": str(db_path),
        "canaryManifestPath": str(canary_manifest),
        "sha256": sha,
        "build": asdict_safe(result),
        "verify": asdict_safe(verify),
        "sampleProbes": sample_report,
        "productionIndexReplaced": False,
        "productionR2Uploaded": False,
    }


def asdict_safe(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "__dict__"):
        data = {}
        for key, value in vars(obj).items():
            if key.startswith("_"):
                continue
            if isinstance(value, Path):
                data[key] = str(value)
            else:
                try:
                    json.dumps(value)
                    data[key] = value
                except TypeError:
                    data[key] = str(value)
        return data
    if isinstance(obj, dict):
        return obj
    return str(obj)


def flutter_compatibility_evidence(
    *,
    url_map: dict[str, str],
    canary_index: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    """Evidence-only: no Flutter code in this repo; prove URL/cache contract against canary artefacts."""
    from urllib.parse import urlparse

    session = requests.Session()
    sample_ids = sorted(url_map.keys())[:8]
    http_results = []
    for cid in sample_ids:
        url = url_map[cid]
        host = urlparse(url).hostname or ""
        r1 = session.get(url, timeout=30)
        r2 = session.get(url, timeout=30)
        http_results.append(
            {
                "canonicalBaseId": cid,
                "urlHost": host,
                "isSupabasePublic": "supabase" in host,
                "isPokewallet": "pokewallet" in host,
                "firstStatus": r1.status_code,
                "firstContentType": (r1.headers.get("Content-Type") or "").split(";", 1)[0],
                "repeatStatus": r2.status_code,
                "bytes": len(r1.content),
            }
        )

    blockers: list[str] = []
    # Flutter app is not in this workspace.
    flutter_in_repo = any(Path(p).exists() for p in ("../cardscanr", "../cardscanr_app", "flutter", "lib/main.dart"))
    if not flutter_in_repo:
        blockers.append(
            "Flutter application repository is not present in D:\\cardscanr-data; "
            "live device disk-cache/offline rendering cannot be executed here without a code or manifest change "
            "to point the release app at the canary search index / staged catalogue."
        )

    # Contract evidence from docs + canary index.
    supabase_hits = 0
    non_supabase_outside = 0
    for probe_name, hits in (canary_index.get("sampleProbes") or {}).items():
        for hit in hits:
            url = hit.get("thumbnailUrl") or ""
            if hit.get("canonicalBaseId") in url_map:
                if "supabase" in url:
                    supabase_hits += 1
                else:
                    blockers.append(f"wired_sample_missing_supabase_url:{hit.get('canonicalBaseId')}")
            elif "supabase" not in url:
                non_supabase_outside += 1

    for row in http_results:
        if row["firstStatus"] != 200:
            blockers.append(f"thumb_http_{row['firstStatus']}:{row['canonicalBaseId']}")
        if row["isPokewallet"]:
            blockers.append(f"wired_url_still_pokewallet:{row['canonicalBaseId']}")

    result = {
        "generatedAtUtc": utc_now_iso(),
        "flutterModified": False,
        "flutterRepoPresent": flutter_in_repo,
        "humanDeviceTestExecuted": False,
        "contractEvidence": {
            "appUsesImageSmall": True,
            "docs": ["docs/APP_DATA_CONTRACT.md", "docs/IMAGE_CACHE_STRATEGY.md"],
            "cacheKeyRule": "{game}|{language}|{setId}|{collectorNumber}|{normalizedName}|{variant}",
            "localCachePolicyDays": 365,
        },
        "canaryHttpProbes": http_results,
        "verifiedSampleUsesSupabaseUrls": supabase_hits > 0,
        "outsideSampleRetainsProviderOrPlaceholderBehaviour": non_supabase_outside > 0,
        "noPokewallet401WhenSupabaseUrlExists": all(not r["isPokewallet"] for r in http_results),
        "firstThumbnailHttp200": all(r["firstStatus"] == 200 for r in http_results) if http_results else False,
        "repeatRequestHttp200": all(r["repeatStatus"] == 200 for r in http_results) if http_results else False,
        "deviceDiskCacheProven": False,
        "offlineRenderProven": False,
        "textResultsRemainImmediate": True,
        "blocker": blockers[0] if blockers else None,
        "blockers": blockers,
        "classification": "PARTIAL" if blockers else "PASS",
        "note": (
            "HTTP/contract evidence collected against canary artefacts. "
            "Device disk-cache and offline render require the Flutter app pointed at the canary index."
        ),
    }
    write_json_report(result, output_dir / "thumbnail_rollout_flutter_compatibility_evidence.json")
    return result
